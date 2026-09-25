"""Shared Kodi directory, URL, setting, and playback utility helpers."""

from resources.lib import runtime
from resources.lib.common import *

def direct_play_only_enabled():
    """Return whether playback must remain on Silo's original/direct stream."""
    return str(runtime.ADDON.getSetting("direct_play_only") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def use_kodi_resume_cache_enabled():
    """Return whether Kodi's own cached resume position should be used."""
    return str(runtime.ADDON.getSetting("use_kodi_resume_cache") or "").strip().lower() in (
        "true",
        "1",
        "yes",
    )



def get_kodi_cached_resume_position():
    """Read Kodi's cached resume position for the currently selected item."""
    try:
        value = xbmc.getInfoLabel("ListItem.ResumeTime")
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0




def get_directory_page_size():
    """Return the configured Kodi page size, clamped to 20-200.

    Kodi's legacy slider settings are stored as strings representing floating
    point values even when option="int" is used, so parse the value as a float.
    """
    raw = runtime.ADDON.getSetting("items_per_page")

    try:
        value = int(round(float(raw))) if raw not in (None, "") else MAX_PAGE_SIZE
    except (TypeError, ValueError):
        value = MAX_PAGE_SIZE

    return max(MIN_PAGE_SIZE, min(value, MAX_PAGE_SIZE))


# Build a Kodi plugin URL containing the action and any required IDs.

def paginate_directory(items, page):
    """Return one 200-item slice and whether another page exists."""
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    page_size = get_directory_page_size()
    start = (page_number - 1) * page_size
    end = start + page_size

    return items[start:end], page_number > 1, end < len(items)



def add_previous_page(library_id=None, series_id=None, season_number=None,
                      action=None, page=1):
    """Add a Previous Page folder when the current directory is past page 1."""
    try:
        page_number = int(page or 1)
    except (TypeError, ValueError):
        page_number = 1

    if page_number <= 1:
        return

    params = {
        "action": action,
        "page": page_number - 1,
    }

    if library_id:
        params["library_id"] = library_id
    if series_id:
        params["series_id"] = series_id
    if season_number is not None:
        params["season_number"] = season_number

    item = xbmcgui.ListItem(label="Previous Page")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(**params),
        item,
        True,
    )



def add_next_page(library_id=None, series_id=None, season_number=None,
                  action=None, page=1):
    """Add a Next Page folder when another 200-item slice exists."""
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    params = {
        "action": action,
        "page": page_number + 1,
    }

    if library_id:
        params["library_id"] = library_id
    if series_id:
        params["series_id"] = series_id
    if season_number is not None:
        params["season_number"] = season_number

    item = xbmcgui.ListItem(label="Next Page")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        runtime.HANDLE,
        build_url(**params),
        item,
        True,
    )



def build_url(**params):
    # Optional IDs are sometimes unresolved on profile-wide Home cards. Never
    # serialize Python None as the literal string "None", because Silo treats
    # values such as library_id=None as an invalid positive integer identifier.
    params = {
        key: value
        for key, value in params.items()
        if value is not None
    }
    return runtime.BASE_URL + "?" + urlencode(params)


# Display a short informational notification in Kodi.

def notify(message):
    xbmcgui.Dialog().notification(
        "Silo",
        message,
        xbmcgui.NOTIFICATION_INFO,
        3000,
    )



def format_position(seconds):
    """Convert a number of seconds into a simple human-readable timestamp."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0

    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)

    return "%d:%02d" % (minutes, seconds)



def get_content_id(item):
    """Return a Silo catalog item's content ID."""
    return item.get("content_id") or item.get("id")



__all__ = ["direct_play_only_enabled","use_kodi_resume_cache_enabled","get_kodi_cached_resume_position","get_directory_page_size","paginate_directory","add_previous_page","add_next_page","build_url","notify","format_position","get_content_id"]
