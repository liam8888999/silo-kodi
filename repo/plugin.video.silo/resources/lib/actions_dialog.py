# -*- coding: utf-8 -*-
"""Native Kodi dialog for Silo title actions."""

import xbmc
import xbmcgui


ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
LIST_ID = 100
TITLE_ID = 200


class ActionsDialog(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self.title = kwargs.pop("title", "")
        self.actions = kwargs.pop("actions", [])
        self.result = None
        super(ActionsDialog, self).__init__(*args, **kwargs)

    def onInit(self):
        self.getControl(TITLE_ID).setLabel(self.title)
        control = self.getControl(LIST_ID)
        control.reset()
        items = []
        for action in self.actions:
            items.append(xbmcgui.ListItem(label=action["label"]))
        control.addItems(items)
        if items:
            self.setFocusId(LIST_ID)

    def onClick(self, control_id):
        if control_id != LIST_ID:
            return
        position = self.getControl(LIST_ID).getSelectedPosition()
        if 0 <= position < len(self.actions):
            self.result = self.actions[position]["id"]
        self.close()

    def onAction(self, action):
        if action.getId() in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.close()


def show_actions(client, content_id, library_id=None):
    """Show the current Silo actions for one title and apply the selection."""
    content_id = str(content_id or "").strip()
    if not content_id:
        raise ValueError("No Silo content ID was supplied.")

    state = client.title_action_state(content_id, library_id=library_id)
    title = (
        state.get("detail", {}).get("title")
        or state.get("detail", {}).get("name")
        or content_id
    )
    actions = [
        {
            "id": "unwatched" if state["played"] else "watched",
            "label": "Mark as unwatched in Silo" if state["played"] else "Mark as watched in Silo",
        },
        {
            "id": "unfavorite" if state["is_favorite"] else "favorite",
            "label": "Remove from Silo Favourites" if state["is_favorite"] else "Add to Silo Favourites",
        },
        {
            "id": "unwatchlist" if state["in_watchlist"] else "watchlist",
            "label": "Remove from Silo Watchlist" if state["in_watchlist"] else "Add to Silo Watchlist",
        },
    ]

    dialog = ActionsDialog(
        "silo-actions.xml",
        xbmcaddon_path(),
        "default",
        "1080i",
        title=title,
        actions=actions,
    )
    try:
        dialog.doModal()
        choice = dialog.result
    finally:
        del dialog

    if not choice:
        return

    if choice in ("watched", "unwatched"):
        client.set_watched(content_id, choice == "watched")
        message = "Marked as watched" if choice == "watched" else "Marked as unwatched"
    elif choice in ("favorite", "unfavorite"):
        client.set_favorite(content_id, choice == "favorite")
        message = "Added to Favourites" if choice == "favorite" else "Removed from Favourites"
    elif choice in ("watchlist", "unwatchlist"):
        client.set_watchlist(content_id, choice == "watchlist")
        message = "Added to Watchlist" if choice == "watchlist" else "Removed from Watchlist"
    else:
        return

    xbmcgui.Dialog().notification("Silo", message, xbmcgui.NOTIFICATION_INFO, 2500)
    xbmc.executebuiltin("Container.Refresh")


def xbmcaddon_path():
    import xbmcaddon
    return xbmcaddon.Addon("plugin.video.silo").getAddonInfo("path")
