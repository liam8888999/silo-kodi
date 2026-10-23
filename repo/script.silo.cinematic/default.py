# -*- coding: utf-8 -*-
"""Standalone Kodi script entry point for Silo Cinematic."""
import sys

import xbmc
import xbmcaddon
import xbmcgui

SCRIPT_ADDON = xbmcaddon.Addon("script.silo.cinematic")
SILO_ADDON = xbmcaddon.Addon("plugin.video.silo")
SILO_PATH = SILO_ADDON.getAddonInfo("path")
if SILO_PATH and SILO_PATH not in sys.path:
    sys.path.insert(0, SILO_PATH)

try:
    # main.py is import-safe; its plugin router only runs when Kodi executes
    # it as the plugin entry point. Reuse its existing directory builders.
    import main as silo_main
    from resources.lib.cinematic import CinematicWindow

    client = silo_main.SiloClient()
    CinematicWindow.configure(
        client,
        "plugin://plugin.video.silo/",
        silo_main.cinematic_root_rows,
        silo_main.cinematic_load_directory,
    )
    window = CinematicWindow(
        "SiloCinematic.xml",
        SILO_PATH,
        "Default",
        "1080i",
    )
    try:
        window.doModal()
    finally:
        del window

    if not SILO_ADDON.getSettingBool("cinematic_interface"):
        xbmc.executebuiltin("Container.Refresh")
except Exception as exc:
    xbmc.log("[plugin.video.silo] Cinematic script failed: %s" % exc, xbmc.LOGERROR)
    xbmcgui.Dialog().notification(
        "Silo",
        "Cinematic interface failed to start. Check the Kodi log.",
        xbmcgui.NOTIFICATION_ERROR,
        5000,
    )
