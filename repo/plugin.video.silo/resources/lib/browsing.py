"""Library, Home, collection, and series browsing UI handling."""

from resources.lib import runtime
from resources.lib.common import *
from resources.lib.utils import *
from resources.lib.ui import *
from resources.lib.metadata import *
from resources.lib.watch_party import *

def open_settings(client):
    """Open this add-on's Kodi settings dialog."""
    client.sync_settings()
    runtime.ADDON.openSettings()
    xbmc.executebuiltin("Container.Refresh")



def add_your_stuff_folder():
    """Add the Silo web client's personal destinations."""
    item = xbmcgui.ListItem(label="Your Stuff")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(action="your_stuff"),
        item,
        True,
    )



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
            runtime.HANDLE,
            build_url(action="personal_list", source=source),
            item,
            True,
        )

    collections_item = xbmcgui.ListItem(label="Collections")
    collections_item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(action="collections"),
        collections_item,
        True,
    )

    xbmcplugin.setContent(runtime.HANDLE, "files")
    xbmcplugin.endOfDirectory(runtime.HANDLE)



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
        runtime.HANDLE,
        {
            "favorites": "Favorites",
            "watchlist": "Watchlist",
            "history": "History",
            "user_collection": "Collection",
        }.get(source, "Your Stuff"),
    )
    xbmcplugin.setContent(runtime.HANDLE, "videos")

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
            runtime.HANDLE,
            batch,
            totalItems=len(batch) + (1 if next_cursor else 0),
        )

    if next_cursor:
        next_item = xbmcgui.ListItem(label="More")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(
                action="personal_list",
                source=source,
                cursor=next_cursor,
                collection_id=collection_id,
            ),
            next_item,
            True,
        )

    xbmcplugin.endOfDirectory(runtime.HANDLE)



def list_collections(client):
    """Display the profile's visible personal Collections."""
    collections, groups = client.collections()

    # Keep the server's collection/group ordering, matching the web client.
    group_names = {str(group.get("id")): group.get("name") for group in groups or []}

    xbmcplugin.setPluginCategory(runtime.HANDLE, "Collections")
    xbmcplugin.setContent(runtime.HANDLE, "files")

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
            runtime.HANDLE,
            build_url(
                action="collection",
                collection_id=collection_id,
                title=name,
            ),
            item,
            True,
        )

    xbmcplugin.endOfDirectory(runtime.HANDLE)



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
                runtime.HANDLE,
                build_url(action="login"),
                login_item,
                False,
            )

            # Settings is deliberately available before login so the user can
            # enter the server/username/profile without using the Login button.
            settings_item = xbmcgui.ListItem(label="Settings")
            xbmcplugin.addDirectoryItem(
                runtime.HANDLE,
                build_url(action="settings"),
                settings_item,
                False,
            )

            xbmcplugin.setContent(runtime.HANDLE, "files")
            xbmcplugin.endOfDirectory(runtime.HANDLE)
            return

    search_item = xbmcgui.ListItem(label="Search")
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(action="search"),
        search_item,
        True,
    )

    # Keep all user libraries under one folder so the root stays focused on
    # global actions and profile-wide Home sections.
    libraries_item = xbmcgui.ListItem(label="Libraries")
    libraries_item.setProperty("Silo.LibraryFolder", "true")
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
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
        runtime.HANDLE,
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

    if runtime.ADDON.getSettingBool("combine_home_sections"):
        add_grouped_home_sections(home_sections)
    else:
        for section in home_sections:
            add_home_section_folder(section)

    profile_item = xbmcgui.ListItem(label="Switch Profile")
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(action="switch_profile"),
        profile_item,
        False,
    )

    logout_item = xbmcgui.ListItem(label="Logout")
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(action="logout"),
        logout_item,
        False,
    )

    # Keep Settings at the bottom of the root list when logged in as well.
    settings_item = xbmcgui.ListItem(label="Settings")
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(action="settings"),
        settings_item,
        False,
    )

    xbmcplugin.setContent(runtime.HANDLE, "files")
    xbmcplugin.endOfDirectory(runtime.HANDLE)

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
            runtime.HANDLE,
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
        xbmcplugin.setPluginCategory(runtime.HANDLE, "Home")
        xbmcplugin.setContent(runtime.HANDLE, "videos")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    matching = [
        section
        for section in sections
        if _home_section_group_key(section) == str(group_key or "")
    ]

    if not matching:
        notify("Home section no longer exists")
        xbmcplugin.setPluginCategory(runtime.HANDLE, "Home")
        xbmcplugin.setContent(runtime.HANDLE, "videos")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
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
        xbmcplugin.setPluginCategory(runtime.HANDLE, "Home")
        xbmcplugin.setContent(runtime.HANDLE, "videos")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
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

    xbmcplugin.setPluginCategory(runtime.HANDLE, "Libraries")
    xbmcplugin.setContent(runtime.HANDLE, "files")

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
        xbmcplugin.addDirectoryItems(runtime.HANDLE, batch, totalItems=len(batch))

    xbmcplugin.endOfDirectory(runtime.HANDLE)



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
        runtime.HANDLE,
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
        xbmcplugin.setPluginCategory(runtime.HANDLE, section_title)
        xbmcplugin.setContent(runtime.HANDLE, "videos")
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    xbmcplugin.setPluginCategory(runtime.HANDLE, section_title)
    xbmcplugin.setContent(runtime.HANDLE, "videos")

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
        xbmcplugin.addDirectoryItems(runtime.HANDLE, batch, totalItems=len(batch))

    xbmcplugin.endOfDirectory(runtime.HANDLE)



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

    xbmcplugin.setContent(runtime.HANDLE, "movies")

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
                runtime.HANDLE,
                batch,
                totalItems=len(items),
            )
            batch = []

    if batch:
        xbmcplugin.addDirectoryItems(
            runtime.HANDLE,
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
        xbmcplugin.addDirectoryItem(runtime.HANDLE, next_url, next_item, True)

    xbmcplugin.endOfDirectory(runtime.HANDLE)


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

    xbmcplugin.setContent(runtime.HANDLE, "seasons")

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
            runtime.HANDLE,
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

    xbmcplugin.endOfDirectory(runtime.HANDLE)

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

    xbmcplugin.setContent(runtime.HANDLE, "episodes")

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
                runtime.HANDLE,
                batch,
                totalItems=len(episodes),
            )
            batch = []

    if batch:
        xbmcplugin.addDirectoryItems(
            runtime.HANDLE,
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

    xbmcplugin.endOfDirectory(runtime.HANDLE)


__all__ = ["open_settings","add_your_stuff_folder","list_your_stuff","_list_personal_catalog","list_collections","list_collection","list_root","_home_section_group_key","_home_section_group_title","_extract_home_library_id","_resolve_home_section_library_id","_home_item_richness","_merge_home_catalog_items","add_grouped_home_sections","_sort_merged_home_items","list_home_section_group","list_home_section","list_libraries","add_home_section_folder","_render_catalog_items","list_library","list_seasons","list_episodes"]
