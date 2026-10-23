# -*- coding: utf-8 -*-
"""Standalone WindowXML runner for the Silo cinematic interface.

Start the window before importing the large plugin router. The router and Silo
Home loaders are initialized in a background thread so Kodi can paint the
cinematic loading state immediately rather than waiting for module startup.
"""
import importlib
import os
import sys
import threading
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON_ID = "plugin.video.silo"
ADDON = xbmcaddon.Addon(ADDON_ID)
ADDON_PATH = ADDON.getAddonInfo("path")
if ADDON_PATH and ADDON_PATH not in sys.path:
    sys.path.insert(0, ADDON_PATH)

try:
    from resources.lib.silo import SiloClient
    from resources.lib.cinematic import CinematicWindow, MIN_ROW_SLOTS, WINDOW_XML

    # Creating the client reads saved configuration only; network requests are
    # made by the worker-backed Home loader after the WindowXML is visible.
    client = SiloClient()

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

    profile_path = ADDON.getAddonInfo("profile")
    if profile_path and not os.path.isabs(profile_path):
        profile_path = xbmcvfs.translatePath(profile_path)
    profile_path = profile_path or ADDON_PATH

    # Importing main.py takes longer than constructing the skin. Let the
    # current WindowXML render first, then publish the backend module for the
    # asynchronous loaders to use.
    bootstrap = {"module": None, "error": None}
    bootstrap_ready = threading.Event()
    bootstrap_lock = threading.Lock()
    bootstrap_started = {"value": False}

    def import_backend():
        try:
            bootstrap["module"] = importlib.import_module("main")
            xbmc.log(
                "[plugin.video.silo] Cinematic backend import completed",
                xbmc.LOGINFO,
            )
        except Exception as exc:
            bootstrap["error"] = exc
            xbmc.log(
                "[plugin.video.silo] Cinematic backend import failed: %s" % exc,
                xbmc.LOGERROR,
            )
        finally:
            bootstrap_ready.set()

    def ensure_backend_started():
        # Start this only from the Home loader after the modal window has
        # rendered its first frame, not while the runner is still preparing.
        with bootstrap_lock:
            if bootstrap_started["value"]:
                return
            bootstrap_started["value"] = True
            threading.Thread(
                target=import_backend,
                name="SiloCinematicBackendImport",
                daemon=True,
            ).start()

    def get_backend():
        ensure_backend_started()
        if not bootstrap_ready.wait(120):
            raise RuntimeError("Silo backend initialization timed out.")
        if bootstrap["error"] is not None:
            raise RuntimeError("Silo backend initialization failed: %s" % bootstrap["error"])
        module = bootstrap["module"]
        if module is None:
            raise RuntimeError("Silo backend did not initialize.")
        return module

    def load_home(_client):
        # Give Kodi's GUI a short opportunity to paint the just-opened
        # WindowXML before loading the large main.py module in parallel.
        xbmc.sleep(100)
        module = get_backend()
        rows = module.cinematic_root_rows(client) or []

        # When there are more Home rails than the bundled skin has slots,
        # prepare the expanded layout on this worker thread. The current skin
        # stays visible while the server rows and expanded XML are prepared.
        row_count = sum(
            1 for row in rows
            if row and len(row) > 1 and row[0] and row[1]
        )
        if row_count > MIN_ROW_SLOTS:
            try:
                CinematicWindow.prepare_runtime_skin(
                    ADDON_PATH, profile_path, row_count
                )
            except Exception as exc:
                xbmc.log(
                    "[plugin.video.silo] Background cinematic skin sizing failed: %s" % exc,
                    xbmc.LOGWARNING,
                )
        return rows

    def load_directory(_client, action, params=None):
        module = get_backend()
        return module.cinematic_load_directory(client, action, params or {}) or []

    # Use the bundled skin immediately. Generating a 32-row runtime XML before
    # doModal() was the avoidable multi-second delay on every add-on launch.
    skin_name, skin_path = WINDOW_XML, ADDON_PATH
    row_capacity = MIN_ROW_SLOTS
    CinematicWindow.configure(
        client,
        "plugin://plugin.video.silo/",
        load_home,
        load_directory,
        initial_rows=None,
        row_capacity=row_capacity,
        initial_screen=initial_screen,
        initial_params=initial_params,
    )

    while True:
        xbmc.log(
            "[plugin.video.silo] Silo Cinematic runner: opening window immediately",
            xbmc.LOGINFO,
        )
        window = CinematicWindow(skin_name, skin_path, "Default", "1080i")
        try:
            window.doModal()
            result = getattr(window, "result", "")
        finally:
            del window
            xbmc.log("[plugin.video.silo] Silo Cinematic runner: window closed", xbmc.LOGINFO)

        if result != "rebuild":
            break

        initial_rows = getattr(CinematicWindow, "_launch_initial_rows", None)
        row_capacity = max(
            MIN_ROW_SLOTS,
            int(getattr(CinematicWindow, "_launch_row_capacity", MIN_ROW_SLOTS) or MIN_ROW_SLOTS),
        )
        skin_name, skin_path = CinematicWindow.prepare_runtime_skin(
            ADDON_PATH, profile_path, row_capacity
        )
        CinematicWindow.configure(
            client,
            "plugin://plugin.video.silo/",
            load_home,
            load_directory,
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
