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

    xbmc.log("[plugin.video.silo] Silo Cinematic runner: main module imported", xbmc.LOGINFO)
    client = silo_main.SiloClient()
    xbmc.log("[plugin.video.silo] Silo Cinematic runner: Silo client created", xbmc.LOGINFO)
    # Load the actual home rows before constructing the WindowXML so the
    # skin can allocate enough row controls for this server/profile.
    initial_rows = silo_main.cinematic_root_rows(client) or []
    row_capacity = max(12, len(initial_rows))
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
    )
    window = CinematicWindow(skin_name, skin_path, "Default", "1080i")
    xbmc.log("[plugin.video.silo] Silo Cinematic runner: window constructed; opening", xbmc.LOGINFO)
    try:
        window.doModal()
    finally:
        del window
        xbmc.log("[plugin.video.silo] Silo Cinematic runner: window closed", xbmc.LOGINFO)

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
