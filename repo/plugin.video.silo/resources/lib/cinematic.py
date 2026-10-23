"""Optional cinematic UI for the Silo Kodi add-on.

This module owns presentation and navigation only. Existing SiloClient and the
main playback route remain authoritative for authentication, versions, resume
positions and playback progress.
"""
import copy
import datetime
import json
import os
import shutil
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.silo import SiloError


ADDON = xbmcaddon.Addon("plugin.video.silo")
WINDOW_XML = "SiloCinematic.xml"

# Static controls defined by resources/skins/Default/1080i/SiloCinematic.xml.
NAV_HOME = 210
NAV_LIBRARIES = 211
NAV_SEARCH = 201
NAV_YOUR_STUFF = 212
NAV_COLLECTIONS = 203
NAV_SETTINGS = 204
NAV_PROFILE = 230
NAV_LOGOUT = 205
NAV_SWITCH_SERVER = 643
NAV_EDIT_HOME = 644
HERO_TITLE = 130
HERO_META = 131
HERO_PLOT = 132
HERO_LOGO = 133
HERO_BADGES = 134
HERO_BADGE_IMAGES = (135, 145, 147, 149)
HERO_BADGE_LABELS = (134, 146, 148, 150)
MIN_ROW_SLOTS = 12
SLOT_BASE = 3000
ROWS_TOP = 530
WIDE_PITCH = 410
POSTER_PITCH = 470
SCREEN_HEIGHT = 1080
GRID_HEADING = 0
GRID_CONTROL = 500
GRID_EMPTY = 550


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2, base + 3, base + 4


def _value(item, *keys, default=""):
    for key in keys:
        value = item.get(key) if isinstance(item, dict) else None
        if value not in (None, ""):
            return value
    return default


def _item_type(item):
    return str(_value(item, "type", "media_type")).lower()


def _playable(item):
    return _item_type(item) in ("movie", "episode", "video")


def _content_id(item):
    return _value(item, "content_id", "id", "play_content_id")


def _display_title(item):
    return str(_value(item, "title", "name", default="Untitled"))

def _positive_number(*values):
    """Use the first positive number, skipping empty and zero-valued API fallbacks."""
    for value in values:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0



class CinematicWindow(xbmcgui.WindowXML):
    """A standalone remote-friendly UI using Kodi's standard XML-first constructor."""

    # Kodi's native WindowXML validates the XML filename during object
    # construction, before this class's __init__ runs. Keep app dependencies
    # separate from those constructor arguments.
    _launch_client = None
    _launch_base_url = "plugin://plugin.video.silo/"
    _launch_root_loader = None
    _launch_directory_loader = None
    _launch_initial_rows = None
    _launch_row_capacity = MIN_ROW_SLOTS

    @classmethod
    def configure(cls, client, base_url, root_loader=None, directory_loader=None,
                  initial_rows=None, row_capacity=MIN_ROW_SLOTS):
        cls._launch_client = client
        cls._launch_base_url = str(base_url or "plugin://plugin.video.silo/")
        cls._launch_root_loader = root_loader
        cls._launch_directory_loader = directory_loader
        cls._launch_initial_rows = initial_rows
        cls._launch_row_capacity = max(MIN_ROW_SLOTS, int(row_capacity or MIN_ROW_SLOTS))

    @staticmethod
    def prepare_runtime_skin(addon_path, profile_path, row_capacity):
        """Allocate extra home rails inside the bundled Theater home group."""
        row_capacity = max(MIN_ROW_SLOTS, int(row_capacity or MIN_ROW_SLOTS))
        if row_capacity <= MIN_ROW_SLOTS:
            return WINDOW_XML, addon_path

        source_path = os.path.join(
            addon_path, "resources", "skins", "Default", "1080i", WINDOW_XML
        )
        tree = ET.parse(source_path)
        root = tree.getroot()
        controls = root.find("controls")
        if controls is None:
            raise RuntimeError("The cinematic skin has no controls section.")

        parents = {}
        for parent in root.iter():
            for child in list(parent):
                parents[id(child)] = parent
        rows = [
            control for control in root.iter("control")
            if control.get("type") == "group"
            and (control.get("id") or "").isdigit()
            and int(control.get("id")) >= SLOT_BASE
            and (int(control.get("id")) - SLOT_BASE) % 10 == 0
        ]
        existing_slots = len(rows)
        if existing_slots < 1:
            raise RuntimeError("The cinematic skin has no home-row template.")
        template = next(
            (control for control in rows if int(control.get("id")) == SLOT_BASE),
            rows[0],
        )
        destination_parent = parents.get(id(template), controls)
        for index in range(existing_slots, row_capacity):
            base = SLOT_BASE + 10 * index
            clone = copy.deepcopy(template)
            for element in clone.iter():
                old_id = element.get("id")
                if old_id and old_id.isdigit() and SLOT_BASE <= int(old_id) <= SLOT_BASE + 4:
                    element.set("id", str(base + int(old_id) - SLOT_BASE))
                if element.text:
                    element.text = element.text.replace(
                        "Control.HasFocus(3003)", "Control.HasFocus({})".format(base + 3)
                    ).replace(
                        "Control.HasFocus(3004)", "Control.HasFocus({})".format(base + 4)
                    )
                    if element.tag in ("onup", "ondown", "onleft", "onright"):
                        if element.text.strip() == "3003":
                            element.text = str(base + 3)
                        elif element.text.strip() == "3004":
                            element.text = str(base + 4)
            destination_parent.append(clone)

        target_root = os.path.join(profile_path, "cinematic_skin")
        target_dir = os.path.join(
            target_root, "resources", "skins", "Default", "1080i"
        )
        target_media = os.path.join(
            target_root, "resources", "skins", "Default", "media"
        )
        source_media = os.path.join(
            addon_path, "resources", "skins", "Default", "media"
        )
        os.makedirs(target_dir, exist_ok=True)
        os.makedirs(target_media, exist_ok=True)
        if os.path.isdir(source_media):
            for filename in os.listdir(source_media):
                source = os.path.join(source_media, filename)
                if os.path.isfile(source):
                    shutil.copy2(source, os.path.join(target_media, filename))
        target_name = "SiloCinematicRuntime.xml"
        tree.write(
            os.path.join(target_dir, target_name),
            encoding="utf-8",
            xml_declaration=True,
        )
        return target_name, target_root
    def __init__(self, xml_filename, script_path, default_skin="Default", default_res="1080i"):
        xbmcgui.WindowXML.__init__(
            self, str(xml_filename), str(script_path), str(default_skin), str(default_res)
        )
        self.client = type(self)._launch_client
        self.base_url = str(type(self)._launch_base_url or "plugin://plugin.video.silo/")
        self.root_loader = type(self)._launch_root_loader
        self.directory_loader = type(self)._launch_directory_loader
        self.addon_path = str(ADDON.getAddonInfo("path") or script_path)
        self._initial_rows = type(self)._launch_initial_rows
        self._row_capacity = max(
            MIN_ROW_SLOTS, int(type(self)._launch_row_capacity or MIN_ROW_SLOTS)
        )
        self.screen = "home"
        self.params = {}
        self.stack = []
        self._rows = []
        self._row_items = {}
        self._grid_items = []
        self._view_mode = "rails"
        self._row_index = 0
        self._hero_timer = None
        self._last_error = ""
        self._hero_logo = ""
        self._hero_badges_text = ""
        self._hero_overview = ""
        self._hero_badge_values = []
        self._initialized = False
        self._on_init_running = False
        self._modal_action_active = False
        self._row_preferences = self._load_row_preferences()
        self._saved_servers = self._load_saved_servers()
        self._home_loading = False
        self._home_load_done = threading.Event()
        self._home_load_result = None
        self._home_load_error = None
        self._home_load_timer = None
        self._window_closed = False

    def onInit(self):
        """Show the window immediately; load Home rows off the UI thread."""
        if self._modal_action_active or self._on_init_running:
            return
        self._on_init_running = True
        try:
            if not self._initialized:
                self._initialized = True
                self._modal_action_active = True
                try:
                    if (
                        not self.client.cfg.get("token")
                        and self.client.base
                        and self.client.cfg.get("username")
                    ):
                        self.client.login()
                        if not self.client.cfg.get("profile_id"):
                            self.client.select_profile(show_loading=False)
                finally:
                    self._modal_action_active = False
                if self.screen == "home" and self._initial_rows is None:
                    self._start_home_load()
                self._render()
            else:
                # On returning from the player or another window, reload Home
                # asynchronously so a slow server never leaves a blank screen.
                if self.screen == "home":
                    self._initial_rows = None
                    self._start_home_load()
                    self._render()
                else:
                    self._refresh_current_screen(preserve_focus=True)
        except Exception as exc:
            self._error(exc)
        finally:
            self._on_init_running = False
    def onAction(self, action):
        action_id = action.getId()
        focus_id = self.getFocusId()
        if action_id in (9, 10, 92, 216, 247, 257, 61448):
            if self.getProperty("silo.menu"):
                self.setProperty("silo.menu", "")
                try:
                    self.setFocusId(NAV_PROFILE)
                except Exception:
                    pass
                return
            if self.stack:
                snapshot = self.stack.pop()
                self.screen = snapshot.get("screen", "home")
                self.params = dict(snapshot.get("params") or {})
                self._row_index = int(snapshot.get("row_index") or 0)
                self._render()
                self._restore_snapshot(snapshot)
            else:
                self._window_closed = True
                if self._home_load_timer is not None:
                    try:
                        self._home_load_timer.cancel()
                    except Exception:
                        pass
                self.close()
            return


        # ACTION_CONTEXT_MENU / mouse right-click emulate Android TV long-press.
        if action_id in (101, 117):
            self._open_context_menu(focus_id)
            return

        # Kodi ACTION_SHOW_INFO (11) mirrors Android TV's detail-page flow.
        if action_id == 11:
            selected = None
            if self._view_mode == "rails":
                row = self._row_of(focus_id)
                if row is not None:
                    items = self._row_items.get(focus_id) or []
                    try:
                        pos = self.getControl(focus_id).getSelectedPosition()
                        selected = items[pos] if 0 <= pos < len(items) else None
                    except Exception:
                        selected = None
            elif focus_id == GRID_CONTROL:
                try:
                    pos = self.getControl(GRID_CONTROL).getSelectedPosition()
                    selected = self._grid_items[pos] if 0 <= pos < len(self._grid_items) else None
                except Exception:
                    selected = None
            if selected:
                action = selected.getProperty("Silo.Cinematic.Action")
                raw = selected.getProperty("Silo.Cinematic.Payload")
                try:
                    payload = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    payload = {}
                if action not in ("play", "start_over", "settings", "search", "libraries"):
                    self._item_click("details", payload)
                    return
        if self._view_mode == "rails":
            row = self._row_of(focus_id)
            if row is not None:
                if action_id == 3:
                    if row > 0:
                        self._focus_row(row - 1)
                    else:
                        # Make the top navigation reachable from the first rail;
                        # don't let Kodi's default focus search land on hero labels.
                        self.setFocusId(self._active_nav_control())
                    return
                if action_id == 4 and row + 1 < len(self._rows):
                    self._focus_row(row + 1)
                    return
        elif self._view_mode == "grid" and focus_id == GRID_CONTROL and action_id == 3:
            # Let Kodi's Panel control handle all normal row movement. Only
            # intercept Up at the first row to return to the top navigation.
            try:
                position = int(self.getControl(GRID_CONTROL).getSelectedPosition())
            except Exception:
                position = 0
            if position < 6:
                self.setFocusId(self._active_nav_control())
                return

        nav_ids = (
            NAV_HOME, NAV_LIBRARIES, NAV_SEARCH, NAV_YOUR_STUFF,
            NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT,
        )
        if focus_id in nav_ids and action_id == 4:
            self._focus_first_content()
            return
        if action_id in (1, 2, 3, 4, 5, 6, 7, 8, 107):
            # ACTION_MOUSE_MOVE (107) lets the featured panel follow the item
            # currently under the pointer, as well as D-pad/keyboard selection.
            self._schedule_hero_update()

    def onFocus(self, control_id):
        """Refresh featured details when mouse or keyboard focus enters a control."""
        if (
            control_id in (
                GRID_CONTROL, NAV_HOME, NAV_LIBRARIES, NAV_SEARCH, NAV_YOUR_STUFF,
                NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT,
            )
            or self._row_of(control_id) is not None
        ):
            self._schedule_hero_update()

    def onClick(self, control_id):
        try:
            if control_id in (
                NAV_HOME, NAV_LIBRARIES, NAV_SEARCH, NAV_YOUR_STUFF,
                NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT,
            ):
                self._nav_click(control_id)
                return
            # Profile menu controls are actual actions in the Theater skin.
            if control_id in (610, 611, 612, 613, 614, 615, 616, 617):
                self._profile_menu_click(control_id)
                return
            if control_id == GRID_CONTROL:
                selected = self.getControl(GRID_CONTROL).getSelectedItem()
            elif self._row_of(control_id) is not None:
                selected = self.getControl(control_id).getSelectedItem()
            else:
                selected = None
            if selected is not None:
                action = selected.getProperty("Silo.Cinematic.Action")
                raw_payload = selected.getProperty("Silo.Cinematic.Payload")
                try:
                    payload = json.loads(raw_payload) if raw_payload else {}
                except (TypeError, ValueError):
                    payload = {}
                self._item_click(action, payload)
        except Exception as exc:
            self._error(exc)
    def _error(self, exc):
        message = str(exc or "Unable to open this screen.")
        self._last_error = message
        xbmc.log("[plugin.video.silo] Silo Cinematic UI: %s" % message, xbmc.LOGWARNING)
        # The Theater-derived XML reads these window properties directly; it
        # doesn't contain the old standalone hero control IDs.
        self.setProperty("silo.hero.title", "Something went wrong")
        self.setProperty("silo.hero.synopsis", message[:240])
        try:
            xbmcgui.Dialog().notification("Silo", message[:180], xbmcgui.NOTIFICATION_ERROR, 4500)
        except Exception:
            pass
    def _push(self, screen, params=None):
        self.stack.append({
            "screen": self.screen,
            "params": dict(self.params),
            "row_index": self._row_index,
            "focus_snapshot": self._focus_snapshot(),
        })
        self.screen = screen
        self.params = dict(params or {})
        self._row_index = 0
        self._render()

    def _refresh_current_screen(self, preserve_focus=True):
        """Reload current server data and optionally restore the prior selection."""
        focus_snapshot = self._focus_snapshot() if preserve_focus else None
        if self.screen == "home" and self._initial_rows is None:
            self._start_home_load()
        self._render()
        if focus_snapshot is not None and getattr(self, "result", "") != "rebuild":
            self._restore_snapshot({"focus_snapshot": focus_snapshot})

    def _open_settings_and_refresh(self):
        """Open Kodi settings, reload the client/config, then refresh visible content."""
        old_cfg = dict(getattr(self.client, "cfg", {}) or {})
        old_context = tuple(old_cfg.get(key) for key in ("server", "username", "profile_id"))
        self._modal_action_active = True
        try:
            ADDON.openSettings()
        finally:
            self._modal_action_active = False

        if not ADDON.getSettingBool("cinematic_interface"):
            self.close()
            return

        try:
            # Re-read settings rather than retaining a client built with old
            # server/profile/auth settings.
            refreshed_client = type(self.client)()
        except Exception as exc:
            xbmc.log(
                "[plugin.video.silo] Cinematic settings reload failed: %s" % exc,
                xbmc.LOGWARNING,
            )
            refreshed_client = self.client

        new_cfg = dict(getattr(refreshed_client, "cfg", {}) or {})
        new_context = tuple(new_cfg.get(key) for key in ("server", "username", "profile_id"))
        self.client = refreshed_client
        self._initial_rows = None
        type(self).configure(
            refreshed_client,
            self.base_url,
            self.root_loader,
            self.directory_loader,
            initial_rows=None,
            row_capacity=self._row_capacity,
        )
        context_changed = old_context != new_context
        if context_changed:
            self.screen, self.params, self.stack = "home", {}, []
            self._row_index = 0
        self._refresh_current_screen(preserve_focus=not context_changed)

    def _load_row_preferences(self):
        try:
            raw = ADDON.getSetting("cinematic_home_row_preferences")
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_row_preferences(self):
        try:
            ADDON.setSetting("cinematic_home_row_preferences", json.dumps(self._row_preferences, sort_keys=True))
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Could not save cinematic row preferences: %s" % exc, xbmc.LOGWARNING)

    def _load_saved_servers(self):
        try:
            raw = ADDON.getSetting("cinematic_servers")
            data = json.loads(raw or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_servers(self):
        try:
            ADDON.setSetting("cinematic_servers", json.dumps(self._saved_servers))
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Could not save cinematic servers: %s" % exc, xbmc.LOGWARNING)

    def _manage_home_rows(self):
        rows = self._home_rows()
        titles = [str(row[0]) for row in rows]
        if not titles:
            xbmcgui.Dialog().notification("Silo", "No Home rows are available to customize.")
            return
        prefs = dict(self._row_preferences)
        order = list(prefs.get("order") or titles)
        hidden = set(prefs.get("hidden") or [])
        order = [title for title in order if title in titles] + [title for title in titles if title not in order]
        choices = ["Show/hide rows", "Reorder rows", "Reset row layout"]
        action = xbmcgui.Dialog().select("Customize Home rows", choices)
        if action < 0:
            return
        if action == 2:
            self._row_preferences = {}
        elif action == 0:
            selected = [i for i, title in enumerate(titles) if title not in hidden]
            picked = xbmcgui.Dialog().multiselect("Visible Home rows", titles, preselect=selected)
            if picked is None:
                return
            self._row_preferences = {"order": order, "hidden": [title for i, title in enumerate(titles) if i not in picked]}
        else:
            while True:
                picked = xbmcgui.Dialog().select("Reorder Home rows (select row to move up)", order)
                if picked < 0:
                    break
                if picked == 0:
                    continue
                order[picked - 1], order[picked] = order[picked], order[picked - 1]
            self._row_preferences = {"order": order, "hidden": list(hidden)}
        self._save_row_preferences()
        self._render()

    def _switch_server(self):
        cfg = getattr(self.client, "cfg", {}) or {}
        current = str(cfg.get("server") or "")
        options = list(self._saved_servers)
        if current and current not in options:
            options.insert(0, current)
        labels = list(options) + ["Add server…", "Remove saved server…"]
        selected = xbmcgui.Dialog().select("Silo servers", labels)
        if selected < 0:
            return
        if selected == len(options):
            server = str(xbmcgui.Dialog().input("Silo server URL", defaultt="https://" if not current else "") or "").strip().rstrip("/")
            if not server:
                return
            if server not in self._saved_servers:
                self._saved_servers.append(server)
                self._save_servers()
            xbmcgui.Dialog().notification("Silo", "Server saved. Sign in through Login to connect.")
            return
        if selected == len(options) + 1:
            if not self._saved_servers:
                return
            remove = xbmcgui.Dialog().select("Remove saved server", self._saved_servers)
            if remove >= 0:
                del self._saved_servers[remove]
                self._save_servers()
            return
        server = options[selected]
        if server == current:
            xbmcgui.Dialog().notification("Silo", "This server is already active.")
            return
        try:
            ADDON.setSetting("server", server)
            for key in ("token", "refresh_token", "profile_id", "profile_token"):
                ADDON.setSetting(key, "")
            self.client = type(self.client)()
            self.screen, self.params, self.stack = "home", {}, []
            self._initial_rows = None
            self._render()
            xbmcgui.Dialog().notification("Silo", "Server changed. Sign in if prompted.")
        except Exception as exc:
            self._error(exc)

    def _focused_list_item(self, focus_id=None):
        focus_id = self.getFocusId() if focus_id is None else focus_id
        try:
            if focus_id == GRID_CONTROL:
                return self.getControl(GRID_CONTROL).getSelectedItem()
            if self._row_of(focus_id) is not None:
                return self.getControl(focus_id).getSelectedItem()
        except Exception:
            return None
        return None

    def _open_context_menu(self, focus_id=None):
        selected = self._focused_list_item(focus_id)
        if selected is None:
            return
        try:
            raw = selected.getProperty("Silo.Cinematic.Payload")
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError):
            payload = {}
        action = str(selected.getProperty("Silo.Cinematic.Action") or "")
        media_type = str(selected.getProperty("Silo.Cinematic.Type") or "video").lower()
        content_id = (
            payload.get("content_id") or payload.get("play_content_id")
            or payload.get("series_id") or selected.getProperty("Silo.ContentID")
            or selected.getProperty("Silo.PlayContentID")
        )
        library_id = payload.get("library_id") or selected.getProperty("Silo.LibraryID") or ""
        title = str(selected.getLabel() or selected.getLabel2() or "Title")
        playable = media_type in ("movie", "episode", "video")
        payload.setdefault("content_id", content_id)
        payload.setdefault("library_id", library_id)
        payload.setdefault("title", title)
        payload.setdefault("type", media_type)

        watchlist = str(selected.getProperty("Silo.UserState.InWatchlist") or "").lower() == "true"
        favorite = str(selected.getProperty("Silo.UserState.IsFavorite") or "").lower() == "true"
        watched = str(selected.getProperty("Silo.Cinematic.Watched") or selected.getProperty("Silo.UserState.Played") or "").lower() == "true"

        actions = []
        callbacks = []
        if action and not playable:
            actions.append("Open")
            callbacks.append(("open", action, payload))
        elif content_id and not playable:
            actions.append("Open details")
            callbacks.append(("details", "details", payload))
        if content_id:
            actions.append("Title details")
            detail_action = "series" if media_type == "series" else "details"
            callbacks.append(("details", detail_action, payload))
        if playable:
            actions.append("Play / Resume")
            callbacks.append(("play", "play", payload))
            actions.append("Start over")
            callbacks.append(("start_over", "start_over", payload))
        if content_id and media_type in ("movie", "series", "season", "episode", "video"):
            actions.append("Remove from Watchlist" if watchlist else "Add to Watchlist")
            callbacks.append(("watchlist", "", payload))
            actions.append("Remove from Favorites" if favorite else "Add to Favorites")
            callbacks.append(("favorite", "", payload))
            actions.append("Mark as unwatched" if watched else "Mark as watched")
            callbacks.append(("watched", "", payload))
        if self.screen == "home" and self._view_mode == "rails":
            row = self._row_of(self.getFocusId() if focus_id is None else focus_id)
            if row is not None and row < len(self._rows):
                actions.append("Hide this Home row")
                callbacks.append(("hide_row", "", {"title": self._rows[row][0]}))
        if not actions:
            return

        self._modal_action_active = True
        try:
            picked = xbmcgui.Dialog().contextmenu(actions)
        finally:
            self._modal_action_active = False
        if not isinstance(picked, int) or picked < 0 or picked >= len(callbacks):
            return
        kind, action_name, action_payload = callbacks[picked]
        if kind in ("open", "details"):
            if kind == "open":
                self._item_click(action_name, action_payload)
            else:
                self._item_click(action_name or "details", action_payload)
            return
        if kind == "play":
            self._item_click("play", action_payload)
            return
        if kind == "start_over":
            action_payload["resume_available"] = 1
            self._item_click("start_over", action_payload)
            return
        if kind == "hide_row":
            hidden = set(self._row_preferences.get("hidden") or [])
            hidden.add(str(action_payload.get("title") or ""))
            self._row_preferences["hidden"] = sorted(value for value in hidden if value)
            self._save_row_preferences()
            self._render()
            return

        mark_id = action_payload.get("content_id")
        if not mark_id:
            return
        try:
            if kind == "watchlist":
                state = not watchlist
                self.client.set_watchlist(mark_id, state)
                selected.setProperty("Silo.UserState.InWatchlist", "true" if state else "false")
            elif kind == "favorite":
                state = not favorite
                self.client.set_favorite(mark_id, state)
                selected.setProperty("Silo.UserState.IsFavorite", "true" if state else "false")
            elif kind == "watched":
                state = not watched
                self.client.set_watched(mark_id, state)
                selected.setProperty("Silo.Cinematic.Watched", "true" if state else "")
                selected.setProperty("Silo.UserState.Played", "true" if state else "false")
            self._refresh_current_screen(preserve_focus=True)
        except Exception as exc:
            self._error(exc)

    def _close_profile_menu(self):
        self.setProperty("silo.menu", "")
        try:
            self.setFocusId(NAV_PROFILE)
        except Exception:
            pass

    def _profile_menu_click(self, control_id):
        self._close_profile_menu()
        if control_id == 610:
            # The profile chooser is only opened on an explicit menu action,
            # never merely because Kodi restores focus to the avatar.
            old_profile = (getattr(self.client, "cfg", {}) or {}).get("profile_id")
            self._modal_action_active = True
            try:
                self.client.select_profile(show_loading=False)
            finally:
                self._modal_action_active = False
            new_profile = (getattr(self.client, "cfg", {}) or {}).get("profile_id")
            if old_profile != new_profile:
                self.screen, self.params, self.stack = "home", {}, []
                self._row_index = 0
                self._initial_rows = None
            self._update_profile_properties()
            self._refresh_current_screen(preserve_focus=old_profile == new_profile)
        elif control_id == 611:
            self._open_settings_and_refresh()
        elif control_id == 612:
            self._modal_action_active = True
            try:
                confirm = xbmcgui.Dialog().yesno("Silo", "Sign out of this Silo account?")
            finally:
                self._modal_action_active = False
            if confirm:
                self.client.logout()
                self.screen, self.params, self.stack = "home", {}, []
                self._row_index = 0
                self._initial_rows = None
                self._refresh_current_screen(preserve_focus=False)
        elif control_id == 613:
            self._push("directory", {"route_action": "personal_list", "source": "watchlist", "title": "Watchlist"})
        elif control_id == 614:
            self._push("directory", {"route_action": "personal_list", "source": "favorites", "title": "Favorites"})
        elif control_id == 615:
            self._push("directory", {"route_action": "personal_list", "source": "history", "title": "History"})
        elif control_id == 616:
            self._manage_home_rows()
        elif control_id == 617:
            self._switch_server()

    def _nav_click(self, control_id):
        if control_id == NAV_HOME:
            self.stack = []
            self.screen, self.params, self._row_index = "home", {}, 0
            self._initial_rows = None
            self._refresh_current_screen(preserve_focus=False)
        elif control_id == NAV_LIBRARIES:
            self._push("libraries")
        elif control_id == NAV_SEARCH:
            query = str(xbmcgui.Dialog().input("Search Silo", defaultt=self.params.get("query", "")) or "").strip()
            if query:
                self._push("search", {"query": query})
        elif control_id == NAV_YOUR_STUFF:
            self._push("your_stuff")
        elif control_id == NAV_COLLECTIONS:
            self._push("collections")
        elif control_id == NAV_SETTINGS:
            self._open_settings_and_refresh()
        elif control_id == NAV_SWITCH_SERVER:
            self._switch_server()
        elif control_id == NAV_EDIT_HOME:
            self._manage_home_rows()
        elif control_id == NAV_PROFILE:
            # Match Silo-Theater: the avatar opens the profile menu; it does not
            # immediately trigger profile selection whenever it regains focus.
            self.setProperty("silo.menu", "profile")
            try:
                self.setFocusId(610)
            except Exception:
                pass
        elif control_id == NAV_LOGOUT:
            self._modal_action_active = True
            try:
                should_logout = xbmcgui.Dialog().yesno("Silo", "Log out of this Silo account?")
            finally:
                self._modal_action_active = False
            if should_logout:
                self.client.logout()
                self.screen, self.params, self.stack = "home", {}, []
                self._row_index = 0
                self._initial_rows = None
                self._refresh_current_screen(preserve_focus=False)
    def _update_profile_properties(self):
        cfg = getattr(self.client, "cfg", {}) or {}
        display_name = str(
            cfg.get("profile_display_name")
            or ADDON.getSetting("profile_display_name")
            or cfg.get("profile_name")
            or cfg.get("username")
            or ""
        ).strip()
        avatar = str(
            cfg.get("profile_avatar")
            or ADDON.getSetting("profile_avatar")
            or ""
        ).strip()
        if avatar and not avatar.startswith(("http://", "https://", "image://")):
            try:
                avatar = self.client.abs_url(avatar)
            except Exception:
                pass
        words = [part for part in display_name.replace("_", " ").split() if part]
        initials = "".join(word[0] for word in words[:2]).upper()
        if not initials and display_name:
            initials = display_name[:1].upper()
        self.setProperty("silo.profile.name", display_name)
        self.setProperty("silo.avatar", avatar)
        self.setProperty("silo.initials", initials)

    def _start_home_load(self):
        """Start Home requests in the background and keep the UI responsive."""
        if self._home_loading:
            # Keep _home_rows() on its cached path while an existing fetch is
            # underway; otherwise a re-entrant onInit can call the loader inline.
            if self._initial_rows is None:
                self._initial_rows = []
            return
        if not callable(self.root_loader):
            self._initial_rows = []
            return
        self._home_loading = True
        self._home_load_done.clear()
        self._home_load_result = None
        self._home_load_error = None
        # An empty cached result prevents _render() from calling the loader
        # synchronously while the worker is still fetching the real sections.
        self._initial_rows = []

        def worker():
            try:
                rows = self.root_loader(self.client) or []
                self._home_load_result = rows
            except Exception as exc:
                self._home_load_error = exc
                self._home_load_result = []
            finally:
                self._home_load_done.set()

        thread = threading.Thread(
            target=worker,
            name="SiloCinematicHomeLoader",
        )
        thread.daemon = True
        self._home_load_thread = thread
        thread.start()
        self._schedule_home_load_poll()

    def _schedule_home_load_poll(self):
        if self._window_closed or not self._home_loading:
            return
        if self._home_load_timer is not None:
            try:
                self._home_load_timer.cancel()
            except Exception:
                pass
        self._home_load_timer = threading.Timer(0.25, self._poll_home_load)
        self._home_load_timer.daemon = True
        self._home_load_timer.start()

    def _poll_home_load(self):
        """Finish asynchronous Home loading from a short-lived UI callback."""
        if self._window_closed or not self._home_loading:
            return
        if not self._home_load_done.is_set():
            self._schedule_home_load_poll()
            return

        self._home_loading = False
        self._home_load_thread = None
        self._initial_rows = list(self._home_load_result or [])
        error = self._home_load_error
        self._home_load_result = None
        self._home_load_error = None
        if error:
            xbmc.log(
                "[plugin.video.silo] Silo Cinematic Home load failed: %s" % error,
                xbmc.LOGWARNING,
            )
            self.setProperty("silo.hero.title", "Unable to load Silo")
            self.setProperty("silo.hero.meta", "CONNECTION ERROR")
            self.setProperty("silo.hero.synopsis", str(error)[:500])
        elif not self._initial_rows:
            self.setProperty("silo.hero.title", "No Home content available")
            self.setProperty("silo.hero.meta", "")
            self.setProperty("silo.hero.synopsis", "The server returned no Home rows for this profile.")
        try:
            self._render()
        except Exception as exc:
            self._error(exc)

    def _render(self):
        self._update_profile_properties()
        if not ADDON.getSettingBool("cinematic_interface"):
            self.close()
            return
        if self.screen == "home":
            rows = self._home_rows()
        elif self.screen == "libraries":
            rows = self._libraries_rows()
        elif self.screen == "library":
            rows = self._library_rows()
        elif self.screen == "series":
            rows = self._series_rows()
        elif self.screen == "details":
            rows = self._details_rows()
        elif self.screen == "season":
            rows = self._season_rows()
        elif self.screen == "your_stuff":
            rows = self._your_stuff_rows()
        elif self.screen == "personal":
            rows = self._personal_rows()
        elif self.screen == "collections":
            rows = self._collections_rows()
        elif self.screen == "collection":
            rows = self._collection_rows()
        elif self.screen == "search":
            rows = self._search_rows()
        elif self.screen == "person":
            rows = self._person_rows()
        elif self.screen == "directory":
            rows = self._directory_rows(self.params.get("route_action"), self.params)
        else:
            rows = [("Silo", [])]

        if self.screen == "home":
            visible_row_count = sum(
                1 for row in rows or []
                if row and len(row) > 1 and row[0] and row[1]
            )
            if visible_row_count > self._row_capacity:
                # Home sections can change after a profile switch or refresh.
                # Reopen with an XML file sized to the new server response.
                self.result = "rebuild"
                type(self)._launch_initial_rows = rows
                type(self)._launch_row_capacity = visible_row_count
                self._window_closed = True
                self.close()
                return

        # Keep the same featured information panel on every screen. Home uses
        # horizontal rails; all nested/browse screens use a downward-scrolling grid.
        self._set_hero_visible(True)
        self._layout_navigation()
        self._set_active_nav()
        self.setFocusId(self._active_nav_control())
        if self.screen == "home":
            self._show_rails(rows)
            if self._home_loading:
                self.setProperty("silo.hero.logo", "")
                self.setProperty("silo.hero.backdrop", "")
                self.setProperty("silo.hero.title", "Loading your Silo library…")
                self.setProperty("silo.hero.meta", "CONNECTING TO SILO")
                self.setProperty("silo.hero.synopsis", "Your Home sections are loading. You can continue navigating while the server responds.")
            if self._rows:
                self._row_index = min(max(0, self._row_index), len(self._rows) - 1)
                self._layout_rows()
                control_id = self._focus_control_for_row(self._row_index)
                items = self._row_items.get(control_id) or []
                if items:
                    try:
                        self.getControl(control_id).selectItem(0)
                        # Keep initial focus on the top nav; Down enters the first rail.
                        self.setFocusId(self._active_nav_control())
                        self._update_hero_from_item(items[0])
                    except Exception as exc:
                        xbmc.log("[plugin.video.silo] Silo Cinematic: initial rail focus failed: %s" % exc, xbmc.LOGDEBUG)
                        self.setFocusId(self._active_nav_control())
        else:
            self._show_grid(rows)
            if self._grid_items:
                try:
                    self.getControl(GRID_CONTROL).selectItem(0)
                    self.setFocusId(GRID_CONTROL)
                    self._update_hero_from_focus()
                except Exception as exc:
                    xbmc.log("[plugin.video.silo] Silo Cinematic: initial grid focus failed: %s" % exc, xbmc.LOGDEBUG)

    def _show_rails(self, rows):
        """Dynamic poster and wide-art rails with the active row anchored near the top."""
        normalized = []
        for row in rows or []:
            if not row:
                continue
            title = row[0] if len(row) else ""
            items = row[1] if len(row) > 1 else []
            style = str(row[2] if len(row) > 2 else "poster").lower()
            if title and items:
                normalized.append((str(title), list(items), style if style == "wide" else "poster"))
        self._rows = normalized
        self._row_items = {}
        self._grid_items = []
        self._view_mode = "rails"
        self.setProperty("silo.cinematic.view", "rails")
        # The Theater XML owns grid placement; 500 is its real grid control.
        self.getControl(GRID_CONTROL).setVisible(False)
        self.getControl(GRID_EMPTY).setVisible(False)

        for index in range(self._row_capacity):
            group_id, header_id, icon_id, wide_id, poster_id = slot_ids(index)
            group = self.getControl(group_id)
            header = self.getControl(header_id)
            icon = self.getControl(icon_id)
            wide_list = self.getControl(wide_id)
            poster_list = self.getControl(poster_id)
            if index >= len(self._rows):
                group.setVisible(False)
                wide_list.reset()
                poster_list.reset()
                wide_list.setVisible(False)
                poster_list.setVisible(False)
                continue
            title, items, style = self._rows[index]
            header.setLabel(title)
            header.setPosition(145 if style == "wide" else 88, 0)
            icon.setVisible(style == "wide")
            if style == "wide":
                poster_list.reset()
                poster_list.setVisible(False)
                listing, control_id = wide_list, wide_id
            else:
                wide_list.reset()
                wide_list.setVisible(False)
                listing, control_id = poster_list, poster_id
            listing.reset()
            listing.setVisible(True)
            self._row_items[control_id] = items
            for item in items:
                display_item = dict(item or {})
                display_item["_cinematic_wide"] = style == "wide"
                # Choose the appropriate rail when artwork is consistently landscape.
                # Keep portrait poster rails for movies/series when portrait art exists.
                display_item["_cinematic_art_wide"] = bool(display_item.get("_cinematic_art_wide"))
                listing.addItem(self._make_list_item(display_item))
        self._row_index = min(max(0, self._row_index), max(0, len(self._rows) - 1))
        self._layout_rows()

    def _show_grid(self, rows):
        """Render all nested destinations as a single downward-scrolling poster grid."""
        self._rows = []
        self._row_items = {}
        self._grid_items = []
        self._view_mode = "grid"
        self.setProperty("silo.cinematic.view", "grid")
        self.setProperty("silo.tab", "search" if self.screen == "search" else "browse")
        self.setProperty("silo.people", "")
        for index in range(self._row_capacity):
            try:
                self.getControl(slot_ids(index)[0]).setVisible(False)
            except Exception:
                pass
        for row in rows or []:
            if row:
                self._grid_items.extend(list(row[1] if len(row) > 1 else []))

        labels = {
            "libraries": "YOUR LIBRARIES",
            "library": self.params.get("title") or "LIBRARY",
            "season": self.params.get("title") or "EPISODES",
            "your_stuff": "YOUR STUFF",
            "personal": self.params.get("title") or "YOUR STUFF",
            "collections": "COLLECTIONS",
            "collection": self.params.get("title") or "COLLECTION",
            "search": "SEARCH RESULTS  ·  " + str(self.params.get("query") or ""),
            "person": self.params.get("title") or "PERSON",
        }
        heading = labels.get(
            self.screen,
            next((str(row[0]) for row in rows if row and row[0]), "SILO"),
        )
        self.setProperty("silo.empty", "" if self._grid_items else "No titles found.")
        grid = self.getControl(GRID_CONTROL)
        grid.setVisible(True)
        grid.reset()
        self._row_items[GRID_CONTROL] = self._grid_items
        for item in self._grid_items:
            display_item = dict(item or {})
            display_item["_cinematic_wide"] = False
            grid.addItem(self._make_list_item(display_item))
        try:
            empty = self.getControl(GRID_EMPTY)
            empty.setLabel("" if self._grid_items else "No titles found.")
        except Exception:
            pass
    def _layout_rows(self):
        top = ROWS_TOP
        for index in range(self._row_capacity):
            group = self.getControl(slot_ids(index)[0])
            if index >= len(self._rows) or index < self._row_index or top >= SCREEN_HEIGHT:
                group.setVisible(False)
                continue
            group.setPosition(0, top)
            group.setVisible(True)
            top += WIDE_PITCH if self._rows[index][2] == "wide" else POSTER_PITCH

    def _focus_control_for_row(self, index):
        return slot_ids(index)[3] if self._rows[index][2] == "wide" else slot_ids(index)[4]

    def _row_of(self, control_id):
        for index, row in enumerate(self._rows):
            if control_id in (slot_ids(index)[3], slot_ids(index)[4]):
                return index
        return None

    def _focus_row(self, index):
        if not self._rows:
            return
        self._row_index = min(max(0, int(index)), len(self._rows) - 1)
        self._layout_rows()
        self.setFocusId(self._focus_control_for_row(self._row_index))
        self._schedule_hero_update()

    def _focus_first_content(self):
        if self._view_mode == "grid":
            if self._grid_items:
                self.setFocusId(GRID_CONTROL)
        elif self._rows:
            self._focus_row(0)

    def _focus_snapshot(self):
        try:
            focus_id = self.getFocusId()
            if focus_id == GRID_CONTROL:
                return ("grid", 0, self.getControl(GRID_CONTROL).getSelectedPosition())
            row = self._row_of(focus_id)
            if row is not None:
                return ("rails", row, self.getControl(focus_id).getSelectedPosition())
            return ("nav", focus_id, 0)
        except Exception:
            return ("nav", self._active_nav_control(), 0)

    def _restore_snapshot(self, snapshot):
        try:
            mode, index, position = snapshot.get("focus_snapshot", ("nav", self._active_nav_control(), 0))
            if mode == "grid" and self._view_mode == "grid" and self._grid_items:
                self.getControl(GRID_CONTROL).selectItem(min(position, len(self._grid_items) - 1))
                self.setFocusId(GRID_CONTROL)
            elif mode == "rails" and self._view_mode == "rails" and self._rows:
                index = min(index, len(self._rows) - 1)
                self._row_index = index
                self._layout_rows()
                control_id = self._focus_control_for_row(index)
                items = self._row_items.get(control_id) or []
                if items:
                    self.getControl(control_id).selectItem(min(position, len(items) - 1))
                self.setFocusId(control_id)
            else:
                self.setFocusId(self._active_nav_control())
        except Exception:
            pass

    def _schedule_hero_update(self):
        if self._hero_timer is not None:
            try:
                self._hero_timer.cancel()
            except Exception:
                pass
        self._hero_timer = threading.Timer(0.15, self._update_hero_from_focus)
        self._hero_timer.daemon = True
        self._hero_timer.start()

    def _active_nav_control(self):
        return {
            "home": NAV_HOME, "libraries": NAV_LIBRARIES, "search": NAV_SEARCH,
            "your_stuff": NAV_YOUR_STUFF, "collections": NAV_COLLECTIONS,
        }.get(self._active_nav(), NAV_HOME)

    def _active_nav(self):
        current = self.screen
        if current in ("details", "series", "season", "library"):
            current = self.stack[-1].get("screen") if self.stack else "libraries"
        if current in ("libraries", "library", "series", "season", "person"):
            if current == "person" and self.stack and self.stack[-1].get("screen") == "search":
                return "search"
            return "libraries"
        if current in ("personal", "your_stuff"):
            return "your_stuff"
        if current in ("collections", "collection"):
            return "collections"
        if current == "search":
            return "search"
        return "home"

    def _layout_navigation(self):
        """Use the Theater top bar, with destinations supported by this add-on."""
        labels = {
            NAV_HOME: "Home",
            NAV_LIBRARIES: "Libraries",
            NAV_SEARCH: "",
            NAV_YOUR_STUFF: "Your Stuff",
            NAV_COLLECTIONS: "Collections",
            NAV_SETTINGS: "Settings",
            NAV_PROFILE: "",
            NAV_LOGOUT: "Log Out",
        }
        for control_id, label in labels.items():
            try:
                self.getControl(control_id).setLabel(label)
            except Exception:
                # Search and profile are artwork-only controls in this skin.
                pass
    def _set_active_nav(self):
        """Expose tab state in the property names consumed by Silo-Theater's skin."""
        active = self._active_nav()
        self.setProperty("silo.cinematic.activeTab", active)
        if self.screen == "home":
            tab = "home"
        elif self.screen == "search":
            tab = "search"
        else:
            tab = "browse"
        self.setProperty("silo.tab", tab)
        self.setProperty("silo.query", str(self.params.get("query") or ""))
    def _make_list_item(self, item):
        # The regular Silo directory renderer has already prepared this exact
        # Kodi ListItem (signed artwork URLs, VideoInfoTag metadata, resume
        # state, library IDs and other properties). Reuse it rather than
        # rebuilding a second item and accidentally dropping skin metadata.
        source = item.get("_cinematic_source")
        if source is not None:
            title = str(item.get("title") or item.get("name") or "Untitled")
            action = item.get("_cinematic_action", "")
            payload = item.get("_cinematic_payload", {})
            source.setProperty("Silo.Cinematic.Action", str(action))
            source.setProperty(
                "Silo.Cinematic.Payload",
                json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
            )
            source.setProperty("Silo.Cinematic.Type", str(item.get("type") or "video"))
            source.setProperty("Silo.Cinematic.ArtWide", "true" if item.get("_cinematic_art_wide") else "")
            source.setProperty("Silo.Cinematic.HeroTitle", title)
            source.setProperty("Silo.Cinematic.Overview", str(item.get("overview") or ""))
            source.setProperty("Silo.Cinematic.Details", self._detail_text(item))
            source.setProperty("Silo.Cinematic.Meta", self._hero_meta(item))
            source.setProperty("Silo.Cinematic.Badges", self._hero_badges(item))
            source.setProperty("Silo.Cinematic.Badge", (self._hero_badge_list(item) or [""])[0])
            source.setProperty("Silo.Cinematic.Wide", "true" if item.get("_cinematic_wide") else "")

            # Keep the API-signed artwork from the original item, normalizing
            # the poster/thumb keys used by our horizontal poster templates.
            art = {}
            for key in ("poster", "thumb", "fanart", "clearlogo", "icon", "banner", "landscape"):
                try:
                    value = source.getArt(key)
                except Exception:
                    value = ""
                if value:
                    value = str(value).strip()
                    lower = value.lower()
                    if lower.startswith("script://") or lower in ("plugin://", "plugin:///"):
                        continue
                    art[key] = value
            poster = art.get("poster") or art.get("thumb") or art.get("landscape") or art.get("icon")
            backdrop = art.get("fanart") or art.get("landscape") or art.get("banner") or poster
            if item.get("_cinematic_wide") or item.get("_cinematic_art_wide"):
                poster = backdrop or poster
            if poster:
                art.update({"poster": poster, "thumb": poster, "icon": poster})
            if backdrop:
                art["fanart"] = backdrop
            try:
                source.setArt(art)
            except Exception:
                pass

            # Make Kodi's percent-played infolabel work alongside our property
            # based progress indicator when a resume point is available.
            try:
                tag = source.getVideoInfoTag()
                playcount = int(tag.getPlaycount() or 0)
                state_played = str(source.getProperty("Silo.UserState.Played") or "").lower() == "true"
                watched = playcount > 0 or state_played
                if watched and playcount <= 0:
                    tag.setPlaycount(1)
                source.setProperty("Silo.Cinematic.Watched", "true" if watched else "")
                source.setProperty("Silo.Cinematic.Progress", "")
                resume = tag.getResumePoint()
                position = _positive_number(
                    resume.timeInSeconds() if resume else 0,
                    item.get("position_seconds"),
                    source.getProperty("Silo.PositionSeconds"),
                )
                duration = _positive_number(
                    resume.totalTimeInSeconds() if resume else 0,
                    item.get("duration_seconds"),
                    source.getProperty("Silo.DurationSeconds"),
                    tag.getDuration(),
                )
                if duration > 0 and 0 < position < duration and not watched:
                    tag.setPlaycount(0)
                    tag.setResumePoint(position, duration)
                    source.setProperty("Silo.Cinematic.Progress", str(max(1, min(99, int(position * 100.0 / duration)))))
            except Exception:
                pass
            badge = (self._hero_badge_list(item) or [""])[0]
            source.setProperty("silo.badge", str(badge or ""))
            source.setProperty("silo.watched", source.getProperty("Silo.Cinematic.Watched"))
            source.setProperty("silo.progress", source.getProperty("Silo.Cinematic.Progress"))
            return source

        media_type = _item_type(item)
        wide = bool(item.get("_cinematic_wide"))
        art_wide = bool(item.get("_cinematic_art_wide"))
        raw_title = _display_title(item)
        if wide and media_type == "episode" and _value(item, "series_title", "series_name"):
            title = str(_value(item, "series_title", "series_name"))
            episode_name = raw_title
            code = ""
            try:
                season_number = _value(item, "season_number")
                episode_number = _value(item, "episode_number")
                if season_number is not None and episode_number is not None:
                    code = "S%02dE%02d" % (int(season_number), int(episode_number))
            except (TypeError, ValueError):
                code = ""
            subtitle = " · ".join(part for part in (code, episode_name) if part)
        else:
            title = raw_title
            subtitle = str(_value(item, "year", "subtitle", default=""))
            if media_type == "episode":
                try:
                    subtitle = "S%02dE%02d · %s" % (
                        int(_value(item, "season_number")), int(_value(item, "episode_number")), title
                    )
                    title = str(_value(item, "series_title", "series_name", default=title))
                except (TypeError, ValueError):
                    pass

        list_item = xbmcgui.ListItem(label=title, label2=subtitle)
        poster = self._art(item, "poster_url", "poster", "image", "thumbnail", "still_url", "still")
        backdrop = self._art(item, "backdrop_url", "backdrop", "fanart_url", "still_url", "still")
        logo = self._art(item, "logo_url", "logo")
        if wide or art_wide:
            poster = backdrop or poster
        art = {}
        if poster:
            art.update({"poster": poster, "thumb": poster, "icon": poster})
        if backdrop:
            art["fanart"] = backdrop
        if logo:
            art["clearlogo"] = logo
        if art:
            list_item.setArt(art)

        action = item.get("_cinematic_action", "")
        payload = item.get("_cinematic_payload", {})
        list_item.setProperty("Silo.Cinematic.Action", str(action))
        list_item.setProperty("Silo.Cinematic.Payload", json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
        list_item.setProperty("Silo.Cinematic.Type", media_type)
        list_item.setProperty("Silo.Cinematic.ArtWide", "true" if art_wide else "")
        list_item.setProperty("Silo.Cinematic.HeroTitle", title)
        list_item.setProperty("Silo.Cinematic.Overview", str(_value(item, "overview", "plot", "description")))
        list_item.setProperty("Silo.Cinematic.Details", self._detail_text(item))
        list_item.setProperty("Silo.Cinematic.Meta", self._hero_meta(item))
        list_item.setProperty("Silo.Cinematic.Badges", self._hero_badges(item))
        list_item.setProperty("Silo.Cinematic.Badge", (self._hero_badge_list(item) or [""])[0])
        list_item.setProperty("Silo.Cinematic.Wide", "true" if wide else "")

        user_state = item.get("user_state") if isinstance(item.get("user_state"), dict) else {}
        user_data = item.get("user_data") if isinstance(item.get("user_data"), dict) else {}
        overlay = item.get("overlay_summary") if isinstance(item.get("overlay_summary"), dict) else {}
        state = user_state or user_data
        played = state.get("played") is True or item.get("played") is True
        list_item.setProperty("Silo.UserState.Played", "true" if played else "false")
        list_item.setProperty("Silo.UserState.InWatchlist", "true" if state.get("in_watchlist") is True else "false")
        list_item.setProperty("Silo.UserState.IsFavorite", "true" if state.get("is_favorite") is True else "false")
        list_item.setProperty("Silo.Cinematic.Watched", "true" if played else "")
        list_item.setProperty("Silo.Cinematic.Progress", "")
        position = _positive_number(
            item.get("position_seconds"),
            state.get("position_seconds"),
            state.get("position"),
            user_data.get("position_seconds"),
            user_data.get("position"),
            overlay.get("position_seconds"),
            overlay.get("position"),
        )
        duration = _positive_number(
            item.get("duration_seconds"),
            state.get("duration_seconds"),
            state.get("duration"),
            user_data.get("duration_seconds"),
            user_data.get("duration"),
            overlay.get("duration_seconds"),
            overlay.get("duration"),
            _value(item, "runtime_minutes", "runtime", default=0) * 60,
        )
        if duration <= 0:
            try:
                duration = max(0.0, float(_value(item, "runtime_minutes", "runtime", default=0) or 0) * 60)
            except (TypeError, ValueError):
                duration = 0.0
        try:
            tag = list_item.getVideoInfoTag()
            tag.setTitle(title)
            tag.setMediaType({
                "movie": "movie", "series": "tvshow", "season": "season",
                "episode": "episode", "video": "video",
            }.get(media_type, "video"))
            if duration > 0:
                tag.setDuration(int(round(duration)))
            if played:
                tag.setPlaycount(1)
                list_item.setProperty("Silo.Cinematic.Watched", "true")
            else:
                tag.setPlaycount(0)
                if position > 0 and duration > 0 and position < duration and not played:
                    tag.setResumePoint(position, duration)
                    list_item.setProperty("Silo.Cinematic.Progress", str(max(1, min(99, int(position * 100.0 / duration)))))
        except Exception:
            pass
        badge = (self._hero_badge_list(item) or [""])[0]
        list_item.setProperty("silo.badge", str(badge or ""))
        list_item.setProperty("silo.watched", list_item.getProperty("Silo.Cinematic.Watched"))
        list_item.setProperty("silo.progress", list_item.getProperty("Silo.Cinematic.Progress"))
        return list_item
    def _art(self, item, *keys):
        if not isinstance(item, dict):
            return ""
        for key in keys:
            value = item.get(key)
            if value in (None, ""):
                for container_key in ("artwork", "images", "image_urls", "art"):
                    container = item.get(container_key)
                    if isinstance(container, dict):
                        value = container.get(key)
                        if value not in (None, ""):
                            break
            if isinstance(value, dict):
                value = _value(value, "url", "src", "path", "href")
            if value in (None, ""):
                continue
            value = str(value).strip()
            lower = value.lower()
            # These are Kodi's script fallback placeholders, not artwork URLs.
            # Asking Kodi to load them emits repeated unsupported-protocol errors.
            if lower.startswith("script://") or lower in ("plugin://", "plugin:///"):
                continue
            try:
                value = self.client.abs_url(value)
            except Exception:
                pass
            if value and value.lower() not in ("plugin://", "plugin:///"):
                return value
        return ""
    def _with_action(self, item, action, **payload):
        result = dict(item or {})
        result["_cinematic_action"] = action
        result["_cinematic_payload"] = payload
        return result

    def _menu_item(self, title, action, **payload):
        return self._with_action(
            {"title": title, "type": "menu"},
            action,
            **payload
        )

    def _home_rows(self):
        if self._initial_rows is not None:
            rows = list(self._initial_rows)
        elif callable(self.root_loader):
            rows = self.root_loader(self.client) or []
        else:
            rows = []
        prefs = self._row_preferences
        hidden = set(prefs.get("hidden") or [])
        order = list(prefs.get("order") or [])
        rows = [row for row in rows if row[0] not in hidden]
        by_title = {row[0]: row for row in rows}
        ordered = [by_title[t] for t in order if t in by_title]
        ordered.extend(row for row in rows if row[0] not in order)
        return ordered
    def _directory_rows(self, action, params=None):
        """Use the existing main.py directory functions for content and metadata."""
        if callable(self.directory_loader):
            return self.directory_loader(self.client, action, params or {}) or []
        return [("SILO", [], "poster")]


    def _libraries_rows(self):
        return self._directory_rows("libraries")

    def _library_rows(self):
        return self._directory_rows("library", self.params)

    def _series_rows(self):
        series_id = self.params.get("series_id") or self.params.get("content_id")
        library_id = self.params.get("library_id")
        if not series_id:
            return [("TV SHOW", []), ("", []), ("", [])]

        if not library_id:
            libraries = self.client.libraries()
            for library in libraries:
                candidate = library.get("id")
                if not candidate:
                    continue
                try:
                    result = self.client.seasons(series_id, candidate, suppress_not_found=True)
                except SiloError:
                    continue
                if result:
                    library_id = candidate
                    break
        if not library_id:
            raise SiloError("Could not find the library for this TV show.")

        self.params["library_id"] = library_id
        try:
            detail = self.client.item_detail(series_id, library_id)
            self._set_hero(detail)
        except SiloError:
            detail = {}
        seasons = self.client.seasons(series_id, library_id)
        cards = []
        for season in seasons:
            number = _value(season, "season_number", "number")
            if number is None:
                continue
            title = _display_title(season)
            if title == "Untitled":
                title = "Specials" if season.get("is_specials") else "Season %s" % number
            season_item = dict(season, title=title, type="season")
            cards.append(self._with_action(
                season_item, "season",
                series_id=series_id, season_number=number,
                library_id=library_id, title=title,
            ))
        return [("SEASONS", cards), ("", []), ("", [])]

    def _details_rows(self):
        """Show a selected title's metadata and explicit playback choices."""
        content_id = self.params.get("content_id")
        library_id = self.params.get("library_id")
        if not content_id:
            return [("TITLE DETAILS", []), ("", []), ("", [])]

        detail = {}
        try:
            detail = self.client.item_detail(content_id, library_id) or {}
        except SiloError as exc:
            xbmc.log("[plugin.video.silo] Detail lookup unavailable: %s" % exc, xbmc.LOGDEBUG)

        if detail:
            self._set_hero(detail)
        else:
            self._set_hero({
                "title": self.params.get("title", "Title details"),
                "type": self.params.get("type", "video"),
                "overview": self.params.get("overview", ""),
                "backdrop_url": self.params.get("backdrop_url", ""),
            })

        playable_id = _value(detail, "play_content_id", "content_id", "id", default=content_id)
        play_payload = {
            "content_id": playable_id,
            "library_id": library_id or "",
            "title": _display_title(detail) if detail else self.params.get("title", "Untitled"),
        }
        duration = _value(detail, "duration_seconds", default=self.params.get("duration_seconds", ""))
        if duration not in (None, ""):
            play_payload["duration_seconds"] = duration
        file_id = self.params.get("file_id") or self._single_file_id(detail)
        if file_id:
            play_payload["file_id"] = file_id

        choices = [
            self._menu_item("Play  /  Resume", "play", **play_payload),
            self._menu_item("Start over", "start_over", **dict(play_payload, resume_available=1)),
        ]
        return [("WATCH", choices), ("", []), ("", [])]

    def _season_rows(self):
        if self.params.get("season_number") is not None:
            return self._directory_rows("season", self.params)
        return self._directory_rows("seasons", self.params)

    def _single_file_id(self, item):
        files = item.get("files") or []
        if len(files) == 1:
            return _value(files[0], "id", "file_id")
        return ""

    def _your_stuff_rows(self):
        return self._directory_rows("your_stuff")

    def _personal_rows(self):
        return self._directory_rows("personal_list", self.params)

    def _collections_rows(self):
        return self._directory_rows("collections")

    def _collection_rows(self):
        return self._directory_rows("collection", self.params)

    def _search_rows(self):
        return self._directory_rows("search", self.params)

    def _person_rows(self):
        return self._directory_rows("person", self.params)

    def _item_click(self, action, payload):
        if not action:
            return
        payload = dict(payload or {})
        if action == "login":
            self.client.login_full()
            self.screen, self.params, self.stack = "home", {}, []
            self._render()
        elif action == "refresh_home":
            self.screen, self.params, self.stack = "home", {}, []
            self._render()
        elif action == "settings":
            self._open_settings_and_refresh()
        elif action == "libraries":
            self._push("libraries")
        elif action in ("library", "library_page"):
            self._push("library", payload)
        elif action == "library_letter":
            self._push("directory", dict(payload, route_action="library_letter"))
        elif action in ("home_section", "home_section_group"):
            self._push("directory", dict(payload, route_action=action))
        elif action in ("seasons", "personal_list"):
            self._push("directory", dict(payload, route_action=action))
        elif action == "series":
            self._push("series", payload)
        elif action == "details":
            self._push("details", payload)
        elif action == "season":
            if payload.get("series_id") and payload.get("season_number") is not None:
                self._push("directory", dict(payload, route_action="season"))
            else:
                self._push("season", payload)
        elif action in ("personal", "personal_page"):
            self._push("personal", payload)
        elif action == "collections":
            self._push("collections")
        elif action in ("collection", "collection_page"):
            self._push("collection", payload)
        elif action == "search":
            if payload.get("query"):
                self._push("directory", dict(payload, route_action="search"))
            else:
                query = str(xbmcgui.Dialog().input("Search Silo") or "").strip()
                if query:
                    self._push("search", {"query": query})
        elif action == "person":
            if payload.get("person_id"):
                self._push("directory", dict(payload, route_action="person"))
            else:
                self._push("person", payload)
        elif action == "play":
            self._play(payload)
        elif action == "start_over":
            # Tell the existing playback handler to suppress any saved resume point.
            payload["resume_available"] = 1
            self._play(payload)

    def _play(self, payload):
        params = {"action": "play", "cinematic_return": 1}
        for key in ("content_id", "library_id", "file_id", "duration_seconds", "resume_available"):
            value = payload.get(key)
            if value not in (None, ""):
                params[key] = value
        if not params.get("content_id"):
            self._error("This item does not have a playable Silo content ID.")
            return
        url = self.base_url
        if not url.endswith("/"):
            url += "/"
        url += "?" + urlencode(params)
        self.close()
        xbmc.sleep(100)
        xbmc.executebuiltin("PlayMedia(%s)" % url)

    def _hero_meta(self, item):
        media_type = _item_type(item)
        labels = {"movie": "MOVIE", "series": "TV SERIES", "episode": "EPISODE", "season": "SEASON", "video": "VIDEO"}
        parts = [labels.get(media_type, media_type.replace("_", " ").upper())]
        year = _value(item, "year", "release_year")
        try:
            year_number = int(str(year).strip())
            if 1888 <= year_number <= datetime.datetime.now().year + 1:
                parts.append(str(year_number))
        except (TypeError, ValueError):
            pass
        if media_type == "episode":
            try:
                parts.append("S%02dE%02d" % (int(_value(item, "season_number")), int(_value(item, "episode_number"))))
            except (TypeError, ValueError):
                pass
        try:
            runtime = int(float(_value(item, "runtime_minutes", "runtime", default=0) or 0))
        except (TypeError, ValueError):
            runtime = 0
        if runtime > 0:
            parts.append("%dh" % (runtime // 60) if runtime >= 60 and runtime % 60 == 0 else "%dh %dm" % divmod(runtime, 60) if runtime >= 60 else "%d min" % runtime)
        rating = ""
        ratings = item.get("ratings") if isinstance(item.get("ratings"), list) else []
        for entry in ratings:
            if isinstance(entry, dict) and entry.get("display"):
                rating = str(entry["display"])
                break
        if not rating:
            for key in ("rating_imdb", "rating_tmdb", "rating"):
                val = item.get(key)
                if isinstance(val, (int, float)) and val:
                    rating = ("IMDb " if key == "rating_imdb" else "TMDb " if key == "rating_tmdb" else "") + ("%.1f" % val)
                    break
        if rating:
            parts.append(rating)
        # Keep genres and certification in the details block/badges only.
        # Kodi's optional premiere/first-aired fields may contain placeholder
        # dates for unknown metadata, so don't present them as facts.
        return "   ·   ".join(part for part in parts if part)

    def _detail_text(self, item):
        """Secondary details without repeating genres, ratings or premiere dates."""
        fields = [
            ("Genres", ("genres", "genre")),
            ("Director", ("directors", "director")),
            ("Studio", ("studios", "studio", "network")),
        ]
        parts = []
        seen_values = set()
        for label, keys in fields:
            value = _value(item, *keys)
            if isinstance(value, (list, tuple, set)):
                value = " · ".join(str(v).strip() for v in value if str(v).strip())
            value = str(value or "").strip()
            if not value or value.casefold() in seen_values:
                continue
            seen_values.add(value.casefold())
            parts.append("%s: %s" % (label, value))
        original_title = str(_value(item, "original_title") or "").strip()
        title = _display_title(item)
        if original_title and original_title.casefold() != title.casefold():
            parts.append("Original title: " + original_title)
        return "   ·   ".join(parts)

    def _hero_badge_list(self, item):
        """Compact quality and rating badges, in the same order as Silo TV."""
        summary = item.get("overlay_summary") if isinstance(item.get("overlay_summary"), dict) else {}
        badges = []
        resolution = str(_value(summary, "resolution", default=_value(item, "resolution"))).lower()
        if resolution in ("4k", "uhd") or resolution.startswith("2160"):
            badges.append("4K")
        elif resolution.startswith(("720", "1080")):
            badges.append("HD")
        hdr = str(_value(summary, "hdr", default=_value(item, "hdr"))).strip().lower()
        if hdr and hdr not in ("false", "0", "none", "sdr"):
            badges.append("DOLBY VISION" if "dolby" in hdr or hdr in ("dv", "true_dv") else "HDR")
        audio = str(_value(summary, "audio", default=_value(item, "codec_audio"))).strip()
        if audio:
            badges.append(audio.upper())
        rating = str(_value(item, "content_rating", "official_rating")).strip()
        if rating:
            badges.append(rating.upper())
        result = []
        for badge in badges:
            if badge not in result:
                result.append(badge)
        return result[:4]

    def _hero_badges(self, item):
        return "     ·     ".join(self._hero_badge_list(item))

    def _set_theater_hero_properties(self, title, meta, synopsis, backdrop, logo, badges):
        """Publish the selected title using the property contract of Silo-Theater's XML."""
        self.setProperty("silo.hero.title", str(title or ""))
        self.setProperty("silo.hero.meta", str(meta or ""))
        self.setProperty("silo.hero.synopsis", str(synopsis or "")[:2400])
        self.setProperty("silo.hero.backdrop", str(backdrop or ""))
        self.setProperty("silo.hero.logo", str(logo or ""))
        values = list(badges or [])[:4]
        for index in range(4):
            value = values[index] if index < len(values) else ""
            self.setProperty("silo.hero.badge%d" % (index + 1), str(value or ""))

    def _set_hero(self, item):
        """Publish item metadata to the property-driven Theater skin."""
        if not isinstance(item, dict):
            return
        title = _display_title(item)
        media_type = _item_type(item)
        logo = self._art(item, "logo_url", "logo")
        backdrop = self._art(item, "backdrop_url", "backdrop", "fanart_url", "poster_url", "poster")
        plot = str(_value(item, "overview", "plot", "description", default=""))
        details = self._detail_text(item)
        synopsis = "\\n".join(part for part in (plot, details) if part)
        meta = self._hero_meta(item)
        badge_values = self._hero_badge_list(item)
        badges = self._hero_badges(item)
        self._hero_logo, self._hero_badges_text, self._hero_overview = logo or "", badges, synopsis
        self._hero_badge_values = badge_values
        self._set_theater_hero_properties(title, meta, synopsis, backdrop, logo, badge_values)
        self.setProperty("silo.cinematic.hero.logo", self._hero_logo)
        self.setProperty("silo.cinematic.hero.type", media_type)
        self.setProperty("silo.cinematic.hero.badges", badges)
        self.setProperty("silo.cinematic.hero.details", details)
        self.setProperty("silo.cinematic.hero.title", title)
        self.setProperty("silo.cinematic.hero.meta", meta)
        self.setProperty("silo.cinematic.hero.overview", plot)
        self.setProperty("silo.cinematic.hero.backdrop", backdrop or "")
        self.setProperty("silo.cinematic.hero.poster", self._art(item, "poster_url", "poster", "thumb", "image", "icon"))
    def _set_hero_synopsis(self, text):
        """Set the synopsis consumed by the property-driven home skin."""
        value = str(text or "")[:2400]
        self._hero_overview = value
        self.setProperty("silo.hero.synopsis", value)
    def _set_hero_visible(self, visible):
        """The Theater skin's hero is controlled by the screen layout, not control IDs."""
        self.setProperty("silo.hero.visible", "true" if visible else "")
    def _layout_hero_badges(self, visible, shift=0):
        """Publish badge properties; the skin owns their geometry and visibility."""
        values = self._hero_badge_values if visible else []
        for index in range(4):
            value = values[index] if index < len(values) else ""
            self.setProperty("silo.hero.badge%d" % (index + 1), str(value or ""))
    def _update_hero_from_focus(self):
        """Update the feature area when focus changes within a poster list."""
        try:
            focus_id = self.getFocusId()
            item = None
            if self._view_mode == "grid" and focus_id == GRID_CONTROL:
                pos = self.getControl(GRID_CONTROL).getSelectedPosition()
                if 0 <= pos < len(self._grid_items):
                    item = self._grid_items[pos]
            else:
                row = self._row_of(focus_id)
                if row is not None:
                    pos = self.getControl(focus_id).getSelectedPosition()
                    items = self._row_items.get(focus_id, [])
                    if 0 <= pos < len(items):
                        item = items[pos]
            if item:
                self._update_hero_from_item(item)
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Silo Cinematic: focus hero update failed: %s" % exc, xbmc.LOGDEBUG)

    def _update_hero_from_item(self, item):
        """Update the Theater property contract from one Kodi directory card."""
        try:
            title = str(item.get("title") or item.get("name") or "Silo")
            meta = self._hero_meta(item)
            overview = str(_value(item, "overview", "plot", "description", default="") or "")
            details = self._detail_text(item)
            synopsis = "\\n".join(part for part in (overview, details) if part)
            poster = self._art(item, "poster_url", "poster", "thumb", "image", "icon")
            backdrop = self._art(item, "backdrop_url", "backdrop", "fanart", "poster_url", "poster")
            logo = self._art(item, "logo_url", "logo", "clearlogo")
            badge_values = self._hero_badge_list(item)
            badges = self._hero_badges(item)
            self._hero_logo, self._hero_badges_text, self._hero_overview = logo or "", badges, synopsis
            self._hero_badge_values = badge_values
            self._set_theater_hero_properties(
                title, meta, synopsis, backdrop or poster, logo, badge_values
            )
            self.setProperty("silo.cinematic.hero.title", title)
            self.setProperty("silo.cinematic.hero.meta", meta)
            self.setProperty("silo.cinematic.hero.overview", overview)
            self.setProperty("silo.cinematic.hero.details", details)
            self.setProperty("silo.cinematic.hero.backdrop", backdrop or poster or "")
            self.setProperty("silo.cinematic.hero.poster", poster or "")
            self.setProperty("silo.cinematic.hero.logo", logo or "")
            self.setProperty("silo.cinematic.hero.badges", badges)
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Silo Cinematic: hero artwork update failed: %s" % exc, xbmc.LOGDEBUG)