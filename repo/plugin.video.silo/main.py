"""Kodi entry point and action router for the Silo Server addon.

Feature implementations live in dedicated modules:
    ui.py          - Kodi skin/UI helpers
    utils.py       - shared directory/URL/settings helpers
    metadata.py    - catalog/detail metadata and artwork
    search.py      - library and people search
    browsing.py    - Home, libraries, collections and series navigation
    playback.py    - playback/session/progress handling
    watch_party.py - Watch Party lobby and synchronization
"""

from resources.lib.common import *
from resources.lib import runtime
from resources.lib.ui import *
from resources.lib.utils import *
from resources.lib.metadata import *
from resources.lib.search import *
from resources.lib.browsing import *
from resources.lib.playback import *
from resources.lib.watch_party import *

# Kodi supplies the numeric handle and base URL for this invocation.
runtime.HANDLE = int(sys.argv[1])
runtime.BASE_URL = sys.argv[0]
runtime.ADDON = xbmcaddon.Addon()

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

    if action == "person":
        list_person_media(
            client,
            params.get("person_id") or "",
            params.get("person_name") or "",
            params.get("cursor"),
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

    if action == "watch_party_show_player":
        _watch_party_show_player()
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



# Kodi executes main.py as the addon entry point.
main()
