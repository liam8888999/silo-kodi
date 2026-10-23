# -*- coding: utf-8 -*-
"""Silo Cinematic lifecycle service, shipped inside plugin.video.silo.

The video plugin returns its directory request immediately. This service
owns the long-running WindowXML instance, avoiding Kodi's plugin-script
timeout while keeping all code/settings in the single Silo add-on.
"""
import sys

import xbmc
import xbmcaddon
import xbmcgui

ADDON_ID = "plugin.video.silo"
LAUNCH_PROPERTY = "Silo.Cinematic.LaunchRequested"
ADDON = xbmcaddon.Addon(ADDON_ID)
ADDON_PATH = ADDON.getAddonInfo("path")

if ADDON_PATH and ADDON_PATH not in sys.path:
    sys.path.insert(0, ADDON_PATH)


def _log(message, level=xbmc.LOGINFO):
    xbmc.log("[plugin.video.silo] Silo Cinematic service: %s" % message, level)


def _run_cinematic_window():
    """Create the client and hand its existing directory loaders to the UI."""
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
        ADDON_PATH,
        "Default",
        "1080i",
    )
    try:
        window.doModal()
    finally:
        del window


def run():
    monitor = xbmc.Monitor()
    host_window = xbmcgui.Window(10000)
    _log("started")

    while not monitor.abortRequested():
        request = host_window.getProperty(LAUNCH_PROPERTY)
        if request:
            # Clear before entering doModal so a stale request cannot reopen
            # the UI after the user closes it.
            host_window.clearProperty(LAUNCH_PROPERTY)

            if ADDON.getSettingBool("cinematic_interface"):
                _log("launch request received")
                try:
                    _run_cinematic_window()
                    _log("window closed")
                except Exception as exc:
                    _log("window failed: %s" % exc, xbmc.LOGERROR)
                    try:
                        xbmcgui.Dialog().notification(
                            "Silo",
                            "Cinematic interface failed to start. Check the Kodi log.",
                            xbmcgui.NOTIFICATION_ERROR,
                            5000,
                        )
                    except Exception:
                        pass
                finally:
                    host_window.clearProperty(LAUNCH_PROPERTY)
                    if not ADDON.getSettingBool("cinematic_interface"):
                        xbmc.executebuiltin("Container.Refresh")

        if monitor.waitForAbort(0.25):
            break

    _log("stopped")


if __name__ == "__main__":
    run()
