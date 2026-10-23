# -*- coding: utf-8 -*-
"""Standalone window runner inside plugin.video.silo.

Kodi runs this file in its own script process so the plugin-directory request
can finish immediately. It is not a separate add-on: every setting and module
belongs to plugin.video.silo.
"""
import sys
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcgui

ADDON_ID = "plugin.video.silo"
ADDON = xbmcaddon.Addon(ADDON_ID)
ADDON_PATH = ADDON.getAddonInfo("path")
if ADDON_PATH and ADDON_PATH not in sys.path:
    sys.path.insert(0, ADDON_PATH)

try:
    import main as silo_main
    from resources.lib.cinematic import CinematicWindow

    xbmc.log("[plugin.video.silo] Silo Cinematic runner: main module imported", xbmc.LOGINFO)
    client = silo_main.SiloClient()
    xbmc.log("[plugin.video.silo] Silo Cinematic runner: Silo client created", xbmc.LOGINFO)
    # Direct links into Silo must use the cinematic window too. Decode the
    # plugin route from main.router() and map it to a cinematic screen.
    incoming_action = str(sys.argv[1] if len(sys.argv) > 1 else "root")
    incoming_query = str(sys.argv[2] if len(sys.argv) > 2 else "").lstrip("?")
    incoming_params = dict(parse_qsl(incoming_query, keep_blank_values=True))
    initial_screen = "home"
    initial_params = {}

    if incoming_action == "libraries":
        initial_screen = "libraries"
    elif incoming_action == "library":
        initial_screen, initial_params = "library", incoming_params
    elif incoming_action == "your_stuff":
        initial_screen = "your_stuff"
    elif incoming_action == "personal_list":
        initial_screen, initial_params = "personal", incoming_params
    elif incoming_action == "collections":
        initial_screen = "collections"
    elif incoming_action == "collection":
        initial_screen, initial_params = "collection", incoming_params
    elif incoming_action in (
        "person", "search", "home_section", "home_section_group",
        "library_letter", "seasons", "season",
    ):
        if incoming_action == "search" and not incoming_params.get("query"):
            query = str(xbmcgui.Dialog().input("Search Silo") or "").strip()
            if query:
                incoming_params["query"] = query
            else:
                incoming_action = "root"
        if incoming_action != "root":
            initial_screen = "directory"
            initial_params = dict(incoming_params, route_action=incoming_action)

    # Show the WindowXML immediately. Home itself loads asynchronously, so
    # slow server responses never expose Kodi's normal unskinned directories.
    initial_rows = None
    row_capacity = 32
    profile_path = ADDON.getAddonInfo("profile")
    if profile_path and not __import__("os").path.isabs(profile_path):
        profile_path = __import__("xbmcvfs").translatePath(profile_path)
    skin_name, skin_path = CinematicWindow.prepare_runtime_skin(
        ADDON_PATH, profile_path or ADDON_PATH, row_capacity
    )
    CinematicWindow.configure(
        client,
        "plugin://plugin.video.silo/",
        silo_main.cinematic_root_rows,
        silo_main.cinematic_load_directory,
        initial_rows=initial_rows,
        row_capacity=row_capacity,
        initial_screen=initial_screen,
        initial_params=initial_params,
    )
    while True:
        window = CinematicWindow(skin_name, skin_path, "Default", "1080i")
        xbmc.log("[plugin.video.silo] Silo Cinematic runner: window constructed; opening", xbmc.LOGINFO)
        try:
            window.doModal()
            result = getattr(window, "result", "")
        finally:
            del window
            xbmc.log("[plugin.video.silo] Silo Cinematic runner: window closed", xbmc.LOGINFO)
        if result != "rebuild":
            break
        # The window publishes the loaded rows and required capacity before it
        # closes. Reuse those results instead of issuing every Home request again.
        initial_rows = getattr(CinematicWindow, "_launch_initial_rows", None)
        row_capacity = max(32, int(getattr(CinematicWindow, "_launch_row_capacity", 32) or 32))
        skin_name, skin_path = CinematicWindow.prepare_runtime_skin(
            ADDON_PATH, profile_path or ADDON_PATH, row_capacity
        )
        CinematicWindow.configure(
            client,
            "plugin://plugin.video.silo/",
            silo_main.cinematic_root_rows,
            silo_main.cinematic_load_directory,
            initial_rows=initial_rows,
            row_capacity=row_capacity,
            initial_screen="home",
            initial_params={},
        )

    if not ADDON.getSettingBool("cinematic_interface"):
        xbmc.executebuiltin("Container.Refresh")
except Exception as exc:
    xbmc.log("[plugin.video.silo] Silo Cinematic runner failed: %s" % exc, xbmc.LOGERROR)
    try:
        xbmcgui.Dialog().notification(
            "Silo", "Cinematic interface failed to start. Check the Kodi log.",
            xbmcgui.NOTIFICATION_ERROR, 5000
        )
    except Exception:
        pass
