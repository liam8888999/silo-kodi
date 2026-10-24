"""Playback skip-marker parsing and one-pass tracking for Silo versions.

The Silo catalog stores intro/credits markers in source-media seconds and
chapter spans in chapter metadata. This module deliberately has no Kodi
imports so its timeline logic remains easy to test.
"""

import math
import re


LABELS = {
    "intro": "Intro",
    "recap": "Recap",
    "credits": "Credits",
}
DONE_LABELS = {
    "intro": "Skipped intro",
    "recap": "Skipped recap",
    "credits": "Skipped credits",
}
MIN_LENGTH_SECONDS = 3.0
_RECAP_TITLE = re.compile(r"^\s*(recap|previously)\b", re.IGNORECASE)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _span(start, end, offset):
    start = _number(start)
    end = _number(end)
    if start is None or end is None:
        return None
    start = max(0.0, start - offset)
    end = end - offset
    if end - start < MIN_LENGTH_SECONDS:
        return None
    return {"start": start, "end": end}


def segments(version, offset=0.0):
    """Build sorted intro, recap and credits spans in the player's timeline."""
    if not isinstance(version, dict):
        return []
    offset = _number(offset)
    if offset is None:
        offset = 0.0

    markers = version.get("markers")
    if not isinstance(markers, dict):
        markers = {}

    result = []
    for kind in ("intro", "credits"):
        marker = version.get(kind)
        if not isinstance(marker, dict):
            marker = markers.get(kind)
        if not isinstance(marker, dict):
            continue
        span = _span(
            marker.get("start", marker.get("start_seconds")),
            marker.get("end", marker.get("end_seconds")),
            offset,
        )
        if span:
            span["kind"] = kind
            result.append(span)

    chapters = version.get("chapters")
    if not isinstance(chapters, list):
        chapters = version.get("chapter_markers")
    if isinstance(chapters, list):
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            title = str(chapter.get("title") or chapter.get("name") or "")
            if not _RECAP_TITLE.match(title):
                continue
            span = _span(
                chapter.get("start_seconds", chapter.get("start")),
                chapter.get("end_seconds", chapter.get("end")),
                offset,
            )
            if span and not any(
                existing["start"] < span["end"]
                and span["start"] < existing["end"]
                for existing in result
            ):
                span["kind"] = "recap"
                result.append(span)
            break

    return sorted(result, key=lambda item: (item["start"], item["end"]))


class SkipTracker:
    """Return each segment once per pass; replaying from before it rearms it."""

    def __init__(self, spans):
        self.spans = list(spans or [])
        self.handled = set()

    def observe(self, position):
        position = _number(position)
        if position is None:
            return None

        for index, span in enumerate(self.spans):
            if position < span["start"] - 1.0:
                self.handled.discard(index)

        for index, span in enumerate(self.spans):
            if (
                index not in self.handled
                and span["start"] <= position < span["end"] - 1.0
            ):
                self.handled.add(index)
                return span
        return None
