"""Kodi plugin entry point for Silo Server.

Responsibilities in this file:
    * Build Kodi's directory UI.
    * Browse Silo libraries, series, seasons and episodes.
    * Select a playable Silo file/version.
    * Ask Silo for the freshest resume position when Play is selected.
    * Start Silo playback at that server position.
    * Report Kodi's live playback position back to Silo.

Important resume behaviour:
    Silo is the source of truth. The catalog supplies the watched flag, while a
    lightweight status=in_progress progress request supplies detailed partial
    positions. This avoids downloading the full progress history every time a
    library is opened while keeping Kodi's partial-watch indicators accurate.

    When Play is selected, play() still performs a SECOND fresh /api/v2/progress
    lookup immediately before playback. That fresh server value is put on the
    resolved Kodi ListItem, allowing Kodi to show its normal single Resume/Play
    prompt using the current Silo position.

    Kodi -> Silo progress reporting is unchanged, so partial playback is still
    stored on the Silo server for other clients to see.

Large Kodi directory lists are sent with xbmcplugin.addDirectoryItems(), which
Kodi documents as more efficient for large lists than one addDirectoryItem() call
at a time. Pagination is handled internally by SiloClient and is never shown
to the user.
"""

import re
import sys
import time
import threading
import json
import datetime
import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode, urlparse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

from resources.lib.silo import (
    SiloClient,
    SiloError,
    _hide_login_loading,
    log,
)


# Kodi supplies a numeric handle for the current plugin directory.
HANDLE = int(sys.argv[1])

# Base plugin URL supplied by Kodi.
BASE_URL = sys.argv[0]

# Media types that this addon can send directly to Kodi's VideoPlayer.
PLAYABLE = (
    "movie",
    "episode",
    "video",
)

# Maximum number of entries shown in one Kodi directory page.
DIRECTORY_PAGE_SIZE = 200

# Maximum number of server-side search results shown in one Kodi page.
SEARCH_PAGE_SIZE = 100

# Kodi setting used to control normal directory page size. Silo search itself
# is capped at 100 results per request, so search uses the smaller of the user
# setting and the server search limit.
MIN_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

ADDON = xbmcaddon.Addon()


_SKIN_EPISODE_NUMBER_DETECTION = None


def _tag_blocks(text, tag_name):
    """Return simple XML tag blocks for one tag name."""
    blocks = []
    opening = "<" + tag_name
    closing = "</" + tag_name + ">"
    position = 0

    while True:
        begin = text.find(opening, position)
        if begin < 0:
            return blocks

        finish = text.find(closing, begin)
        if finish < 0:
            return blocks

        finish += len(closing)
        blocks.append(text[begin:finish])
        position = finish


def _tag_attribute(block, name):
    """Read one simple XML attribute without relying on a quote-sensitive regex."""
    lower_block = block.lower()
    marker = name.lower() + "="
    start = lower_block.find(marker)
    if start < 0:
        return ""

    start += len(marker)

    while start < len(block) and block[start].isspace():
        start += 1

    if start >= len(block):
        return ""

    quote = block[start]
    if quote in ("'", '"'):
        finish = block.find(quote, start + 1)
        if finish < 0:
            return ""
        return block[start + 1:finish]

    finish = start
    while finish < len(block) and not block[finish].isspace() and block[finish] != ">":
        finish += 1

    return block[start:finish]


def _label_expression_uses_episode_number(block):
    """Return True when one rendered label expression contains episode + title."""
    lower = block.lower()
    return (
        "listitem.episode" in lower
        and "listitem.title" in lower
    )


def skin_episode_number_in_label():
    """Return True when the active skin already puts episode number in its label.

    Only a label expression that is applicable to normal episode directory
    rendering counts. Playlist-only expressions must not suppress the add-on's
    episode-number prefix.
    """
    global _SKIN_EPISODE_NUMBER_DETECTION

    if _SKIN_EPISODE_NUMBER_DETECTION is not None:
        return _SKIN_EPISODE_NUMBER_DETECTION

    detected = False

    try:
        pending = ["special://skin/xml"]
        visited = set()

        while pending and not detected:
            directory = pending.pop()

            if directory in visited:
                continue

            visited.add(directory)

            try:
                subdirectories, filenames = xbmcvfs.listdir(directory)
            except Exception:
                continue

            for subdirectory in subdirectories:
                child = directory.rstrip("/") + "/" + subdirectory
                pending.append(child)

            for filename in filenames:
                if not str(filename).lower().endswith(".xml"):
                    continue

                path = directory.rstrip("/") + "/" + str(filename)

                try:
                    handle = xbmcvfs.File(path)
                    data = handle.read()
                    handle.close()

                    if isinstance(data, bytes):
                        data = data.decode("utf-8", "ignore")

                    lower = str(data).lower()
                except Exception:
                    continue

                # First inspect the dedicated ListLabelVar. The previous
                # implementation checked the whole variable at once, which
                # incorrectly matched playlist-only values in Estuary.
                for variable in _tag_blocks(lower, "variable"):
                    name = _tag_attribute(variable, "name").strip().lower()

                    if name != "listlabelvar":
                        continue

                    for value in _tag_blocks(variable, "value"):
                        if not _label_expression_uses_episode_number(value):
                            continue

                        condition = _tag_attribute(value, "condition").lower()

                        # A playlist-only label does not affect a normal
                        # directory opened by this add-on.
                        if "window.isactive(videoplaylist)" in condition:
                            continue

                        detected = True
                        break

                    if detected:
                        break

                if detected:
                    break

                # Some skins place the rendered label directly inside an
                # itemlayout. Inspect actual <label> controls rather than
                # treating every ListItem.Episode reference in the layout as
                # evidence that the number is displayed beside the title.
                for layout in _tag_blocks(lower, "itemlayout"):
                    for label in _tag_blocks(layout, "label"):
                        if _label_expression_uses_episode_number(label):
                            detected = True
                            break

                    if detected:
                        break

                if detected:
                    break

        log(
            "Skin episode-number label detection: %s"
            % (
                "already supplied by skin"
                if detected
                else "not supplied by skin"
            )
        )
    except Exception as exc:
        # If the skin cannot be inspected, prefer adding the episode number so
        # the add-on still provides it rather than silently losing it.
        log(
            "Unable to inspect active skin for episode numbering: %s" % exc,
            xbmc.LOGDEBUG,
        )
        detected = False

    _SKIN_EPISODE_NUMBER_DETECTION = detected
    return detected


def episode_display_label(title, episode_number):
    """Return an episode label without duplicating a skin-provided number."""
    title = str(title or "Episode")

    try:
        number = int(episode_number)
    except (TypeError, ValueError):
        return title

    if number <= 0:
        return title

    if skin_episode_number_in_label():
        return title

    return "%d. %s" % (number, title)


def direct_play_only_enabled():
    """Return whether playback must remain on Silo's original/direct stream."""
    return str(ADDON.getSetting("direct_play_only") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )

def use_kodi_resume_cache_enabled():
    """Return whether Kodi's own cached resume position should be used."""
    return str(ADDON.getSetting("use_kodi_resume_cache") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def get_kodi_cached_resume_position():
    """Read Kodi's cached resume position for the currently selected item."""
    try:
        value = xbmc.getInfoLabel("ListItem.ResumeTime")
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0



def get_directory_page_size():
    """Return the configured Kodi page size, clamped to 20-200.

    Kodi's legacy slider settings are stored as strings representing floating
    point values even when option="int" is used, so parse the value as a float.
    """
    raw = ADDON.getSetting("items_per_page")

    try:
        value = int(round(float(raw))) if raw not in (None, "") else MAX_PAGE_SIZE
    except (TypeError, ValueError):
        value = MAX_PAGE_SIZE

    return max(MIN_PAGE_SIZE, min(value, MAX_PAGE_SIZE))


# Build a Kodi plugin URL containing the action and any required IDs.
def paginate_directory(items, page):
    """Return one 200-item slice and whether another page exists."""
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    page_size = get_directory_page_size()
    start = (page_number - 1) * page_size
    end = start + page_size

    return items[start:end], page_number > 1, end < len(items)


def add_previous_page(library_id=None, series_id=None, season_number=None,
                      action=None, page=1):
    """Add a Previous Page folder when the current directory is past page 1."""
    try:
        page_number = int(page or 1)
    except (TypeError, ValueError):
        page_number = 1

    if page_number <= 1:
        return

    params = {
        "action": action,
        "page": page_number - 1,
    }

    if library_id:
        params["library_id"] = library_id
    if series_id:
        params["series_id"] = series_id
    if season_number is not None:
        params["season_number"] = season_number

    item = xbmcgui.ListItem(label="Previous Page")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(**params),
        item,
        True,
    )


def add_next_page(library_id=None, series_id=None, season_number=None,
                  action=None, page=1):
    """Add a Next Page folder when another 200-item slice exists."""
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    params = {
        "action": action,
        "page": page_number + 1,
    }

    if library_id:
        params["library_id"] = library_id
    if series_id:
        params["series_id"] = series_id
    if season_number is not None:
        params["season_number"] = season_number

    item = xbmcgui.ListItem(label="Next Page")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(**params),
        item,
        True,
    )


def build_url(**params):
    # Optional IDs are sometimes unresolved on profile-wide Home cards. Never
    # serialize Python None as the literal string "None", because Silo treats
    # values such as library_id=None as an invalid positive integer identifier.
    params = {
        key: value
        for key, value in params.items()
        if value is not None
    }
    return BASE_URL + "?" + urlencode(params)


# Display a short informational notification in Kodi.
def notify(message):
    xbmcgui.Dialog().notification(
        "Silo",
        message,
        xbmcgui.NOTIFICATION_INFO,
        3000,
    )


def format_position(seconds):
    """Convert a number of seconds into a simple human-readable timestamp."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0

    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)

    return "%d:%02d" % (minutes, seconds)


def fetch_detail_metadata(client, items, library_id, max_workers=2, per_item_library=False):
    """Fetch extended metadata concurrently and retry transient failures.

    Catalog data is fast and contains most metadata. The detail endpoint adds
    cast, crew and full file stream information. Requests run concurrently,
    while transient timeouts, connection failures and server throttling/errors
    are retried before an item is considered unavailable.

    When per_item_library is true, every supplied card occurrence is fetched
    separately using its source library ID. This is required by merged Home
    sections because the same content ID can exist in multiple libraries and
    each occurrence can have a different added_at timestamp.
    """
    requests = []
    seen = set()

    for index, item in enumerate(items):
        content_id = get_content_id(item)
        if not content_id:
            continue

        item_library_id = library_id
        if per_item_library:
            item_library_id = (
                item.get("_silo_home_source_library_id")
                or item_library_id
            )

        if per_item_library:
            # Preserve one request for each card occurrence/library pair.
            request_key = "%s|%s|%s" % (
                str(content_id),
                str(item_library_id) if item_library_id is not None else "",
                index,
            )
        else:
            request_key = str(content_id)

        if request_key in seen:
            continue
        seen.add(request_key)
        requests.append(
            (content_id, item_library_id, request_key)
        )

    if not requests:
        return {}

    def fetch_one(request):
        content_id, request_library_id, request_key = request
        attempts = 3

        for attempt in range(attempts):
            try:
                # Use a separate session per worker. requests.Session should not
                # be shared across concurrent requests.
                worker_client = SiloClient()
                worker_client.cfg.update(client.cfg)

                detail = worker_client.item_detail(
                    content_id,
                    request_library_id,
                )

                if detail:
                    # Silo's v2 item detail embeds CatalogItem, including
                    # added_at. The merged Recently Added sorter consumes this
                    # value from the occurrence-specific detail below.
                    return request_key, detail

                # An empty document is unusual but should get one retry.
                if attempt < attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
                    continue

                return request_key, None

            except SiloError as exc:
                status = getattr(exc, "status", None)

                # Retry transient HTTP failures and network errors. For 429,
                # Silo supplies the authoritative Retry-After delay.
                transient = (
                    status is None
                    or status == 408
                    or status == 429
                    or status >= 500
                )

                if attempt < attempts - 1 and transient:
                    retry_after = getattr(exc, "retry_after", None)

                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 0.0

                    if status == 429 and delay <= 0:
                        delay = 2.0
                    elif delay <= 0:
                        delay = 0.5 * (attempt + 1)

                    # Give the server a little breathing room before the next
                    # attempt, especially after a rate-limit response.
                    time.sleep(max(0.25, delay))
                    continue

                log(
                    "Unable to retrieve detail metadata for %s: %s"
                    % (content_id, exc),
                    xbmc.LOGWARNING,
                )
                return request_key, None

        return request_key, None

    worker_count = max(
        1,
        min(int(max_workers or 2), len(requests)),
    )

    details = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, request)
            for request in requests
        ]

        for future in as_completed(futures):
            request_key, detail = future.result()
            if detail:
                details[request_key] = detail

    return details

def fetch_home_added_at_by_section(client, section, card_items):
    """Return added_at values only for cards already present in a Home row.

    The Home-card endpoint limits the displayed items but omits added_at.
    Silo's section-backed catalog endpoint exposes added_at, so use it only
    as a metadata lookup and never add its extra catalogue items to Kodi.
    """
    if not card_items:
        return {}

    section_id = section.get("id") or section.get("section_id")
    if not section_id:
        return {}

    wanted = set()
    for item in card_items:
        content_id = get_content_id(item)
        if content_id:
            wanted.add(str(content_id))

    if not wanted:
        return {}

    try:
        data = client.home_section_catalog_items(
            section_id,
            image_size="medium",
            limit=200,
        ) or {}
    except SiloError as exc:
        log(
            "Unable to retrieve added_at lookup for Home section %s: %s"
            % (section_id, exc),
            xbmc.LOGINFO,
        )
        return {}

    result = {}
    for catalog_item in data.get("items") or []:
        content_id = get_content_id(catalog_item)
        if not content_id or str(content_id) not in wanted:
            continue

        added_at = catalog_item.get("added_at")
        if added_at:
            result[str(content_id)] = added_at

    log(
        "Home added_at lookup section=%s matched=%d/%d"
        % (section_id, len(result), len(wanted)),
        xbmc.LOGINFO,
    )
    return result


def get_runtime_seconds(item):
    """Convert Silo's catalog runtime (minutes) into Kodi seconds."""
    if not item:
        return 0.0

    try:
        runtime = float(item.get("runtime", 0) or 0)
    except (TypeError, ValueError):
        runtime = 0.0

    return max(0.0, runtime * 60.0)


def get_progress_position(progress):
    """Safely read a Silo progress position and duration."""
    if not progress:
        return 0.0, 0.0

    try:
        position = float(progress.get("position_seconds", 0) or 0)
    except (TypeError, ValueError):
        position = 0.0

    try:
        duration = float(progress.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0

    position = max(0.0, position)
    duration = max(0.0, duration)

    # Protect against malformed data where Silo reports a position beyond duration.
    if duration > 0:
        position = min(position, duration)

    return position, duration


def has_usable_resume(progress):
    """Return whether a progress record contains a real resume position."""
    if not progress or bool(progress.get("completed", False)):
        return False

    position, duration = get_progress_position(progress)
    return position > 0 and duration > 0


def normalize_watch_rollup(user_data, fallback_episode_count=0):
    """Normalize Silo's aggregate season/series viewer state."""
    if not isinstance(user_data, dict):
        user_data = {}

    def as_int(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    watched_count = as_int(user_data.get("watched_count"))
    unplayed_count = as_int(user_data.get("unplayed_count"))
    in_progress_count = as_int(user_data.get("in_progress_count"))

    if watched_count + unplayed_count <= 0:
        unplayed_count = as_int(fallback_episode_count)

    total_count = watched_count + unplayed_count

    return {
        "played": bool(user_data.get("played", False)) or (
            total_count > 0 and watched_count >= total_count
        ),
        "watched_count": min(watched_count, total_count),
        "unplayed_count": max(0, total_count - watched_count),
        "in_progress_count": min(
            in_progress_count,
            max(0, total_count - watched_count),
        ),
        "total_count": total_count,
    }


def merge_season_watch_rollup(existing, candidate):
    """Keep the richest aggregate row when the same season is seen twice."""
    if existing is None:
        return candidate

    existing_score = (
        existing.get("total_count", 0),
        existing.get("watched_count", 0),
        existing.get("in_progress_count", 0),
    )
    candidate_score = (
        candidate.get("total_count", 0),
        candidate.get("watched_count", 0),
        candidate.get("in_progress_count", 0),
    )

    return candidate if candidate_score > existing_score else existing


def fetch_series_watch_data(client, items, library_id=None, max_workers=4):
    """Fetch Silo's season rollups and aggregate them to each series."""
    series_ids = []
    seen = set()

    for item in items:
        media_type = (
            item.get("type")
            or item.get("media_type")
            or ""
        ).lower()

        if media_type == "series":
            series_id = get_content_id(item)
        elif media_type == "season":
            series_id = item.get("series_id")
        else:
            continue

        if not series_id:
            continue

        key = str(series_id)
        if key not in seen:
            seen.add(key)
            series_ids.append(series_id)

    if not series_ids:
        return {}, {}

    if library_id:
        library_ids = [library_id]
    else:
        try:
            library_ids = [
                library.get("id")
                for library in client.libraries()
                if library.get("id")
            ]
        except SiloError as exc:
            log(
                "Unable to retrieve libraries for series watch-state lookup: %s"
                % exc,
                xbmc.LOGWARNING,
            )
            return {}, {}

    if not library_ids:
        return {}, {}

    def fetch_one(series_id):
        season_map = {}

        for candidate_library_id in library_ids:
            try:
                seasons = client.seasons(
                    series_id,
                    candidate_library_id,
                    suppress_not_found=True,
                ) or []
            except SiloError as exc:
                log(
                    "Unable to retrieve seasons for series %s in library %s: %s"
                    % (series_id, candidate_library_id, exc),
                    xbmc.LOGDEBUG,
                )
                continue

            for season in seasons:
                season_content_id = get_content_id(season)
                season_number = season.get(
                    "season_number",
                    season.get("number"),
                )
                rollup = normalize_watch_rollup(
                    season.get("user_data"),
                    season.get("episode_count") or 0,
                )
                candidate = {
                    "content_id": (
                        str(season_content_id)
                        if season_content_id
                        else ""
                    ),
                    "series_id": str(series_id),
                    "season_number": season_number,
                    **rollup,
                }
                key = (
                    str(season_content_id)
                    if season_content_id
                    else "%s:%s" % (series_id, season_number)
                )
                season_map[key] = merge_season_watch_rollup(
                    season_map.get(key),
                    candidate,
                )

        watched_count = 0
        unplayed_count = 0
        in_progress_count = 0

        for season in season_map.values():
            watched_count += season.get("watched_count", 0)
            unplayed_count += season.get("unplayed_count", 0)
            in_progress_count += season.get("in_progress_count", 0)

        total_count = watched_count + unplayed_count

        return (
            str(series_id),
            {
                "played": (
                    total_count > 0
                    and watched_count >= total_count
                ),
                "watched_count": watched_count,
                "unplayed_count": unplayed_count,
                "in_progress_count": in_progress_count,
                "total_count": total_count,
                "season_count": len(season_map),
            },
            season_map,
        )

    worker_count = max(
        1,
        min(int(max_workers or 4), len(series_ids)),
    )
    series_map = {}
    season_map = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, series_id)
            for series_id in series_ids
        ]

        for future in as_completed(futures):
            series_key, series_rollup, series_seasons = future.result()
            series_map[series_key] = series_rollup

            for key, season in series_seasons.items():
                season_map[key] = merge_season_watch_rollup(
                    season_map.get(key),
                    season,
                )

    return series_map, season_map


def set_container_watch_state(list_item, rollup):
    """Apply Silo's aggregate watch state to a series or season folder."""
    if not rollup:
        return

    watched_count = max(0, int(rollup.get("watched_count", 0) or 0))
    unplayed_count = max(0, int(rollup.get("unplayed_count", 0) or 0))
    in_progress_count = max(
        0,
        int(rollup.get("in_progress_count", 0) or 0),
    )
    total_count = max(
        0,
        int(
            rollup.get("total_count")
            or watched_count + unplayed_count
            or 0
        ),
    )

    if total_count <= 0:
        return

    completed = watched_count >= total_count
    partial = (
        not completed
        and (watched_count > 0 or in_progress_count > 0)
    )
    unwatched_count = max(0, total_count - watched_count)

    # Populate Kodi's native TV-show/season episode-count properties so
    # skins can display the same watched/total information they use for items
    # from Kodi's own video database.
    watched_percent = (
        (watched_count * 100.0) / float(total_count)
        if total_count > 0
        else 0.0
    )

    properties = {
        "totalepisodes": str(total_count),
        "numepisodes": str(total_count),
        "watchedepisodes": str(watched_count),
        "unwatchedepisodes": str(unwatched_count),
        "inprogressepisodes": str(in_progress_count),
        "watchedepisodepercent": str(int(round(watched_percent))),
        "WatchedEpisodes": str(watched_count),
        "UnWatchedEpisodes": str(unwatched_count),
        "UnwatchedEpisodes": str(unwatched_count),
        "InProgressEpisodes": str(in_progress_count),
        "InProgressCount": str(in_progress_count),
        "TotalEpisodes": str(total_count),
        "Silo.EpisodeCount": str(total_count),
        "Silo.WatchedEpisodes": str(watched_count),
        "Silo.UnwatchedEpisodes": str(unwatched_count),
        "Silo.InProgressEpisodes": str(in_progress_count),
        "Silo.WatchedEpisodePercent": str(int(round(watched_percent))),
        "Silo.TotalEpisodes": str(total_count),
        "Silo.PartiallyWatched": "true" if partial else "false",
    }

    season_count = rollup.get("season_count")
    if season_count is not None:
        properties.update({
            "totalseasons": str(int(season_count)),
            "numseasons": str(int(season_count)),
            "Silo.SeasonCount": str(int(season_count)),
        })

    for key, value in properties.items():
        list_item.setProperty(key, value)

    tag = list_item.getVideoInfoTag()

    if completed:
        tag.setPlaycount(1)
        tag.setResumePoint(0.0, 0.0)
        return

    tag.setPlaycount(0)

    if partial:
        fraction = (
            watched_count + (0.5 * in_progress_count)
        ) / float(total_count)
        fraction = min(0.999, max(0.001, fraction))
        tag.setResumePoint(fraction, 1.0)
    else:
        tag.setResumePoint(0.0, 0.0)


def _art_url(client, value):
    """Return an artwork URL from either a string or a small artwork dict."""
    if not value:
        return ""

    if isinstance(value, dict):
        value = (
            value.get("url")
            or value.get("src")
            or value.get("path")
        )

    if not value:
        return ""

    return client.abs_url(str(value))


def set_art(list_item, client, poster=None, backdrop=None, logo=None, still=None):
    """Apply Silo artwork to the Kodi ListItem.

    Current Silo CatalogItem responses expose poster_url, backdrop_url and
    logo_url. Older field names and dictionary-style artwork values are also
    accepted as fallbacks so artwork remains compatible with older servers.
    """
    poster_url = _art_url(client, poster)
    backdrop_url = _art_url(client, backdrop)
    logo_url = _art_url(client, logo)
    still_url = _art_url(client, still)

    art = {}

    if poster_url:
        art.update({
            "thumb": poster_url,
            "poster": poster_url,
            "icon": poster_url,
        })

    if backdrop_url:
        art["fanart"] = backdrop_url

    if logo_url:
        art["clearlogo"] = logo_url

    # Episode stills are useful as thumbnails when no poster is available.
    if still_url and not poster_url:
        art.update({
            "thumb": still_url,
            "icon": still_url,
        })

    if art:
        list_item.setArt(art)


def catalog_progress(item):
    """Convert Silo's catalog-level viewer state into our common progress shape.

    The Silo CatalogItem schema contains:
        user_state.played
        position_seconds
        duration_seconds

    Using these fields avoids a full /api/v2/progress download just to draw
    watched/resume markers in the library. The detailed progress endpoint is
    still queried fresh when the user actually starts playback.
    """
    if not item:
        return None

    # Catalog listings expose viewer flags as user_state, while the
    # series-season and season-episode endpoints expose the same watched
    # state as user_data. Accept both shapes.
    user_state = item.get("user_state")
    user_data = item.get("user_data")

    if not isinstance(user_state, dict) or not user_state:
        user_state = user_data if isinstance(user_data, dict) else {}

    if not user_state:
        return None

    # Silo exposes the current resume position directly when available.
    # Dedicated in-progress records are still preferred by the callers that
    # need an exact position.
    position = (
        item.get("position_seconds")
        if item.get("position_seconds") is not None
        else user_data.get("position_seconds", 0)
        if isinstance(user_data, dict)
        else 0
    )
    duration = (
        item.get("duration_seconds")
        if item.get("duration_seconds") is not None
        else user_data.get("duration_seconds", 0)
        if isinstance(user_data, dict)
        else 0
    )

    try:
        position = max(0.0, float(position or 0))
    except (TypeError, ValueError):
        position = 0.0

    try:
        duration = max(0.0, float(duration or 0))
    except (TypeError, ValueError):
        duration = 0.0

    # Catalog runtime is in minutes; progress duration is in seconds.
    # Unwatched items may not have a progress duration, so use the catalog
    # runtime as the Kodi duration in that case.
    if duration <= 0:
        duration = get_runtime_seconds(item)

    return {
        "completed": bool(user_state.get("played", False)),
        "position_seconds": position,
        "duration_seconds": duration,
        "updated_at": item.get("progress_updated_at") or "",
    }


def set_catalog_metadata(list_item, item, client):
    """Apply metadata that Silo includes directly in CatalogItem responses.

    This deliberately uses only catalog data so opening a large library does
    not trigger one detail request per movie or episode.
    """
    if not item:
        return

    tag = list_item.getVideoInfoTag()
    media_type = (item.get("type") or item.get("media_type") or "").lower()

    # Kodi media types map Silo's catalog types to the native video types.
    kodi_media_type = {
        "movie": "movie",
        "series": "tvshow",
        "season": "season",
        "episode": "episode",
        "video": "video",
    }.get(media_type, "video")

    tag.setMediaType(kodi_media_type)

    title = item.get("title") or item.get("name")
    if title:
        tag.setTitle(title)

    # Silo normally supplies year on CatalogItem, but some TV series
    # can have a missing/zero year while their first-air/release date exists.
    # Derive the year from that date so Kodi does not lose it.
    year = item.get("year")

    try:
        year = int(year or 0)
    except (TypeError, ValueError):
        year = 0

    if year <= 0:
        date_candidates = []

        if media_type == "series":
            date_candidates.extend([
                item.get("first_air_date"),
            ])

        date_candidates.extend([
            item.get("release_date"),
            item.get("first_air_date"),
            item.get("air_date"),
        ])

        for date_value in date_candidates:
            if date_value:
                try:
                    year = int(str(date_value)[:4])
                except (TypeError, ValueError):
                    year = 0

                if year > 0:
                    break

    if year > 0:
        tag.setYear(year)

    genres = [str(value) for value in (item.get("genres") or []) if value]
    if genres:
        tag.setGenres(genres)

    studios = [str(value) for value in (item.get("studios") or []) if value]
    if studios:
        tag.setStudios(studios)

    countries = [str(value) for value in (item.get("countries") or []) if value]
    if countries:
        tag.setCountries(countries)

    keywords = [str(value) for value in (item.get("keywords") or []) if value]
    if keywords:
        tag.setTags(keywords)

    plot = item.get("overview") or item.get("plot")
    if plot:
        tag.setPlot(plot)
        tag.setPlotOutline(plot)

    if item.get("tagline"):
        tag.setTagLine(item["tagline"])

    if item.get("content_rating"):
        tag.setMpaa(item["content_rating"])

    if item.get("original_language"):
        try:
            tag.setOriginalLanguage(item["original_language"])
        except Exception:
            # Keep the catalog value available even when Kodi cannot recognise
            # the language code supplied by the server.
            list_item.setProperty(
                "Silo.OriginalLanguage",
                str(item["original_language"]),
            )

    if item.get("series_title"):
        tag.setTvShowTitle(item["series_title"])

    if item.get("show_status"):
        tag.setTvShowStatus(item["show_status"])

    if item.get("season_number") is not None:
        try:
            tag.setSeason(int(item["season_number"]))
        except (TypeError, ValueError):
            pass

    if item.get("episode_number") is not None:
        try:
            tag.setEpisode(int(item["episode_number"]))
        except (TypeError, ValueError):
            pass

    release_date = item.get("release_date")
    if release_date:
        if media_type == "episode":
            tag.setFirstAired(str(release_date))
        else:
            tag.setPremiered(str(release_date))

    if item.get("runtime"):
        try:
            duration_seconds = int(round(float(item["runtime"]) * 60))
        except (TypeError, ValueError):
            duration_seconds = 0

        if duration_seconds > 0:
            tag.setDuration(duration_seconds)

    # Preserve every rating Silo exposes. Kodi supports multiple named rating
    # types; IMDb is preferred as the default when it exists.
    ratings = {}
    rating_map = (
        ("imdb", item.get("rating_imdb")),
        ("tmdb", item.get("rating_tmdb")),
        ("rotten_tomatoes_critic", item.get("rating_rt_critic")),
        ("rotten_tomatoes_audience", item.get("rating_rt_audience")),
    )

    for rating_type, value in rating_map:
        if value is None:
            continue
        try:
            ratings[rating_type] = (float(value), 0)
        except (TypeError, ValueError):
            pass

    if ratings:
        default_rating = "imdb" if "imdb" in ratings else next(iter(ratings))
        try:
            tag.setRatings(ratings, default_rating)
        except AttributeError:
            # Kodi versions before the InfoTagVideo rating API can still
            # receive named ratings through ListItem.
            for rating_type, (value, votes) in ratings.items():
                list_item.setRating(
                    rating_type,
                    value,
                    votes,
                    rating_type == default_rating,
                )

    # Store identifiers that are useful to Kodi and to skins/addons.
    unique_ids = {}
    for key in ("imdb_id", "tmdb_id", "tvdb_id"):
        value = item.get(key)
        if value:
            unique_ids[key.replace("_id", "")] = str(value)

    if unique_ids:
        default_id = (
            "imdb" if "imdb" in unique_ids
            else "tmdb" if "tmdb" in unique_ids
            else "tvdb"
        )
        try:
            tag.setUniqueIDs(unique_ids, default_id)
        except AttributeError:
            list_item.setUniqueIDs(unique_ids, default_id)

        for key, value in unique_ids.items():
            list_item.setProperty("Silo.%sID" % key.upper(), value)

    # Preserve catalog viewer state that has no direct VideoInfoTag setter.
    user_state = item.get("user_state")
    if isinstance(user_state, dict):
        for key in (
            "played",
            "is_favorite",
            "in_watchlist",
        ):
            if key in user_state:
                list_item.setProperty(
                    "Silo.UserState.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    "true" if bool(user_state[key]) else "false",
                )

    play_content_id = item.get("play_content_id")
    if play_content_id:
        list_item.setProperty("Silo.PlayContentID", str(play_content_id))

    series_id = item.get("series_id")
    if series_id:
        list_item.setProperty("Silo.SeriesID", str(series_id))

    content_id = item.get("content_id") or item.get("id")
    if content_id:
        list_item.setProperty("Silo.ContentID", str(content_id))

    # Keep the server's catalog progress fields available even when the
    # native watch marker is applied separately by set_watch_state().
    for key in (
        "position_seconds",
        "duration_seconds",
        "progress_updated_at",
    ):
        value = item.get(key)
        if value not in (None, ""):
            list_item.setProperty(
                "Silo.%s" % "".join(
                    part.title() for part in key.split("_")
                ),
                str(value),
            )

    if item.get("added_at"):
        try:
            tag.setDateAdded(str(item["added_at"]))
        except Exception:
            list_item.setProperty(
                "Silo.AddedAt",
                str(item["added_at"]),
            )

    for key in (
        "poster_thumbhash",
        "backdrop_thumbhash",
    ):
        value = item.get(key)
        if value:
            list_item.setProperty(
                "Silo.%s" % "".join(
                    part.title() for part in key.split("_")
                ),
                str(value),
            )

    badges = item.get("badges") or []
    if badges:
        list_item.setProperty(
            "Silo.Badges",
            " / ".join(str(value) for value in badges if value),
        )

    sort_metrics = item.get("sort_metrics")
    if isinstance(sort_metrics, dict):
        for key in (
            "release_date",
            "runtime_minutes",
            "resolution",
            "bitrate_kbps",
            "progress_ratio",
            "viewed_at",
            "play_count",
            "author",
            "narrator",
            "series_name",
        ):
            value = sort_metrics.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Sort.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

    upcoming = item.get("upcoming_event")
    if isinstance(upcoming, dict):
        for key in (
            "type",
            "air_date",
            "air_time",
            "episode_title",
            "season_number",
            "episode_number",
        ):
            value = upcoming.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Upcoming.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

    # The catalog has a few useful fields with no dedicated Kodi video-info
    # field. Expose them as ListItem properties so skins can still access them.
    if item.get("networks"):
        list_item.setProperty(
            "Silo.Networks",
            " / ".join(str(value) for value in item["networks"] if value),
        )

    for key in (
        "status",
        "item_source",
        "work_id",
        "work_title",
    ):
        value = item.get(key)
        if value:
            list_item.setProperty("Silo.%s" % key.title(), str(value))

    overlay = item.get("overlay_summary") or {}
    if isinstance(overlay, dict):
        for key in (
            "resolution",
            "hdr",
            "audio",
            "audio_channels",
            "video_codec",
            "container",
            "aspect_ratio",
            "release_type",
            "edition",
            "multi_audio",
            "multi_sub",
        ):
            value = overlay.get(key)
            if value not in (None, "", False):
                list_item.setProperty(
                    "Silo.%s" % "".join(part.title() for part in key.split("_")),
                    str(value),
                )


def _detail_version(detail, file_id=None):
    """Select the Silo file version Kodi should use for pre-play details."""
    versions = detail.get("versions") or []

    if file_id is not None:
        wanted = str(file_id)

        for version in versions:
            if str(version.get("file_id") or version.get("id")) == wanted:
                return version

    # The detail endpoint tells us which version its library/playback
    # presentation resolved to. Prefer that instead of assuming versions[0].
    effective_resolution = detail.get("effective_version_resolution")
    effective_hdr = detail.get("effective_version_hdr")
    effective_codec = detail.get("effective_version_codec_video")
    effective_edition = detail.get("effective_version_edition_key")

    best = None
    best_score = -1

    for version in versions:
        score = 0

        if effective_resolution and str(version.get("resolution") or "") == str(effective_resolution):
            score += 4

        if effective_hdr is not None and bool(version.get("hdr")) == bool(effective_hdr):
            score += 2

        if effective_codec and str(version.get("codec_video") or "") == str(effective_codec):
            score += 2

        if effective_edition and str(version.get("edition_key") or "") == str(effective_edition):
            score += 2

        if score > best_score:
            best = version
            best_score = score

    return best or (versions[0] if versions else {})


def _aspect_ratio(value):
    """Convert Silo's aspect ratio into Kodi's numeric float form."""
    if value in (None, ""):
        return 0.0

    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    text = str(value).strip()

    if ":" in text:
        parts = text.split(":", 1)
        try:
            width = float(parts[0])
            height = float(parts[1])
            if height:
                return width / height
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return 0.0


def set_stream_details(list_item, version):
    """Populate Kodi's pre-playback video, audio and subtitle stream details."""
    if not version:
        return

    tag = list_item.getVideoInfoTag()
    video_tracks = version.get("video_tracks") or []
    audio_tracks = version.get("audio_tracks") or []
    subtitle_tracks = version.get("subtitle_tracks") or []
    duration = int(version.get("duration") or 0)

    for track in video_tracks:
        aspect = _aspect_ratio(track.get("aspect_ratio"))

        info = {}
        if track.get("codec"):
            info["codec"] = track["codec"]
        if track.get("width"):
            info["width"] = int(track["width"])
        if track.get("height"):
            info["height"] = int(track["height"])
        if aspect > 0:
            info["aspect"] = aspect
        if duration > 0:
            info["duration"] = duration
        if track.get("language"):
            info["language"] = track["language"]

        hdr = (
            track.get("dolby_vision")
            or ("dolbyvision" if track.get("dv_profile") else "")
            or (
                "hdr10"
                if str(
                    track.get("video_range_type", "")
                ).upper().startswith("HDR10")
                else ""
            )
            or (
                "hlg"
                if str(
                    track.get("video_range_type", "")
                ).upper().startswith("HLG")
                else ""
            )
        )

        if hdr:
            info["hdrtype"] = hdr

        # Preserve additional probed values for skins/addons even where Kodi's
        # native stream API has no corresponding setter.
        for key in (
            "profile",
            "level",
            "bitrate",
            "frame_rate",
            "bit_depth",
            "color_space",
            "color_transfer",
            "color_primaries",
            "pixel_format",
        ):
            value = track.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Video.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

        try:
            stream = xbmc.VideoStreamDetail(
                int(track.get("width") or 0),
                int(track.get("height") or 0),
                aspect,
                duration,
                str(track.get("codec") or ""),
                "",
                str(track.get("language") or ""),
                str(hdr or ""),
            )
            tag.addVideoStream(stream)
        except Exception:
            pass

    # Fallback for detail responses containing only version-level video data.
    if not video_tracks and (
        version.get("codec_video") or version.get("resolution")
    ):
        resolution = str(version.get("resolution") or "")
        width = height = 0

        if "x" in resolution.lower():
            try:
                width, height = [
                    int(v)
                    for v in resolution.lower().split("x", 1)
                ]
            except (TypeError, ValueError):
                pass
        elif resolution.lower().endswith("p"):
            try:
                height = int(resolution[:-1])
            except ValueError:
                pass

        info = {
            "codec": version.get("codec_video") or "",
            "duration": duration,
        }

        if width:
            info["width"] = width
        if height:
            info["height"] = height

        try:
            tag.addVideoStream(
                xbmc.VideoStreamDetail(
                    width,
                    height,
                    _aspect_ratio(version.get("aspect_ratio")),
                    duration,
                    str(version.get("codec_video") or ""),
                )
            )
        except Exception:
            pass

    for track in audio_tracks:
        info = {}

        if track.get("codec"):
            info["codec"] = track["codec"]
        if track.get("language"):
            info["language"] = track["language"]
        if track.get("channels"):
            info["channels"] = int(track["channels"])

        for key in (
            "title",
            "profile",
            "layout",
            "bitrate",
            "sample_rate",
            "bit_depth",
        ):
            value = track.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Audio.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

        try:
            tag.addAudioStream(
                xbmc.AudioStreamDetail(
                    int(track.get("channels") or 0),
                    str(track.get("codec") or ""),
                    str(track.get("language") or ""),
                )
            )
        except Exception:
            pass

    for track in subtitle_tracks:
        language = str(
            track.get("language")
            or track.get("title")
            or ""
        )

        if language:
            try:
                tag.addSubtitleStream(
                    xbmc.SubtitleStreamDetail(language)
                )
            except Exception:
                pass


def set_detail_metadata(list_item, detail, client, file_id=None):
    """Apply Silo detail-only metadata such as cast, crew and stream tracks."""
    if not detail:
        return

    # CatalogItemDetail embeds the complete CatalogItem. Apply those
    # fields here too because detail-only responses contain IDs, countries and
    # other values that are not present on the browse card.
    set_catalog_metadata(
        list_item,
        detail,
        client,
    )

    tag = list_item.getVideoInfoTag()

    if detail.get("sort_title"):
        try:
            tag.setSortTitle(detail["sort_title"])
        except Exception:
            pass

    if detail.get("original_title"):
        try:
            tag.setOriginalTitle(detail["original_title"])
        except Exception:
            pass

    if detail.get("first_air_date"):
        try:
            tag.setFirstAired(str(detail["first_air_date"]))
        except Exception:
            pass

    if detail.get("air_date"):
        try:
            tag.setPremiered(str(detail["air_date"]))
        except Exception:
            pass

    cast = []
    cast_info = []
    cast_names = []
    cast_and_roles = []

    for person in detail.get("cast") or []:
        name = person.get("name")
        if not name:
            continue

        name = str(name)
        role = str(person.get("character") or "")
        thumbnail = client.abs_url(person.get("photo_url") or "")

        try:
            order = int(person.get("order") or 0)
        except (TypeError, ValueError):
            order = 0

        # Kodi 20+ uses xbmc.Actor objects for InfoTagVideo.setCast().
        try:
            cast.append(
                xbmc.Actor(
                    name,
                    role,
                    order,
                    thumbnail,
                )
            )
        except Exception:
            pass

        actor_info = {"name": name}
        if role:
            actor_info["role"] = role
        if thumbnail:
            actor_info["thumbnail"] = thumbnail
        if order:
            actor_info["order"] = order

        cast_info.append(actor_info)
        cast_names.append(name)

        if role:
            cast_and_roles.append((name, role))
        else:
            cast_and_roles.append((name, ""))

    if cast:
        try:
            tag.setCast(cast)
        except Exception:
            pass

    # Keep Kodi's older ListItem representation as well. It is deprecated in
    # Kodi 20+, but remains supported and some skins/add-ons still consume it.
    if cast_info:
        # InfoTagVideo.setCast() above is the current Kodi API and retains the
        # actor thumbnail supplied by Silo.
        list_item.setProperty(
            "Silo.CastNames",
            " / ".join(cast_names),
        )

    directors = []
    writers = []
    credits = []

    for person in detail.get("crew") or []:
        name = person.get("name")
        job = str(person.get("job") or "").strip()

        if not name:
            continue

        name = str(name)
        job_lower = job.lower()

        if job:
            credits.append(name)

        # Silo can return detailed crew job labels, not only the bare "Director"
        # or "Writer" values.
        if (
            job_lower == "director"
            or job_lower.endswith(" director")
            or "director" in job_lower
        ):
            directors.append(name)

        if any(
            word in job_lower
            for word in (
                "writer",
                "screenplay",
                "screenwriter",
                "story",
                "novel",
            )
        ):
            writers.append(name)

    # Remove duplicates while preserving Silo's order.
    directors = list(dict.fromkeys(directors))
    writers = list(dict.fromkeys(writers))
    credits = list(dict.fromkeys(credits))

    try:
        if directors:
            tag.setDirectors(directors)
    except Exception:
        pass

    try:
        if writers:
            tag.setWriters(writers)
    except Exception:
        pass

    if credits:
        list_item.setProperty(
            "Silo.Crew",
            " / ".join(credits),
        )

    # Keep complete crew names/jobs available without requiring JSON in the
    # Kodi runtime. Thumbnail-bearing crew data remains in the structured
    # Silo.CrewDetails property only when a skin specifically needs it.
    crew_details = []
    for person in detail.get("crew") or []:
        if not person.get("name"):
            continue
        crew_details.append(
            "%s (%s)" % (
                str(person.get("name")),
                str(person.get("job") or ""),
            )
        )

    if crew_details:
        list_item.setProperty(
            "Silo.CrewDetails",
            " / ".join(crew_details),
        )

    # Detail-only viewer and series information.
    if detail.get("user_rating") is not None:
        try:
            tag.setUserRating(int(detail["user_rating"]))
        except (TypeError, ValueError):
            pass

    for key in (
        "season_count",
        "episode_count",
        "air_time",
        "air_timezone",
        "effective_subtitle_language",
        "effective_subtitle_mode",
        "effective_version_resolution",
        "effective_version_codec_video",
        "effective_version_edition_key",
    ):
        value = detail.get(key)
        if value not in (None, ""):
            list_item.setProperty(
                "Silo.%s" % "".join(
                    part.title() for part in key.split("_")
                ),
                str(value),
            )

    # Marker data remains available as a simple property. Complex marker JSON
    # is intentionally not serialised here because it is not required by Kodi
    # for pre-playback metadata.
    for marker_name in ("intro", "credits", "recap", "preview"):
        marker = detail.get(marker_name)
        if marker:
            list_item.setProperty(
                "Silo.Marker.%s" % marker_name.title(),
                str(marker),
            )

    # Native date-added and ID infolabels are still consumed by some skins.
    added_at = detail.get("added_at")
    if added_at:
        try:
            tag.setDateAdded(str(added_at))
        except Exception:
            pass

    unique_ids = {}
    if detail.get("imdb_id"):
        unique_ids["imdb"] = str(detail["imdb_id"])
    if detail.get("tmdb_id"):
        unique_ids["tmdb"] = str(detail["tmdb_id"])
    if detail.get("tvdb_id"):
        unique_ids["tvdb"] = str(detail["tvdb_id"])

    if unique_ids:
        try:
            default_id = "imdb" if "imdb" in unique_ids else next(iter(unique_ids))
            tag.setUniqueIDs(
                unique_ids,
                default_id,
            )
        except Exception:
            for key, value in unique_ids.items():
                try:
                    tag.setUniqueID(value, key, key == "imdb")
                except Exception:
                    pass

    version = _detail_version(detail, file_id)
    set_stream_details(list_item, version)

    # Full-detail runtime is the actual selected file duration in seconds.
    if version.get("duration"):
        try:
            duration = int(version["duration"])
            tag.setDuration(duration)
        except (TypeError, ValueError):
            pass


def set_season_metadata(list_item, season, client, series_id=None):
    """Populate a Kodi season ListItem from Silo's season response."""
    if not isinstance(season, dict):
        return

    tag = list_item.getVideoInfoTag()
    tag.setMediaType("season")

    season_number = season.get(
        "season_number",
        season.get("number"),
    )

    if season_number is not None:
        try:
            tag.setSeason(int(season_number))
        except (TypeError, ValueError):
            pass

    title = season.get("title")
    if title:
        tag.setTitle(str(title))

    overview = season.get("overview")
    if overview:
        try:
            tag.setPlot(str(overview))
            tag.setPlotOutline(str(overview))
        except Exception:
            pass

    air_date = season.get("air_date")
    if air_date:
        try:
            tag.setPremiered(str(air_date))
        except Exception:
            pass

    episode_count = season.get("episode_count")
    try:
        episode_count = max(0, int(episode_count or 0))
    except (TypeError, ValueError):
        episode_count = 0

    if episode_count > 0:
        # Keep the standard Kodi episode-count fields available to skins.
        for key, value in (
            ("totalepisodes", episode_count),
            ("numepisodes", episode_count),
        ):
            list_item.setProperty(key, str(value))
        list_item.setProperty("Silo.EpisodeCount", str(episode_count))

    content_id = get_content_id(season)
    if content_id:
        list_item.setProperty("Silo.ContentID", str(content_id))

    play_content_id = season.get("play_content_id")
    if play_content_id:
        list_item.setProperty(
            "Silo.PlayContentID",
            str(play_content_id),
        )

    if series_id:
        list_item.setProperty("Silo.SeriesID", str(series_id))

    list_item.setProperty(
        "Silo.IsSpecials",
        "true" if bool(season.get("is_specials", False)) else "false",
    )

    poster_url = season.get("poster_url")
    poster_thumbhash = season.get("poster_thumbhash")

    if poster_url:
        set_art(
            list_item,
            client,
            poster=poster_url,
        )

    if poster_thumbhash:
        list_item.setProperty(
            "Silo.PosterThumbhash",
            str(poster_thumbhash),
        )

    # Preserve every season field supplied by the endpoint so skins/add-ons
    # can access the server data even where Kodi has no native setter.
    for key in (
        "content_id",
        "play_content_id",
        "series_id",
        "season_number",
        "title",
        "overview",
        "air_date",
        "episode_count",
        "is_specials",
    ):
        value = season.get(key)
        if value not in (None, ""):
            property_name = "Silo.Season.%s" % "".join(
                part.title() for part in key.split("_")
            )
            list_item.setProperty(property_name, str(value))


def set_watch_state(list_item, progress, content_type=None):
    """Apply Silo's current watched/resume state to a Kodi ListItem.

    Kodi uses the VideoInfoTag methods setPlaycount() and setResumePoint().
    The exact capitalization matters: it is setPlaycount, not setPlayCount.

    This state is primarily for Kodi's library UI. play() still performs a
    fresh server lookup immediately before playback, so the displayed value is
    never trusted as the final resume position.
    """
    if not progress:
        return

    completed = bool(progress.get("completed", False))
    position, duration = get_progress_position(progress)
    tag = list_item.getVideoInfoTag()

    # Runtime is independent of resume state. Use Kodi's native
    # VideoInfoTag duration field so directory views receive the duration.
    if duration > 0:
        duration_int = int(round(duration))
        tag.setDuration(duration_int)

    if completed:
        # Silo says the item is fully watched.
        tag.setPlaycount(1)
        return

    # Anything incomplete is explicitly unwatched/in progress.
    tag.setPlaycount(0)

    # Store the server resume marker so Kodi/skins can show the item as
    # partially played. Kodi documents setResumePoint(time, totalTime) for this.
    if position > 0 and duration > 0:
        tag.setResumePoint(position, duration)


def get_content_id(item):
    """Return a Silo catalog item's content ID."""
    return item.get("content_id") or item.get("id")


def add_catalog_item(client, item, library_id):
    """Convert one Silo catalog object into a Kodi ListItem."""
    content_id = get_content_id(item)

    if not content_id:
        log("Skipping catalog item with no content ID", xbmc.LOGWARNING)
        return

    title = item.get("title") or item.get("name") or "Unknown"
    media_type = (item.get("type") or item.get("media_type") or "").lower()

    list_item = xbmcgui.ListItem(label=title)
    tag = list_item.getVideoInfoTag()
    tag.setTitle(title)

    set_catalog_metadata(list_item, item, client)

    # Copy basic metadata that Kodi can display.
    if item.get("year"):
        try:
            tag.setYear(int(item["year"]))
        except (TypeError, ValueError):
            pass

    if item.get("plot"):
        tag.setPlot(item["plot"])

    set_art(
        list_item,
        client,
        # Current Silo v2 artwork fields.
        poster=(
            item.get("poster_url")
            or item.get("poster")
            or item.get("image")
            or item.get("artwork")
            or item.get("thumbnail")
        ),
        backdrop=item.get("backdrop_url"),
        logo=item.get("logo_url"),
    )

    # Use the watched/resume state already included in the catalog response.
    # This avoids an extra full progress-table request for every library load.
    set_watch_state(
        list_item,
        catalog_progress(item),
        media_type,
    )

    is_playable = media_type in PLAYABLE
    play_content_id = item.get("play_content_id") or content_id

    if is_playable:
        list_item.setProperty("IsPlayable", "true")

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="play",
                content_id=play_content_id,
                library_id=library_id,
            ),
            list_item,
            False,
        )
        return

    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(
            action="seasons",
            series_id=content_id,
            library_id=library_id,
        ),
        list_item,
        True,
    )



def search_silo(client):
    """Prompt for a search term and display Silo's library-wide results."""
    query = xbmcgui.Dialog().input(
        "Search Silo",
    ).strip()

    if not query:
        xbmcplugin.setContent(HANDLE, "files")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    list_search_results(client, query, 1)


def display_title_for_catalog_item(catalog_item):
    """Build the same visible label used by Search for catalog items."""
    title = (
        catalog_item.get("title")
        or catalog_item.get("name")
        or "Unknown"
    )
    media_type = (
        catalog_item.get("type")
        or catalog_item.get("media_type")
        or ""
    ).lower()

    if media_type == "movie":
        return "[Movie] %s" % title

    if media_type == "series":
        return "[TV Show] %s" % title

    if media_type == "episode":
        series_title = (
            catalog_item.get("series_title")
            or catalog_item.get("series_name")
            or ""
        )
        season_number = catalog_item.get("season_number")
        episode_number = catalog_item.get("episode_number")

        episode_code = ""
        if season_number is not None and episode_number is not None:
            try:
                episode_code = "S%02dE%02d" % (
                    int(season_number),
                    int(episode_number),
                )
            except (TypeError, ValueError):
                episode_code = ""

        if series_title and episode_code:
            return "[Episode] %s - %s - %s" % (
                series_title,
                episode_code,
                title,
            )

        if series_title:
            return "[Episode] %s - %s" % (
                series_title,
                title,
            )

        if episode_code:
            return "[Episode] %s - %s" % (
                episode_code,
                title,
            )

        return "[Episode] %s" % title

    return title


def build_catalog_list_item(client, catalog_item, detail=None, progress=None, series_rollup=None, season_rollup=None):
    """Build a Kodi ListItem using the same metadata/watch pipeline everywhere."""
    content_id = get_content_id(catalog_item)
    title = catalog_item.get("title") or catalog_item.get("name") or "Unknown"
    media_type = (catalog_item.get("type") or catalog_item.get("media_type") or "").lower()

    item = xbmcgui.ListItem(label=display_title_for_catalog_item(catalog_item))
    item.getVideoInfoTag().setTitle(title)

    set_catalog_metadata(item, catalog_item, client)
    set_art(
        item,
        client,
        poster=(catalog_item.get("poster_url") or catalog_item.get("poster")
                or catalog_item.get("image") or catalog_item.get("artwork")
                or catalog_item.get("thumbnail")),
        backdrop=catalog_item.get("backdrop_url"),
        logo=catalog_item.get("logo_url"),
        still=catalog_item.get("still_url") or catalog_item.get("still"),
    )

    if detail:
        set_detail_metadata(item, detail, client)

    display_progress = progress if progress is not None else catalog_progress(catalog_item)
    set_watch_state(item, display_progress, media_type)

    if media_type == "series":
        set_container_watch_state(item, series_rollup)
    elif media_type == "season":
        set_container_watch_state(item, season_rollup)

    return item, media_type, content_id, title, display_progress


def list_search_results(client, query, page=1):
    """Display one page of Silo's server-side library-wide search results."""
    query = str(query or "").strip()

    if not query:
        xbmcplugin.setContent(HANDLE, "files")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    search_page_size = min(get_directory_page_size(), SEARCH_PAGE_SIZE)
    offset = (page_number - 1) * search_page_size

    try:
        data = client.search_catalog(
            query,
            limit=search_page_size,
            offset=offset,
        ) or {}
    except SiloError as exc:
        log(
            "Silo search failed for %r: %s" % (query, exc),
            xbmc.LOGERROR,
        )
        notify("Search failed: %s" % str(exc)[:180])
        xbmcplugin.endOfDirectory(HANDLE)
        return

    items = data.get("items") or []
    has_more = bool(data.get("has_more"))

    log(
        "Silo search query=%r returned %d item(s), has_more=%s"
        % (query, len(items), has_more)
    )

    xbmcplugin.setPluginCategory(
        HANDLE,
        "Search: %s" % query,
    )
    xbmcplugin.setContent(HANDLE, "videos")

    if page_number > 1:
        previous_item = xbmcgui.ListItem(label="Previous Page")
        previous_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="search",
                query=query,
                page=page_number - 1,
            ),
            previous_item,
            True,
        )

    # Match normal library/episode browsing: fetch all extended detail
    # metadata before Kodi receives the search result directory. This means
    # cast, crew, ratings, runtime and full stream information are available
    # immediately, rather than waiting until playback.
    detail_map = fetch_detail_metadata(
        client,
        items,
        None,
    )

    # Match normal library browsing: fetch the current in-progress
    # server records so search results reflect watch activity that was added
    # on Silo after the search results were generated.
    try:
        in_progress_map = client.in_progress_map()
    except SiloError as exc:
        log(
            "Unable to retrieve in-progress Silo records for search results: %s"
            % exc,
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    # Fetch the same authoritative season rollups used by library browsing
    # so TV shows returned by search can also show server-side watch state.
    series_watch_map, season_watch_map = fetch_series_watch_data(
        client,
        items,
    )

    # Keep all media types in the same result page, but group them into
    # Movies, TV Shows and Episodes so a common title (for example "Christmas")
    # is immediately distinguishable.
    grouped_items = {
        "movie": [],
        "series": [],
        "episode": [],
    }
    other_items = []

    for catalog_item in items:
        media_type = (
            catalog_item.get("type")
            or catalog_item.get("media_type")
            or ""
        ).lower()
        if media_type in grouped_items:
            grouped_items[media_type].append(catalog_item)
        else:
            other_items.append(catalog_item)

    ordered_items = (
        grouped_items["movie"]
        + grouped_items["series"]
        + grouped_items["episode"]
        + other_items
    )

    batch = []

    for catalog_item in ordered_items:
        content_id = get_content_id(catalog_item)
        if not content_id:
            continue

        title = (
            catalog_item.get("title")
            or catalog_item.get("name")
            or "Unknown"
        )
        media_type = (
            catalog_item.get("type")
            or catalog_item.get("media_type")
            or ""
        ).lower()

        display_title = display_title_for_catalog_item(catalog_item)

        display_progress = in_progress_map.get(str(content_id)) or catalog_progress(catalog_item)

        item, media_type, content_id, title, display_progress = build_catalog_list_item(
            client,
            catalog_item,
            detail=detail_map.get(str(content_id)),
            progress=display_progress,
            series_rollup=series_watch_map.get(str(content_id)),
            season_rollup=(
                season_watch_map.get(str(content_id))
                or season_watch_map.get(
                    "%s:%s" % (
                        catalog_item.get("series_id"),
                        catalog_item.get("season_number"),
                    )
                )
            ),
        )

        if media_type in PLAYABLE:
            item.setProperty("IsPlayable", "true")
            url = build_url(
                action="play",
                content_id=(
                    catalog_item.get("play_content_id")
                    or content_id
                ),
                resume_available=int(
                    has_usable_resume(display_progress)
                ),
            )
            batch.append((url, item, False))
        elif media_type == "series":
            url = build_url(
                action="seasons",
                series_id=content_id,
            )
            batch.append((url, item, True))
        elif media_type == "season":
            url = build_url(
                action="season",
                series_id=catalog_item.get("series_id") or "",
                season_number=catalog_item.get("season_number"),
            )
            batch.append((url, item, True))
        else:
            # Keep unusual/non-playable results visible rather than creating a
            # broken seasons URL.
            url = build_url(
                action="search",
                query=query,
                page=page_number,
            )
            batch.append((url, item, False))

    if batch:
        xbmcplugin.addDirectoryItems(
            HANDLE,
            batch,
            totalItems=len(ordered_items) + (1 if has_more else 0),
        )

    if has_more:
        next_item = xbmcgui.ListItem(label="Next Page")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="search",
                query=query,
                page=page_number + 1,
            ),
            next_item,
            True,
        )

    if not items:
        notify("No results found for: %s" % query)

    xbmcplugin.endOfDirectory(HANDLE)


def open_settings(client):
    """Open this add-on's Kodi settings dialog."""
    client.sync_settings()
    ADDON.openSettings()
    xbmc.executebuiltin("Container.Refresh")


def add_your_stuff_folder():
    """Add the Silo web client's personal destinations."""
    item = xbmcgui.ListItem(label="Your Stuff")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="your_stuff"),
        item,
        True,
    )


def _watch_party_socket_url(client, room_id):
    """Build the room WebSocket URL like Silo's web client."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(client.base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = (
        "/api/v2/watch-together/rooms/%s/ws"
        % quote(str(room_id), safe="")
    )
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def _watch_party_http_origin(client):
    """Return the HTTP origin Silo expects on native WebSocket handshakes."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(client.base)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "",
            "",
            "",
            "",
        )
    )


class _WatchPartyUiState:
    """Thread-safe Watch Party state shared by the socket monitor and UI."""

    def __init__(self, status):
        self._lock = threading.Lock()
        self.status = str(status)
        self.lobby = True
        self.finished = False
        self.ended = False

    def update(self, status=None, lobby=None, finished=None, ended=None):
        with self._lock:
            if status is not None:
                self.status = str(status)
            if lobby is not None:
                self.lobby = bool(lobby)
            if finished is not None:
                self.finished = bool(finished)
            if ended is not None:
                self.ended = bool(ended)

    def snapshot(self):
        with self._lock:
            return (
                self.status,
                self.lobby,
                self.finished,
                self.ended,
            )


def _watch_party_window():
    return xbmcgui.Window(10000)


def _watch_party_set_members(members):
    """Store the server Watch Party member readiness snapshot."""
    window = _watch_party_window()
    normalized = []

    for member in members or []:
        if not isinstance(member, dict):
            continue

        normalized.append(
            {
                "display_name": str(
                    member.get("display_name") or "Participant"
                ),
                "is_host": bool(member.get("is_host")),
                "is_self": bool(member.get("is_self")),
                "connected": bool(member.get("connected")),
                "is_ready": bool(member.get("is_ready")),
                "is_buffering": bool(member.get("is_buffering")),
                "is_syncing": bool(member.get("is_syncing")),
                "lobby_ready": bool(member.get("lobby_ready")),
            }
        )

    try:
        window.setProperty(
            "Silo.WatchParty.Members",
            json.dumps(
                normalized,
                separators=(",", ":"),
            ),
        )
    except Exception:
        window.setProperty("Silo.WatchParty.Members", "[]")


def _watch_party_clear_properties(window=None):
    window = window or _watch_party_window()
    for property_name in (
        "Silo.WatchParty.RoomID",
        "Silo.WatchParty.RoomToken",
        "Silo.WatchParty.Code",
        "Silo.WatchParty.Status",
        "Silo.WatchParty.Lobby",
        "Silo.WatchParty.Finished",
        "Silo.WatchParty.Ended",
        "Silo.WatchParty.LeaveRequested",
        "Silo.WatchParty.Members",
        "Silo.WatchParty.LobbyVisible",
    ):
        try:
            window.clearProperty(property_name)
        except Exception:
            try:
                window.setProperty(property_name, "")
            except Exception:
                pass


def _watch_party_reset_lobby():
    """Reset the Watch Party folder to its disconnected/joinable state."""
    window = _watch_party_window()

    # Clear room credentials and stale finished/playing state together so a
    # refreshed lobby cannot continue displaying the previous room.
    _watch_party_clear_properties(window)

    window.setProperty(
        "Silo.WatchParty.Status",
        "Not connected to a Watch Party.",
    )
    window.setProperty("Silo.WatchParty.Lobby", "true")
    window.setProperty("Silo.WatchParty.Finished", "false")
    window.setProperty("Silo.WatchParty.Ended", "false")
    window.setProperty("Silo.WatchParty.Members", "[]")
    window.setProperty("Silo.WatchParty.LobbyVisible", "true")


def _watch_party_refresh_lobby():
    """Refresh the live Watch Party lobby directory."""
    try:
        window = _watch_party_window()
        if window.getProperty("Silo.WatchParty.LobbyVisible").lower() != "true":
            return

        # The lobby directory is deliberately non-cacheable. A normal Kodi
        # container refresh can therefore invoke the plugin again and rebuild
        # the visible items from the latest Watch Party window properties.
        xbmc.executebuiltin("Container.Refresh")
    except Exception as exc:
        log(
            "Unable to refresh Watch Party lobby directory: %s" % exc,
            xbmc.LOGDEBUG,
        )

def _watch_party_update_window_state(ui_state):
    """Mirror monitor state into Kodi global window properties."""
    status, lobby, finished, ended = ui_state.snapshot()
    window = _watch_party_window()

    window.setProperty("Silo.WatchParty.Status", status)
    window.setProperty("Silo.WatchParty.Lobby", "true" if lobby else "false")
    window.setProperty("Silo.WatchParty.Finished", "true" if finished else "false")
    window.setProperty("Silo.WatchParty.Ended", "true" if ended else "false")

    return status, lobby, finished, ended


def list_watch_party_lobby():
    """Display the current Watch Party state and participants."""
    window = _watch_party_window()
    status = window.getProperty("Silo.WatchParty.Status") or "Connecting to Watch Party..."
    room_code = window.getProperty("Silo.WatchParty.Code") or ""
    lobby = window.getProperty("Silo.WatchParty.Lobby").lower() == "true"
    finished = window.getProperty("Silo.WatchParty.Finished").lower() == "true"
    ended = window.getProperty("Silo.WatchParty.Ended").lower() == "true"

    xbmcplugin.setPluginCategory(HANDLE, "Watch Party")
    xbmcplugin.setContent(HANDLE, "files")

    if not room_code and not finished:
        info_item = xbmcgui.ListItem(label=status)
        info_item.setArt({"icon": "DefaultInfo.png"})
        info_item.setInfo(
            "video",
            {
                "title": "Watch Party",
                "plot": status,
            },
        )
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="watch_party_join"),
            info_item,
            True,
        )

        join_item = xbmcgui.ListItem(label="Join Watch Party")
        join_item.setArt({"icon": "DefaultFolder.png"})
        join_item.setInfo(
            "video",
            {
                "title": "Join Watch Party",
                "plot": "Enter a Watch Party room code.",
            },
        )
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="watch_party_join"),
            join_item,
            True,
        )

    elif finished:
        message = (
            "The Watch Party has ended."
            if ended
            else "Disconnected from the Watch Party."
        )
        info_item = xbmcgui.ListItem(label=message)
        info_item.setArt({"icon": "DefaultInfo.png"})
        info_item.setInfo(
            "video",
            {
                "title": "Watch Party",
                "plot": message,
            },
        )
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="watch_party_join"),
            info_item,
            True,
        )

    else:
        # There is one explicit Leave action for every connected room state.
        # Kodi's normal '..' parent entry remains available independently.
        leave_item = xbmcgui.ListItem(label="Leave Watch Party")
        leave_item.setArt({"icon": "DefaultFolder.png"})
        leave_item.setInfo(
            "video",
            {
                "title": "Leave Watch Party",
                "plot": "Leave the room and return to Silo.",
            },
        )
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="watch_party_leave"),
            leave_item,
            False,
        )

        state_label = status
        if room_code:
            state_label = "%s — Room %s" % (status, room_code)

        state_item = xbmcgui.ListItem(label=state_label)
        state_item.setArt({
            "icon": "DefaultVideo.png" if not lobby else "DefaultInfo.png"
        })
        state_item.setInfo(
            "video",
            {
                "title": "Watch Party",
                "plot": status,
            },
        )
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="watch_party_lobby"),
            state_item,
            False,
        )

        try:
            members = json.loads(
                window.getProperty("Silo.WatchParty.Members") or "[]"
            )
        except (TypeError, ValueError):
            members = []

        if members:
            if lobby:
                ready_count = sum(
                    1
                    for member in members
                    if member.get("lobby_ready")
                )
                ready_label = "lobby ready"
            else:
                ready_count = sum(
                    1
                    for member in members
                    if member.get("is_ready")
                )
                ready_label = "ready"

            header = xbmcgui.ListItem(
                label="Participants — %d/%d %s"
                % (ready_count, len(members), ready_label)
            )
            header.setArt({"icon": "DefaultInfo.png"})
            header.setInfo(
                "video",
                {
                    "title": "Watch Party Participants",
                    "plot": "%d of %d participants are %s."
                    % (ready_count, len(members), ready_label),
                },
            )
            xbmcplugin.addDirectoryItem(
                HANDLE,
                build_url(action="watch_party_lobby"),
                header,
                False,
            )

            for member in members:
                name = str(member.get("display_name") or "Participant")
                if member.get("is_self"):
                    name += " (You)"
                if member.get("is_host"):
                    name += " (Host)"

                if not member.get("connected"):
                    member_state = "Disconnected"
                elif lobby:
                    member_state = (
                        "Lobby ready"
                        if member.get("lobby_ready")
                        else "Not ready"
                    )
                elif member.get("is_buffering"):
                    member_state = "Buffering"
                elif member.get("is_syncing"):
                    member_state = "Syncing"
                elif member.get("is_ready"):
                    member_state = "Ready"
                else:
                    member_state = "Not ready"

                member_item = xbmcgui.ListItem(
                    label="%s — %s" % (name, member_state)
                )
                member_item.setArt({"icon": "DefaultInfo.png"})
                member_item.setInfo(
                    "video",
                    {
                        "title": name,
                        "plot": "Watch Party status: %s." % member_state,
                    },
                )
                xbmcplugin.addDirectoryItem(
                    HANDLE,
                    build_url(action="watch_party_lobby"),
                    member_item,
                    False,
                )

    # The Watch Party lobby is live state, not a cacheable media directory.
    # Disable Kodi's directory cache so Container.Refresh rebuilds it from the
    # current server/member state.
    xbmcplugin.endOfDirectory(
        HANDLE,
        succeeded=True,
        updateListing=False,
        cacheToDisc=False,
    )


def _watch_party_connection_active():
    """Return whether this Kodi session still has an active Watch Party connection."""
    window = _watch_party_window()

    room_id = window.getProperty("Silo.WatchParty.RoomID")
    room_token = window.getProperty("Silo.WatchParty.RoomToken")
    finished = window.getProperty("Silo.WatchParty.Finished").lower() == "true"
    leaving = window.getProperty("Silo.WatchParty.LeaveRequested").lower() == "true"

    return bool(room_id and room_token and not finished and not leaving)


def _watch_party_join(client):
    """Open the existing Watch Party or join a new one by room code."""
    if _watch_party_connection_active():
        # The background monitor remains alive when the user navigates away
        # from the lobby. Reuse that connection instead of creating another
        # monitor or asking for the room code again.
        list_watch_party_lobby()
        return
    code = xbmcgui.Dialog().input(
        "Watch Party code",
        type=xbmcgui.INPUT_ALPHANUM,
    ).strip().upper()

    if not code:
        xbmcplugin.endOfDirectory(HANDLE)
        return

    response = client.watch_party_join(code=code)
    room = response.get("room") or {}
    room_token = response.get("room_access_token")
    room_id = room.get("room_id")

    if not room_id or not room_token:
        raise SiloError("Silo did not return Watch Party room credentials.")

    room_code = str(room.get("code") or code)
    window = _watch_party_window()

    # Keep room proof only in Kodi's current window/session. It is not written
    # to addon settings or the config file.
    window.setProperty("Silo.WatchParty.RoomID", str(room_id))
    window.setProperty("Silo.WatchParty.RoomToken", str(room_token))
    window.setProperty("Silo.WatchParty.Code", room_code)
    window.setProperty(
        "Silo.WatchParty.Status",
        "Joined Watch Party %s. Waiting for the host to start playback."
        % room_code,
    )
    window.setProperty("Silo.WatchParty.Lobby", "true")
    window.setProperty("Silo.WatchParty.Finished", "false")
    window.setProperty("Silo.WatchParty.Ended", "false")
    window.clearProperty("Silo.WatchParty.LeaveRequested")

    stop_event = threading.Event()
    state = _WatchPartyUiState(
        "Joined Watch Party %s. Waiting for the host to start playback."
        % room_code
    )
    result = {"error": None}

    def monitor_runner():
        try:
            _watch_party_monitor(
                client,
                room_id,
                room_token,
                stop_event=stop_event,
                ui_state=state,
            )
        except Exception as exc:
            result["error"] = exc
            state.update(
                status="Watch Party disconnected: %s" % exc,
                lobby=False,
                finished=True,
            )
            _watch_party_update_window_state(state)
            _watch_party_reset_lobby()
            _watch_party_refresh_lobby()

    thread = threading.Thread(
        target=monitor_runner,
        name="SiloWatchParty",
    )
    # The monitor owns the room WebSocket. Keeping it non-daemon prevents
    # Kodi from ending the plugin invocation immediately after the lobby
    # directory is returned, which otherwise leaves playback running with no
    # Watch Party transport connection.
    thread.daemon = False
    thread.start()

    # Finish the current join action with the normal Kodi directory
    # contents. The monitor remains alive in the background. Later refreshes
    # are limited to an already-open lobby container, so they never re-open
    # the room-code prompt.
    list_watch_party_lobby()


def _watch_party_leave():
    """Leave the Watch Party, stop its local playback, and leave the lobby."""
    window = _watch_party_window()

    if not window.getProperty("Silo.WatchParty.RoomID"):
        _watch_party_clear_properties(window)
        xbmc.executebuiltin("Container.Back")
        return

    # LeaveRequested must remain set until the background monitor handles it.
    # Do not call _watch_party_reset_lobby() here because that clears the flag
    # before the monitor can close the room connection and playback session.
    window.setProperty("Silo.WatchParty.LeaveRequested", "true")
    window.setProperty("Silo.WatchParty.Status", "Leaving Watch Party...")
    window.setProperty("Silo.WatchParty.Lobby", "false")
    window.setProperty("Silo.WatchParty.Finished", "true")
    window.setProperty("Silo.WatchParty.Ended", "false")

    # Explicitly stop the Kodi player as part of leaving. The Watch Party
    # player's onPlayBackStopped callback will see the leave request and
    # perform the normal room/session shutdown as well. The monitor also polls
    # LeaveRequested, so this remains safe when nothing is currently playing.
    try:
        player = xbmc.Player()
        if player.isPlaying():
            log(
                "Stopping Kodi playback because the user left the Watch Party.",
                xbmc.LOGINFO,
            )
            player.stop()
    except Exception as exc:
        log(
            "Unable to stop Kodi playback while leaving Watch Party: %s"
            % exc,
            xbmc.LOGWARNING,
        )

    xbmcgui.Dialog().notification(
        "Watch Party",
        "Left Watch Party.",
        xbmcgui.NOTIFICATION_INFO,
        2000,
    )

    # Navigate back out of the dedicated Watch Party lobby rather than
    # rendering a disconnected state inside it.
    xbmc.executebuiltin("Container.Back")




class _SiloWebSocket:
    """Minimal RFC 6455 WebSocket client for Silo's room protocol."""

    def __init__(self, url, protocols, timeout=15, origin=None):
        self.url = url
        self.protocols = list(protocols or [])
        self.timeout = float(timeout or 15)
        self.origin = str(origin or "").strip()
        self.sock = None
        self._buffer = b""

    def connect(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise SiloError("Invalid Watch Party WebSocket URL.")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        host = parsed.hostname
        host_header = host
        if ":" in host and not host.startswith("["):
            host_header = "[%s]" % host

        if (parsed.scheme == "ws" and port != 80) or (
            parsed.scheme == "wss" and port != 443
        ):
            host_header = "%s:%d" % (host_header, port)

        try:
            sock = socket.create_connection(
                (host, port),
                timeout=self.timeout,
            )
        except Exception as exc:
            log(
                "Watch Party WebSocket TCP connection failed to %s:%d: %s"
                % (host, port, exc),
                xbmc.LOGERROR,
            )
            raise SiloError(
                "Watch Party WebSocket TCP connection failed: %s" % exc
            )

        log(
            "Watch Party WebSocket TCP connection established to %s:%d"
            % (host, port),
            xbmc.LOGDEBUG,
        )

        if parsed.scheme == "wss":
            try:
                context = ssl.create_default_context()
                sock = context.wrap_socket(
                    sock,
                    server_hostname=host,
                )
            except Exception as exc:
                try:
                    sock.close()
                except Exception:
                    pass

                log(
                    "Watch Party WebSocket TLS handshake failed: %s"
                    % exc,
                    xbmc.LOGERROR,
                )
                raise SiloError(
                    "Watch Party WebSocket TLS handshake failed: %s" % exc
                )

            log(
                "Watch Party WebSocket TLS connection established",
                xbmc.LOGDEBUG,
            )

        self.sock = sock
        self.sock.settimeout(self.timeout)

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Origin: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: %s\r\n"
            "\r\n"
        ) % (
            path,
            host_header,
            self.origin,
            key,
            ", ".join(self.protocols),
        )

        try:
            self.sock.sendall(request.encode("ascii"))
        except Exception as exc:
            self.close()
            log(
                "Watch Party WebSocket upgrade request failed to send: %s"
                % exc,
                xbmc.LOGERROR,
            )
            raise SiloError(
                "Watch Party WebSocket upgrade request failed: %s" % exc
            )

        log(
            "Watch Party WebSocket upgrade request sent to %s"
            % path,
            xbmc.LOGDEBUG,
        )

        try:
            response = self._read_http_headers()
        except socket.timeout:
            self.close()
            log(
                "Watch Party WebSocket handshake timed out waiting for HTTP 101",
                xbmc.LOGERROR,
            )
            raise SiloError(
                "Watch Party WebSocket handshake timed out waiting for HTTP 101"
            )
        except Exception:
            self.close()
            raise

        lines = response.decode("latin-1").split("\r\n")
        status = lines[0] if lines else ""
        headers = {}

        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        log(
            "Watch Party WebSocket handshake response received: %s"
            % status,
            xbmc.LOGDEBUG,
        )

        if not status.startswith("HTTP/1.1 101"):
            self.close()
            raise SiloError(
                "Silo rejected the Watch Party WebSocket handshake: %s"
                % status
            )

        expected = base64.b64encode(
            hashlib.sha1(
                (
                    key
                    + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).encode("ascii")
            ).digest()
        ).decode("ascii")

        if headers.get("sec-websocket-accept", "").strip() != expected:
            self.close()
            raise SiloError("Invalid Watch Party WebSocket handshake response.")

        if headers.get("sec-websocket-protocol", "").strip() != "silo.room.v2":
            self.close()
            raise SiloError(
                "Silo did not select the expected Watch Party socket protocol."
            )

        # Keep the room monitor responsive enough to immediately undo
        # unauthorized Kodi pause/seek actions.
        self.sock.settimeout(0.20)
        return self

    def _read_http_headers(self):
        data = b""

        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SiloError("Watch Party WebSocket closed during handshake.")
            data += chunk

            if len(data) > 32768:
                raise SiloError("Watch Party WebSocket handshake is too large.")

        separator = data.index(b"\r\n\r\n") + 4
        self._buffer = data[separator:]
        return data[:separator]

    def _recv_exact(self, size):
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise SiloError("Watch Party WebSocket connection closed.")
            self._buffer += chunk

        value = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return value

    def recv(self):
        """Return one complete text message, or None on a short read timeout."""
        while True:
            try:
                first, second = self._recv_exact(2)
                fin = bool(first & 0x80)
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                length = second & 0x7F

                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(8))[0]

                if length > 8 * 1024 * 1024:
                    raise SiloError("Watch Party WebSocket frame is too large.")

                mask = self._recv_exact(4) if masked else None
                payload = self._recv_exact(length) if length else b""

            except socket.timeout:
                return None

            if mask:
                payload = bytes(
                    value ^ mask[index % 4]
                    for index, value in enumerate(payload)
                )

            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue

            if opcode == 0xA:
                continue

            if opcode == 0x8:
                try:
                    self._send_frame(0x8, payload[:125])
                except Exception:
                    pass
                raise SiloError("Watch Party WebSocket was closed by Silo.")

            if opcode == 0x0 or not fin:
                raise SiloError(
                    "Watch Party sent an unsupported fragmented WebSocket message."
                )

            if opcode != 0x1:
                continue

            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                raise SiloError("Watch Party sent invalid UTF-8 data.")

    def send(self, message):
        self._send_frame(
            0x1,
            json.dumps(
                message,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def _send_frame(self, opcode, payload=b""):
        if self.sock is None:
            raise SiloError("Watch Party WebSocket is not connected.")

        payload = payload or b""
        length = len(payload)
        mask = os.urandom(4)

        if length <= 125:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 65535:
            header = struct.pack(
                "!BBH",
                0x80 | opcode,
                0x80 | 126,
                length,
            )
        else:
            header = struct.pack(
                "!BBQ",
                0x80 | opcode,
                0x80 | 127,
                length,
            )

        masked = bytes(
            value ^ mask[index % 4]
            for index, value in enumerate(payload)
        )

        self.sock.sendall(header + mask + masked)

    def close(self):
        sock = self.sock
        self.sock = None

        if sock is None:
            return

        try:
            payload = b""
            mask = os.urandom(4)
            header = struct.pack("!BB", 0x88, 0x80)
            masked = bytes(
                value ^ mask[index % 4]
                for index, value in enumerate(payload)
            )
            sock.sendall(header + mask + masked)
        except Exception:
            pass

        try:
            sock.close()
        except Exception:
            pass

def _watch_party_monitor(
    client,
    room_id,
    room_token,
    stop_event=None,
    ui_state=None,
):
    """Follow a host-controlled Watch Party as a guest."""

    url = _watch_party_socket_url(client, room_id)
    socket = None

    def connect_watch_party_socket():
        """Mint a fresh ticket and establish a new room WebSocket connection."""
        ticket = client.watch_party_socket_ticket(room_id, room_token)
        ticket_value = ticket["ticket"]

        return _SiloWebSocket(
            url,
            [
                "silo.room.v2",
                "silo.ticket.%s" % ticket_value,
            ],
            timeout=15,
            origin=_watch_party_http_origin(client),
        ).connect()

    try:
        socket = connect_watch_party_socket()
    except Exception as exc:
        log(
            "Watch Party WebSocket connection failed: %s (%s)"
            % (exc, type(exc).__name__),
            xbmc.LOGERROR,
        )
        raise SiloError(
            "Unable to connect to the Watch Party: %s"
            % exc
        )

    try:
        socket.send({"type": "lobby_ready", "ready": True})
        log(
            "Watch Party lobby readiness sent after room connection.",
            xbmc.LOGDEBUG,
        )
    except Exception as exc:
        log(
            "Unable to send initial Watch Party lobby readiness: %s" % exc,
            xbmc.LOGWARNING,
        )

    monitor = xbmc.Monitor()
    if stop_event is None:
        stop_event = threading.Event()
    if ui_state is None:
        ui_state = _WatchPartyUiState("Connected to Watch Party.")
    session_id = None
    attached = False
    current_selection_revision = None
    last_command_id = None
    last_ping = 0.0
    last_state_report = 0.0
    playback_sequence = 0
    last_reported_paused = None
    next_progress_report_at = time.time()
    server_time_offset = 0.0
    last_ready_report = 0.0
    last_lobby_ready_report = 0.0
    last_lobby_ui_refresh = 0.0
    self_member_ready = False
    waiting_command_id = None

    # The room is authoritative for guest transport. Snapshots and host
    # commands update this state; it is then enforced continuously whenever
    # this profile is not allowed to control transport locally.
    room_phase = None
    room_playback_state = None
    room_can_control_transport = False
    room_target_position = 0.0
    room_target_updated_at = time.time()
    room_transport_known = False
    transport_sync_hold_until = 0.0
    transport_guard_until = 0.0
    remote_stop_until = 0.0
    was_room_playing = False
    last_transport_enforcement = 0.0
    last_transport_offset_log = 0.0
    send_lock = threading.Lock()

    # Kodi invokes onPlayBackStopped for an explicit user Stop as well as for
    # normal media replacement. Ignore the callback briefly while switching to
    # a new host-selected item, but treat a genuine stop as an immediate room
    # disconnect.
    disconnect_requested = threading.Event()
    switching_media_until = 0.0
    watch_party_input_lock_enabled = False

    # Kodi keymaps run before xbmc.Player callbacks. Those callbacks can undo
    # a seek or speed change after it happens, but they cannot make the original
    # physical button/hotkey press a true no-op. Watch Party therefore installs
    # a temporary keymap while playback is active and maps common seek/FF/RW
    # inputs to Kodi's documented noop action.
    watch_party_keymap_path = os.path.join(
        xbmcvfs.translatePath("special://profile"),
        "keymaps",
        "silo_watch_party.xml",
    )
    watch_party_keymap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<keymap>
  <FullscreenVideo>
    <keyboard>
      <p>noop</p>
      <space>noop</space>
      <f>noop</f>
      <r>noop</r>
      <period>noop</period>
      <comma>noop</comma>
      <quote>noop</quote>
      <opensquarebracket>noop</opensquarebracket>
      <closesquarebracket>noop</closesquarebracket>
      <pageup>noop</pageup>
      <pagedown>noop</pagedown>
      <channel_up>noop</channel_up>
      <channel_down>noop</channel_down>
      <fastforward>noop</fastforward>
      <rewind>noop</rewind>
      <left mod="ctrl">noop</left>
      <right mod="ctrl">noop</right>
    </keyboard>
    <remote>
      <play>noop</play>
      <pause>noop</pause>
      <play_pause>noop</play_pause>
      <forward>noop</forward>
      <reverse>noop</reverse>
      <skipplus>noop</skipplus>
      <skipminus>noop</skipminus>
    </remote>
  </FullscreenVideo>
  <FullscreenInfo>
    <keyboard>
      <p>noop</p>
      <space>noop</space>
      <f>noop</f>
      <r>noop</r>
      <period>noop</period>
      <comma>noop</comma>
      <quote>noop</quote>
      <opensquarebracket>noop</opensquarebracket>
      <closesquarebracket>noop</closesquarebracket>
      <pageup>noop</pageup>
      <pagedown>noop</pagedown>
      <channel_up>noop</channel_up>
      <channel_down>noop</channel_down>
      <fastforward>noop</fastforward>
      <rewind>noop</rewind>
      <left mod="ctrl">noop</left>
      <right mod="ctrl">noop</right>
    </keyboard>
    <remote>
      <play>noop</play>
      <pause>noop</pause>
      <play_pause>noop</play_pause>
      <forward>noop</forward>
      <reverse>noop</reverse>
      <skipplus>noop</skipplus>
      <skipminus>noop</skipminus>
    </remote>
  </FullscreenInfo>
</keymap>
"""

    def set_watch_party_input_lock(enabled):
        """Temporarily make Watch Party seek/FF/RW inputs true no-ops."""
        nonlocal watch_party_input_lock_enabled

        enabled = bool(enabled)
        if enabled == watch_party_input_lock_enabled:
            return True

        try:
            keymap_dir = os.path.dirname(watch_party_keymap_path)

            if enabled:
                if not xbmcvfs.exists(keymap_dir):
                    xbmcvfs.mkdirs(keymap_dir)

                handle = xbmcvfs.File(watch_party_keymap_path, "w")
                try:
                    handle.write(watch_party_keymap_xml)
                finally:
                    handle.close()
            else:
                if xbmcvfs.exists(watch_party_keymap_path):
                    xbmcvfs.delete(watch_party_keymap_path)

            xbmc.executebuiltin("ReloadKeymaps")
            watch_party_input_lock_enabled = enabled

            log(
                "Watch Party input lock %s."
                % ("enabled" if enabled else "disabled"),
                xbmc.LOGDEBUG,
            )
            return True
        except Exception as exc:
            log(
                "Unable to %s Watch Party input keymap: %s"
                % ("enable" if enabled else "disable", exc),
                xbmc.LOGWARNING,
            )
            return False

    def clear_watch_party_state():
        window = xbmcgui.Window(10000)
        for property_name in (
            "Silo.WatchParty.RoomID",
            "Silo.WatchParty.RoomToken",
            "Silo.WatchParty.Code",
        ):
            try:
                window.clearProperty(property_name)
            except Exception:
                try:
                    window.setProperty(property_name, "")
                except Exception:
                    pass

    def close_watch_party_playback_session(reason):
        """Stop Kodi media and explicitly terminate its Silo playback session."""
        nonlocal session_id, attached, playback_sequence

        active_session_id = session_id
        active_player = player

        try:
            last_position = player_position(active_player)
        except Exception:
            last_position = 0.0

        # A permanent Watch Party shutdown must always stop the Kodi player,
        # even if the playback session ID was already cleared by a room
        # transition. This prevents direct media from continuing independently
        # after the Watch Party itself has disappeared.
        try:
            if active_player.isPlaying():
                log(
                    "Stopping local Watch Party playback: %s" % reason,
                    xbmc.LOGINFO,
                )
                active_player.stop()
        except Exception as exc:
            log(
                "Unable to stop local Watch Party playback: %s"
                % exc,
                xbmc.LOGWARNING,
            )

        if active_session_id:
            # Stop Kodi first so the stream consumer closes as well. Then tell
            # Silo explicitly that this Watch Party playback attempt is over.
            try:
                stop_sequence = max(1, playback_sequence + 1)
                client.stop_playback(
                    active_session_id,
                    stop_sequence,
                    last_position,
                )
                log(
                    "Stopped Silo Watch Party playback session %s"
                    % active_session_id,
                    xbmc.LOGDEBUG,
                )
            except SiloError as exc:
                log(
                    "Unable to stop Silo Watch Party playback session %s: %s"
                    % (active_session_id, exc),
                    xbmc.LOGWARNING,
                )
            except Exception as exc:
                log(
                    "Unexpected error stopping Silo Watch Party playback session %s: %s"
                    % (active_session_id, exc),
                    xbmc.LOGWARNING,
                )

        session_id = None
        attached = False

    def request_watch_party_disconnect(reason):
        nonlocal switching_media_until
        if (
            switching_media_until > time.time()
            and reason == "Kodi playback stopped"
        ):
            return

        if disconnect_requested.is_set():
            return

        log(
            "Watch Party participant disconnecting: %s" % reason,
            xbmc.LOGINFO,
        )
        set_watch_party_input_lock(False)
        disconnect_requested.set()
        close_watch_party_playback_session(reason)
        _watch_party_reset_lobby()
        _watch_party_refresh_lobby()
        update_ui(
            status="Disconnected from Watch Party.",
            lobby=False,
            finished=True,
        )
        clear_watch_party_state()

        try:
            socket.close()
        except Exception:
            pass

    def update_ui(status=None, lobby=None, finished=None, ended=None):
        ui_state.update(
            status=status,
            lobby=lobby,
            finished=finished,
            ended=ended,
        )
        _watch_party_update_window_state(ui_state)

    def notify(status, level=xbmcgui.NOTIFICATION_INFO, ms=3000):
        try:
            xbmcgui.Dialog().notification(
                "Watch Party",
                status,
                level,
                ms,
            )
        except Exception:
            pass

    def send(message):
        try:
            with send_lock:
                socket.send(message)
            return True
        except Exception:
            return False

    def player_paused(player):
        try:
            return int(player.getPlaySpeed()) == 0
        except Exception:
            return bool(xbmc.getCondVisibility("Player.Paused"))

    def player_position(player):
        try:
            return max(0.0, float(player.getTime()))
        except Exception:
            return 0.0

    def parse_server_timestamp(value):
        try:
            return datetime.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    def authoritative_position(now=None):
        now = time.time() if now is None else now

        if (
            room_transport_known
            and room_playback_state == "playing"
        ):
            return max(
                0.0,
                room_target_position
                + max(0.0, now - room_target_updated_at),
            )

        return max(0.0, room_target_position)

    def set_transport_guard(seconds=1.0):
        nonlocal transport_guard_until
        transport_guard_until = max(
            transport_guard_until,
            time.time() + max(0.0, float(seconds or 0.0)),
        )

    def update_authoritative_room_state(room):
        nonlocal room_phase, room_playback_state, room_can_control_transport
        nonlocal room_target_position, room_target_updated_at
        nonlocal room_transport_known

        room_phase = room.get("phase")
        room_playback_state = room.get("playback_state")
        room_can_control_transport = bool(
            room.get("self_can_control_transport")
        )

        try:
            room_target_position = max(
                0.0,
                float(room.get("anchor_position_seconds") or 0),
            )
        except (TypeError, ValueError):
            room_target_position = 0.0

        # Anchor the snapshot to the local receive time. The server's anchor
        # timestamp uses the server clock and may not yet have a calibrated
        # client offset during startup.
        room_target_updated_at = time.time()

        room_transport_known = (
            room_phase == "playing"
            and room_playback_state in ("playing", "paused", "waiting")
        )

    def handle_local_transport_event(action, player):
        """Correct a local Kodi play/pause change back to the room state.

        Watch Party playback is server-authoritative for this client even when
        Silo reports that the profile is allowed to control transport.
        """
        if action not in ("play", "pause"):
            return False

        if not (
            session_id
            and attached
            and room_transport_known
        ):
            return False

        log(
            "Ignoring local Watch Party %s control; restoring server state."
            % action,
            xbmc.LOGDEBUG,
        )
        enforce_guest_transport(player)
        return True


    def reconcile_guest_transport(player, now):
        """Keep Kodi on the room's authoritative transport state."""
        if (
            not session_id
            or not attached
            or not room_transport_known
            or now < transport_guard_until
            or now < transport_sync_hold_until
            or room_playback_state == "waiting"
        ):
            return

        enforce_guest_transport(player)

    def enforce_guest_transport(player):
        """Undo unauthorized local pause/seek/play changes immediately."""

        nonlocal last_transport_enforcement

        if (
            not session_id
            or not attached
            or not room_transport_known
            or not player.isPlaying()
            or time.time() < transport_guard_until
        ):
            return

        now = time.time()
        target_position = authoritative_position(now)
        target_paused = room_playback_state in ("paused", "waiting")
        current_position = player_position(player)
        current_paused = player_paused(player)

        position_drift = abs(current_position - target_position)
        needs_seek = position_drift > 0.50
        needs_pause_change = current_paused != target_paused

        if not needs_seek and not needs_pause_change:
            return

        if position_drift >= 3.0:
            log(
                "Large Watch Party transport drift detected: "
                "kodi=%s server=%s delta=%+.3fs"
                % (
                    format_position(current_position),
                    format_position(target_position),
                    current_position - target_position,
                ),
                xbmc.LOGWARNING,
            )

        if now - last_transport_enforcement >= 1.0:
            log(
                "Enforcing locked Watch Party transport: local=%s%s "
                "target=%s%s"
                % (
                    format_position(current_position),
                    " paused" if current_paused else " playing",
                    format_position(target_position),
                    " paused" if target_paused else " playing",
                ),
                xbmc.LOGDEBUG,
            )
            last_transport_enforcement = now

        set_transport_guard(2.0 if needs_seek else 1.0)
        _watch_party_apply_transport_state(
            player,
            target_position,
            target_paused,
        )

    class _WatchPartyPlayer(xbmc.Player):
        """Kodi callbacks for immediate enforcement of locked room transport.

        The transport guard suppresses callbacks generated by our own
        corrective play/pause/seek operations, preventing those corrections
        from recursively triggering another correction.
        """

        def _transport_locked(self):
            return (
                session_id
                and attached
                and room_transport_known
                and room_phase == "playing"
            )

        def onPlayBackPaused(self):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            # A local pause is never allowed to change the Watch Party state.
            # Restore the room's authoritative playing state immediately.
            if room_playback_state == "playing":
                set_transport_guard()
                _kodi_set_watch_party_play_state(self, True)

        def onPlayBackResumed(self):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            # A local resume is never allowed to change the Watch Party state.
            # Restore the room's authoritative paused state immediately,
            # including the temporary "waiting" state used during resync.
            if room_playback_state in ("paused", "waiting"):
                set_transport_guard()
                _kodi_set_watch_party_play_state(self, False)

        def onPlayBackSpeedChanged(self, speed):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            try:
                current_speed = int(speed)
            except (TypeError, ValueError):
                return

            # Fast-forward and rewind are implemented by Kodi as playback
            # speed changes. Guests are not allowed to control transport, so
            # immediately return to normal playback speed.
            if current_speed in (0, 1):
                return

            log(
                "Ignoring local Watch Party playback speed change: %s"
                % current_speed,
                xbmc.LOGDEBUG,
            )
            set_transport_guard(1.0)

            try:
                # PlayerControl(Play) returns FF/RW to normal 1x playback.
                xbmc.executebuiltin("PlayerControl(Play)")
            except Exception:
                pass

            # If the room is paused/waiting, the Play control above may have
            # resumed Kodi as part of leaving FF/RW. Restore the authoritative
            # paused state immediately.
            if room_playback_state in ("paused", "waiting"):
                set_transport_guard(1.0)
                _kodi_set_watch_party_play_state(self, False)

        def onPlayBackSeek(self, time_value, seek_offset):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            target = authoritative_position()
            try:
                local_seek = max(0.0, float(time_value))
            except (TypeError, ValueError):
                local_seek = target

            # Pull Kodi straight back to the room position after a native
            # skip or timeline seek. Pause during the correction so the player
            # cannot advance while the authoritative position is restored.
            if abs(local_seek - target) > 0.75:
                set_transport_guard(2.0)
                _watch_party_apply_transport_state(
                    self,
                    target,
                    room_playback_state in ("paused", "waiting"),
                )

        def onPlayBackSeekChapter(self, chapter):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            # Chapter/previous/next seek actions are also local seeks and are
            # not permitted for Watch Party guests.
            set_transport_guard(2.0)
            _watch_party_apply_transport_state(
                self,
                authoritative_position(),
                room_playback_state in ("paused", "waiting"),
            )

        def onPlayBackStarted(self):
            if not self._transport_locked():
                return

            # Starting the media is allowed, but the room's paused state is
            # still authoritative once Kodi has a playable item.
            if room_playback_state in ("paused", "waiting"):
                set_transport_guard()
                try:
                    self.pause()
                except Exception:
                    pass

        def onPlayBackStopped(self):
            # A host stopping room playback also causes Kodi to stop its local
            # player. That is a remote room event, not a request to leave the
            # Watch Party, so only a genuine local Stop disconnects the guest.
            if time.time() < remote_stop_until:
                return

            # Kodi also fires this for host-driven media replacement; the
            # replacement window prevents those transitions being mistaken for
            # a deliberate local Stop.
            request_watch_party_disconnect("Kodi playback stopped")

    player = _WatchPartyPlayer()

    try:
        while (
            not monitor.abortRequested()
            and not disconnect_requested.is_set()
            and not stop_event.is_set()
        ):
            now = time.time()

            # Keep the visible lobby text moving even when the server has not
            # produced a new snapshot in the last interval. The directory is
            # refreshed only while the Watch Party lobby is actually visible,
            # so playback is never interrupted.
            if (
                room_phase == "lobby"
                and now - last_lobby_ui_refresh >= 2.5
            ):
                _watch_party_refresh_lobby()
                last_lobby_ui_refresh = now

            if (
                _watch_party_window().getProperty(
                    "Silo.WatchParty.LeaveRequested"
                ).lower()
                == "true"
            ):
                request_watch_party_disconnect("user left Watch Party lobby")
                break

            # Kodi does not expose a reliable folder-navigation callback
            # from a background plugin thread. Leaving the lobby is therefore
            # handled by the explicit Leave Watch Party item, while the socket
            # remains connected through idle lobby time.
            if (
                room_phase == "lobby"
                and now - last_lobby_ready_report >= 0.5
            ):
                send({
                    "type": "lobby_ready",
                    "ready": True,
                })
                last_lobby_ready_report = now

            if now - last_ping >= 15:
                send({
                    "type": "ping",
                    "client_sent_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                })
                last_ping = now

            try:
                raw = socket.recv()
            except Exception as exc:
                if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                    raw = None
                else:
                    # Silo intentionally limits each room WebSocket to five
                    # minutes. That is a socket credential lifetime, not a room
                    # lifetime. Reconnect with a freshly minted ticket instead
                    # of treating the normal socket expiry as leaving the room.
                    log(
                        "Watch Party WebSocket ended; reconnecting with a fresh "
                        "socket credential: %s" % exc,
                        xbmc.LOGINFO,
                    )
                    try:
                        socket.close()
                    except Exception:
                        pass

                    reconnected = False
                    reconnect_delay = 0.5

                    while (
                        not monitor.abortRequested()
                        and not disconnect_requested.is_set()
                        and not stop_event.is_set()
                    ):
                        try:
                            socket = connect_watch_party_socket()
                            reconnected = True

                            # The previous WebSocket connection's attachment
                            # does not carry over to the new connection. Keep
                            # the existing playback session ID, but explicitly
                            # attach that session again after reconnecting.
                            attached = False
                            last_state_report = 0.0

                            log(
                                "Watch Party WebSocket reconnected successfully; "
                                "reattaching playback session.",
                                xbmc.LOGINFO,
                            )
                            break
                        except SiloError as reconnect_exc:
                            log(
                                "Watch Party reconnect attempt failed: %s"
                                % reconnect_exc,
                                xbmc.LOGWARNING,
                            )

                            if reconnect_exc.status in (403, 404, 410):
                                disconnect_reason = (
                                    "Watch Party room is no longer available"
                                )
                                request_watch_party_disconnect(
                                    disconnect_reason
                                )
                                reconnected = False
                                break

                            xbmc.sleep(int(reconnect_delay * 1000))
                            reconnect_delay = min(5.0, reconnect_delay * 2.0)

                        except Exception as reconnect_exc:
                            log(
                                "Watch Party reconnect attempt failed: %s"
                                % reconnect_exc,
                                xbmc.LOGWARNING,
                            )
                            xbmc.sleep(int(reconnect_delay * 1000))
                            reconnect_delay = min(5.0, reconnect_delay * 2.0)

                    if not reconnected:
                        break

                    # The room coordinator keeps playback state/session
                    # attachment in shared runtime. The first post-reconnect
                    # snapshot lets us converge back to that authoritative
                    # state; no new playback session is created here.
                    raw = None

            if raw:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    message = {}

                message_type = message.get("type")

                if message_type == "snapshot":
                    room = message.get("room") or {}
                    previous_room_phase = room_phase
                    update_authoritative_room_state(room)
                    members = room.get("members") or []
                    _watch_party_set_members(members)
                    phase = room.get("phase")
                    selection_revision = room.get("selection_revision")
                    selected_content_id = room.get("selected_content_id")
                    selected_file_id = room.get("selected_file_id")
                    selected_library_id = room.get("selected_library_id")

                    # Every room snapshot can change the visible lobby:
                    # participants can join/leave, become lobby-ready, or the
                    # room can transition between waiting/playing/lobby.
                    # Refresh immediately so the directory reflects the
                    # server state without requiring the user to navigate away.
                    _watch_party_refresh_lobby()

                    if phase == "playing":
                        was_room_playing = True
                        set_watch_party_input_lock(True)

                        # The room is already playing as soon as Silo reports
                        # phase=playing. Kodi may still be opening/buffering the
                        # local stream, so never describe this as "starting"
                        # merely because player.isPlaying() is not true yet.
                        update_ui(
                            status="Playing with the Watch Party host.",
                            lobby=False,
                        )


                        if (
                            selected_content_id
                            and selection_revision != current_selection_revision
                        ):
                            current_selection_revision = selection_revision
                            switching_media_until = time.time() + 5.0
                            session_id = _start_watch_party_guest_playback(
                                client,
                                selected_content_id,
                                selected_file_id,
                                selected_library_id,
                                player=player,
                            )
                            playback_sequence = 0
                            last_reported_paused = None
                            next_progress_report_at = time.time()

                            # Kodi starts the stream asynchronously. Keep it
                            # paused while the initial Watch Party position is
                            # being synchronised so the guest cannot run ahead
                            # of the room while the seek is applied.
                            set_transport_guard(3.0)
                            transport_sync_hold_until = time.time() + 5.0
                            if _wait_for_watch_party_player(player, timeout=15.0):
                                _kodi_set_watch_party_play_state(player, False)

                                sync_position = authoritative_position()
                                current_position = player_position(player)

                                if abs(current_position - sync_position) > 0.25:
                                    try:
                                        player.seekTime(sync_position)
                                        xbmc.sleep(75)
                                    except Exception as exc:
                                        log(
                                            "Unable to apply initial Watch Party "
                                            "sync seek to %.3fs: %s"
                                            % (sync_position, exc),
                                            xbmc.LOGWARNING,
                                        )

                                # Resume only after the authoritative starting
                                # position has been applied.
                                if room_playback_state == "playing":
                                    set_transport_guard(0.75)
                                    _kodi_set_watch_party_play_state(player, True)

                            attached = False
                            last_command_id = None
                            last_state_report = 0.0
                            set_transport_guard(0.75)
                            update_ui(
                                status="Playing with the Watch Party host.",
                                lobby=False,
                            )

                    elif phase == "lobby":
                        # Silo's host "Stop Playback" operation returns the
                        # still-open room directly from playing -> lobby.
                        # Detect that transition from the previous snapshot
                        # instead of relying on a mutable readiness flag.
                        stopped_from_playback = (
                            previous_room_phase == "playing"
                            or was_room_playing
                        )

                        if stopped_from_playback:
                            was_room_playing = False

                            # Mark the stop as remote before calling stop(),
                            # because Kodi may invoke onPlayBackStopped from
                            # the stop operation itself.
                            remote_stop_until = time.time() + 5.0

                            close_watch_party_playback_session(
                                "Host stopped Watch Party playback"
                            )
                            set_watch_party_input_lock(False)
                            session_id = None
                            attached = False
                            last_command_id = None
                            current_selection_revision = selection_revision

                        else:
                            was_room_playing = False
                            set_watch_party_input_lock(False)

                        ready_count = sum(
                            1
                            for member in members
                            if isinstance(member, dict)
                            and bool(member.get("lobby_ready"))
                        )
                        participant_count = len(
                            [member for member in members if isinstance(member, dict)]
                        )

                        if participant_count:
                            if stopped_from_playback:
                                lobby_status = (
                                    "Watch Party %s — %d/%d participants ready. "
                                    "Playback stopped; waiting for the host."
                                    % (
                                        _watch_party_window().getProperty(
                                            "Silo.WatchParty.Code"
                                        ) or "lobby",
                                        ready_count,
                                        participant_count,
                                    )
                                )
                            else:
                                lobby_status = (
                                    "Watch Party %s — %d/%d participants ready. "
                                    "Waiting for the host to start playback."
                                    % (
                                        _watch_party_window().getProperty(
                                            "Silo.WatchParty.Code"
                                        ) or "lobby",
                                        ready_count,
                                        participant_count,
                                    )
                                )
                        else:
                            lobby_status = (
                                "Watch Party %s — waiting for participants."
                                % (
                                    _watch_party_window().getProperty(
                                        "Silo.WatchParty.Code"
                                    ) or "lobby",
                                )
                            )

                        update_ui(
                            status=lobby_status,
                            lobby=True,
                        )

                        if stopped_from_playback:
                            # close_watch_party_playback_session() normally
                            # stops Kodi. Keep one final guarded stop for any
                            # player that is still alive during callback teardown.
                            try:
                                if player.isPlaying():
                                    log(
                                        "Host stopped Watch Party playback; "
                                        "stopping local Kodi player.",
                                        xbmc.LOGINFO,
                                    )
                                    player.stop()
                            except Exception as exc:
                                log(
                                    "Unable to stop Kodi playback after remote "
                                    "Watch Party stop: %s" % exc,
                                    xbmc.LOGWARNING,
                                )

                            _watch_party_refresh_lobby()

                    elif phase == "paused":
                        was_room_playing = True
                        set_watch_party_input_lock(True)
                        update_ui(
                            status="Playback paused with the Watch Party host.",
                            lobby=False,
                        )

                elif message_type == "transport_command":
                    command = message.get("command") or {}
                    command_id = command.get("command_id")

                    if not command_id or command_id == last_command_id:
                        continue

                    if not session_id:
                        continue

                    action = command.get("action")
                    if action not in ("play", "pause", "seek"):
                        continue

                    try:
                        position = max(
                            0.0,
                            float(command.get("position_seconds") or 0),
                        )
                    except (TypeError, ValueError):
                        position = 0.0

                    execute_at = command.get("execute_at")
                    if action != "seek" and execute_at:
                        target = parse_server_timestamp(execute_at)
                        if target is not None:
                            delay = max(
                                0.0,
                                target - (time.time() + server_time_offset),
                            )
                            if delay > 0:
                                xbmc.sleep(int(delay * 1000))

                    # A seek command can arrive behind the
                    # snapshot for a newer seek. The room's waiting anchor is
                    # the latest authoritative target, so never apply an older
                    # command after that newer target is already known.
                    if (
                        action == "seek"
                        and room_playback_state == "waiting"
                        and abs(position - room_target_position) > 0.75
                    ):
                        log(
                            "Ignoring stale Watch Party seek command %s at %.3fs; "
                            "current waiting target is %.3fs."
                            % (
                                command_id,
                                position,
                                room_target_position,
                            ),
                            xbmc.LOGDEBUG,
                        )
                        continue

                    command_playback_state = (
                        command.get("playback_state") or "playing"
                    )

                    # The command is itself authoritative. Establish its
                    # position/state before touching Kodi so callbacks and the
                    # enforcement loop cannot mistake our correction for a
                    # prohibited local action.
                    room_target_position = position
                    room_target_updated_at = time.time()
                    room_playback_state = command_playback_state
                    waiting_command_id = (
                        command_id
                        if command_playback_state == "waiting"
                        else None
                    )
                    last_ready_report = 0.0
                    self_member_ready = False
                    room_transport_known = (
                        room_phase == "playing"
                        and command_playback_state in (
                            "playing",
                            "paused",
                            "waiting",
                        )
                    )
                    # Ignore local transport callbacks while Kodi settles
                    # this server-scheduled command.
                    set_transport_guard(
                        2.0 if action == "seek" else 0.75
                    )

                    applied = _apply_watch_party_guest_command(
                        action,
                        position,
                        player,
                        command_playback_state,
                    )

                    if applied:
                        transport_sync_hold_until = max(
                            transport_sync_hold_until,
                            time.time() + 3.0,
                        )

                    if not applied:
                        log(
                            "Watch Party command %s was not applied yet."
                            % command_id,
                            xbmc.LOGWARNING,
                        )
                        # The next snapshot/repeated command can retry once the
                        # player is actually available. Do not acknowledge it.
                        continue

                    last_command_id = command_id

                    # A guest acknowledges only after the player actually
                    # matches the requested transport state.
                    xbmc.sleep(100)
                    actual_position = player_position(player)
                    actual_paused = player_paused(player)
                    target_position = authoritative_position()
                    target_paused = command_playback_state in ("paused", "waiting")

                    if (
                        abs(actual_position - target_position) <= 1.0
                        and actual_paused == target_paused
                    ):
                        if command_playback_state == "waiting":
                            self_member_ready = True
                        send({
                            "type": "ready",
                            "session_id": session_id,
                            "command_id": command_id,
                            "position_seconds": actual_position,
                            "is_paused": actual_paused,
                        })
                    else:
                        log(
                            "Watch Party command %s applied locally but did "
                            "not settle at the authoritative state yet."
                            % command_id,
                            xbmc.LOGDEBUG,
                        )

                elif message_type == "pong":
                    sent = parse_server_timestamp(message.get("client_sent_at"))
                    server_received = parse_server_timestamp(
                        message.get("server_received_at")
                    )
                    server_sent = parse_server_timestamp(
                        message.get("server_sent_at")
                    )
                    received = time.time()
                    if (
                        sent is not None
                        and server_received is not None
                        and server_sent is not None
                    ):
                        server_time_offset = (
                            server_received
                            - sent
                            + server_sent
                            - received
                        ) / 2.0

                elif message_type == "room_closed":
                    # The host has ended the Watch Party itself, rather than
                    # merely stopping playback. Permanently close both the
                    # Watch Party connection and the Silo playback session.
                    disconnect_requested.set()
                    remote_stop_until = time.time() + 5.0
                    close_watch_party_playback_session("Watch Party ended remotely")
                    clear_watch_party_state()

                    xbmcgui.Dialog().notification(
                        "Watch Party",
                        "The Watch Party has ended.",
                        xbmcgui.NOTIFICATION_INFO,
                        4000,
                    )

                    try:
                        socket.close()
                    except Exception:
                        pass
                    break

                elif message_type == "connection_replaced":
                    disconnect_requested.set()
                    remote_stop_until = time.time() + 5.0
                    close_watch_party_playback_session(
                        "Watch Party connection replaced"
                    )
                    clear_watch_party_state()
                    xbmcgui.Dialog().notification(
                        "Watch Party",
                        "This profile joined the Watch Party somewhere else.",
                        xbmcgui.NOTIFICATION_WARNING,
                        5000,
                    )
                    break

            # Kodi play/pause is always locked while Watch Party playback
            # is active. Polling is retained as a fallback for remotes/skins
            # that do not reliably emit the native callbacks; it never sends a
            # transport request back to Silo.
            if (
                session_id
                and attached
                and room_transport_known
                and player.isPlaying()
                and time.time() >= transport_guard_until
                and time.time() >= transport_sync_hold_until
            ):
                if player_paused(player) != (room_playback_state in ("paused", "waiting")):
                    enforce_guest_transport(player)


            # Attach the current Silo playback session exactly once. The
            # session ID must remain unchanged for all later state reports.
            if session_id and not attached:
                if send({
                    "type": "attach_session",
                    "session_id": session_id,
                }):
                    attached = True

            # Enforce the current room transport after applying any new socket
            # message, so an unauthorized local pause/seek is corrected against
            # the newest host state before the next guest report.
            reconcile_guest_transport(player, now)

            # Persistent Silo progress is independent from Watch
            # Party room attachment and transport synchronization. This keeps
            # resume tracking alive while the room socket is reattaching and
            # allows a server-terminated playback session to be detected from
            # its authoritative progress response.
            if session_id and player.isPlaying():
                actual_paused = player_paused(player)
                current_position = player_position(player)

                pause_state_changed = (
                    last_reported_paused is not None
                    and bool(actual_paused) != bool(last_reported_paused)
                )

                if now >= next_progress_report_at or pause_state_changed:
                    playback_sequence += 1
                    try:
                        client.report_progress(
                            session_id,
                            playback_sequence,
                            current_position,
                            actual_paused,
                        )
                        last_reported_paused = bool(actual_paused)
                        next_progress_report_at = now + 5.0
                        log(
                            "Watch Party Silo progress reported: "
                            "sequence=%d position=%.3f paused=%s"
                            % (
                                playback_sequence,
                                current_position,
                                bool(actual_paused),
                            ),
                            xbmc.LOGDEBUG,
                        )
                    except SiloError as exc:
                        if playback_session_was_terminated(exc):
                            log(
                                "Silo Watch Party playback session was terminated "
                                "by the server; stopping Kodi playback.",
                                xbmc.LOGWARNING,
                            )
                            disconnect_requested.set()
                            remote_stop_until = time.time() + 5.0
                            try:
                                if player.isPlaying():
                                    player.stop()
                            except Exception as stop_exc:
                                log(
                                    "Unable to stop Kodi Watch Party playback "
                                    "after server termination: %s"
                                    % stop_exc,
                                    xbmc.LOGWARNING,
                                )

                            update_ui(
                                status="Playback was terminated by the server.",
                                lobby=False,
                                finished=True,
                            )
                            _watch_party_reset_lobby()
                            _watch_party_refresh_lobby()
                            try:
                                socket.close()
                            except Exception:
                                pass
                            break

                        next_progress_report_at = now + 5.0
                        log(
                            "Unable to report Watch Party playback progress: %s"
                            % exc,
                            xbmc.LOGWARNING,
                        )

            # Room synchronization state is separate from persistent progress.
            if (
                session_id
                and attached
                and player.isPlaying()
                and now - last_state_report >= 1.5
            ):
                current_position = player_position(player)
                actual_paused = player_paused(player)
                send({
                    "type": "state_report",
                    "session_id": session_id,
                    "position_seconds": current_position,
                    "is_paused": actual_paused,
                })
                last_state_report = now

            xbmc.sleep(25)

    except Exception as exc:
        log(
            "Watch Party monitor crashed: %s (%s)"
            % (exc, type(exc).__name__),
            xbmc.LOGERROR,
        )
        raise

    finally:
        set_watch_party_input_lock(False)
        if not disconnect_requested.is_set():
            disconnect_requested.set()
            close_watch_party_playback_session(
                "Watch Party monitor closed unexpectedly"
            )

        update_ui(
            finished=True,
            lobby=False,
        )
        clear_watch_party_state()
        try:
            socket.close()
        except Exception:
            pass


def _wait_for_watch_party_player(player, timeout=20.0):
    """Wait for Kodi to finish opening Watch Party media before transport changes."""
    deadline = time.time() + max(0.0, float(timeout or 0.0))

    while time.time() < deadline:
        try:
            if player.isPlaying():
                return True
        except Exception:
            pass

        xbmc.sleep(50)

    try:
        return bool(player.isPlaying())
    except Exception:
        return False


def _kodi_set_watch_party_play_state(player, should_play):
    """Set the current Kodi video player's play state explicitly."""
    if not player.isPlaying():
        return False

    try:
        response = xbmc.executeJSONRPC(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "Player.PlayPause",
                    "params": {
                        "playerid": 1,
                        "play": bool(should_play),
                    },
                    "id": "silo-watch-party-play-state",
                },
                separators=(",", ":"),
            )
        )
        payload = json.loads(response or "{}")
        result = payload.get("result") or {}
        speed = result.get("speed")
        if speed is not None:
            return (int(speed) != 0) == bool(should_play)
    except Exception as exc:
        log(
            "Kodi JSON-RPC Watch Party play-state change failed: %s"
            % exc,
            xbmc.LOGDEBUG,
        )

    # Fall back to Kodi's Player API if JSON-RPC did not return a usable state.
    try:
        if bool(player_paused(player)) == (not bool(should_play)):
            player.pause()
            xbmc.sleep(50)
    except Exception:
        pass

    try:
        return bool(player_paused(player)) == (not bool(should_play))
    except Exception:
        return False


def _watch_party_apply_transport_state(player, position, paused):
    """Force Kodi onto one authoritative Watch Party transport state.

    Seek corrections preserve the room's authoritative transport whenever
    possible. A playing room is allowed to seek while Kodi keeps playing;
    paused/waiting rooms pause before the seek and remain paused afterwards.
    This avoids repeatedly pausing a guest when the server is still playing.
    """
    if not player.isPlaying():
        return False

    try:
        current = max(0.0, float(player.getTime()))
    except Exception:
        current = 0.0

    needs_seek = abs(current - position) > 0.75

    if needs_seek:
        try:
            currently_paused = int(player.getPlaySpeed()) == 0
        except Exception:
            currently_paused = bool(xbmc.getCondVisibility("Player.Paused"))

        # Only pause during the seek when the authoritative target is
        # actually paused. For a playing room, seeking while already playing
        # lets Kodi land on the target without creating an artificial pause
        # that the server immediately has to undo.
        if paused and not currently_paused:
            if not _kodi_set_watch_party_play_state(player, False):
                log(
                    "Unable to pause Kodi before Watch Party resynchronisation.",
                    xbmc.LOGWARNING,
                )
                return False

        try:
            player.seekTime(position)
            xbmc.sleep(75)
        except Exception as exc:
            log(
                "Unable to apply Watch Party seek to %.3fs: %s"
                % (position, exc),
                xbmc.LOGWARNING,
            )
            return False

    try:
        currently_paused = int(player.getPlaySpeed()) == 0
    except Exception:
        currently_paused = bool(xbmc.getCondVisibility("Player.Paused"))

    if paused:
        if not currently_paused:
            if not _kodi_set_watch_party_play_state(player, False):
                log(
                    "Watch Party pause command did not leave Kodi paused.",
                    xbmc.LOGWARNING,
                )
                return False
    else:
        if currently_paused:
            if not _kodi_set_watch_party_play_state(player, True):
                log(
                    "Watch Party play command did not leave Kodi playing.",
                    xbmc.LOGWARNING,
                )
                return False

    return True


def _apply_watch_party_guest_command(
    action,
    position,
    player,
    playback_state="playing",
):
    """Apply a host transport command after Kodi has opened the media."""
    if action not in ("seek", "play", "pause"):
        return False

    if not player.isPlaying():
        if not _wait_for_watch_party_player(player):
            log(
                "Watch Party command deferred because Kodi did not start "
                "the selected media within the startup window.",
                xbmc.LOGWARNING,
            )
            return False

    return _watch_party_apply_transport_state(
        player,
        position,
        playback_state in ("paused", "waiting"),
    )


def _start_watch_party_guest_playback(
    client,
    content_id,
    file_id=None,
    library_id=None,
    player=None,
):
    """Start the host-selected item using the normal Silo Kodi playback path."""
    library_id = resolve_playback_library_id(client, content_id, library_id)

    if not file_id:
        file_id = choose_file(client, content_id, library_id)

    if not file_id:
        raise SiloError("The Watch Party item has no playable version.")

    info = client.start_playback(
        file_id,
        start_position=0.0,
        direct_play_only=direct_play_only_enabled(),
    )

    if not info.get("url"):
        raise SiloError("Silo did not provide a Watch Party playback URL.")

    item = xbmcgui.ListItem(path=info["url"])
    item.setProperty("OverrideInfotag", "true")

    try:
        detail = client.item_detail(content_id, library_id, file_id)
        if detail:
            set_catalog_metadata(item, detail, client)
            set_detail_metadata(item, detail, client, file_id=file_id)
            set_art(
                item,
                client,
                poster=detail.get("poster_url") or detail.get("poster"),
                backdrop=detail.get("backdrop_url") or detail.get("backdrop"),
                logo=detail.get("logo_url") or detail.get("logo"),
                still=detail.get("still_url") or detail.get("still"),
            )
    except Exception as exc:
        log(
            "Unable to apply Watch Party playback metadata for %s: %s"
            % (content_id, exc),
            xbmc.LOGWARNING,
        )

    # Watch Party playback is initiated from the non-playable "Join Watch Party"
    # action, so there is no Kodi playable-item resolution context for
    # setResolvedUrl() to hand back to the VideoPlayer. Start the resolved media
    # explicitly through xbmc.Player() instead.
    if player is None:
        player = xbmc.Player()
    player.play(item=info["url"], listitem=item)
    log(
        "Started Watch Party guest playback through Kodi Player: %s"
        % content_id,
        xbmc.LOGDEBUG,
    )
    return info.get("session_id")


def list_your_stuff(client):
    """Display the personal destinations shown under Silo's web Your Stuff menu."""
    entries = (
        ("Favorites", "favorites", "DefaultFavorites.png"),
        ("Watchlist", "watchlist", "DefaultVideo.png"),
        ("History", "history", "DefaultRecentlyWatched.png"),
    )

    for title, source, icon in entries:
        item = xbmcgui.ListItem(label=title)
        item.setArt({"icon": icon})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="personal_list", source=source),
            item,
            True,
        )

    collections_item = xbmcgui.ListItem(label="Collections")
    collections_item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="collections"),
        collections_item,
        True,
    )

    xbmcplugin.setContent(HANDLE, "files")
    xbmcplugin.endOfDirectory(HANDLE)


def _list_personal_catalog(client, source, cursor=None, collection_id=None):
    """Display a Silo personal catalog source using the normal media pipeline."""
    if source not in ("favorites", "watchlist", "history", "user_collection"):
        raise SiloError("Unsupported personal catalog source.")

    items, next_cursor = client.personal_catalog_page(
        source,
        cursor=cursor,
        limit=get_directory_page_size(),
        collection_id=collection_id,
    )

    xbmcplugin.setPluginCategory(
        HANDLE,
        {
            "favorites": "Favorites",
            "watchlist": "Watchlist",
            "history": "History",
            "user_collection": "Collection",
        }.get(source, "Your Stuff"),
    )
    xbmcplugin.setContent(HANDLE, "videos")

    # Keep personal-list cards in lockstep with Search and Library browsing:
    # full detail metadata first, then fresh in-progress positions, then the
    # authoritative series/season watch rollups.
    detail_map = fetch_detail_metadata(
        client,
        items,
        None,
    )

    try:
        in_progress_map = client.in_progress_map()
    except SiloError as exc:
        log(
            "Unable to retrieve in-progress Silo records for %s: %s"
            % (source, exc),
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    try:
        series_watch_map, season_watch_map = fetch_series_watch_data(
            client,
            items,
        )
    except SiloError as exc:
        log(
            "Unable to retrieve %s series watch-state rollups: %s"
            % (source, exc),
            xbmc.LOGWARNING,
        )
        series_watch_map, season_watch_map = {}, {}

    batch = []

    for catalog_item in items:
        content_id = get_content_id(catalog_item)
        if not content_id:
            continue

        media_type = (
            catalog_item.get("type")
            or catalog_item.get("media_type")
            or ""
        ).lower()

        display_progress = (
            in_progress_map.get(str(content_id))
            or catalog_progress(catalog_item)
        )

        item, media_type, content_id, title, display_progress = build_catalog_list_item(
            client,
            catalog_item,
            detail=detail_map.get(str(content_id)),
            progress=display_progress,
            series_rollup=series_watch_map.get(str(content_id)),
            season_rollup=(
                season_watch_map.get(str(content_id))
                or season_watch_map.get(
                    "%s:%s" % (
                        catalog_item.get("series_id"),
                        catalog_item.get("season_number"),
                    )
                )
            ),
        )

        if media_type in PLAYABLE:
            item.setProperty("IsPlayable", "true")
            # Match Search: do not bake a possibly stale library ID or runtime
            # into the URL. Playback resolves any missing library at play time
            # and performs its own fresh server resume lookup.
            url = build_url(
                action="play",
                content_id=(
                    catalog_item.get("play_content_id")
                    or content_id
                ),
                resume_available=int(
                    has_usable_resume(display_progress)
                ),
            )
            is_folder = False
        elif media_type == "series":
            url = build_url(
                action="seasons",
                series_id=content_id,
            )
            is_folder = True
        else:
            # Keep non-video catalog types visible without inventing a
            # playback URL. The normal item-details route can be added later
            # alongside the dedicated Kodi details screen.
            continue

        batch.append((url, item, is_folder))

    if batch:
        xbmcplugin.addDirectoryItems(
            HANDLE,
            batch,
            totalItems=len(batch) + (1 if next_cursor else 0),
        )

    if next_cursor:
        next_item = xbmcgui.ListItem(label="More")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="personal_list",
                source=source,
                cursor=next_cursor,
                collection_id=collection_id,
            ),
            next_item,
            True,
        )

    xbmcplugin.endOfDirectory(HANDLE)


def list_collections(client):
    """Display the profile's visible personal Collections."""
    collections, groups = client.collections()

    # Keep the server's collection/group ordering, matching the web client.
    group_names = {str(group.get("id")): group.get("name") for group in groups or []}

    xbmcplugin.setPluginCategory(HANDLE, "Collections")
    xbmcplugin.setContent(HANDLE, "files")

    for collection in collections or []:
        collection_id = collection.get("id")
        if not collection_id:
            continue

        name = collection.get("name") or "Collection"
        group_name = group_names.get(str(collection.get("group_id")))
        label = "%s - %s" % (group_name, name) if group_name else name

        item = xbmcgui.ListItem(label=label)
        poster = collection.get("poster_url")
        if poster:
            item.setArt({"thumb": poster, "poster": poster})

        item.setInfo(
            "video",
            {
                "title": name,
                "plot": collection.get("description") or "",
            },
        )

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="collection",
                collection_id=collection_id,
                title=name,
            ),
            item,
            True,
        )

    xbmcplugin.endOfDirectory(HANDLE)


def list_collection(client, collection_id, title=None, cursor=None):
    """Display the media contained in one personal Collection."""
    if not collection_id:
        raise SiloError("No collection ID was supplied.")

    _list_personal_catalog(
        client,
        "user_collection",
        cursor=cursor,
        collection_id=collection_id,
    )


def list_root(client, page=None):
    """Display the initial screen or the logged-in Silo libraries.

    Authentication works from either place:
        * The Login button performs a complete fresh login.
        * Server/username/profile entered in Kodi Settings are used automatically.
    """

    # Watch Party is the only directory that temporarily suppresses Kodi's
    # automatic parent-folder item. Ensure ordinary addon navigation always
    # starts with the user's normal back-navigation setting.

    # If server and username were entered in Kodi Settings, authenticate
    # automatically. Kodi never stores the password, so ask for it here.
    if not client.cfg.get("token"):
        if client.base and client.cfg.get("username"):
            client.login()

            # Authenticate the account first, then select the configured
            # profile or show the normal profile selector.
            if not client.cfg.get("profile_id"):
                client.select_profile()
        else:
            # No saved account details: retain the Login button behaviour.
            login_item = xbmcgui.ListItem(label="Login")
            xbmcplugin.addDirectoryItem(
                HANDLE,
                build_url(action="login"),
                login_item,
                False,
            )

            # Settings is deliberately available before login so the user can
            # enter the server/username/profile without using the Login button.
            settings_item = xbmcgui.ListItem(label="Settings")
            xbmcplugin.addDirectoryItem(
                HANDLE,
                build_url(action="settings"),
                settings_item,
                False,
            )

            xbmcplugin.setContent(HANDLE, "files")
            xbmcplugin.endOfDirectory(HANDLE)
            return

    search_item = xbmcgui.ListItem(label="Search")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="search"),
        search_item,
        True,
    )

    # Keep all user libraries under one folder so the root stays focused on
    # global actions and profile-wide Home sections.
    libraries_item = xbmcgui.ListItem(label="Libraries")
    libraries_item.setProperty("Silo.LibraryFolder", "true")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="libraries"),
        libraries_item,
        True,
    )

    # Keep personal lists and collections immediately below Libraries.
    add_your_stuff_folder()

    # Keep Watch Party directly underneath Your Stuff in the root list.
    watch_party_item = xbmcgui.ListItem(
        label=(
            "Open Watch Party"
            if _watch_party_connection_active()
            else "Watch Party"
        )
    )
    watch_party_item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="watch_party_join"),
        watch_party_item,
        True,
    )

    # Silo Home sections are profile-wide: they combine content across all
    # libraries visible to the selected profile. This keeps Continue Watching,
    # Recently Added, Recommended, Next Up, etc. from being split by library.
    try:
        home_sections = client.home_sections(image_size="medium")
    except SiloError as exc:
        log("Unable to load Silo Home sections: %s" % exc, xbmc.LOGWARNING)
        home_sections = []

    if ADDON.getSettingBool("combine_home_sections"):
        add_grouped_home_sections(home_sections)
    else:
        for section in home_sections:
            add_home_section_folder(section)

    profile_item = xbmcgui.ListItem(label="Switch Profile")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="switch_profile"),
        profile_item,
        False,
    )

    logout_item = xbmcgui.ListItem(label="Logout")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="logout"),
        logout_item,
        False,
    )

    # Keep Settings at the bottom of the root list when logged in as well.
    settings_item = xbmcgui.ListItem(label="Settings")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="settings"),
        settings_item,
        False,
    )

    xbmcplugin.setContent(HANDLE, "files")
    xbmcplugin.endOfDirectory(HANDLE)

    # A successful login leaves this flag set while Container.Refresh starts
    # the new root-page invocation. Close the spinner only after the libraries
    # have actually been added to the Kodi directory.
    if xbmcgui.Window(10000).getProperty("Silo.LoginLoading") == "true":
        _hide_login_loading()


def _home_section_group_key(section):
    """Return a merge key only for Recently Added and Recently Released."""
    section_type = str(section.get("section_type") or "").strip().lower()
    title = str(section.get("title") or section_type or "").strip()
    lowered = title.casefold()

    # These are the only Home categories that are combined across libraries.
    if section_type == "recently_added" or lowered.startswith("recently added"):
        return "recently added"

    if section_type == "recently_released" or lowered.startswith("recently released"):
        return "recently released"

    # Every other section keeps its own Silo section ID. This prevents its
    # title from being rewritten or accidentally merged with another section.
    return "unique:" + str(section.get("id") or section.get("section_id") or title)


def _home_section_group_title(section):
    """Return the common title for a merged category, otherwise Silo's title."""
    key = _home_section_group_key(section)

    if key == "recently added":
        return "Recently Added"

    if key == "recently released":
        return "Recently Released"

    # Non-merged sections must retain the exact title supplied by Silo.
    return section.get("title") or section.get("section_type") or "Home"


def _extract_home_library_id(source):
    """Read an explicit library ID from a Home section/item when one is supplied."""
    if not isinstance(source, dict):
        return ""

    for key in (
        "library_id",
        "source_library_id",
        "home_origin_library_id",
    ):
        value = source.get(key)
        if value not in (None, ""):
            return str(value)

    library_ids = source.get("library_ids") or source.get("source_library_ids")
    if isinstance(library_ids, (list, tuple)) and len(library_ids) == 1:
        value = library_ids[0]
        if value not in (None, ""):
            return str(value)

    return ""


def _resolve_home_section_library_id(client, section, libraries=None):
    """Resolve a Home section's originating library when Silo exposes enough information.

    Current Silo Home section cards do not include library_id. The default
    library-generated Home titles do include the library name, so use that as
    a deterministic fallback for Recently Added/Recently Released sections.
    """
    library_id = _extract_home_library_id(section)
    if library_id:
        return library_id

    if not isinstance(section, dict):
        return ""

    section_type = str(section.get("section_type") or "").strip().lower()
    title = str(section.get("title") or "").strip()

    library_name = ""
    if section_type == "recently_added":
        prefix = "Recently Added "
        if title.startswith(prefix):
            library_name = title[len(prefix):].strip()
    elif section_type == "recently_released":
        prefix = "Recently Released "
        if title.startswith(prefix):
            library_name = title[len(prefix):].strip()
    elif section_type == "custom_filter":
        prefix = "Recently Released Episodes in "
        if title.startswith(prefix):
            library_name = title[len(prefix):].strip()

    if not library_name:
        return ""

    if libraries is None:
        try:
            libraries = client.libraries()
        except SiloError as exc:
            log(
                "Unable to resolve Home section library %r: %s"
                % (title, exc),
                xbmc.LOGDEBUG,
            )
            return ""

    wanted = library_name.casefold()

    for library in libraries or []:
        candidate_name = str(
            library.get("name")
            or library.get("title")
            or ""
        ).strip()

        if candidate_name and candidate_name.casefold() == wanted:
            value = library.get("id")
            if value not in (None, ""):
                return str(value)

    return ""


def _home_item_richness(item):
    """Return a deterministic quality score used when duplicate Home cards collide."""
    detail_fields = (
        "overview",
        "genres",
        "keywords",
        "studios",
        "networks",
        "rating_imdb",
        "rating_tmdb",
        "poster_url",
        "backdrop_url",
        "logo_url",
        "runtime",
        "duration_seconds",
        "play_content_id",
    )

    score = 0
    for field in detail_fields:
        value = item.get(field)
        if value not in (None, "", [], {}):
            score += 1

    if _extract_home_library_id(item):
        score += 4

    return score


def _merge_home_catalog_items(existing, candidate):
    """Keep the duplicate that occurs highest in the already-sorted combined list.

    For merged recent sections, callers invoke this only after the complete
    combined list has been globally sorted. Therefore the first copy is the
    one that appears higher on the final Kodi page and must remain the primary
    copy. Later duplicates can contribute missing metadata and source IDs.
    """
    # The list has already been sorted before this function is called.
    chosen = dict(existing)

    existing_library = _extract_home_library_id(existing)
    candidate_library = _extract_home_library_id(candidate)

    existing_section = str(existing.get("_silo_home_source_section_id") or "")
    candidate_section = str(candidate.get("_silo_home_source_section_id") or "")

    # Fill missing fields from the later copy without changing the selected
    # copy's position, library, or existing values.
    other = candidate

    for key, value in other.items():
        if key.startswith("_silo_home_"):
            continue
        if chosen.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            chosen[key] = value

    source_sections = set(
        existing.get("_silo_home_source_section_ids") or []
    )
    source_sections.update(
        candidate.get("_silo_home_source_section_ids") or []
    )
    if existing_section:
        source_sections.add(existing_section)
    if candidate_section:
        source_sections.add(candidate_section)
    chosen["_silo_home_source_section_ids"] = sorted(source_sections)

    source_libraries = set(
        existing.get("_silo_home_source_library_ids") or []
    )
    source_libraries.update(
        candidate.get("_silo_home_source_library_ids") or []
    )
    if existing_library:
        source_libraries.add(existing_library)
    if candidate_library:
        source_libraries.add(candidate_library)
    chosen["_silo_home_source_library_ids"] = sorted(source_libraries)

    return chosen


def add_grouped_home_sections(sections):
    """Merge same-category Home sections into one folder while preserving order."""
    groups = {}
    order = []

    for section in sections or []:
        section_id = section.get("id") or section.get("section_id")
        if not section_id:
            continue

        total_count = section.get("total_count")
        try:
            if total_count is not None and int(total_count) <= 0:
                continue
        except (TypeError, ValueError):
            pass

        key = _home_section_group_key(section)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(section)

    for key in order:
        entries = groups[key]
        first = entries[0]
        title = _home_section_group_title(first)

        item = xbmcgui.ListItem(label=title)
        item.setProperty("Silo.HomeSectionGrouped", "true")
        item.setProperty("Silo.HomeSectionGroupKey", key)
        item.setProperty("Silo.HomeSectionCount", str(len(entries)))

        total_count = 0
        has_total = False
        any_featured = False
        any_custom = False
        any_customized = False
        item_limit = 0

        for entry in entries:
            value = entry.get("total_count")
            try:
                if value is not None:
                    total_count += max(0, int(value))
                    has_total = True
            except (TypeError, ValueError):
                pass

            any_featured = any_featured or bool(entry.get("featured"))
            any_custom = any_custom or bool(entry.get("is_custom"))
            any_customized = any_customized or bool(entry.get("customized"))

            try:
                item_limit = max(item_limit, int(entry.get("item_limit") or 0))
            except (TypeError, ValueError):
                pass

        item.setProperty("Silo.HomeSectionFeatured", "true" if any_featured else "false")
        item.setProperty("Silo.HomeSectionIsCustom", "true" if any_custom else "false")
        item.setProperty("Silo.HomeSectionCustomized", "true" if any_customized else "false")
        if item_limit:
            item.setProperty("Silo.HomeSectionItemLimit", str(item_limit))
        if has_total:
            item.setProperty("Silo.HomeSectionTotalCount", str(total_count))

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="home_section_group",
                group_key=key,
            ),
            item,
            True,
        )


def _sort_merged_home_items(items, detail_map, group_key):
    """Apply the correct global date ordering for a merged recent Home category."""
    if group_key == "recently added":
        # The Home card itself does not expose added_at. Use the per-card
        # /api/v2/catalog/items/{id} detail response, which does expose it.
        # Do not fall back to release/air/updated dates here.
        fields = (
            "added_at",
        )
    elif group_key == "recently released":
        fields = (
            "release_date",
            "air_date",
            "first_air_date",
            "updated_at",
            "added_at",
        )
    else:
        return

    def date_key(item):
        detail = detail_map.get(str(get_content_id(item))) or {}
        # Merged Home cards carry an occurrence-specific timestamp
        # fetched from /api/v2/catalog/items/{id}.
        if group_key == "recently added":
            value = item.get("_silo_home_added_at")
            if value:
                return (1, str(value))

        for field in fields:
            value = item.get(field) or detail.get(field)
            if value:
                return (1, str(value))
        return (0, "")

    # Cards with a usable date are ordered newest-first. Undated cards stay
    # after dated cards and retain their deterministic source order.
    # Recently Added timestamps are populated from the source-section lookup
    # above, allowing movies and TV to be interleaved by the same timestamp.
    dated = []
    undated = []

    for item in items:
        key = date_key(item)
        (dated if key[0] else undated).append(item)

    dated.sort(key=lambda item: date_key(item)[1], reverse=True)
    items[:] = dated + undated


def list_home_section_group(client, group_key):
    """Load and combine all Home sections represented by one category folder."""
    try:
        sections = client.home_sections(image_size="medium")
    except SiloError as exc:
        log("Unable to load Home sections for grouped view: %s" % exc, xbmc.LOGWARNING)
        notify("Home section unavailable")
        xbmcplugin.setPluginCategory(HANDLE, "Home")
        xbmcplugin.setContent(HANDLE, "videos")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    matching = [
        section
        for section in sections
        if _home_section_group_key(section) == str(group_key or "")
    ]

    if not matching:
        notify("Home section no longer exists")
        xbmcplugin.setPluginCategory(HANDLE, "Home")
        xbmcplugin.setContent(HANDLE, "videos")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    try:
        libraries = client.libraries()
    except SiloError as exc:
        log("Unable to load libraries for Home source resolution: %s" % exc, xbmc.LOGDEBUG)
        libraries = []

    combined = []
    seen = {}

    for section in matching:
        section_id = section.get("id") or section.get("section_id")
        if not section_id:
            continue

        source_library_id = _resolve_home_section_library_id(
            client,
            section,
            libraries=libraries,
        )

        try:
            # Keep exactly the cards Silo returned for this Home row. Extended
            # card metadata, including added_at, is fetched separately below.
            data = client.home_section_items(
                section_id,
                image_size="medium",
            ) or {}
        except SiloError as exc:
            log(
                "Unable to load Home section %s: %s"
                % (section_id, exc),
                xbmc.LOGWARNING,
            )
            continue

        section_data = data.get("section") if isinstance(data.get("section"), dict) else data
        source_items = (
            data.get("items")
            or (
                section_data.get("items")
                if isinstance(section_data, dict)
                else []
            )
            or []
        )

        for catalog_item in source_items:
            content_id = get_content_id(catalog_item)
            key = str(content_id) if content_id else None
            if not key:
                continue

            candidate = dict(catalog_item)
            candidate["_silo_home_source_section_id"] = str(section_id)
            candidate["_silo_home_source_section_ids"] = [str(section_id)]

            if source_library_id:
                candidate["_silo_home_source_library_id"] = source_library_id
                candidate["_silo_home_source_library_ids"] = [source_library_id]

            # Keep every occurrence until the merged list has been globally
            # sorted. The first copy after sorting is the one that sits highest
            # on the final combined page and should be the copy we display.
            combined.append(candidate)

    title = _home_section_group_title(matching[0])
    _render_catalog_items(
        client,
        {
            "title": title,
            "section_type": matching[0].get("section_type"),
            "items": combined,
            "_silo_home_source_sections": [
                {
                    "section": section,
                    "items": [
                        item
                        for item in combined
                        if str(item.get("_silo_home_source_section_id") or "") ==
                           str(section.get("id") or section.get("section_id") or "")
                    ],
                }
                for section in matching
            ],
        },
        section_title=title,
        merged_group_key=str(group_key or ""),
    )

def list_home_section(client, section_id):
    """Display one profile-wide Silo Home section with resilient source metadata."""
    try:
        data = client.home_section_items(
            section_id,
            image_size="medium",
        ) or {}
    except SiloError as exc:
        log(
            "Unable to load Home section %s: %s"
            % (section_id, exc),
            xbmc.LOGWARNING,
        )
        notify("Home section unavailable")
        xbmcplugin.setPluginCategory(HANDLE, "Home")
        xbmcplugin.setContent(HANDLE, "videos")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    section = data.get("section") if isinstance(data.get("section"), dict) else {}
    if not section:
        try:
            section = next(
                (
                    candidate
                    for candidate in client.home_sections(image_size="medium")
                    if str(candidate.get("id") or candidate.get("section_id") or "") == str(section_id)
                ),
                {},
            )
        except SiloError as exc:
            log(
                "Unable to resolve Home section metadata %s: %s"
                % (section_id, exc),
                xbmc.LOGDEBUG,
            )

    source_library_id = _resolve_home_section_library_id(client, section)

    source_items = (
        data.get("items")
        or (
            section.get("items")
            if isinstance(section, dict)
            else []
        )
        or []
    )

    annotated_items = []
    for catalog_item in source_items:
        item = dict(catalog_item)
        item["_silo_home_source_section_id"] = str(section_id)
        item["_silo_home_source_section_ids"] = [str(section_id)]

        if source_library_id:
            item["_silo_home_source_library_id"] = source_library_id
            item["_silo_home_source_library_ids"] = [source_library_id]

        annotated_items.append(item)

    normalized = dict(data)
    normalized["items"] = annotated_items
    if section:
        normalized.update(
            {
                key: value
                for key, value in section.items()
                if key != "items"
            }
        )

    _render_catalog_items(client, normalized)

def list_libraries(client):
    """Display the accessible Silo libraries inside the Libraries folder."""
    libraries = client.libraries()

    xbmcplugin.setPluginCategory(HANDLE, "Libraries")
    xbmcplugin.setContent(HANDLE, "files")

    batch = []

    for library in libraries:
        library_id = library.get("id")
        if not library_id:
            continue

        title = library.get("name") or library.get("title") or "Library"
        item = xbmcgui.ListItem(label=title)

        # Preserve library metadata for skins and future library-aware features.
        if library.get("id") is not None:
            item.setProperty("Silo.LibraryID", str(library.get("id")))
        if library.get("type"):
            item.setProperty("Silo.LibraryType", str(library.get("type")))
        if library.get("sort_order") is not None:
            item.setProperty("Silo.LibrarySortOrder", str(library.get("sort_order")))

        set_art(
            item,
            client,
            poster=library.get("poster_url"),
        )

        batch.append(
            (
                build_url(action="library", library_id=library_id),
                item,
                True,
            )
        )

    if batch:
        xbmcplugin.addDirectoryItems(HANDLE, batch, totalItems=len(batch))

    xbmcplugin.endOfDirectory(HANDLE)


def add_home_section_folder(section):
    """Add one non-empty profile-wide Silo Home section in server order."""
    section_id = section.get("id") or section.get("section_id")
    if not section_id:
        return

    total_count = section.get("total_count")
    try:
        if total_count is not None and int(total_count) <= 0:
            return
    except (TypeError, ValueError):
        pass

    title = section.get("title") or section.get("section_type") or section_id
    item = xbmcgui.ListItem(label=title)
    item.setProperty("Silo.HomeSectionID", str(section_id))
    item.setProperty("Silo.HomeSectionTitle", str(title))

    if section.get("section_type"):
        item.setProperty("Silo.HomeSectionType", str(section.get("section_type")))
    if section.get("featured") is not None:
        item.setProperty("Silo.HomeSectionFeatured", "true" if section.get("featured") else "false")
    if section.get("item_limit") is not None:
        item.setProperty("Silo.HomeSectionItemLimit", str(section.get("item_limit")))
    if total_count is not None:
        item.setProperty("Silo.SectionTotalCount", str(total_count))
        item.setProperty("Silo.HomeSectionTotalCount", str(total_count))
    if section.get("is_custom") is not None:
        item.setProperty(
            "Silo.HomeSectionIsCustom",
            "true" if section.get("is_custom") else "false",
        )
    if section.get("customized") is not None:
        item.setProperty(
            "Silo.HomeSectionCustomized",
            "true" if section.get("customized") else "false",
        )

    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="home_section", section_id=section_id),
        item,
        True,
    )


def _render_catalog_items(client, data, section_title=None, merged_group_key=None):
    """Display a profile-wide Silo Home section using the shared catalog renderer."""
    data = data or {}
    section_title = section_title or data.get("title") or data.get("section_type") or "Section"
    source_items = data.get("items") or []

    # For a merged recent section, keep every occurrence until after the
    # global date sort. This means a duplicate keeps whichever copy is higher
    # on the final combined page. Non-merged sections retain their original
    # first-occurrence deduplication behaviour.
    items = []
    if merged_group_key:
        items = list(source_items)
    else:
        seen = set()
        for catalog_item in source_items:
            content_id = get_content_id(catalog_item)
            key = str(content_id) if content_id else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            items.append(catalog_item)

    if not items:
        xbmcplugin.setPluginCategory(HANDLE, section_title)
        xbmcplugin.setContent(HANDLE, "videos")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    xbmcplugin.setPluginCategory(HANDLE, section_title)
    xbmcplugin.setContent(HANDLE, "videos")

    try:
        # Keep the normal detail prefetch for artwork, cast, crew, runtime and
        # stream metadata. Its result is also reused by the card renderer.
        detail_map = fetch_detail_metadata(client, items, None)

        if merged_group_key == "recently added":
            # For ordering, look up added_at from each source Home section,
            # but only apply timestamps to the cards already present in this
            # combined Home result. Do not replace or expand the Home card list.
            source_sections = data.get("_silo_home_source_sections") or []
            for source_section in source_sections:
                section = source_section.get("section") or {}
                source_items = source_section.get("items") or []
                added_at_map = fetch_home_added_at_by_section(
                    client,
                    section,
                    source_items,
                )
                for catalog_item in source_items:
                    content_id = get_content_id(catalog_item)
                    if content_id and str(content_id) in added_at_map:
                        catalog_item["_silo_home_added_at"] = added_at_map[str(content_id)]
    except Exception as exc:
        log(
            "Unable to prefetch Home detail metadata: %s"
            % exc,
            xbmc.LOGWARNING,
        )
        detail_map = {}

    if merged_group_key:
        _sort_merged_home_items(items, detail_map, str(merged_group_key))

        # Deduplicate only after the global merged sort. The first occurrence
        # is therefore the card that appears highest on the combined page.
        deduped = []
        seen = {}
        for catalog_item in items:
            content_id = get_content_id(catalog_item)
            key = str(content_id) if content_id else None

            if key and key in seen:
                index = seen[key]
                deduped[index] = _merge_home_catalog_items(
                    deduped[index],
                    catalog_item,
                )
                continue

            if key:
                seen[key] = len(deduped)
            deduped.append(catalog_item)

        items = deduped


    try:
        in_progress_map = client.in_progress_map()
    except SiloError as exc:
        log("Unable to retrieve in-progress Silo records for Home section: %s" % exc, xbmc.LOGWARNING)
        in_progress_map = {}

    try:
        series_watch_map, season_watch_map = fetch_series_watch_data(
            client,
            items,
        )
    except Exception as exc:
        log(
            "Unable to retrieve Home series watch-state rollups: %s"
            % exc,
            xbmc.LOGWARNING,
        )
        series_watch_map, season_watch_map = {}, {}

    batch = []

    for catalog_item in items:
        content_id = get_content_id(catalog_item)
        if not content_id:
            continue

        progress = in_progress_map.get(str(content_id)) or catalog_progress(catalog_item)
        media_type = (catalog_item.get("type") or catalog_item.get("media_type") or "").lower()
        season_rollup = (
            season_watch_map.get(str(content_id))
            or season_watch_map.get("%s:%s" % (catalog_item.get("series_id"), catalog_item.get("season_number")))
        )

        detail = detail_map.get(str(content_id))
        item_library_id = (
            _extract_home_library_id(catalog_item)
            or _extract_home_library_id(detail)
            or ""
        )

        source_library_ids = set(
            catalog_item.get("_silo_home_source_library_ids") or []
        )
        source_library_ids.update(
            item_library_id and [str(item_library_id)] or []
        )

        source_section_ids = sorted(
            set(catalog_item.get("_silo_home_source_section_ids") or [])
        )

        item, media_type, content_id, title, display_progress = build_catalog_list_item(
            client,
            catalog_item,
            detail=detail,
            progress=progress,
            series_rollup=series_watch_map.get(str(content_id)),
            season_rollup=season_rollup,
        )

        if item_library_id:
            item.setProperty("Silo.LibraryID", str(item_library_id))
            item.setProperty("Silo.HomeOriginLibraryID", str(item_library_id))

        if source_library_ids:
            item.setProperty(
                "Silo.HomeOriginLibraryIDs",
                ",".join(sorted(str(value) for value in source_library_ids)),
            )
        if source_section_ids:
            item.setProperty(
                "Silo.HomeSourceSections",
                ",".join(source_section_ids),
            )

        if media_type in PLAYABLE:
            item.setProperty("IsPlayable", "true")
            url = build_url(
                action="play",
                content_id=catalog_item.get("play_content_id") or content_id,
                library_id=item_library_id or None,
                duration_seconds=catalog_item.get("duration_seconds") or "",
                resume_available=int(has_usable_resume(display_progress)),
            )
            batch.append((url, item, False))
        elif media_type == "series":
            batch.append(
                (
                    build_url(
                        action="seasons",
                        series_id=content_id,
                        library_id=item_library_id or None,
                    ),
                    item,
                    True,
                )
            )
        elif media_type == "season":
            batch.append(
                (
                    build_url(
                        action="season",
                        series_id=catalog_item.get("series_id") or "",
                        season_number=catalog_item.get("season_number"),
                        library_id=item_library_id or None,
                    ),
                    item,
                    True,
                )
            )
        else:
            batch.append((build_url(action="search", query=title, page=1), item, False))

    if batch:
        xbmcplugin.addDirectoryItems(HANDLE, batch, totalItems=len(batch))

    xbmcplugin.endOfDirectory(HANDLE)


def list_library(client, library_id, cursor=None):
    """Display every item in a Silo library as efficiently as possible.

    The catalog supplies the viewer's watched flag. A small in-progress-only
    progress request supplies detailed partial positions; the full progress
    history remains reserved for the fresh playback check when Play is pressed.

    Kodi's addDirectoryItems() is used in batches because Kodi documents it as
    more efficient for large lists than repeatedly calling addDirectoryItem().
    """
    if not library_id:
        raise SiloError("No library ID was supplied.")

    # Use the user's configured page size for each Silo catalog request.
    # Silo supports up to 200 items per page; get_directory_page_size() is
    # already clamped to that range by the Kodi setting.
    page_size = get_directory_page_size()
    items, next_cursor = client.catalog_page(
        library_id,
        cursor=cursor,
        limit=page_size,
    )

    # The normal catalog tells us whether an item is played, but the detailed
    # partial position is not guaranteed to be present on every catalog row.
    # Fetch ONLY currently in-progress records so Kodi can display accurate
    # resume bars without downloading the user's entire progress history.
    try:
        in_progress_map = client.in_progress_map(
            library_id=library_id
        )
    except SiloError as exc:
        # A progress-display failure must never stop the library from loading.
        log(
            "Unable to retrieve in-progress Silo records: %s" % exc,
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    xbmcplugin.setContent(HANDLE, "movies")

    # Fetch cast, crew and full stream details before Kodi receives the list.
    # Requests run concurrently so the entire library still renders once.
    detail_map = fetch_detail_metadata(
        client,
        items,
        library_id,
    )

    # Fetch authoritative series watch totals from Silo so a show can
    # display partial/watched state even when Kodi has no local TV library data.
    series_watch_map, _season_watch_map = fetch_series_watch_data(
        client,
        items,
        library_id=library_id,
    )

    # Build Kodi entries first, then send them in batches. A batch size keeps
    # memory usage reasonable for very large libraries while still avoiding
    # thousands of individual Kodi plugin calls.
    batch = []
    batch_size = 500

    for catalog_item in items:
        content_id = get_content_id(catalog_item)
        if not content_id:
            log("Skipping catalog item with no content ID", xbmc.LOGWARNING)
            continue

        title = catalog_item.get("title") or catalog_item.get("name") or "Unknown"
        media_type = (
            catalog_item.get("type")
            or catalog_item.get("media_type")
            or ""
        ).lower()

        server_progress = in_progress_map.get(str(content_id))
        display_progress = server_progress or catalog_progress(catalog_item)

        list_item, media_type, content_id, title, display_progress = build_catalog_list_item(
            client,
            catalog_item,
            detail=detail_map.get(str(content_id)),
            progress=display_progress,
            series_rollup=series_watch_map.get(str(content_id)),
        )

        if media_type in PLAYABLE:
            list_item.setProperty("IsPlayable", "true")
            url = build_url(
                action="play",
                content_id=catalog_item.get("play_content_id") or content_id,
                library_id=library_id,
                duration_seconds=catalog_item.get("duration_seconds") or "",
                resume_available=int(has_usable_resume(display_progress)),
            )
            batch.append((url, list_item, False))
        else:
            url = build_url(
                action="seasons",
                series_id=content_id,
                library_id=library_id,
            )
            batch.append((url, list_item, True))

        # Flush a batch so extremely large libraries do not require the entire
        # Kodi list to remain in one Python tuple list at once.
        if len(batch) >= batch_size:
            xbmcplugin.addDirectoryItems(
                HANDLE,
                batch,
                totalItems=len(items),
            )
            batch = []

    if batch:
        xbmcplugin.addDirectoryItems(
            HANDLE,
            batch,
            totalItems=len(items) + (1 if next_cursor else 0),
        )

    if next_cursor:
        next_url = build_url(
            action="library",
            library_id=library_id,
            cursor=next_cursor,
        )
        next_item = xbmcgui.ListItem(label="Next Page")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(HANDLE, next_url, next_item, True)

    xbmcplugin.endOfDirectory(HANDLE)

def list_seasons(client, series_id, library_id, page=None):
    """Display all seasons belonging to a series.

    Library browsing already supplies the library ID because the user opened
    the series from a specific library. Search results are different: Silo's
    global catalog search intentionally does not include library membership on
    the returned series card, while the v2 season/episode endpoints require a
    positive library ID for the episode lookup.

    When search opened this page without a library ID, resolve the series
    against the user's accessible libraries and then carry the resolved ID
    forward into every season URL.
    """
    if not series_id:
        raise SiloError("No series ID was supplied.")

    resolved_library_id = library_id
    seasons = None

    if resolved_library_id:
        seasons = client.seasons(
            series_id,
            resolved_library_id,
        )
    else:
        # A global search result does not identify which accessible library
        # contains the series. Check each accessible library until the v2
        # seasons endpoint returns a matching set of seasons.
        libraries = client.libraries()

        for library in libraries:
            candidate_library_id = library.get("id")
            if not candidate_library_id:
                continue

            try:
                candidate_seasons = client.seasons(
                    series_id,
                    candidate_library_id,
                    suppress_not_found=True,
                )
            except SiloError as exc:
                log(
                    "Unable to check series %s in library %s: %s"
                    % (series_id, candidate_library_id, exc),
                    xbmc.LOGDEBUG,
                )
                continue

            if candidate_seasons:
                resolved_library_id = candidate_library_id
                seasons = candidate_seasons
                break

        if resolved_library_id is None:
            raise SiloError(
                "Unable to determine the Silo library for this TV show."
            )

    seasons = seasons or []
    page_items, has_previous, has_next = paginate_directory(
        seasons,
        page,
    )

    xbmcplugin.setContent(HANDLE, "seasons")

    add_previous_page(
        series_id=series_id,
        library_id=resolved_library_id,
        action="seasons",
        page=page,
    )

    for season in page_items:
        season_number = season.get("season_number", season.get("number"))
        if season_number is None:
            continue

        title = season.get("title") or "Season %s" % season_number
        item = xbmcgui.ListItem(label=title)

        set_season_metadata(
            item,
            season,
            client,
            series_id=series_id,
        )

        set_container_watch_state(
            item,
            normalize_watch_rollup(
                season.get("user_data"),
                season.get("episode_count") or 0,
            ),
        )

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="season",
                series_id=series_id,
                season_number=season_number,
                library_id=resolved_library_id,
            ),
            item,
            True,
        )

    if has_next:
        add_next_page(
            series_id=series_id,
            library_id=resolved_library_id,
            action="seasons",
            page=page,
        )

    xbmcplugin.endOfDirectory(HANDLE)
def list_episodes(client, series_id, season_number, library_id, page=None):
    """Display all episodes for a season and apply their current watched state."""
    if not series_id:
        raise SiloError("No series ID was supplied.")

    if season_number is None:
        raise SiloError("No season number was supplied.")

    all_episodes = client.episodes(series_id, season_number, library_id)
    episodes, has_previous, has_next = paginate_directory(
        all_episodes,
        page,
    )

    # Fetch only currently in-progress records for accurate episode resume
    # markers. Completed state comes from each catalog item's user_state.played
    # field, so we do not need the full progress history here.
    try:
        in_progress_map = client.in_progress_map(
            library_id=library_id
        )
    except SiloError as exc:
        log(
            "Unable to retrieve in-progress Silo records: %s" % exc,
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    xbmcplugin.setContent(HANDLE, "episodes")

    add_previous_page(
        series_id=series_id,
        season_number=season_number,
        library_id=library_id,
        action="season",
        page=page,
    )

    # Fetch extended episode metadata concurrently before Kodi receives the list.
    detail_map = fetch_detail_metadata(
        client,
        episodes,
        library_id,
    )

    # The episode endpoint returns CatalogItem objects as well, including the
    # viewer's watched flag. The in-progress map supplies detailed positions.
    batch = []
    batch_size = 500

    for episode in episodes:
        content_id = get_content_id(episode)
        if not content_id:
            continue

        title = episode.get("title") or episode.get("name") or "Episode"
        display_title = episode_display_label(
            title,
            episode.get("episode_number"),
        )
        item = xbmcgui.ListItem(label=display_title)
        tag = item.getVideoInfoTag()
        tag.setTitle(title)

        set_catalog_metadata(item, episode, client)

        if episode.get("episode_number") is not None:
            try:
                tag.setEpisode(int(episode["episode_number"]))
            except (TypeError, ValueError):
                pass

        if episode.get("season_number") is not None:
            try:
                tag.setSeason(int(episode["season_number"]))
            except (TypeError, ValueError):
                pass

        if episode.get("plot"):
            tag.setPlot(episode["plot"])

        set_art(
            item,
            client,
            # Current Silo v2 artwork fields.
            poster=(
                episode.get("poster_url")
                or episode.get("poster")
                or episode.get("image")
                or episode.get("thumbnail")
            ),
            backdrop=episode.get("backdrop_url"),
            logo=episode.get("logo_url"),
            # Episode still is retained as a thumbnail fallback.
            still=(
                episode.get("still_url")
                or episode.get("still")
            ),
        )

        # Apply the extended metadata fetched concurrently above.
        detail = detail_map.get(str(content_id))
        if detail:
            set_detail_metadata(item, detail, client)

        # Start with the catalog snapshot and prefer the dedicated in-progress
        # server record when Silo has one for this episode.
        display_progress = catalog_progress(episode)
        server_progress = in_progress_map.get(str(content_id))

        if server_progress:
            display_progress = server_progress

        set_watch_state(
            item,
            display_progress,
            "episode",
        )

        item.setProperty("IsPlayable", "true")

        # Reuse an already-known single file when the episode exposes one.
        files = episode.get("files") or []
        params = {
            "action": "play",
            "content_id": content_id,
            "library_id": library_id,
            "resume_available": int(has_usable_resume(display_progress)),
        }

        if len(files) == 1:
            file_id = files[0].get("id") or files[0].get("file_id")
            if file_id:
                params["file_id"] = file_id

        if episode.get("duration_seconds") is not None:
            params["duration_seconds"] = episode.get("duration_seconds")

        batch.append((
            build_url(**params),
            item,
            False,
        ))

        if len(batch) >= batch_size:
            xbmcplugin.addDirectoryItems(
                HANDLE,
                batch,
                totalItems=len(episodes),
            )
            batch = []

    if batch:
        xbmcplugin.addDirectoryItems(
            HANDLE,
            batch,
            totalItems=len(episodes),
        )

    if has_next:
        add_next_page(
            series_id=series_id,
            season_number=season_number,
            library_id=library_id,
            action="season",
            page=page,
        )

    xbmcplugin.endOfDirectory(HANDLE)

def resolve_playback_library_id(client, content_id, library_id=None):
    """Resolve a playable item's library using the same pattern as search."""
    if library_id not in (None, "", "None", "null"):
        return library_id

    libraries = client.libraries()

    # Search does not always know which library a result came from. Mirror its
    # resolution strategy here: test the item against each accessible library
    # until Silo accepts one and returns the item's detail document.
    for library in libraries:
        candidate_library_id = library.get("id")
        if candidate_library_id in (None, ""):
            continue

        try:
            detail = client.item_detail(
                content_id,
                candidate_library_id,
            )
        except SiloError as exc:
            log(
                "Unable to check playback item %s in library %s: %s"
                % (content_id, candidate_library_id, exc),
                xbmc.LOGDEBUG,
            )
            continue

        if detail:
            log(
                "Resolved playback library for %s: %s"
                % (content_id, candidate_library_id),
            )
            return candidate_library_id

    raise SiloError(
        "Unable to determine the Silo library for this item."
    )


def choose_file(client, content_id, library_id):
    """Return the file/version selected by the user."""
    versions = client.versions(content_id, library_id)

    if not versions:
        raise SiloError("Silo returned no playable versions.")

    if len(versions) == 1:
        return versions[0].get("id") or versions[0].get("file_id")

    labels = []

    for version in versions:
        labels.append(
            version.get("name")
            or version.get("title")
            or version.get("filename")
            or version.get("file_name")
            or str(version.get("id") or version.get("file_id"))
        )

    selected = xbmcgui.Dialog().select("Select version", labels)

    if selected < 0:
        return None

    version = versions[selected]
    return version.get("id") or version.get("file_id")


def apply_fresh_resume_to_resolved_item(list_item, progress, fallback_duration=0.0):
    """Put the freshly retrieved Silo resume state onto the resolved item.

    Kodi itself owns the resume dialog. We deliberately do not show our own
    yes/no dialog because Kodi will ask once using this freshly supplied
    resume point when its normal "Ask if resumable" behaviour is enabled.

    We also do not use StartOffset here. StartOffset would bypass the normal
    Kodi resume decision. The native Kodi resume system should perform the
    seek after the user chooses Resume.
    """
    tag = list_item.getVideoInfoTag()

    try:
        fallback_duration = max(0.0, float(fallback_duration or 0))
    except (TypeError, ValueError):
        fallback_duration = 0.0

    if not progress:
        # An unwatched item may have no progress record, but the catalog still
        # supplies its runtime. Pass that runtime to Kodi independently.
        if fallback_duration > 0:
            duration_int = int(round(fallback_duration))
            tag.setDuration(duration_int)

        tag.setPlaycount(0)
        tag.setResumePoint(0.0, 0.0)
        return

    completed = bool(progress.get("completed", False))
    position, duration = get_progress_position(progress)

    # Prefer the fresh progress duration when available; otherwise use the
    # catalog duration carried through the plugin URL.
    if duration <= 0:
        duration = fallback_duration

    if duration > 0:
        duration_int = int(round(duration))
        tag.setDuration(duration_int)

    if completed:
        # A completed item must not be offered as resumable.
        tag.setPlaycount(1)
        tag.setResumePoint(0.0, 0.0)
        return

    tag.setPlaycount(0)

    if position > 0 and duration > 0:
        # This is the fresh server position. Kodi's own resume prompt will use
        # this value and perform the seek if the user selects Resume.
        tag.setResumePoint(position, duration)
    else:
        # Incomplete but with no usable resume position.
        tag.setResumePoint(0.0, 0.0)


def play(
    client,
    content_id,
    file_id,
    library_id,
    duration_seconds=None,
    resume=False,
    resume_available=False,
):
    """Play media using Kodi's native Resume/Start-over choice.

    Kodi passes resume:true when the user chose Resume and resume:false when
    the user chose Start from beginning. A fresh Silo progress lookup is made
    immediately before playback. Silo always starts the transport at zero;
    when Resume was chosen, the fresh Silo position is placed on the resolved
    item so Kodi performs the seek to the server-authoritative position.
    """
    if not content_id:
        raise SiloError("No content ID was supplied for playback.")

    # Home and global-search items do not always carry a library ID. Resolve
    # the item against the same accessible-library set used by search before
    # asking Silo for versions or starting playback.
    library_id = resolve_playback_library_id(
        client,
        content_id,
        library_id,
    )

    use_kodi_resume_cache = use_kodi_resume_cache_enabled()
    kodi_cached_resume = (
        get_kodi_cached_resume_position()
        if use_kodi_resume_cache
        else 0.0
    )

    if not file_id:
        file_id = choose_file(client, content_id, library_id)

    if not file_id:
        return

    try:
        detail = client.item_detail(
            content_id,
            library_id,
            file_id,
        )
    except SiloError as exc:
        detail = None
        log(
            "Unable to retrieve extended metadata for %s: %s" % (
                content_id,
                exc,
            ),
            xbmc.LOGWARNING,
        )

    latest_progress = None

    if not use_kodi_resume_cache:
        try:
            latest_progress = client.get_progress(
                content_id,
                library_id,
            )
        except SiloError as exc:
            log(
                "Fresh progress lookup failed; continuing without Silo resume: %s" % exc,
                xbmc.LOGWARNING,
            )
    else:
        log(
            "Using Kodi cached resume position %.3fs for content %s"
            % (kodi_cached_resume, content_id)
        )

    # Silo starts the transport at zero in both modes. This is important:
    # Resume is implemented by Kodi seeking the resolved item to the fresh
    # Silo position, while Start from beginning receives no resume point.
    direct_play_only = direct_play_only_enabled()

    info = client.start_playback(
        file_id,
        start_position=0.0,
        direct_play_only=direct_play_only,
    )

    if not info.get("url"):
        raise SiloError("Silo did not provide a playback URL.")

    resolved_item = xbmcgui.ListItem(path=info["url"])

    # The item Kodi originally clicked may already contain a locally cached
    # resume point. Kodi normally merges the resolved ListItem's metadata with
    # that original item, which means an old local resume point can survive when
    # Silo now reports no resume data. Override the original video InfoTag so the
    # fresh server state below completely replaces Kodi's cached resume state.
    # This is also what lets a server-side reset to position 0 actually take
    # effect instead of falling back to Kodi's stale local bookmark.
    resolved_item.setProperty("OverrideInfotag", "true")

    if detail:
        try:
            set_catalog_metadata(resolved_item, detail, client)
            set_detail_metadata(
                resolved_item,
                detail,
                client,
                file_id=file_id,
            )

            # Apply the same Silo artwork to the actual playback ListItem.
            # The directory item already has artwork, but Kodi can create/use
            # this separate resolved item for playback, so copy all available
            # poster/backdrop/logo/still artwork here as well.
            set_art(
                resolved_item,
                client,
                poster=(
                    detail.get("poster_url")
                    or detail.get("poster")
                    or detail.get("image")
                    or detail.get("artwork")
                    or detail.get("thumbnail")
                ),
                backdrop=detail.get("backdrop_url") or detail.get("backdrop"),
                logo=detail.get("logo_url") or detail.get("logo"),
                still=detail.get("still_url") or detail.get("still"),
            )
        except Exception as exc:
            log(
                "Unable to apply extended playback metadata for %s: %s" % (
                    content_id,
                    exc,
                ),
                xbmc.LOGWARNING,
            )

    if use_kodi_resume_cache:
        if kodi_cached_resume > 0:
            apply_fresh_resume_to_resolved_item(
                resolved_item,
                {
                    "completed": False,
                    "position_seconds": kodi_cached_resume,
                    "duration_seconds": duration_seconds or 0,
                },
                fallback_duration=duration_seconds,
            )
        elif duration_seconds:
            try:
                resolved_item.getVideoInfoTag().setDuration(
                    int(round(float(duration_seconds)))
                )
            except (TypeError, ValueError):
                pass
    else:
        fresh_server_resume = has_usable_resume(latest_progress)

        if (
            latest_progress
            and (
                resume
                or (
                    not resume
                    and not resume_available
                    and fresh_server_resume
                )
            )
        ):
            apply_fresh_resume_to_resolved_item(
                resolved_item,
                latest_progress,
                fallback_duration=duration_seconds,
            )
            if resume:
                log(
                    "Kodi requested Resume; applied fresh Silo resume position "
                    "to the resolved item for content %s" % content_id
                )
            else:
                fresh_position, _fresh_duration = get_progress_position(
                    latest_progress
                )
                resolved_item.setProperty(
                    "StartOffset",
                    "%.3f" % fresh_position,
                )
                log(
                    "Kodi had no resume prompt, but Silo now has a fresh resume "
                    "position of %.3fs for content %s"
                    % (fresh_position, content_id)
                )
        elif resume:
            try:
                resume_duration = max(0.0, float(duration_seconds or 0))
            except (TypeError, ValueError):
                resume_duration = 0.0

            if resume_duration <= 0 and detail:
                version = _detail_version(detail, file_id)
                try:
                    resume_duration = max(0.0, float(version.get("duration") or 0))
                except (TypeError, ValueError):
                    resume_duration = 0.0
                if resume_duration <= 0:
                    resume_duration = get_runtime_seconds(detail)

            synthetic_progress = {
                "completed": False,
                "position_seconds": 0.1,
                "duration_seconds": resume_duration,
            }
            apply_fresh_resume_to_resolved_item(
                resolved_item,
                synthetic_progress,
                fallback_duration=resume_duration,
            )
        else:
            try:
                tag = resolved_item.getVideoInfoTag()
                tag.setPlaycount(0)
                tag.setResumePoint(0.0, 0.0)
            except Exception:
                pass

    resolved_item.setProperty("IsPlayable", "true")

    xbmcplugin.setResolvedUrl(
        HANDLE,
        True,
        resolved_item,
    )

    session_id = info.get("session_id")

    if session_id:
        track_progress(
            client,
            session_id,
            playback_info=info,
            resolved_item=resolved_item,
            force_start_zero=False,
        )
    else:
        log(
            "Silo playback started without a session ID; "
            "playback progress cannot be reported.",
            xbmc.LOGWARNING,
        )



def playback_session_was_terminated(exc):
    """Return True only for Silo's exact progress-session-not-found response.

    After an admin terminates playback, the progress endpoint returns this
    response because the playback session is no longer active. The admin UI's
    human-readable "Playback authority revoked" message is not sent to Kodi.
    """
    problem = getattr(exc, "problem", {}) or {}
    return (
        getattr(exc, "status", None) == 404
        and problem.get("type")
        == "https://siloserver.org/docs/api/v2/problems/not_found"
        and problem.get("detail") == "Playback session not found"
    )

def track_progress(
    client,
    session_id,
    playback_info=None,
    resolved_item=None,
    force_start_zero=False,
):
    """Monitor Kodi playback, report progress, and learn a stable quality.

    Silo publishes the authoritative quality ladder in playback_plan.available_qualities.
    Kodi does not invent its own bitrate ladder. On sustained buffering we move one
    published rung downward. Upward changes are treated as probes: a higher rung must
    survive a full confirmation period before it becomes the new trusted quality.

    Each quality that causes a real sustained playback failure is remembered for this
    video session. Its retry cooldown increases after repeated failures, which prevents
    the controller from repeatedly bouncing between the same two rungs.

    "original" is Silo's source-preserving ceiling. It remains the highest rung, but
    it is not repeatedly retried simply because playback has been healthy for 90 seconds.
    """
    player = xbmc.Player()
    monitor = xbmc.Monitor()

    direct_play_only = direct_play_only_enabled()

    playback_info = dict(playback_info or {})
    playback_info["session_id"] = session_id

    if direct_play_only:
        log("Direct-play-only mode enabled; adaptive quality switching is disabled.")

    # Wait for Kodi to actually begin playing the resolved stream.
    for _ in range(60):
        if player.isPlaying():
            break

        if monitor.abortRequested():
            return

        xbmc.sleep(500)

    if not player.isPlaying():
        log(
            "Kodi playback did not start; Silo playback session will not be tracked.",
            xbmc.LOGWARNING,
        )
        return

    # Kodi can apply a resume bookmark from its local database after the
    # resolved URL has been opened. If Silo says there is no resume state but
    # Kodi nevertheless invoked this request with resume=true, the only
    # reliable point at which we can override that local seek is after the
    # player is actually running. Force the player to time zero a few times
    # during the initial startup window so a late Kodi bookmark cannot win.
    if force_start_zero:
        for attempt in range(4):
            if not player.isPlaying() or monitor.abortRequested():
                break

            try:
                before = float(player.getTime())
            except Exception:
                before = 0.0

            try:
                player.seekTime(0.0)
                xbmc.sleep(150)
                after = float(player.getTime())
            except Exception as exc:
                log(
                    "Unable to force Kodi playback to the beginning "
                    "(attempt %d): %s" % (attempt + 1, exc),
                    xbmc.LOGWARNING,
                )
                break

            log(
                "Forced Kodi playback to 0.000s because Silo has no "
                "resume data (attempt %d; before=%.3f after=%.3f)"
                % (attempt + 1, before, after)
            )

    def quality_ladder(plan):
        """Return Silo's published quality ladder in server order."""
        entries = []
        seen = set()

        for quality in (plan or {}).get("available_qualities") or []:
            if not isinstance(quality, dict):
                continue

            label = str(quality.get("label") or "").strip()
            if not label or label in seen:
                continue

            seen.add(label)

            try:
                height = int(quality.get("height") or 0)
            except (TypeError, ValueError):
                height = 0

            try:
                bitrate = int(quality.get("bitrate_kbps") or 0)
            except (TypeError, ValueError):
                bitrate = 0

            entries.append({
                "label": label,
                "height": height,
                "bitrate_kbps": bitrate,
            })

        # Silo's v3 contract publishes original first. Preserve that order
        # instead of inventing a local ordering of the server's rungs.
        return entries

    def detect_quality_label(info):
        """Identify the current rung from the last adopted plan."""
        plan = (info or {}).get("playback_plan") or {}
        ladder = quality_ladder(plan)

        preferred = str((info or {}).get("adaptive_quality") or "").strip()
        labels = [entry["label"] for entry in ladder]

        if preferred and preferred in labels:
            return preferred

        recipe = plan.get("effective_recipe") or {}

        try:
            current_height = int(recipe.get("height") or 0)
        except (TypeError, ValueError):
            current_height = 0

        try:
            current_bitrate = int(recipe.get("bitrate_kbps") or 0)
        except (TypeError, ValueError):
            current_bitrate = 0

        # Prefer an exact height/bitrate match. This also lets an initial plan
        # that was capped by Silo identify its actual rung instead of assuming
        # that "original" was necessarily the active recipe.
        if current_height > 0 or current_bitrate > 0:
            for entry in ladder:
                if (
                    entry["height"] == current_height
                    and entry["bitrate_kbps"] == current_bitrate
                ):
                    return entry["label"]

        if labels:
            return labels[0]

        return ""


    # Per-video-session quality memory. A quality that actually caused a sustained
    # stall is not immediately retried as an upward probe. Repeated failures extend
    # its cooldown so the controller can settle on the highest quality that has
    # demonstrated stable playback for this video.
    quality_failures = {}
    quality_failure_cooldowns = {}

    # The currently trusted quality is the quality playback should fall back to
    # if a higher probe fails. This starts as the actual initial server selection.
    known_good_quality = ""
    known_good_since = 0.0

    # An upward switch is a probe until it remains healthy for the full
    # confirmation interval. While a probe is active, another upward switch is
    # forbidden and a stall returns directly to known_good_quality.
    probe_quality = None

    # A higher quality must prove itself for five minutes before becoming the
    # trusted quality. Failed rungs get an increasing 10/20/40/60 minute cooldown.
    quality_confirmation_threshold = 300.0
    quality_failure_cooldown_base = 600.0
    quality_failure_cooldown_max = 3600.0


    def next_quality_label(plan, current_label, direction):
        """Return the adjacent published rung.

        direction=-1 moves upward toward the source/original ceiling.
        direction=1 moves downward toward lower quality.
        """
        ladder = quality_ladder(plan)

        if len(ladder) < 2:
            return None

        labels = [entry["label"] for entry in ladder]

        if current_label in labels:
            index = labels.index(current_label)
        else:
            # The plan normally starts at original, so unknown state safely
            # falls back to the highest published rung.
            index = 0

        target_index = index + direction

        if target_index < 0 or target_index >= len(labels):
            return None

        return labels[target_index]

    def switch_stream(new_info, position, target_label):
        """Adopt an Silo replan without visibly jumping back to zero."""
        new_url = new_info.get("url")
        if not new_url:
            return False

        new_plan = new_info.get("playback_plan") or {}
        timeline = new_plan.get("timeline") or {}

        # Silo's player_start_seconds is the position the client should use
        # inside the newly planned stream. This is deliberately not always the
        # same number as the source/media position: a transcode can begin from
        # a seek anchor and expose a shorter player-relative timeline.
        try:
            start_offset = float(
                timeline.get("player_start_seconds")
            )
        except (TypeError, ValueError):
            start_offset = max(0.0, float(position or 0.0))

        start_offset = max(0.0, start_offset)

        log(
            "Adapting playback to quality=%s at position=%.3f "
            "(player_start_seconds=%.3f)"
            % (
                target_label,
                position,
                start_offset,
            )
        )

        # Reuse the original resolved ListItem so Kodi keeps the movie's
        # title, artwork, video info and other metadata across an adaptive
        # quality change. Only its playback path is replaced.
        list_item = resolved_item
        if list_item is None:
            # Defensive fallback for callers that do not provide the original
            # item; normal plugin playback always does.
            list_item = xbmcgui.ListItem(path=new_url)

        try:
            list_item.setPath(new_url)
        except Exception:
            # Older Kodi builds may not expose setPath() on ListItem. In that
            # case fall back to a new item rather than failing the quality
            # switch entirely.
            list_item = xbmcgui.ListItem(path=new_url)

        list_item.setProperty("IsPlayable", "true")
        list_item.setProperty(
            "StartOffset",
            "%.3f" % start_offset,
        )

        player.play(new_url, list_item)

        # Kodi can report isPlaying() while the previous HLS input is still
        # being torn down. Wait until Kodi reports the replacement URL as the
        # actual current playing file, otherwise the adaptive monitor can begin
        # measuring the old stream and immediately issue another replan.
        attached = False
        expected_url = str(new_url).split("?", 1)[0]
        for _ in range(120):
            if monitor.abortRequested():
                break

            if player.isPlaying():
                try:
                    playing_url = str(player.getPlayingFile() or "")
                except Exception:
                    playing_url = ""

                playing_base = playing_url.split("?", 1)[0]

                if (
                    playing_url == str(new_url)
                    or playing_base == expected_url
                ):
                    attached = True
                    break

            xbmc.sleep(250)

        if not attached:
            log(
                "Kodi did not attach the adaptive stream for quality=%s"
                % target_label,
                xbmc.LOGWARNING,
            )
            return False

        playback_info.clear()
        playback_info.update(new_info)
        playback_info["session_id"] = session_id
        playback_info["adaptive_quality"] = target_label

        return True

    # Determine the initial active quality from the server plan.
    playback_info["adaptive_quality"] = detect_quality_label(playback_info)
    known_good_quality = playback_info["adaptive_quality"]
    known_good_since = time.time()

    sequence = 0
    last_position = 0.0
    last_progress_position = None
    last_progress_change_at = time.time()
    last_reported_paused = None
    last_paused = None
    progress_report_interval = 5.0
    next_progress_report_at = time.time()
    progress_confirmed = True
    stall_started_at = None
    caching_started_at = None
    post_switch_grace_until = 0.0

    # Downward changes happen relatively quickly once sustained buffering is
    # detected. Upward changes require a longer healthy period and must also pass
    # the per-quality failure cooldown before a higher rung is probed.
    last_down_replan_at = 0.0
    last_up_replan_at = 0.0
    last_upshift_at = 0.0
    downshift_retry_at = 0.0
    downshift_retry_count = 0
    downshift_retry_exhausted = False
    down_cooldown = 10.0
    up_cooldown = 90.0
    stall_threshold = 8.0
    # Five minutes of healthy playback is required before a higher rung can be
    # probed. A successful probe has to satisfy the same full interval before
    # becoming the new trusted quality.
    healthy_recovery_threshold = quality_confirmation_threshold
    upshift_downshift_grace = 30.0
    adaptive_retry_delay = 5.0
    adaptive_retry_limit = 1

    healthy_since = time.time()

    # Kodi can briefly report isPlaying() == false while it replaces an HLS
    # input during an adaptive quality switch. Do not treat that short teardown
    # window as the end of the user's playback session.
    not_playing_since = None
    playback_teardown_grace = 120.0

    while True:
        if monitor.abortRequested():
            break

        now = time.time()

        if not player.isPlaying():
            # A true stop removes the media from Kodi. A temporary false
            # isPlaying() state during an adaptive handoff normally retains
            # Player.HasMedia, so preserve the existing handoff grace only for
            # that case.
            has_media = xbmc.getCondVisibility("Player.HasMedia")

            if not has_media:
                log(
                    "Kodi reports playback stopped; ending adaptive monitor "
                    "and closing the Silo playback session.",
                    xbmc.LOGDEBUG,
                )
                break

            if not_playing_since is None:
                not_playing_since = now
                log(
                    "Kodi temporarily reports playback stopped; keeping "
                    "adaptive monitor alive during stream handoff.",
                    xbmc.LOGDEBUG,
                )

            not_playing_for = now - not_playing_since

            if not_playing_for >= playback_teardown_grace:
                log(
                    "Adaptive monitor ending after Kodi reported no active "
                    "playback for %.1fs." % not_playing_for,
                    xbmc.LOGWARNING,
                )
                break

            xbmc.sleep(100)
            continue

        not_playing_since = None

        try:
            position = float(player.getTime())
        except Exception:
            position = last_position

        position = max(0.0, position)
        paused = bool(xbmc.getCondVisibility("Player.Paused"))

        if paused:
            # Paused time is not playback health and must never count as a
            # buffering stall or toward the healthy recovery timer.
            if last_paused is not True:
                log(
                    "Kodi playback paused; suspending adaptive quality timers.",
                    xbmc.LOGDEBUG,
                )

            caching_started_at = None
            stall_started_at = None
            healthy_since = 0.0
        elif last_paused is True:
            # Start a completely fresh health/stall measurement when playback
            # resumes. Without this reset, a pause longer than stall_threshold
            # could make the unchanged paused position look like buffering.
            last_progress_position = position
            last_progress_change_at = now
            caching_started_at = None
            stall_started_at = None
            healthy_since = now
            log(
                "Kodi playback resumed; resetting adaptive quality timers.",
                xbmc.LOGDEBUG,
            )

        last_paused = paused

        if not paused:
            # A probe becomes trusted only after Kodi has actually resumed
            # advancing on the replacement stream and maintained healthy
            # playback for the full confirmation threshold.
            if (
                probe_quality
                and progress_confirmed
                and healthy_since > 0
                and now - healthy_since >= quality_confirmation_threshold
            ):
                known_good_quality = probe_quality
                known_good_since = now

                # A long successful probe clears the quality's previous failure
                # history, because the current network conditions have now
                # demonstrated that the higher rung is sustainable.
                quality_failures.pop(probe_quality, None)
                quality_failure_cooldowns.pop(probe_quality, None)

                log(
                    "Adaptive quality probe confirmed: %s is now the "
                    "trusted quality after %.0fs of healthy playback"
                    % (
                        probe_quality,
                        now - healthy_since,
                    )
                )

                probe_quality = None

                # Start a fresh recovery interval so another upward probe is
                # never launched immediately after confirming one.
                healthy_since = now

            # Give Kodi a short handoff window after changing streams. During
            # this period the old player state may still be visible even though
            # the replacement URL has already been requested.
            in_post_switch_grace = now < post_switch_grace_until

            if in_post_switch_grace:
                caching_started_at = None
                stall_started_at = None
                stalled_for = 0.0
            else:
                # Player.Caching catches Kodi's internal rebuffering state while
                # position movement catches stalls where Kodi does not expose
                # the caching flag for the whole duration.
                caching = xbmc.getCondVisibility("Player.Caching")

                if caching:
                    if caching_started_at is None:
                        caching_started_at = now
                else:
                    caching_started_at = None

                position_stalled = (
                    last_progress_position is not None
                    and now - last_progress_change_at >= stall_threshold
                )
                caching_stalled = (
                    caching_started_at is not None
                    and now - caching_started_at >= stall_threshold
                )

                if position_stalled or caching_stalled:
                    if stall_started_at is None:
                        healthy_since = 0.0
                        stall_started_at = (
                            caching_started_at
                            if caching_stalled and caching_started_at is not None
                            else last_progress_change_at
                        )

                    stalled_for = now - stall_started_at
                else:
                    if last_progress_position is None:
                        last_progress_position = position
                        last_progress_change_at = now
                    elif position > last_progress_position + 0.25:
                        last_progress_position = position
                        last_progress_change_at = now

                        # Do not start the healthy recovery clock until the
                        # replacement stream has actually advanced.
                        if not progress_confirmed:
                            progress_confirmed = True
                            healthy_since = now
                            log(
                                "Adaptive replacement stream confirmed progressing "
                                "at position=%.3f" % position,
                                xbmc.LOGDEBUG,
                            )

                    if stall_started_at is not None:
                        if progress_confirmed:
                            healthy_since = now
                        stall_started_at = None

                    if healthy_since <= 0 and progress_confirmed:
                        healthy_since = now

                    # A new healthy playback interval starts a fresh adaptive
                    # retry budget for the next independent stall.
                    if downshift_retry_exhausted:
                        downshift_retry_exhausted = False
                        downshift_retry_count = 0
                        downshift_retry_at = 0.0
                        log(
                            "Adaptive retry budget reset after playback recovery",
                            xbmc.LOGDEBUG,
                        )

                    stalled_for = 0.0

            # -------------------------------------------------- downshift
            if (
                not direct_play_only
                and stall_started_at is not None
                and stalled_for >= stall_threshold
                and now - last_down_replan_at >= down_cooldown
                and now >= downshift_retry_at
                and not downshift_retry_exhausted
                and (
                    # A failed upward probe is allowed to fall back immediately;
                    # the normal grace still protects ordinary post-switch playback
                    # from false-positive stalls.
                    probe_quality == detect_quality_label(playback_info)
                    or last_upshift_at <= 0
                    or now - last_upshift_at >= upshift_downshift_grace
                )
            ):
                try:
                    plan = playback_info.get("playback_plan") or {}
                    current_label = detect_quality_label(playback_info)

                    # If the current rung is an upward probe, return directly
                    # to the last trusted quality. This avoids testing another
                    # rung in the same direction after a failed probe.
                    if (
                        probe_quality
                        and current_label == probe_quality
                        and known_good_quality
                        and known_good_quality != current_label
                    ):
                        target_label = known_good_quality
                    else:
                        target_label = next_quality_label(
                            plan,
                            current_label,
                            1,
                        )

                    if target_label is None:
                        # We are already at the lowest published rung. Do not
                        # repeatedly ask Silo for another recovery plan.
                        last_down_replan_at = now
                        downshift_retry_at = 0.0
                        downshift_retry_count = 0
                        downshift_retry_exhausted = False
                    else:
                        recipe = plan.get("effective_recipe") or {}

                        try:
                            current_bitrate = int(
                                recipe.get("bitrate_kbps") or 0
                            )
                        except (TypeError, ValueError):
                            current_bitrate = 0

                        estimated_bandwidth = max(
                            100,
                            int(current_bitrate * 0.60)
                            if current_bitrate > 0
                            else 1500,
                        )

                        # quality_change names the exact next published rung.
                        # This starts a fresh intent replan chain rather than
                        # permanently exhausting the playback recovery chain.
                        new_info = client.replan_playback(
                            playback_info,
                            position,
                            estimated_bandwidth,
                            quality_preference=target_label,
                            operation="quality_change",
                        )

                        new_plan = new_info.get("playback_plan") or {}
                        new_label = (
                            str(
                                (new_info or {}).get("adaptive_quality") or ""
                            ).strip()
                            or target_label
                        )

                        if (
                            new_info.get("url")
                            and new_label == target_label
                            and switch_stream(
                                new_info,
                                position,
                                target_label,
                            )
                        ):
                            switch_time = time.time()

                            # A successful downshift proves that the previous
                            # quality was not sustainable under the conditions
                            # that caused the stall. Remember this quality so it
                            # is not immediately selected again as an upshift.
                            failure_count = quality_failures.get(
                                current_label,
                                0,
                            ) + 1
                            quality_failures[current_label] = failure_count

                            cooldown = min(
                                quality_failure_cooldown_max,
                                quality_failure_cooldown_base
                                * (2 ** min(failure_count - 1, 3)),
                            )
                            quality_failure_cooldowns[current_label] = (
                                switch_time + cooldown
                            )

                            log(
                                "Adaptive quality %s marked unstable after "
                                "sustained stall #%d; upward probe blocked for %.0fs"
                                % (
                                    current_label,
                                    failure_count,
                                    cooldown,
                                )
                            )

                            # The selected lower rung becomes the trusted
                            # fallback. Any active upward probe is abandoned.
                            known_good_quality = target_label
                            known_good_since = switch_time
                            probe_quality = None

                            last_down_replan_at = switch_time
                            last_up_replan_at = 0.0
                            downshift_retry_at = 0.0
                            downshift_retry_count = 0
                            downshift_retry_exhausted = False
                            last_progress_position = None
                            last_progress_change_at = last_down_replan_at
                            progress_confirmed = False
                            healthy_since = 0.0
                            post_switch_grace_until = (
                                last_down_replan_at + 15.0
                            )
                            stall_started_at = None

                            log(
                                "Adaptive downshift complete: %s -> %s"
                                % (current_label, target_label)
                            )
                        else:
                            last_down_replan_at = now
                            log(
                                "Silo returned no exact adaptive downshift for "
                                "%s -> %s (delivery=%s)"
                                % (
                                    current_label,
                                    target_label,
                                    new_plan.get("delivery"),
                                ),
                                xbmc.LOGWARNING,
                            )

                except SiloError as exc:
                    retryable = bool(
                        isinstance(exc.problem, dict)
                        and exc.problem.get("retryable") is True
                    )

                    if retryable and downshift_retry_count < adaptive_retry_limit:
                        downshift_retry_count += 1
                        downshift_retry_at = now + adaptive_retry_delay
                        # The retry timer, rather than the normal cooldown, controls
                        # the next attempt for a retryable server-side startup failure.
                        last_down_replan_at = now - down_cooldown
                        log(
                            "Adaptive downshift failed with retryable Silo error; "
                            "retry %d/%d in %.1fs: %s"
                            % (
                                downshift_retry_count,
                                adaptive_retry_limit,
                                adaptive_retry_delay,
                                exc,
                            ),
                            xbmc.LOGWARNING,
                        )
                    else:
                        last_down_replan_at = now
                        downshift_retry_at = 0.0
                        if retryable:
                            # A retryable startup failure gets one immediate retry
                            # for this stall episode. If that also fails, wait for
                            # playback to recover before allowing another retry cycle.
                            downshift_retry_exhausted = True
                            downshift_retry_count = 0
                            log(
                                "Adaptive downshift retry failed; waiting for "
                                "playback recovery before trying again: %s" % exc,
                                xbmc.LOGWARNING,
                            )
                        else:
                            downshift_retry_count = 0
                            downshift_retry_exhausted = False
                            log(
                                "Adaptive downshift failed: %s" % exc,
                                xbmc.LOGWARNING,
                            )
                except Exception as exc:
                    last_down_replan_at = now
                    log(
                        "Unexpected adaptive downshift error: %s" % exc,
                        xbmc.LOGWARNING,
                    )

            # ---------------------------------------------------- upshift
            if (
                not direct_play_only
                and stall_started_at is None
                and healthy_since > 0
                and probe_quality is None
                and now - healthy_since >= healthy_recovery_threshold
                and now - last_up_replan_at >= up_cooldown
            ):
                try:
                    plan = playback_info.get("playback_plan") or {}
                    current_label = detect_quality_label(playback_info)
                    target_label = next_quality_label(
                        plan,
                        current_label,
                        -1,
                    )

                    if target_label is None:
                        # Already at the source/original ceiling. There is
                        # nothing higher to request until a future downshift.
                        last_up_replan_at = now
                        healthy_since = now
                    elif now < quality_failure_cooldowns.get(target_label, 0.0):
                        # This rung previously caused a sustained stall. Do not
                        # immediately probe it again just because the current
                        # quality has been healthy for five minutes.
                        remaining = max(
                            0.0,
                            quality_failure_cooldowns[target_label] - now,
                        )

                        last_up_replan_at = now

                        log(
                            "Adaptive upward probe held at %s: target quality "
                            "%s remains on failure cooldown for %.0fs"
                            % (
                                current_label,
                                target_label,
                                remaining,
                            ),
                            xbmc.LOGDEBUG,
                        )
                    else:
                        log(
                            "Adaptive upshift eligible: %s -> %s after %.1fs healthy playback"
                            % (
                                current_label,
                                target_label,
                                now - healthy_since,
                            )
                        )

                        recipe = plan.get("effective_recipe") or {}

                        try:
                            current_bitrate = int(
                                recipe.get("bitrate_kbps") or 0
                            )
                        except (TypeError, ValueError):
                            current_bitrate = 0

                        estimated_bandwidth = max(
                            1500,
                            int(current_bitrate * 1.35)
                            if current_bitrate > 0
                            else 8000,
                        )

                        # Never send "auto" for an adaptive quality recovery.
                        # Silo expects the label of the exact ladder rung wanted.
                        new_info = client.replan_playback(
                            playback_info,
                            position,
                            estimated_bandwidth,
                            quality_preference=target_label,
                            operation="quality_change",
                        )

                        new_plan = new_info.get("playback_plan") or {}
                        new_recipe = new_plan.get("effective_recipe") or {}

                        # quality_change is an explicit selection from Silo's
                        # published ladder. Once Silo accepts it and returns a
                        # replacement stream, do not second-guess the response
                        # with a local quality detector. The previous detector
                        # could mislabel a valid rung as "original" and leave
                        # the server on the new plan while Kodi kept the old
                        # stream, causing subsequent 409 stale-plan conflicts.
                        if (
                            new_info.get("url")
                            and switch_stream(
                                new_info,
                                position,
                                target_label,
                            )
                        ):
                            now = time.time()
                            last_up_replan_at = now
                            last_upshift_at = now
                            last_down_replan_at = 0.0
                            last_progress_position = None
                            last_progress_change_at = now
                            progress_confirmed = False
                            healthy_since = 0.0
                            post_switch_grace_until = now + 15.0

                            # The higher rung is now a probe. It must actually
                            # advance and then remain healthy for the full
                            # confirmation interval before replacing the current
                            # trusted quality.
                            probe_quality = target_label

                            log(
                                "Adaptive quality probe started: %s -> %s; "
                                "target must remain healthy for %.0fs before "
                                "becoming trusted"
                                % (
                                    current_label,
                                    target_label,
                                    quality_confirmation_threshold,
                                )
                            )

                            log(
                                "Adaptive upshift complete: %s -> %s "
                                "(server recipe height=%s bitrate=%s)"
                                % (
                                    current_label,
                                    target_label,
                                    new_recipe.get("height"),
                                    new_recipe.get("bitrate_kbps"),
                                )
                            )
                        else:
                            last_up_replan_at = now
                            healthy_since = now

                            log(
                                "Kodi could not adopt the Silo adaptive "
                                "upshift %s -> %s (delivery=%s)"
                                % (
                                    current_label,
                                    target_label,
                                    new_plan.get("delivery"),
                                ),
                                xbmc.LOGWARNING,
                            )

                except SiloError as exc:
                    last_up_replan_at = now
                    healthy_since = now

                    # A failed replan request is not enough evidence that the
                    # target quality itself is unstable. The server may simply
                    # have failed to create the replacement transport.
                    log(
                        "Adaptive quality probe request failed for %s: %s"
                        % (target_label, exc),
                        xbmc.LOGWARNING,
                    )
                except Exception as exc:
                    last_up_replan_at = now
                    healthy_since = now
                    log(
                        "Unexpected adaptive quality recovery error: %s"
                        % exc,
                        xbmc.LOGWARNING,
                    )

        last_position = max(last_position, position)
        pause_state_changed = (
            last_reported_paused is not None
            and bool(paused) != bool(last_reported_paused)
        )
        should_report_progress = (
            now >= next_progress_report_at or pause_state_changed
        )

        if should_report_progress:
            sequence += 1
            try:
                client.report_progress(
                    session_id,
                    sequence,
                    last_position,
                    paused,
                )
                last_reported_paused = bool(paused)
                next_progress_report_at = now + progress_report_interval
            except SiloError as exc:
                if playback_session_was_terminated(exc):
                    log(
                        "Silo playback session was terminated by the server; "
                        "stopping Kodi playback.",
                        xbmc.LOGWARNING,
                    )
                    try:
                        player.stop()
                    except Exception as stop_exc:
                        log(
                            "Unable to stop Kodi playback after server termination: %s"
                            % stop_exc,
                            xbmc.LOGWARNING,
                        )
                    break

                log(
                    "Unable to report playback progress: %s" % exc,
                    xbmc.LOGWARNING,
                )
                # Keep the next scheduled report based on the current time so
                # a slow/unreachable server does not alter adaptive monitoring.
                next_progress_report_at = now + progress_report_interval

        for _ in range(50):
            if not player.isPlaying() or monitor.abortRequested():
                break

            xbmc.sleep(100)

    try:
        if player.isPlaying():
            last_position = max(
                0.0,
                float(player.getTime()),
            )
    except Exception:
        pass

    sequence += 1

    def finish_session():
        try:
            client.stop_playback(
                session_id,
                sequence,
                last_position,
            )
        except SiloError as exc:
            log(
                "Unable to stop Silo playback session: %s" % exc,
                xbmc.LOGWARNING,
            )
        except Exception as exc:
            log(
                "Unexpected error stopping Silo playback session: %s" % exc,
                xbmc.LOGWARNING,
            )

    log(
        "Adaptive playback monitor ended for session %s at quality=%s"
        % (
            session_id,
            playback_info.get("adaptive_quality") or "unknown",
        ),
        xbmc.LOGDEBUG,
    )

    cleanup_thread = threading.Thread(
        target=finish_session,
        name="SiloPlaybackCleanup",
    )
    cleanup_thread.daemon = True
    cleanup_thread.start()

def kodi_requested_resume():
    """Return Kodi's native resume choice for the current plugin request.

    Kodi passes this to plugin scripts as the fourth argument:
        resume:true  -> the user chose Resume
        resume:false -> the user chose Start from beginning
    """
    if len(sys.argv) < 4:
        return False

    value = str(sys.argv[3] or "").strip().lower()

    if value.startswith("resume:"):
        value = value.split(":", 1)[1]

    return value == "true"


def router(client):
    """Route Kodi's current plugin request to the appropriate addon action."""
    query = sys.argv[2]

    if query.startswith("?"):
        query = query[1:]

    params = dict(
        parse_qsl(
            query,
            keep_blank_values=True,
        )
    )

    action = params.get("action")

    # Track whether the dedicated Watch Party lobby is the currently rendered
    # plugin directory. Normal navigation, including Kodi's '..', clears it.
    window = _watch_party_window()
    window.setProperty(
        "Silo.WatchParty.LobbyVisible",
        "true" if action == "watch_party_lobby" else "false",
    )

    if not action:
        list_root(client, params.get("page"))
        return

    if action == "root":
        list_root(
            client,
            params.get("page"),
        )
        return

    if action == "search":
        query = params.get("query", "")
        if query:
            list_search_results(
                client,
                query,
                params.get("page"),
            )
        else:
            search_silo(client)
        return

    if action == "settings":
        open_settings(client)
        return

    if action == "login":
        # A Login button starts the complete authentication flow.
        # This asks for server, username and password, then selects/verifies
        # the Silo profile before returning to the library screen.
        client.login_full()
        xbmc.executebuiltin("Container.Refresh")
        return

    if action == "home_section":
        list_home_section(
            client,
            params.get("section_id"),
        )
        return

    if action == "home_section_group":
        list_home_section_group(
            client,
            params.get("group_key"),
        )
        return

    if action == "your_stuff":
        list_your_stuff(client)
        return

    if action == "personal_list":
        _list_personal_catalog(
            client,
            params.get("source"),
            params.get("cursor"),
            params.get("collection_id"),
        )
        return

    if action == "watch_party_join":
        _watch_party_join(client)
        return

    if action == "watch_party_lobby":
        list_watch_party_lobby()
        return

    if action == "watch_party_leave":
        _watch_party_leave()
        return

    if action == "collections":
        list_collections(client)
        return

    if action == "collection":
        list_collection(
            client,
            params.get("collection_id"),
            params.get("title"),
            params.get("cursor"),
        )
        return

    if action == "libraries":
        list_libraries(client)
        return

    if action == "library":
        list_library(
            client,
            params.get("library_id"),
            params.get("cursor"),
        )
        return

    if action == "seasons":
        list_seasons(
            client,
            params.get("series_id"),
            params.get("library_id"),
            params.get("page"),
        )
        return

    if action == "season":
        list_episodes(
            client,
            params.get("series_id"),
            params.get("season_number"),
            params.get("library_id"),
            params.get("page"),
        )
        return

    if action == "play":
        play(
            client,
            params.get("content_id"),
            params.get("file_id"),
            params.get("library_id"),
            params.get("duration_seconds"),
            resume=kodi_requested_resume(),
            resume_available=str(params.get("resume_available", "")).strip().lower()
            in ("1", "true", "yes"),
        )
        return

    if action == "switch_profile":
        client.select_profile()
        xbmc.executebuiltin("Container.Refresh")
        return

    if action == "logout":
        client.logout()
        notify("Logged out of Silo")
        xbmc.executebuiltin("Container.Refresh")
        return

    log(
        "Unknown Kodi plugin action: %s" % action,
        xbmc.LOGWARNING,
    )


def main():
    """Create the Silo client and process Kodi's current plugin request."""
    client = SiloClient()

    try:
        router(client)

    except SiloError as exc:
        log(
            "Silo error: %s" % exc,
            xbmc.LOGERROR,
        )

        xbmcgui.Dialog().notification(
            "Silo",
            str(exc),
            xbmcgui.NOTIFICATION_ERROR,
            5000,
        )

    except Exception as exc:
        log(
            "Unexpected addon error: %s" % exc,
            xbmc.LOGERROR,
        )

        xbmcgui.Dialog().notification(
            "Silo",
            "Unexpected error: %s" % exc,
            xbmcgui.NOTIFICATION_ERROR,
            5000,
        )

    finally:
        # Do not close the login spinner here. A successful login calls
        # Container.Refresh, which starts a new main.py invocation to build
        # the library. list_root() in that new invocation closes the spinner
        # after the library directory has been populated.
        pass


# Kodi executes main.py as the addon entry point.
main()
