# -*- coding: utf-8 -*-
"""Standalone entry point for Silo Cinematic.

Kodi's plugin:// entry point must return promptly; this script owns the
long-lived WindowXML, following Silo-Theater's application model.
"""
import sys

import xbmc
import xbmcaddon
import xbmcgui

ADDON_ID = "plugin.video.silo"
addon = xbmcaddon.Addon(ADDON_ID)
addon_path = addon.getAddonInfo("path")
if addon_path and addon_path not in sys.path:
    sys.path.insert(0, addon_path)

try:
    import main as silo_main
    from resources.lib.cinematic import CinematicWindow

    client = silo_main.SiloClient()
    CinematicWindow.configure(
        client,
        "plugin://plugin.video.silo/",
        silo_main.cinematic_root_rows,
        silo_main.cinematic_load_directory,
    )
    window = CinematicWindow("SiloCinematic.xml", addon_path, "Default", "1080i")
    try:
        window.doModal()
    finally:
        del window
    if not addon.getSettingBool("cinematic_interface"):
        xbmc.executebuiltin("Container.Refresh")
except Exception as exc:
    xbmc.log("[plugin.video.silo] Cinematic runner failed: %s" % exc, xbmc.LOGERROR)
    xbmcgui.Dialog().notification(
        "Silo",
        "Cinematic interface failed to start. Check the Kodi log.",
        xbmcgui.NOTIFICATION_ERROR,
        5000,
    )
