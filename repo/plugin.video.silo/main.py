    ADDON.openSettings()
    xbmc.executebuiltin("Container.Refresh")


def list_root(client, page=None):
    """Display the initial screen or the logged-in Silo libraries.

    Authentication works in either direction:
        * The Login button can perform a complete fresh login.
        * Server/username entered in Kodi Settings are also used automatically.
    """

    # --------------------------------------------------------------
    # AUTHENTICATE SAVED SETTINGS
    # --------------------------------------------------------------
    # When the user has already entered a server and username in the Kodi
    # settings screen, do not require them to press Login as well. The password
    # is still requested securely because Kodi settings do not store it.
    if not client.cfg.get("token"):
        if client.base and client.cfg.get("username"):
            client.login()

            # login() authenticates the account but does not choose the
            # household/profile. Do that here so the settings-first path is
            # equivalent to the Login-button path.
            if not client.cfg.get("profile_id"):
                client.select_profile()
        else:
            login_item = xbmcgui.ListItem(label="Login")

            xbmcplugin.addDirectoryItem(
                HANDLE,
                build_url(action="login"),
                login_item,
                False,
            )

            xbmcplugin.setContent(HANDLE, "files")
            xbmcplugin.endOfDirectory(HANDLE)
            return

    # --------------------------------------------------------------
    # LOGGED IN
    # --------------------------------------------------------------
    search_item = xbmcgui.ListItem(label="Search")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="search"),
        search_item,
        True,
    )

    settings_item = xbmcgui.ListItem(label="Settings")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="settings"),
        settings_item,
        False,
    )

    libraries = client.libraries()
    page_items, has_previous, has_next = paginate_directory(
        libraries,
        page,
    )

    add_previous_page(
        action="root",
        page=page,
    )

    for library in page_items:
        library_id = library.get("id")
        if not library_id:
            continue

        title = library.get("name") or library.get("title") or "Library"
        item = xbmcgui.ListItem(label=title)

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="library", library_id=library_id),
            item,
            True,
        )
