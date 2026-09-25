"""Silo search and people-search UI handling."""

from resources.lib import runtime
from resources.lib.common import *
from resources.lib.utils import *
from resources.lib.metadata import *

def display_title_for_person(person):
    """Build a concise label for a Silo people-search result."""
    name = str(person.get("name") or "Unknown")
    return "[Person] %s" % name



def build_person_list_item(client, person):
    """Build a Kodi directory item for a Silo person search result."""
    name = str(person.get("name") or "Unknown")
    item = xbmcgui.ListItem(label=display_title_for_person(person))

    photo = person.get("photo_url") or person.get("photo")
    if photo:
        item.setArt({
            "thumb": client.abs_url(str(photo)),
            "icon": client.abs_url(str(photo)),
        })

    tag = item.getVideoInfoTag()
    try:
        tag.setTitle(name)
        tag.setMediaType("actor")
    except Exception:
        pass

    if person.get("bio"):
        item.setProperty("Silo.Person.Bio", str(person["bio"]))
    if person.get("birth_date"):
        item.setProperty("Silo.Person.BirthDate", str(person["birth_date"]))
    if person.get("birthplace"):
        item.setProperty("Silo.Person.Birthplace", str(person["birthplace"]))
    if person.get("tmdb_id"):
        item.setProperty("Silo.Person.TmdbId", str(person["tmdb_id"]))

    return item



def list_person_media(client, person_id, person_name="", cursor=None):
    """Display one paged set of Silo media associated with one person."""
    if not str(person_id or "").strip():
        notify("Invalid person.")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    page_size = min(get_directory_page_size(), 100)

    try:
        items, next_cursor = client.person_catalog_page(
            person_id,
            cursor=cursor,
            limit=page_size,
        )
    except SiloError as exc:
        log(
            "Silo person media lookup failed for %s: %s"
            % (person_id, exc),
            xbmc.LOGERROR,
        )
        notify("Unable to load person credits: %s" % str(exc)[:180])
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    log(
        "Silo person media id=%s returned %d item(s), has_more=%s"
        % (person_id, len(items), bool(next_cursor)),
    )

    xbmcplugin.setPluginCategory(
        runtime.HANDLE,
        person_name or "Person",
    )
    xbmcplugin.setContent(runtime.HANDLE, "videos")

    # Use the same extended metadata pipeline as normal Search. This
    # preserves cast, crew, ratings, runtime, artwork, stream details,
    # identifiers and all other detail-only fields before Kodi renders the
    # person results.
    detail_map = fetch_detail_metadata(
        client,
        items,
        None,
    )

    try:
        in_progress_map = client.in_progress_map()
    except SiloError as exc:
        log(
            "Unable to retrieve in-progress Silo records for person results: %s"
            % exc,
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    series_watch_map, season_watch_map = fetch_series_watch_data(
        client,
        items,
    )

    grouped = {
        "movie": [],
        "series": [],
        "episode": [],
        "season": [],
    }
    other = []

    for catalog_item in items:
        media_type = (
            catalog_item.get("type")
            or catalog_item.get("media_type")
            or ""
        ).lower()
        grouped.get(media_type, other).append(catalog_item)

    ordered = (
        grouped["movie"]
        + grouped["series"]
        + grouped["season"]
        + grouped["episode"]
        + other
    )

    batch = []

    for catalog_item in ordered:
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

        item, media_type, content_id, title, display_progress = (
            build_catalog_list_item(
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
        )

        if media_type in PLAYABLE:
            item.setProperty("IsPlayable", "true")
            batch.append((
                build_url(
                    action="play",
                    content_id=(
                        catalog_item.get("play_content_id")
                        or content_id
                    ),
                    resume_available=int(
                        has_usable_resume(display_progress)
                    ),
                ),
                item,
                False,
            ))
        elif media_type == "series":
            batch.append((
                build_url(action="seasons", series_id=content_id),
                item,
                True,
            ))
        elif media_type == "season":
            batch.append((
                build_url(
                    action="season",
                    series_id=catalog_item.get("series_id") or "",
                    season_number=catalog_item.get("season_number"),
                ),
                item,
                True,
            ))
        else:
            continue

    if batch:
        xbmcplugin.addDirectoryItems(
            runtime.HANDLE,
            batch,
            totalItems=len(batch) + (1 if next_cursor else 0),
        )

    if next_cursor:
        next_item = xbmcgui.ListItem(label="Next Page")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(
                action="person",
                person_id=str(person_id),
                person_name=person_name or "",
                cursor=next_cursor,
            ),
            next_item,
            True,
        )

    if not batch:
        notify("No Silo media found for this person.")

    xbmcplugin.endOfDirectory(runtime.HANDLE)


def search_silo(client):
    """Prompt for a search term and display Silo's library-wide results."""
    query = xbmcgui.Dialog().input(
        "Search Silo",
    ).strip()

    if not query:
        xbmcplugin.setContent(runtime.HANDLE, "files")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    # Search people and media together so cast/crew names are first-class
    # results in the same Silo search interface.
    list_search_results(client, query, 1)



def list_search_results(client, query, page=1):
    """Display one page of Silo's server-side library-wide search results."""
    query = str(query or "").strip()

    if not query:
        xbmcplugin.setContent(runtime.HANDLE, "files")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
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
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    items = data.get("items") or []
    has_more = bool(data.get("has_more"))

    try:
        people = client.search_people(query, limit=50) or []
    except SiloError as exc:
        log(
            "Silo people search failed for %r: %s" % (query, exc),
            xbmc.LOGWARNING,
        )
        people = []

    log(
        "Silo search query=%r returned %d person result(s)"
        % (query, len(people))
    )

    log(
        "Silo search query=%r returned %d item(s), has_more=%s"
        % (query, len(items), has_more)
    )

    xbmcplugin.setPluginCategory(
        runtime.HANDLE,
        "Search: %s" % query,
    )
    xbmcplugin.setContent(runtime.HANDLE, "videos")

    # People are shown before media so a matching actor/director
    # can be selected directly from the normal Search directory.
    if page_number == 1 and people:
        for person in people:
            person_id = person.get("id")
            if person_id is None:
                continue
            person_item = build_person_list_item(client, person)
            xbmcplugin.addDirectoryItem(
                runtime.HANDLE,
                build_url(
                    action="person",
                    person_id=str(person_id),
                    person_name=person.get("name") or "",
                ),
                person_item,
                True,
            )

    if page_number > 1:
        previous_item = xbmcgui.ListItem(label="Previous Page")
        previous_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
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
            runtime.HANDLE,
            batch,
            totalItems=len(ordered_items) + (1 if has_more else 0),
        )

    if has_more:
        next_item = xbmcgui.ListItem(label="Next Page")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(
                action="search",
                query=query,
                page=page_number + 1,
            ),
            next_item,
            True,
        )

    if not items and not people:
        notify("No results found for: %s" % query)

    xbmcplugin.endOfDirectory(runtime.HANDLE)



__all__ = ["display_title_for_person","build_person_list_item","list_person_media","search_silo","list_search_results"]
