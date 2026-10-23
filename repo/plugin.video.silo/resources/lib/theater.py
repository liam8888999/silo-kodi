"""Optional cinematic UI for the Silo Kodi add-on.

This module owns presentation and navigation only. Existing SiloClient and the
main playback route remain authoritative for authentication, versions, resume
positions and playback progress.
"""
import json
from urllib.parse import urlencode

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.silo import SiloError


ADDON = xbmcaddon.Addon()
WINDOW_XML = "SiloTheater.xml"

# Static controls defined by resources/skins/Default/1080i/SiloTheater.xml.
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
ROW_LABELS = (200, 210, 220)
ROW_CONTROLS = (201, 211, 221)


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


class TheaterWindow(xbmcgui.WindowXMLDialog):
    """A standalone remote-friendly theater UI with a reversible settings toggle."""

    def __init__(self, client, base_url, addon_path=None):
        self.client = client
        self.base_url = str(base_url or "plugin://plugin.video.silo/")
        self.addon_path = addon_path or ADDON.getAddonInfo("path")
        self.screen = "home"
        self.params = {}
        self.stack = []
        self._row_items = {}
        self._last_error = ""
        super(TheaterWindow, self).__init__(
            WINDOW_XML, self.addon_path, "Default", "1080i"
        )

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
        # Back, Previous Menu, and remote Stop should unwind the UI stack first.
        if action_id in (9, 10, 92, 216, 247, 257, 61448):
            if self.stack:
                self.screen, self.params = self.stack.pop()
                self._render()
            else:
                self.close()
            return

        # Keep the title and backdrop in sync with the currently focused poster.
        if action_id in (1, 2, 3, 4, 5, 6, 7, 8):
            self._update_hero_from_focus()

    def onClick(self, control_id):
        try:
            if control_id in (NAV_HOME, NAV_LIBRARIES, NAV_SEARCH, NAV_YOUR_STUFF,
                              NAV_COLLECTIONS, NAV_SETTINGS, NAV_PROFILE, NAV_LOGOUT):
                self._nav_click(control_id)
                return
            if control_id in ROW_CONTROLS:
                selected = self.getControl(control_id).getSelectedItem()
                if selected is not None:
                    action = selected.getProperty("Silo.Theater.Action")
                    raw_payload = selected.getProperty("Silo.Theater.Payload")
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
        xbmc.log("[plugin.video.silo] Theater UI: %s" % message, xbmc.LOGWARNING)
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
        self.stack.append((self.screen, dict(self.params)))
        self.screen = screen
        self.params = dict(params or {})
        self._render()

    def _nav_click(self, control_id):
        if control_id == NAV_HOME:
            self.stack = []
            self.screen, self.params = "home", {}
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
            # The window setting can be toggled while this dialog is open.
            if not ADDON.getSettingBool("theater_interface"):
                self.close()
            else:
                self._render()
        elif control_id == NAV_PROFILE:
            self.client.select_profile()
            self._render()
        elif control_id == NAV_LOGOUT:
            answer = xbmcgui.Dialog().yesno("Silo", "Log out of this Silo account?")
            if answer:
                self.client.logout()
                self.screen, self.params, self.stack = "home", {}, []
                self._render()

    def _render(self):
        if not ADDON.getSettingBool("theater_interface"):
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

        self._show_rows(rows)
        self._update_hero_from_focus()

        if not any(items for _title, items in rows):
            try:
                self.setFocusId(NAV_HOME)
            except Exception:
                pass
        else:
            try:
                self.setFocusId(ROW_CONTROLS[0])
            except Exception:
                pass

    def _show_rows(self, rows):
        self._row_items = {}
        for index, control_id in enumerate(ROW_CONTROLS):
            label_id = ROW_LABELS[index]
            row_title, items = rows[index] if index < len(rows) else ("", [])
            label = self.getControl(label_id)
            listing = self.getControl(control_id)
            label.setLabel(str(row_title or ""))
            label.setVisible(bool(row_title))
            listing.reset()
            listing.setVisible(bool(row_title))
            self._row_items[control_id] = list(items or [])

            for item in (items or []):
                li = self._make_list_item(item)
                listing.addItem(li)

    def _make_list_item(self, item):
        title = _display_title(item)
        subtitle = str(_value(item, "year", "overview", "plot", default=""))
        list_item = xbmcgui.ListItem(label=title, label2=subtitle)
        poster = self._art(item, "poster_url", "poster", "image", "thumbnail", "still_url", "still")
        backdrop = self._art(item, "backdrop_url", "backdrop", "fanart_url")
        logo = self._art(item, "logo_url", "logo")
        art = {}
        if poster:
            art.update({"poster": poster, "thumb": poster, "icon": poster})
        if backdrop:
            art["fanart"] = backdrop
        if logo:
            art["clearlogo"] = logo
        if art:
            list_item.setArt(art)

        action = item.get("_theater_action", "")
        payload = item.get("_theater_payload", {})
        list_item.setProperty("Silo.Theater.Action", str(action))
        list_item.setProperty(
            "Silo.Theater.Payload",
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
        )
        list_item.setProperty("Silo.Theater.Overview", str(_value(item, "overview", "plot")))
        list_item.setProperty("Silo.Theater.Type", _item_type(item))
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
        result["_theater_action"] = action
        result["_theater_payload"] = payload
        return result

    def _menu_item(self, title, action, **payload):
        return self._with_action(
            {"title": title, "type": "menu"},
            action,
            **payload
        )

    def _home_rows(self):
        if not self.client.cfg.get("token"):
            return [
                ("WELCOME TO SILO", [
                    self._menu_item("Sign in to Silo", "login"),
                    self._menu_item("Open settings", "settings"),
                ]),
                ("", []),
                ("", []),
            ]

        try:
            sections = self.client.home_sections(image_size="medium")
        except SiloError:
            raise
        except Exception as exc:
            xbmc.log("[plugin.video.silo] Home sections unavailable: %s" % exc, xbmc.LOGWARNING)
            sections = []

        def priority(section):
            name = str(_value(section, "title", "name", default="")).lower()
            if "continue" in name:
                return 0
            if "recently added" in name:
                return 1
            if "recommended" in name or "for you" in name:
                return 2
            return 3

        sections = sorted(sections, key=priority)
        rows = []
        for section in sections:
            if len(rows) >= 3:
                break
            section_id = _value(section, "id", "section_id")
            title = str(_value(section, "title", "name", default="Silo"))
            items = []
            if section_id:
                response = self.client.home_section_catalog_items(
                    section_id, image_size="medium", limit=200
                ) or {}
                items = response.get("items") or []
                if not items:
                    items = response.get("results") or []
            if not items:
                items = section.get("items") or []
            cards = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                media_type = _item_type(item)
                if media_type in ("movie", "episode", "video"):
                    action = "play"
                elif media_type == "series":
                    action = "series"
                elif media_type == "season":
                    action = "season"
                else:
                    continue
                payload = {
                    "content_id": (_value(item, "play_content_id", "content_id", "id") if action == "play" else _content_id(item)),
                    "library_id": _value(item, "library_id", default=""),
                    "title": _display_title(item),
                }
                if action == "season":
                    payload.update({
                        "series_id": _value(item, "series_id"),
                        "season_number": _value(item, "season_number"),
                    })
                cards.append(self._with_action(item, action, **payload))
            if cards:
                rows.append((title, cards))
        while len(rows) < 3:
            if not rows:
                rows.append(("SILO", [self._menu_item("Browse libraries", "libraries")]))
            else:
                rows.append(("", []))
        return rows[:3]

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
                action = "play"
            elif media_type == "series":
                action = "series"
            elif media_type == "season":
                action = "season"
            else:
                continue
            payload = {
                "content_id": content_id,
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
                episode, "play",
                content_id=_value(item, "play_content_id", "content_id", "id"),
                library_id=library_id,
                title=_display_title(item),
                duration_seconds=_value(item, "duration_seconds", default=""),
                file_id=self._single_file_id(item),
            ))
        title = self.params.get("title") or "EPISODES"
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
            action = "series" if media_type == "series" else "play" if _playable(item) else ""
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
            action = "series" if media_type == "series" else "play" if _playable(item) else ""
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
            action = "series" if media_type == "series" else "play" if _playable(item) else ""
            if action:
                cards.append(self._with_action(
                    item, action, content_id=content_id,
                    library_id=_value(item, "library_id", default=""),
                    title=_display_title(item),
                ))
        people = []
        try:
            for person in self.client.search_people(query, limit=20):
                person_id = _value(person, "id", "person_id")
                if person_id:
                    people.append(self._with_action(
                        dict(person, title=_display_title(person), type="person"),
                        "person", person_id=person_id, title=_display_title(person)
                    ))
        except SiloError as exc:
            xbmc.log("[plugin.video.silo] People search unavailable: %s" % exc, xbmc.LOGDEBUG)
        return [("PEOPLE", people), ("RESULTS  ·  " + str(query), cards), ("", [])]

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
            action = "series" if media_type == "series" else "play" if _playable(item) else ""
            if action:
                cards.append(self._with_action(item, action,
                    content_id=content_id, library_id=_value(item, "library_id", default=""),
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
        elif action == "play":
            self._play(payload)

    def _play(self, payload):
        params = {"action": "play", "theater_return": 1}
        for key in ("content_id", "library_id", "file_id", "duration_seconds"):
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

    def _set_hero(self, item):
        if not isinstance(item, dict):
            return
        title = _display_title(item)
        media_type = _item_type(item).replace("_", " ").title()
        year = _value(item, "year", "release_year")
        runtime = _value(item, "runtime_minutes", "runtime")
        meta = "  ·  ".join(str(value) for value in (media_type, year, runtime) if value not in (None, ""))
        plot = str(_value(item, "overview", "plot", "description", default=""))
        backdrop = self._art(item, "backdrop_url", "backdrop", "fanart_url", "poster_url", "poster")
        try:
            self.getControl(HERO_TITLE).setLabel(title)
            self.getControl(HERO_META).setLabel(meta)
            self.getControl(HERO_PLOT).setLabel(plot[:360])
            self.getControl(100).setImage(backdrop or "")
        except Exception:
            pass

    def _update_hero_from_focus(self):
        try:
            focused_id = self.getFocusId()
            if focused_id not in ROW_CONTROLS:
                return
            item = self.getControl(focused_id).getSelectedItem()
            if item is None:
                return
            self.getControl(HERO_TITLE).setLabel(item.getLabel())
            self.getControl(HERO_META).setLabel(item.getProperty("Silo.Theater.Type").replace("_", " ").title())
            self.getControl(HERO_PLOT).setLabel(item.getProperty("Silo.Theater.Overview")[:360])
            backdrop = item.getArt("fanart") or item.getArt("poster") or item.getArt("thumb")
            self.getControl(100).setImage(backdrop or "")
        except Exception:
            pass
