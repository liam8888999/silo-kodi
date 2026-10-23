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


ADDON = xbmcaddon.Addon()
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
ROWS_TOP = 455
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


class CinematicWindow(xbmcgui.WindowXMLDialog):
    """A standalone remote-friendly UI using Kodi's standard XML-first constructor."""

    # Kodi's native WindowXMLDialog validates the XML filename during object
    # construction, before this class's __init__ runs. Keep app dependencies
    # separate from those constructor arguments.
    _launch_client = None
    _launch_base_url = "plugin://plugin.video.silo/"

    @classmethod
    def configure(cls, client, base_url):
        cls._launch_client = client
        cls._launch_base_url = str(base_url or "plugin://plugin.video.silo/")

    def __init__(self, xml_filename, script_path, default_skin="Default", default_res="1080i"):
        xbmcgui.WindowXMLDialog.__init__(
            self, str(xml_filename), str(script_path), str(default_skin), str(default_res)
        )
        self.client = type(self)._launch_client
        self.base_url = str(type(self)._launch_base_url or "plugin://plugin.video.silo/")
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
                NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT, NAV_WATCH_PARTY,
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
            query = str(xbmcgui.Dialog().input("Search Silo") or "").strip()
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
        else:
            rows = [("Silo", [])]

        self._set_active_nav()
        if self.screen in ("home", "series", "details"):
            self._show_rails(rows)
            if self._rows:
                self._row_index = min(max(0, self._row_index), len(self._rows) - 1)
                self._layout_rows()
                self.setFocusId(self._focus_control_for_row(self._row_index))
                if self.screen not in ("details", "series"):
                    self._update_hero_from_focus()
            else:
                self.setFocusId(self._active_nav_control())
        else:
            self._show_grid(rows)
            if self._grid_items:
                self.setFocusId(GRID_CONTROL)
                self._update_hero_from_focus()
            else:
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
        value = _value(item, *keys)
        if isinstance(value, dict):
            value = _value(value, "url", "src", "path")
        if not value:
            return ""
        try:
            return self.client.abs_url(str(value))
        except Exception:
            return str(value)

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
        if not self.client.cfg.get("token"):
            return [("WELCOME TO SILO", [
                self._menu_item("Sign in to Silo", "login"),
                self._menu_item("Open settings", "settings"),
            ], "poster")]

        try:
            sections = self.client.home_sections(image_size="medium")
        except SiloError:
            raise
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Home sections unavailable: %s" % exc, xbmc.LOGWARNING)
            sections = []

        rows = []
        # Keep the server's home ordering intact. Continue Watching / Next Up
        # use landscape art, while other sections use poster cards.
        for section in sections:
            section_id = _value(section, "id", "section_id")
            title = str(_value(section, "title", "name", default="Silo"))
            items = []
            if section_id:
                try:
                    response = self.client.home_section_catalog_items(
                        section_id, image_size="medium", limit=200
                    ) or {}
                except SiloError as exc:
                    xbmc.log("[plugin.video.silo] Home section %s unavailable: %s" % (section_id, exc), xbmc.LOGDEBUG)
                    response = {}
                items = response.get("items") or response.get("results") or []
            if not items:
                items = section.get("items") or []
            cards = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                media_type = _item_type(item)
                content_id = _content_id(item)
                if not content_id:
                    continue
                action = "details" if media_type in ("movie", "episode", "video") else "series" if media_type == "series" else "season" if media_type == "season" else ""
                if not action:
                    continue
                payload = {
                    "content_id": content_id,
                    "library_id": _value(item, "library_id", "source_library_id", default=""),
                    "title": _display_title(item),
                    "type": media_type,
                    "overview": _value(item, "overview", "plot", default=""),
                    "backdrop_url": _value(item, "backdrop_url", "backdrop", default=""),
                }
                if action == "season":
                    payload.update({"series_id": _value(item, "series_id"), "season_number": _value(item, "season_number")})
                cards.append(self._with_action(item, action, **payload))
            if not cards:
                continue
            section_type = str(_value(section, "section_type", "type", default="")).lower()
            title_key = title.lower()
            wide = section_type in ("continue_watching", "in_progress", "next_up") or "continue watching" in title_key or title_key == "next up"
            rows.append((title, cards, "wide" if wide else "poster"))
            if len(rows) >= MAX_ROWS:
                break
        if not rows:
            rows.append(("SILO", [self._menu_item("Browse libraries", "libraries")], "poster"))
        return rows

    def _libraries_rows(self):
        items = self.client.libraries()
        cards = []
        for library in items:
            library_id = _value(library, "id")
            if library_id is None:
                continue
            item = dict(library)
            item.setdefault("title", _display_title(library))
            cards.append(self._with_action(
                item, "library",
                library_id=library_id,
                title=_display_title(item),
                cursor="",
            ))
        return [("YOUR LIBRARIES", cards), ("", []), ("", [])]

    def _library_rows(self):
        library_id = self.params.get("library_id")
        if not library_id:
            return [("LIBRARY", []) , ("", []), ("", [])]
        items, next_cursor = self.client.catalog_page(
            library_id, cursor=self.params.get("cursor") or None, limit=200
        )
        cards = []
        for item in items:
            media_type = _item_type(item)
            content_id = _content_id(item)
            if not content_id:
                continue
            if media_type in ("movie", "episode", "video"):
                action = "details"
            elif media_type == "series":
                action = "series"
            elif media_type == "season":
                action = "season"
            else:
                continue
            payload = {
                "content_id": (_value(item, "play_content_id", "content_id", "id") if action == "play" else content_id),
                "library_id": library_id,
                "title": _display_title(item),
            }
            if action == "season":
                payload.update({
                    "series_id": _value(item, "series_id"),
                    "season_number": _value(item, "season_number"),
                })
            cards.append(self._with_action(item, action, **payload))
        if next_cursor:
            cards.append(self._menu_item("Next page  ›", "library_page",
                                         library_id=library_id, cursor=next_cursor,
                                         title=self.params.get("title", "Library")))
        return [(self.params.get("title") or "LIBRARY", cards), ("", []), ("", [])]

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
        series_id = self.params.get("series_id")
        season_number = self.params.get("season_number")
        library_id = self.params.get("library_id")
        episodes = self.client.episodes(series_id, season_number, library_id)
        cards = []
        for item in episodes:
            content_id = _content_id(item)
            if not content_id:
                continue
            title = _display_title(item)
            try:
                number = int(item.get("episode_number"))
                title = "%02d  %s" % (number, title)
            except (TypeError, ValueError):
                pass
            episode = dict(item, title=title)
            cards.append(self._with_action(
                episode, "details",
                content_id=content_id,
                library_id=library_id,
                title=_display_title(item),
                duration_seconds=_value(item, "duration_seconds", default=""),
                file_id=self._single_file_id(item),
            ))
        title = "EPISODES  ·  " + str(self.params.get("title")) if self.params.get("title") else "EPISODES"
        return [(title, cards), ("", []), ("", [])]

    def _single_file_id(self, item):
        files = item.get("files") or []
        if len(files) == 1:
            return _value(files[0], "id", "file_id")
        return ""

    def _your_stuff_rows(self):
        return [("YOUR STUFF", [
            self._menu_item("Watchlist", "personal", source="watchlist", title="WATCHLIST"),
            self._menu_item("Favorites", "personal", source="favorites", title="FAVORITES"),
            self._menu_item("History", "personal", source="history", title="HISTORY"),
        ]), ("", []), ("", [])]

    def _personal_rows(self):
        items, next_cursor = self.client.personal_catalog_page(
            self.params.get("source"), cursor=self.params.get("cursor") or None, limit=200
        )
        cards = []
        for item in items:
            media_type = _item_type(item)
            content_id = _content_id(item)
            if not content_id:
                continue
            action = "series" if media_type == "series" else "details" if _playable(item) else ""
            if not action:
                continue
            cards.append(self._with_action(
                item, action,
                content_id=(_value(item, "play_content_id", "content_id", "id") if action == "play" else content_id), library_id=_value(item, "library_id", default=""),
                title=_display_title(item),
            ))
        if next_cursor:
            cards.append(self._menu_item("Next page  ›", "personal_page",
                source=self.params.get("source"), cursor=next_cursor, title=self.params.get("title")))
        return [(self.params.get("title") or "YOUR STUFF", cards), ("", []), ("", [])]

    def _collections_rows(self):
        collections, groups = self.client.collections()
        cards = []
        # Groups are shown first only when they are independently navigable.
        for collection in collections or []:
            collection_id = _value(collection, "id", "collection_id")
            if not collection_id:
                continue
            title = _display_title(collection)
            cards.append(self._with_action(
                dict(collection, title=title), "collection",
                collection_id=collection_id, title=title, cursor="",
            ))
        if not cards and groups:
            for group in groups:
                name = _display_title(group)
                cards.append(dict(group, title=name))
        return [("COLLECTIONS", cards), ("", []), ("", [])]

    def _collection_rows(self):
        items, next_cursor = self.client.personal_catalog_page(
            "user_collection", collection_id=self.params.get("collection_id"),
            cursor=self.params.get("cursor") or None, limit=200
        )
        cards = []
        for item in items:
            media_type = _item_type(item)
            content_id = _content_id(item)
            if not content_id:
                continue
            action = "series" if media_type == "series" else "details" if _playable(item) else ""
            if action:
                cards.append(self._with_action(
                    item, action, content_id=(_value(item, "play_content_id", "content_id", "id") if action == "play" else content_id),
                    library_id=_value(item, "library_id", default=""),
                    title=_display_title(item),
                ))
        if next_cursor:
            cards.append(self._menu_item("Next page  ›", "collection_page",
                collection_id=self.params.get("collection_id"), cursor=next_cursor,
                title=self.params.get("title")))
        return [(self.params.get("title") or "COLLECTION", cards), ("", []), ("", [])]

    def _search_rows(self):
        query = self.params.get("query", "")
        data = self.client.search_catalog(query, limit=100, offset=0) or {}
        media = data.get("items") or []
        cards = []
        for item in media:
            media_type = _item_type(item)
            content_id = _content_id(item)
            if not content_id:
                continue
            action = "series" if media_type == "series" else "details" if _playable(item) else ""
            if action:
                cards.append(self._with_action(item, action, content_id=content_id,
                    library_id=_value(item, "library_id", default=""), title=_display_title(item)))
        people = []
        try:
            for person in self.client.search_people(query, limit=20):
                person_id = _value(person, "id", "person_id")
                if person_id:
                    person_copy = dict(person, title="[Person] " + _display_title(person), type="person")
                    people.append(self._with_action(person_copy, "person", person_id=person_id, title=_display_title(person)))
        except SiloError as exc:
            xbmc.log("[plugin.video.silo] People search unavailable: %s" % exc, xbmc.LOGDEBUG)
        return [("SEARCH RESULTS  ·  " + str(query), people + cards, "poster")]

    def _person_rows(self):
        items, next_cursor = self.client.person_catalog_page(
            self.params.get("person_id"), cursor=self.params.get("cursor") or None, limit=200
        )
        cards = []
        for item in items:
            media_type = _item_type(item)
            content_id = _content_id(item)
            if not content_id:
                continue
            action = "series" if media_type == "series" else "details" if _playable(item) else ""
            if action:
                cards.append(self._with_action(item, action,
                    content_id=(_value(item, "play_content_id", "content_id", "id") if action == "play" else content_id), library_id=_value(item, "library_id", default=""),
                    title=_display_title(item)))
        if next_cursor:
            cards.append(self._menu_item("Next page  ›", "person_page",
                person_id=self.params.get("person_id"), cursor=next_cursor,
                title=self.params.get("title")))
        return [(self.params.get("title") or "PERSON", cards), ("", []), ("", [])]

    def _item_click(self, action, payload):
        if not action:
            return
        if action == "login":
            self.client.login_full()
            self.screen, self.params, self.stack = "home", {}, []
            self._render()
        elif action == "settings":
            ADDON.openSettings()
            self._render()
        elif action == "libraries":
            self._push("libraries")
        elif action == "library":
            self._push("library", payload)
        elif action == "library_page":
            self._push("library", payload)
        elif action == "series":
            self._push("series", payload)
        elif action == "details":
            self._push("details", payload)
        elif action == "season":
            self._push("season", payload)
        elif action == "personal":
            self._push("personal", payload)
        elif action == "personal_page":
            self._push("personal", payload)
        elif action == "collections":
            self._push("collections")
        elif action == "collection":
            self._push("collection", payload)
        elif action == "collection_page":
            self._push("collection", payload)
        elif action == "search":
            query = str(xbmcgui.Dialog().input("Search Silo") or "").strip()
            if query:
                self._push("search", {"query": query})
        elif action == "person":
            self._push("person", payload)
        elif action == "person_page":
            self._push("person", payload)
        elif action in ("play", "start_over"):
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
        if self.screen in ("details", "series"):
            return
        try:
            focus_id = self.getFocusId()
            if self._view_mode == "grid" and focus_id == GRID_CONTROL:
                item = self.getControl(GRID_CONTROL).getSelectedItem()
            else:
                row = self._row_of(focus_id)
                item = self.getControl(focus_id).getSelectedItem() if row is not None else None
            if item is None:
                return
            title = item.getProperty("Silo.Cinematic.HeroTitle") or item.getLabel()
            logo = item.getArt("clearlogo") or ""
            backdrop = item.getArt("fanart") or item.getArt("poster") or item.getArt("thumb") or ""
            self.setProperty("silo.cinematic.hero.logo", logo)
            self.setProperty("silo.cinematic.hero.type", item.getProperty("Silo.Cinematic.Type"))
            self.setProperty("silo.cinematic.hero.badges", item.getProperty("Silo.Cinematic.Badges"))
            self.getControl(HERO_TITLE).setLabel(title)
            self.getControl(HERO_META).setLabel(item.getProperty("Silo.Cinematic.Meta"))
            self.getControl(HERO_BADGES).setLabel(item.getProperty("Silo.Cinematic.Badges"))
            self.getControl(HERO_PLOT).setText(item.getProperty("Silo.Cinematic.Overview")[:500])
            self.getControl(100).setImage(backdrop)
            self.getControl(HERO_LOGO).setImage(logo)
        except Exception:
            pass

