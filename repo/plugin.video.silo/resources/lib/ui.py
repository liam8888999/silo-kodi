"""Kodi skin/UI helpers for the Silo addon."""

from resources.lib import runtime
from resources.lib.common import *
from resources.lib.utils import *

_SKIN_EPISODE_NUMBER_DETECTION = None

def _tag_blocks(text, tag_name):
    """Return simple XML tag blocks for one tag name."""
    blocks = []
    opening = "<" + tag_name
    closing = "</" + tag_name + ">"
    position = 0

    while True:
        begin = text.find(opening, position)
        if begin < 0:
            return blocks

        finish = text.find(closing, begin)
        if finish < 0:
            return blocks

        finish += len(closing)
        blocks.append(text[begin:finish])
        position = finish



def _tag_attribute(block, name):
    """Read one simple XML attribute without relying on a quote-sensitive regex."""
    lower_block = block.lower()
    marker = name.lower() + "="
    start = lower_block.find(marker)
    if start < 0:
        return ""

    start += len(marker)

    while start < len(block) and block[start].isspace():
        start += 1

    if start >= len(block):
        return ""

    quote = block[start]
    if quote in ("'", '"'):
        finish = block.find(quote, start + 1)
        if finish < 0:
            return ""
        return block[start + 1:finish]

    finish = start
    while finish < len(block) and not block[finish].isspace() and block[finish] != ">":
        finish += 1

    return block[start:finish]



def _label_expression_uses_episode_number(block):
    """Return True when one rendered label expression contains episode + title."""
    lower = block.lower()
    return (
        "listitem.episode" in lower
        and "listitem.title" in lower
    )



def skin_episode_number_in_label():
    """Return True when the active skin already puts episode number in its label.

    Only a label expression that is applicable to normal episode directory
    rendering counts. Playlist-only expressions must not suppress the add-on's
    episode-number prefix.
    """
    global _SKIN_EPISODE_NUMBER_DETECTION

    if _SKIN_EPISODE_NUMBER_DETECTION is not None:
        return _SKIN_EPISODE_NUMBER_DETECTION

    detected = False

    try:
        pending = ["special://skin/xml"]
        visited = set()

        while pending and not detected:
            directory = pending.pop()

            if directory in visited:
                continue

            visited.add(directory)

            try:
                subdirectories, filenames = xbmcvfs.listdir(directory)
            except Exception:
                continue

            for subdirectory in subdirectories:
                child = directory.rstrip("/") + "/" + subdirectory
                pending.append(child)

            for filename in filenames:
                if not str(filename).lower().endswith(".xml"):
                    continue

                path = directory.rstrip("/") + "/" + str(filename)

                try:
                    handle = xbmcvfs.File(path)
                    data = handle.read()
                    handle.close()

                    if isinstance(data, bytes):
                        data = data.decode("utf-8", "ignore")

                    lower = str(data).lower()
                except Exception:
                    continue

                # First inspect the dedicated ListLabelVar. The previous
                # implementation checked the whole variable at once, which
                # incorrectly matched playlist-only values in Estuary.
                for variable in _tag_blocks(lower, "variable"):
                    name = _tag_attribute(variable, "name").strip().lower()

                    if name != "listlabelvar":
                        continue

                    for value in _tag_blocks(variable, "value"):
                        if not _label_expression_uses_episode_number(value):
                            continue

                        condition = _tag_attribute(value, "condition").lower()

                        # A playlist-only label does not affect a normal
                        # directory opened by this add-on.
                        if "window.isactive(videoplaylist)" in condition:
                            continue

                        detected = True
                        break

                    if detected:
                        break

                if detected:
                    break

                # Some skins place the rendered label directly inside an
                # itemlayout. Inspect actual <label> controls rather than
                # treating every ListItem.Episode reference in the layout as
                # evidence that the number is displayed beside the title.
                for layout in _tag_blocks(lower, "itemlayout"):
                    for label in _tag_blocks(layout, "label"):
                        if _label_expression_uses_episode_number(label):
                            detected = True
                            break

                    if detected:
                        break

                if detected:
                    break

        log(
            "Skin episode-number label detection: %s"
            % (
                "already supplied by skin"
                if detected
                else "not supplied by skin"
            )
        )
    except Exception as exc:
        # If the skin cannot be inspected, prefer adding the episode number so
        # the add-on still provides it rather than silently losing it.
        log(
            "Unable to inspect active skin for episode numbering: %s" % exc,
            xbmc.LOGDEBUG,
        )
        detected = False

    _SKIN_EPISODE_NUMBER_DETECTION = detected
    return detected



def episode_display_label(title, episode_number):
    """Return an episode label without duplicating a skin-provided number."""
    title = str(title or "Episode")

    try:
        number = int(episode_number)
    except (TypeError, ValueError):
        return title

    if number <= 0:
        return title

    if skin_episode_number_in_label():
        return title

    return "%d. %s" % (number, title)



__all__ = ["_tag_blocks","_tag_attribute","_label_expression_uses_episode_number","skin_episode_number_in_label","episode_display_label"]
