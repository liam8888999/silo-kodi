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

import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin
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


def fetch_detail_metadata(client, items, library_id, max_workers=2):
    """Fetch extended metadata concurrently and retry transient failures.

    Catalog data is fast and contains most metadata. The detail endpoint adds
    cast, crew and full file stream information. Requests run concurrently,
    while transient timeouts, connection failures and server throttling/errors
    are retried before an item is considered unavailable.
    """
    content_ids = []
    seen = set()

    for item in items:
        content_id = get_content_id(item)
        if not content_id:
            continue

        key = str(content_id)
        if key not in seen:
            seen.add(key)
            content_ids.append(content_id)

    if not content_ids:
        return {}

    def fetch_one(content_id):
        attempts = 3

        for attempt in range(attempts):
            try:
                # Use a separate session per worker. requests.Session should not
                # be shared across concurrent requests.
                worker_client = SiloClient()
                worker_client.cfg.update(client.cfg)

                detail = worker_client.item_detail(
                    content_id,
                    library_id,
                )

                if detail:
                    return content_id, detail

                # An empty document is unusual but should get one retry.
                if attempt < attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
                    continue

                return content_id, None

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
                return content_id, None

        return content_id, None

    details = {}
    worker_count = max(
        1,
        min(int(max_workers or 2), len(content_ids)),
    )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, content_id)
            for content_id in content_ids
        ]

        for future in as_completed(futures):
            content_id, detail = future.result()

            if detail:
                details[str(content_id)] = detail

    return details


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

    user_state = item.get("user_state") or {}

    # A profile-scoped catalog should normally contain user_state. If it is
    # absent, return None rather than guessing the watch state.
    if not isinstance(user_state, dict) or not user_state:
        return None

    # Silo's catalog exposes the current resume position directly.
    position = item.get("position_seconds", 0)
    duration = item.get("duration_seconds", 0)

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

        # Make each result's media type obvious. Episodes also include their
        # parent series and season/episode number so dozens of identically
        # titled episodes can be distinguished immediately.
        display_title = title

        if media_type == "movie":
            display_title = "[Movie] %s" % title
        elif media_type == "series":
            display_title = "[TV Show] %s" % title
        elif media_type == "episode":
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
                display_title = "[Episode] %s - %s - %s" % (
                    series_title,
                    episode_code,
                    title,
                )
            elif series_title:
                display_title = "[Episode] %s - %s" % (
                    series_title,
                    title,
                )
            elif episode_code:
                display_title = "[Episode] %s - %s" % (
                    episode_code,
                    title,
                )
            else:
                display_title = "[Episode] %s" % title

        item = xbmcgui.ListItem(label=display_title)
        tag = item.getVideoInfoTag()
        tag.setTitle(title)

        set_catalog_metadata(item, catalog_item, client)

        set_art(
            item,
            client,
            poster=(
                catalog_item.get("poster_url")
                or catalog_item.get("poster")
                or catalog_item.get("image")
                or catalog_item.get("artwork")
                or catalog_item.get("thumbnail")
            ),
            backdrop=catalog_item.get("backdrop_url"),
            logo=catalog_item.get("logo_url"),
            still=(
                catalog_item.get("still_url")
                or catalog_item.get("still")
            ),
        )

        # Apply the extended metadata fetched above before this result is
        # handed to Kodi, matching the normal library and episode pages.
        detail = detail_map.get(str(content_id))
        if detail:
            set_detail_metadata(
                item,
                detail,
                client,
            )

        # Search results are already profile-scoped by Silo. Use the catalog
        # watch state directly so this search does not download the full
        # progress table spanning every library.
        set_watch_state(
            item,
            catalog_progress(catalog_item),
            media_type,
        )

        if media_type in PLAYABLE:
            item.setProperty("IsPlayable", "true")
            url = build_url(
                action="play",
                content_id=(
                    catalog_item.get("play_content_id")
                    or content_id
                ),
            )
            batch.append((url, item, False))
        elif media_type == "series":
            url = build_url(
                action="seasons",
                series_id=content_id,
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


def list_root(client, page=None):
    """Display the initial screen or the logged-in Silo libraries.

    Authentication works from either place:
        * The Login button performs a complete fresh login.
        * Server/username/profile entered in Kodi Settings are used automatically.
    """

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

    libraries = client.libraries()

    # The root library list is deliberately not paginated. Pagination only
    # applies to search results and the contents of individual libraries.
    for library in libraries:
        library_id = library.get("id")
        if not library_id:
            continue

        title = library.get("name") or library.get("title") or "Library"
        item = xbmcgui.ListItem(label=title)

        # Silo provides a library-level poster_url for custom library artwork.
        # Apply it as Kodi's poster, thumbnail and icon so the artwork is used
        # consistently by skins that prefer different art keys.
        set_art(
            item,
            client,
            poster=library.get("poster_url"),
        )

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="library", library_id=library_id),
            item,
            True,
        )

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

        list_item = xbmcgui.ListItem(label=title)
        tag = list_item.getVideoInfoTag()
        tag.setTitle(title)

        set_catalog_metadata(list_item, catalog_item, client)

        if catalog_item.get("year"):
            try:
                tag.setYear(int(catalog_item["year"]))
            except (TypeError, ValueError):
                pass

        if catalog_item.get("plot") or catalog_item.get("overview"):
            tag.setPlot(catalog_item.get("plot") or catalog_item.get("overview"))

        # Silo's current CatalogItem fields are poster_url/backdrop_url/logo_url.
        # Older names remain as fallbacks.
        set_art(
            list_item,
            client,
            poster=(
                catalog_item.get("poster_url")
                or catalog_item.get("poster")
                or catalog_item.get("image")
                or catalog_item.get("artwork")
                or catalog_item.get("thumbnail")
            ),
            backdrop=catalog_item.get("backdrop_url"),
            logo=catalog_item.get("logo_url"),
        )

        # Apply the extended metadata fetched concurrently above.
        detail = detail_map.get(str(content_id))
        if detail:
            set_detail_metadata(list_item, detail, client)

        # Start with the fast catalog snapshot. For an in-progress item, use
        # the dedicated server progress record because it contains the detailed
        # position and duration required for Kodi's partial-watch indicator.
        display_progress = catalog_progress(catalog_item)
        server_progress = in_progress_map.get(str(content_id))

        if server_progress:
            display_progress = server_progress

        set_watch_state(
            list_item,
            display_progress,
            media_type,
        )

        if media_type in PLAYABLE:
            list_item.setProperty("IsPlayable", "true")
            url = build_url(
                action="play",
                content_id=catalog_item.get("play_content_id") or content_id,
                library_id=library_id,
                duration_seconds=catalog_item.get("duration_seconds") or "",
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
        item = xbmcgui.ListItem(label=title)
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


def play(client, content_id, file_id, library_id, duration_seconds=None, resume=False):
    """Play media using Kodi's native Resume/Start-over choice.

    Kodi passes resume:true when the user chose Resume and resume:false when
    the user chose Start from beginning. A fresh Silo progress lookup is made
    immediately before playback. Silo always starts the transport at zero;
    when Resume was chosen, the fresh Silo position is placed on the resolved
    item so Kodi performs the seek to the server-authoritative position.
    """
    if not content_id:
        raise SiloError("No content ID was supplied for playback.")

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

    # Always check Silo immediately before starting the stream so Kodi never
    # has to rely on a stale local resume position for the actual seek.
    latest_progress = None

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

    if latest_progress:
        fresh_position, fresh_duration = get_progress_position(latest_progress)
        log(
            "Fresh Silo state before playback: content=%s position=%.3f duration=%.3f completed=%s"
            % (
                content_id,
                fresh_position,
                fresh_duration,
                latest_progress.get("completed", False),
            )
        )
    else:
        log(
            "No Silo progress record found immediately before playback for content %s"
            % content_id
        )

    # Silo starts the transport at zero in both modes. This is important:
    # Resume is implemented by Kodi seeking the resolved item to the fresh
    # Silo position, while Start from beginning receives no resume point.
    info = client.start_playback(
        file_id,
        start_position=0.0,
    )

    if not info.get("url"):
        raise SiloError("Silo did not provide a playback URL.")

    resolved_item = xbmcgui.ListItem(path=info["url"])

    if detail:
        try:
            set_catalog_metadata(resolved_item, detail, client)
            set_detail_metadata(
                resolved_item,
                detail,
                client,
                file_id=file_id,
            )
        except Exception as exc:
            log(
                "Unable to apply extended playback metadata for %s: %s" % (
                    content_id,
                    exc,
                ),
                xbmc.LOGWARNING,
            )

    if resume and latest_progress:
        # Replace Kodi's potentially stale local resume position with the
        # position we just fetched from Silo.
        apply_fresh_resume_to_resolved_item(
            resolved_item,
            latest_progress,
            fallback_duration=duration_seconds,
        )
        log(
            "Kodi requested Resume; applied fresh Silo resume position "
            "to the resolved item for content %s" % content_id
        )
    else:
        # Start from beginning must not carry a Kodi/Silo resume point.
        try:
            tag = resolved_item.getVideoInfoTag()
            tag.setPlaycount(0)
            tag.setResumePoint(0.0, 0.0)
        except Exception:
            pass

        if resume:
            log(
                "Kodi requested Resume but Silo returned no progress; "
                "starting from the beginning for content %s" % content_id
            )
        else:
            log(
                "Kodi requested Start from beginning; no resume point applied "
                "for content %s" % content_id
            )

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
        )
    else:
        log(
            "Silo playback started without a session ID; "
            "playback progress cannot be reported.",
            xbmc.LOGWARNING,
        )


def track_progress(
    client,
    session_id,
    playback_info=None,
    resolved_item=None,
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

    playback_info = dict(playback_info or {})
    playback_info["session_id"] = session_id

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
    probe_started_at = 0.0
    probe_confirmed = False

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
                probe_started_at = 0.0
                probe_confirmed = False

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
                stall_started_at is not None
                and stalled_for >= stall_threshold
                and now - last_down_replan_at >= down_cooldown
                and now >= downshift_retry_at
                and not downshift_retry_exhausted
                and (
                    last_upshift_at <= 0
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
                            probe_started_at = 0.0
                            probe_confirmed = False

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
                stall_started_at is None
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
                            probe_started_at = now
                            probe_confirmed = False

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
