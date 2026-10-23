# -*- coding: utf-8 -*-
"""Standalone window runner inside plugin.video.silo.

Kodi runs this file in its own script process so the plugin-directory request
can finish immediately. It is not a separate add-on: every setting and module
belongs to plugin.video.silo.
"""
import sys

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

    client = silo_main.SiloClient()
    CinematicWindow.configure(
        client,
        "plugin://plugin.video.silo/",
        silo_main.cinematic_root_rows,
        silo_main.cinematic_load_directory,
    )
    window = CinematicWindow("SiloCinematic.xml", ADDON_PATH, "Default", "1080i")
    try:
        window.doModal()
    finally:
        del window

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
