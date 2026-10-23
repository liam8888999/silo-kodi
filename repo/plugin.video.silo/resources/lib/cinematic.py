"""Optional cinematic UI for the Silo Kodi add-on.

This module owns presentation and navigation only. Existing SiloClient and the
main playback route remain authoritative for authentication, versions, resume
positions and playback progress.
"""
import json
import threading
from urllib.parse import urlencode

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.silo import SiloError


ADDON = xbmcaddon.Addon("plugin.video.silo")
WINDOW_XML = "SiloCinematic.xml"

# Static controls defined by resources/skins/Default/1080i/SiloCinematic.xml.
NAV_HOME = 110
NAV_LIBRARIES = 111
NAV_SEARCH = 112
NAV_YOUR_STUFF = 113
NAV_COLLECTIONS = 114
NAV_SETTINGS = 115
NAV_PROFILE = 116
NAV_LOGOUT = 117
HERO_TITLE = 130
HERO_META = 131
HERO_PLOT = 132
HERO_LOGO = 133
HERO_BADGES = 134
MAX_ROWS = 12
SLOT_BASE = 3000
ROWS_TOP = 530
WIDE_PITCH = 170
POSTER_PITCH = 230
SCREEN_HEIGHT = 1080
GRID_HEADING = 800
GRID_CONTROL = 801
GRID_EMPTY = 802


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


class CinematicWindow(xbmcgui.WindowXML):
    """A standalone remote-friendly UI using Kodi's standard XML-first constructor."""

    # Kodi's native WindowXML validates the XML filename during object
    # construction, before this class's __init__ runs. Keep app dependencies
    # separate from those constructor arguments.
    _launch_client = None
    _launch_base_url = "plugin://plugin.video.silo/"
    _launch_root_loader = None
    _launch_directory_loader = None

    @classmethod
    def configure(cls, client, base_url, root_loader=None, directory_loader=None):
        cls._launch_client = client
        cls._launch_base_url = str(base_url or "plugin://plugin.video.silo/")
        cls._launch_root_loader = root_loader
        cls._launch_directory_loader = directory_loader

    def __init__(self, xml_filename, script_path, default_skin="Default", default_res="1080i"):
        xbmcgui.WindowXML.__init__(
            self, str(xml_filename), str(script_path), str(default_skin), str(default_res)
        )
        self.client = type(self)._launch_client
        self.base_url = str(type(self)._launch_base_url or "plugin://plugin.video.silo/")
        self.root_loader = type(self)._launch_root_loader
        self.directory_loader = type(self)._launch_directory_loader
        self.addon_path = str(script_path or ADDON.getAddonInfo("path"))
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

    def onInit(self):
        # Honour the same settings-first login path used by the standard root.
        try:
            if (
                not self.client.cfg.get("token")
                and self.client.base
                and self.client.cfg.get("username")
            ):
                self.client.login()
                if not self.client.cfg.get("profile_id"):
                    self.client.select_profile()
            self._render()
        except Exception as exc:
            self._error(exc)

    def onAction(self, action):
        action_id = action.getId()
        focus_id = self.getFocusId()
        if action_id in (9, 10, 92, 216, 247, 257, 61448):
            if self.stack:
                snapshot = self.stack.pop()
                self.screen = snapshot.get("screen", "home")
                self.params = dict(snapshot.get("params") or {})
                self._row_index = int(snapshot.get("row_index") or 0)
                self._render()
                self._restore_snapshot(snapshot)
            else:
                self.close()
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
                if action_id == 3 and row > 0:
                    self._focus_row(row - 1)
                    return
                if action_id == 4 and row + 1 < len(self._rows):
                    self._focus_row(row + 1)
                    return

        nav_ids = (
            NAV_HOME, NAV_LIBRARIES, NAV_SEARCH, NAV_YOUR_STUFF,
            NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT,
        )
        if focus_id in nav_ids and action_id == 4:
            self._focus_first_content()
            return
        if action_id in (1, 2, 3, 4, 5, 6, 7, 8):
            self._schedule_hero_update()

    def onClick(self, control_id):
        try:
            if control_id in (
                NAV_HOME, NAV_LIBRARIES, NAV_SEARCH, NAV_YOUR_STUFF,
                NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT,
            ):
                self._nav_click(control_id)
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
        try:
            xbmcgui.Dialog().notification("Silo", message[:180], xbmcgui.NOTIFICATION_ERROR, 4500)
        except Exception:
            pass
        try:
            if hasattr(self, "getControl"):
                self.getControl(HERO_TITLE).setLabel("Something went wrong")
                self.getControl(HERO_PLOT).setText(message[:240])
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

    def _nav_click(self, control_id):
        if control_id == NAV_HOME:
            self.stack = []
            self.screen, self.params, self._row_index = "home", {}, 0
            self._render()
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
            ADDON.openSettings()
            if not ADDON.getSettingBool("cinematic_interface"):
                self.close()
            else:
                self._render()
        elif control_id == NAV_PROFILE:
            self.client.select_profile()
            self._render()
        elif control_id == NAV_LOGOUT:
            if xbmcgui.Dialog().yesno("Silo", "Log out of this Silo account?"):
                self.client.logout()
                self.screen, self.params, self.stack = "home", {}, []
                self._render()

    def _render(self):
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

        self._set_active_nav()
        # Start on an always-visible control. Poster rails can be hidden or
        # empty during the first XML layout, so never force focus prematurely.
        self.setFocusId(self._active_nav_control())
        if self.screen in ("home", "series", "details"):
            self._show_rails(rows)
            if self._rows:
                self._row_index = min(max(0, self._row_index), len(self._rows) - 1)
                self._layout_rows()
                control_id = self._focus_control_for_row(self._row_index)
                items = self._row_items.get(control_id) or []
                if items:
                    try:
                        self.getControl(control_id).selectItem(0)
                        if self.screen != "home":
                            self.setFocusId(control_id)
                        else:
                            # Keep initial focus on the persistent top nav;
                            # move into the first poster rail on D-pad Down.
                            self.setFocusId(self._active_nav_control())
                        self._update_hero_from_item(self._row_items[control_id][0])
                    except Exception as exc:
                        xbmc.log("[plugin.video.silo] Silo Cinematic: initial rail focus failed: %s" % exc, xbmc.LOGDEBUG)
                        self.setFocusId(self._active_nav_control())
                if self.screen in ("details", "series"):
                    self._update_hero_from_focus()
        else:
            self._show_grid(rows)
            if self._grid_items:
                try:
                    self.getControl(GRID_CONTROL).selectItem(0)
                    self.setFocusId(GRID_CONTROL)
                    self._update_hero_from_focus()
                except Exception:
                    self.setFocusId(self._active_nav_control())

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
        self._rows = normalized[:MAX_ROWS]
        self._row_items = {}
        self._grid_items = []
        self._view_mode = "rails"
        self.setProperty("silo.cinematic.view", "rails")
        self.getControl(GRID_HEADING).setVisible(False)
        self.getControl(GRID_CONTROL).setVisible(False)
        self.getControl(GRID_EMPTY).setVisible(False)

        for index in range(MAX_ROWS):
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
                listing.addItem(self._make_list_item(display_item))
        self._row_index = min(max(0, self._row_index), max(0, len(self._rows) - 1))
        self._layout_rows()

    def _show_grid(self, rows):
        """Poster grid used by library, collection, personal-list and search screens."""
        self._rows = []
        self._row_items = {}
        self._grid_items = []
        self._view_mode = "grid"
        self.setProperty("silo.cinematic.view", "grid")
        self.getControl(GRID_HEADING).setVisible(True)
        self.getControl(GRID_CONTROL).setVisible(True)
        for index in range(MAX_ROWS):
            self.getControl(slot_ids(index)[0]).setVisible(False)
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
        heading = labels.get(self.screen, next((str(row[0]) for row in rows if row and row[0]), "SILO"))
        self.getControl(GRID_HEADING).setLabel(heading)
        grid = self.getControl(GRID_CONTROL)
        grid.reset()
        self._row_items[GRID_CONTROL] = self._grid_items
        for item in self._grid_items:
            grid.addItem(self._make_list_item(item))
        self.getControl(GRID_EMPTY).setVisible(not self._grid_items)

    def _layout_rows(self):
        top = ROWS_TOP
        for index in range(MAX_ROWS):
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

    def _set_active_nav(self):
        self.setProperty("silo.cinematic.activeTab", self._active_nav())

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
            source.setProperty("Silo.Cinematic.HeroTitle", title)
            source.setProperty("Silo.Cinematic.Overview", str(item.get("overview") or ""))
            source.setProperty("Silo.Cinematic.Meta", self._hero_meta(item))
            source.setProperty("Silo.Cinematic.Badges", self._hero_badges(item))
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
                    art[key] = value
            poster = art.get("poster") or art.get("thumb") or art.get("landscape") or art.get("icon")
            backdrop = art.get("fanart") or art.get("landscape") or poster
            if item.get("_cinematic_wide"):
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
                resume = tag.getResumePoint()
                position = float(resume.timeInSeconds()) if resume else 0.0
                duration = float(resume.totalTimeInSeconds()) if resume else 0.0
                if duration > 0 and position > 0 and playcount == 0:
                    source.setProperty("Silo.Cinematic.Progress", str(int(position * 100.0 / duration)))
            except Exception:
                pass
            return source

        media_type = _item_type(item)
        wide = bool(item.get("_cinematic_wide"))
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
        if wide:
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
        list_item.setProperty("Silo.Cinematic.HeroTitle", title)
        list_item.setProperty("Silo.Cinematic.Overview", str(_value(item, "overview", "plot", "description")))
        list_item.setProperty("Silo.Cinematic.Meta", self._hero_meta(item))
        list_item.setProperty("Silo.Cinematic.Badges", self._hero_badges(item))
        list_item.setProperty("Silo.Cinematic.Wide", "true" if wide else "")

        user_state = item.get("user_state") if isinstance(item.get("user_state"), dict) else {}
        user_data = item.get("user_data") if isinstance(item.get("user_data"), dict) else {}
        state = user_state or user_data
        played = bool(state.get("played"))
        position = _value(item, "position_seconds", default=state.get("position_seconds", 0))
        duration = _value(item, "duration_seconds", default=state.get("duration_seconds", 0))
        try:
            position = max(0.0, float(position or 0))
        except (TypeError, ValueError):
            position = 0.0
        try:
            duration = max(0.0, float(duration or 0))
        except (TypeError, ValueError):
            duration = 0.0
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
                if position > 0 and duration > 0 and position < duration:
                    tag.setResumePoint(position, duration)
                    list_item.setProperty("Silo.Cinematic.Progress", str(int(position * 100.0 / duration)))
        except Exception:
            pass
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
            if value not in (None, ""):
                value = str(value)
                try:
                    return self.client.abs_url(value)
                except Exception:
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
        """Load Silo's home rails, with a useful fallback when the server has none."""
        if callable(self.root_loader):
            rows = self.root_loader(self.client) or []
            # An empty home response should never leave the user staring at a
            # hero backdrop with no content or way to continue navigating.
            if rows:
                return rows
        return [("GET STARTED", [
            self._menu_item("Browse libraries", "libraries"),
            self._menu_item("Search Silo", "search"),
            self._menu_item("Your stuff", "your_stuff"),
            self._menu_item("Collections", "collections"),
            self._menu_item("Refresh home", "refresh_home"),
            self._menu_item("Settings", "settings"),
        ], "poster")]

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
            ADDON.openSettings()
            self._render()
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
            payload["resume_available"] = 0
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
        if year:
            parts.append(str(year))
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
        return "   ·   ".join(part for part in parts if part)

    def _hero_badges(self, item):
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
        return "     ·     ".join(dict.fromkeys(badges))

    def _set_hero(self, item):
        if not isinstance(item, dict):
            return
        title = _display_title(item)
        media_type = _item_type(item)
        logo = self._art(item, "logo_url", "logo")
        backdrop = self._art(item, "backdrop_url", "backdrop", "fanart_url", "poster_url", "poster")
        plot = str(_value(item, "overview", "plot", "description", default=""))
        meta = self._hero_meta(item)
        badges = self._hero_badges(item)
        self.setProperty("silo.cinematic.hero.logo", logo or "")
        self.setProperty("silo.cinematic.hero.type", media_type)
        self.setProperty("silo.cinematic.hero.badges", badges)
        try:
            self.getControl(HERO_TITLE).setLabel(title)
            self.getControl(HERO_META).setLabel(meta)
            self.getControl(HERO_BADGES).setLabel(badges)
            self.getControl(HERO_PLOT).setText(plot[:500])
            self.getControl(100).setImage(backdrop or "")
            self.getControl(HERO_LOGO).setImage(logo or "")
        except Exception:
            pass

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
        """Update the hero area from one converted main.py directory card."""
        try:
            title = str(item.get("title") or item.get("name") or "Silo")
            meta = self._hero_meta(item)
            overview = str(_value(item, "overview", "plot", "description", default="") or "")
            poster = self._art(item, "poster_url", "poster", "thumb", "image", "icon")
            backdrop = self._art(item, "backdrop_url", "backdrop", "fanart", "poster_url", "poster")
            logo = self._art(item, "logo_url", "logo", "clearlogo")
            self.getControl(HERO_TITLE).setLabel(title)
            self.getControl(HERO_META).setLabel(meta)
            self.getControl(HERO_PLOT).setText(overview[:500])
            self.getControl(HERO_LOGO).setImage(logo or "")
            self.getControl(100).setImage(backdrop or poster or "")
            self.setProperty("silo.cinematic.hero.title", title)
            self.setProperty("silo.cinematic.hero.meta", meta)
            self.setProperty("silo.cinematic.hero.overview", overview)
            self.setProperty("silo.cinematic.hero.backdrop", backdrop or poster or "")
            self.setProperty("silo.cinematic.hero.poster", poster or "")
            self.setProperty("silo.cinematic.hero.logo", logo or "")
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Silo Cinematic: hero artwork update failed: %s" % exc, xbmc.LOGDEBUG)
