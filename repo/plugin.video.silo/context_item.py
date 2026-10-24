# -*- coding: utf-8 -*-
"""Skin-independent Silo context actions for Kodi's native context menu."""

import sys
from urllib.parse import urlencode

import xbmc
import xbmcgui


ADDON_ID = "plugin.video.silo"


def _bool_property(item, name):
    value = str(item.getProperty(name) or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _run_mark(content_id, mark, enabled):
    """Dispatch the action through the add-on's normal plugin/API route."""
    query = urlencode({
        "action": "set_mark",
        "content_id": content_id,
        "mark": mark,
        "value": "1" if enabled else "0",
    })
    xbmc.executebuiltin("RunPlugin(plugin://%s/?%s)" % (ADDON_ID, query))


def main():
    try:
        item = sys.listitem
    except AttributeError:
        item = None

    if item is None:
        xbmcgui.Dialog().notification("Silo", "No selected Silo item was supplied.")
        return

    content_id = str(item.getProperty("Silo.ContentID") or "").strip()
    if not content_id:
        # Some skins/items expose the season identifier through the dedicated
        # metadata property; accept it as a fallback.
        content_id = str(item.getProperty("Silo.Season.ContentID") or "").strip()

    if not content_id:
        xbmcgui.Dialog().notification("Silo", "This item has no Silo content ID.")
        return

    watched = _bool_property(item, "Silo.UserState.Played")
    favorite = _bool_property(item, "Silo.UserState.IsFavorite")
    watchlist = _bool_property(item, "Silo.UserState.InWatchlist")

    labels = [
        "Mark as unwatched" if watched else "Mark as watched",
        "Remove from Silo Favourites" if favorite else "Add to Silo Favourites",
        "Remove from Silo Watchlist" if watchlist else "Add to Silo Watchlist",
    ]
    selected = xbmcgui.Dialog().select("Silo Actions", labels)
    if selected < 0:
        return

    if selected == 0:
        _run_mark(content_id, "watched", not watched)
    elif selected == 1:
        _run_mark(content_id, "favorite", not favorite)
    elif selected == 2:
        _run_mark(content_id, "watchlist", not watchlist)


if __name__ == "__main__":
    main()
