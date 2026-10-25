"""Generate Kodi-native STRM/NFO entries from the active Silo profile.

Only files inside this add-on's managed native-library directory are written or
removed. The source folders themselves are user-added to Kodi once, with
Local information only selected as their content provider.
"""

import hashlib
import json
import os
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import posixpath
import re
import time
from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.silo import SiloClient, log
from resources.lib.artwork_proxy import make_artwork_url, start_artwork_proxy


ADDON_ID = "plugin.video.silo"
MEDIA_ROOT = "special://profile/addon_data/plugin.video.silo/native-library"
MANIFEST_VERSION = 2
MAX_ART_BYTES = 20 * 1024 * 1024
MAX_SYNC_WORKERS = 8
MAX_PENDING_ITEMS = 32
_SYNC_LOCAL = threading.local()
ADDON = xbmcaddon.Addon()


class LibrarySyncDisabled(Exception):
    """Raised when native library integration is disabled during a sync."""


def _integration_enabled():
    return str(ADDON.getSetting("native_library_enabled") or "").strip().lower() in (
        "true", "1", "yes"
    )


def _require_enabled():
    if not _integration_enabled():
        raise LibrarySyncDisabled("Native Kodi library integration is disabled.")


def _translated(path):
    return xbmcvfs.translatePath(path)


def _is_special(path):
    return str(path or "").startswith("special://")


def _join(root, *parts):
    """Join components without corrupting Kodi special paths."""
    if _is_special(root):
        value = str(root).rstrip("/")
        for part in parts:
            value += "/" + str(part).replace("\\", "/").strip("/")
        return value
    return os.path.join(root, *parts)


def _dirname(path):
    return posixpath.dirname(path) if _is_special(path) else os.path.dirname(path)


def _basename(path):
    return posixpath.basename(str(path).replace("\\", "/"))


def _relative_to_root(path, root=None):
    root = root or MEDIA_ROOT
    value = str(path or "").replace("\\", "/")
    base = str(root or "").replace("\\", "/").rstrip("/")
    if value.startswith(base + "/"):
        return value[len(base) + 1:]

    native_root = _translated(base).replace("\\", "/").rstrip("/")
    if value.startswith(native_root + "/"):
        return value[len(native_root) + 1:]
    raise ValueError("Path is outside the managed library root: %s" % path)


def _ensure_dir(path):
    """Create folders via Kodi VFS using the special:// path."""
    _require_enabled()
    path = str(path or "")
    if not path:
        raise IOError("Kodi library folder path is empty")
    if xbmcvfs.exists(path):
        return True

    vfs_error = None
    try:
        xbmcvfs.mkdirs(path)
    except Exception as exc:
        vfs_error = exc

    if xbmcvfs.exists(path):
        return True

    native_path = _translated(path) if _is_special(path) else path
    try:
        os.makedirs(native_path, exist_ok=True)
    except Exception as exc:
        raise IOError(
            "Could not create Kodi library directory %s (VFS: %s; filesystem: %s)"
            % (path, vfs_error or "no error reported", exc)
        )
    if not xbmcvfs.exists(path) and not os.path.isdir(native_path):
        raise IOError("Kodi could not create directory: %s" % path)
    return True


def native_library_paths(special=True):
    """Return managed library folders; special:// is the canonical form."""
    root = MEDIA_ROOT if special else _translated(MEDIA_ROOT)
    if special:
        return {
            "root": root,
            "movies": root.rstrip("/") + "/Movies",
            "tvshows": root.rstrip("/") + "/TV Shows",
        }
    return {
        "root": root,
        "movies": os.path.join(root, "Movies"),
        "tvshows": os.path.join(root, "TV Shows"),
    }


def ensure_library_directories():
    """Create managed folders through Kodi VFS only when integration is enabled."""
    _require_enabled()
    paths = native_library_paths(special=True)
    for path in paths.values():
        _ensure_dir(path)
    log(
        "Native library folders ready via Kodi VFS: Movies=%s; TV Shows=%s"
        % (paths["movies"], paths["tvshows"]),
        xbmc.LOGINFO,
    )
    return paths


def _read_text(path):
    if not xbmcvfs.exists(path):
        return ""
    handle = None
    try:
        handle = xbmcvfs.File(path, "r")
        return handle.read()
    except Exception:
        return ""
    finally:
        if handle:
            handle.close()


def _write_text(path, value):
    _require_enabled()
    parent = _dirname(path)
    _ensure_dir(parent)
    old = _read_text(path)
    if old == value:
        return False
    handle = None
    try:
        handle = xbmcvfs.File(path, "w")
        handle.write(value)
        return True
    finally:
        if handle:
            handle.close()


def _write_bytes(path, value):
    _require_enabled()
    parent = _dirname(path)
    _ensure_dir(parent)
    handle = None
    try:
        handle = xbmcvfs.File(path, "wb")
        handle.write(value)
        return True
    finally:
        if handle:
            handle.close()


def _safe_component(value, fallback="item"):
    value = str(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:90].rstrip(" .")
    if value in ("", ".", ".."):
        return fallback
    return value


def _short_hash(value, length=12):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _managed_folder_name(item_kind, scope_key, library_id, content_id):
    """Return a collision-resistant, Kodi-friendly top-level media folder name.

    Kodi's TV scanner expects each show's tvshow.nfo in a show folder directly
    under the configured TV Shows source. Namespace the folder name itself
    instead of introducing scope/library wrapper directories.
    """
    prefix = "movie" if item_kind == "movie" else "show"
    return "%s_%s_%s_%s" % (
        prefix,
        str(scope_key or ""),
        _short_hash(library_id, 6),
        _short_hash(content_id, 12),
    )


def _content_id(item):
    value = item.get("content_id") or item.get("media_item_id") or item.get("id")
    return str(value).strip() if value not in (None, "") else ""


def _media_type(item):
    return str(item.get("type") or item.get("media_type") or "").strip().lower()


def _year(item):
    try:
        value = int(item.get("year") or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    for key in ("release_date", "first_air_date", "air_date"):
        value = str(item.get(key) or "")
        if len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return 0


def _value(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def _xml_tag(name, value):
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    return "<%s>%s</%s>" % (name, escape(str(value)), name)


def _xml_values(name, values):
    result = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or ""
        if value not in (None, ""):
            result.append(_xml_tag(name, value))
    return "".join(result)


def _unique_id(content_id):
    return _xml_tag("uniqueid", content_id).replace(
        "<uniqueid>", '<uniqueid type="silo" default="true">', 1
    )


def _nfo(item, tag, content_id, artwork=None, extra=""):
    title = _value(item, "title", "name")
    plot = _value(item, "overview", "plot", "description")
    genres = _xml_values("genre", item.get("genres") or [])
    studios = _xml_values("studio", item.get("studios") or [])
    countries = _xml_values("country", item.get("countries") or [])
    keywords = _xml_values("tag", item.get("keywords") or [])
    year = _year(item)
    runtime = _value(item, "runtime", "runtime_minutes")
    try:
        runtime = int(float(runtime)) if runtime not in (None, "") else 0
    except (TypeError, ValueError):
        runtime = 0
    # CatalogItem runtimes in Silo are minutes; guard against seconds-shaped
    # values from older server versions.
    if runtime > 600:
        runtime = int(round(runtime / 60.0))

    art = artwork or {}
    # When artwork is explicitly supplied, it has already been checked for a
    # safe direct URL or converted to the authenticated local proxy. Do not
    # fall back to a raw API URL that might contain a short-lived credential.
    thumbs = ""
    if art.get("poster"):
        thumbs += _xml_tag("thumb", art["poster"])
    elif artwork is None:
        poster = _value(item, "poster_url", "poster", "image", "artwork", "thumbnail")
        if poster:
            thumbs += _xml_tag("thumb", poster)
    fanart = ""
    if art.get("fanart"):
        fanart = "<fanart>%s</fanart>" % _xml_tag("thumb", art["fanart"])
    elif artwork is None:
        backdrop = _value(item, "backdrop_url", "backdrop", "fanart_url", "fanart")
        if backdrop:
            fanart = "<fanart>%s</fanart>" % _xml_tag("thumb", backdrop)

    fields = [
        _xml_tag("title", title),
        _xml_tag("originaltitle", _value(item, "original_title")),
        _xml_tag("sorttitle", _value(item, "sort_title")),
        _xml_tag("year", year or ""),
        _xml_tag("plot", plot),
        _xml_tag("premiered", _value(item, "release_date", "first_air_date", "air_date")),
        _xml_tag("runtime", runtime or ""),
        genres,
        studios,
        countries,
        keywords,
        thumbs,
        fanart,
        _unique_id(content_id),
        extra,
    ]
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<%s>%s</%s>\n' % (
        tag, "".join(fields), tag
    )


def _art_url(item, kind):
    candidates = (
        ("still_url", "poster_url", "poster", "image", "artwork", "thumbnail")
        if kind == "poster"
        else ("backdrop_url", "backdrop", "fanart_url", "fanart")
    )
    for key in candidates:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _canonical_url(value):
    parsed = urlsplit(str(value or ""))
    return parsed._replace(query="", fragment="").geturl()


def _image_extension(content_type):
    content_type = str(content_type or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }.get(content_type, ".jpg")


def _download_art(client, source, folder, prefix, old_art_refs, new_art_refs, files,
                  previous_files=None):
    """Download artwork once, refreshing it if its stable URL changes."""
    if not source:
        return ""
    _require_enabled()
    try:
        absolute = client.abs_url(source)
        canonical = _canonical_url(absolute)
        key_prefix = _relative_to_root(folder)
        ref_key = key_prefix + "/" + prefix
        previous = old_art_refs.get(ref_key) or ""
        if previous == canonical:
            for rel in (previous_files or set()):
                if rel.startswith(key_prefix + "/" + prefix + "."):
                    candidate = _join(MEDIA_ROOT, rel)
                    if xbmcvfs.exists(candidate):
                        new_art_refs[ref_key] = canonical
                        files.add(rel)
                        return _basename(candidate)

        response = client.session.get(
            absolute,
            headers=client._client_headers(),
            timeout=20,
        )
        if not response.ok:
            return ""
        data = response.content
        if not data or len(data) > MAX_ART_BYTES:
            return ""
        extension = _image_extension(response.headers.get("Content-Type"))
        path = _join(folder, prefix + extension)
        _write_bytes(path, data)
        rel = _relative_to_root(path)
        files.add(rel)
        new_art_refs[ref_key] = canonical
        return _basename(path)
    except Exception as exc:
        log("Native library artwork download failed: %s" % exc, xbmc.LOGDEBUG)
        return ""


def _plugin_url(content_id, library_id):
    query = urlencode({
        "action": "play",
        "content_id": str(content_id),
        "library_id": str(library_id),
    })
    return "plugin://%s/?%s" % (ADDON_ID, query)


def _write_entry(client, item, library_id, base_folder, item_kind,
                 old_art_refs, new_art_refs, files, refresh_paths,
                 previous_files=None, folder_name=None):
    content_id = _content_id(item)
    if not content_id:
        return False

    item_hash = _short_hash(content_id)
    title = str(_value(item, "title", "name") or content_id)
    art = {}
    folder = base_folder

    if item_kind == "movie":
        folder = _join(base_folder, folder_name or ("movie_" + item_hash))
        filename = "movie_" + item_hash
        nfo_tag = "movie"
    elif item_kind == "show":
        folder = _join(base_folder, folder_name or ("show_" + item_hash))
        filename = ""
        nfo_tag = "tvshow"
    else:
        return False

    _ensure_dir(folder)
    # Store URLs, not image files. Same-server artwork is served by the
    # authenticated loopback proxy; safe public CDN URLs remain remote.
    art["poster"] = make_artwork_url(
        _art_url(item, "poster"), client.base,
        client.cfg.get("username"), client.cfg.get("profile_id")
    )
    art["fanart"] = make_artwork_url(
        _art_url(item, "fanart"), client.base,
        client.cfg.get("username"), client.cfg.get("profile_id")
    )
    art_root = MEDIA_ROOT

    if item_kind == "movie":
        strm_path = _join(folder, filename + ".strm")
        nfo_path = _join(folder, filename + ".nfo")
        uri = _plugin_url(content_id, library_id) + "\n"
        nfo = _nfo(item, nfo_tag, content_id, art)
        for path, value in ((strm_path, uri), (nfo_path, nfo)):
            if _write_text(path, value):
                refresh_paths.add(path)
            files.add(_relative_to_root(path, art_root))
    else:
        nfo_path = _join(folder, "tvshow.nfo")
        nfo = _nfo(item, nfo_tag, content_id, art)
        if _write_text(nfo_path, nfo):
            refresh_paths.add(nfo_path)
        files.add(_relative_to_root(nfo_path, art_root))

    return True


def _write_episode(client, episode, series, library_id, series_folder,
                  season_number, old_art_refs, new_art_refs, files, refresh_paths,
                  previous_files=None):
    content_id = _content_id(episode)
    if not content_id:
        return False
    try:
        episode_number = int(episode.get("episode_number") or episode.get("episode") or 0)
    except (TypeError, ValueError):
        episode_number = 0
    try:
        season_number = int(season_number or 0)
    except (TypeError, ValueError):
        season_number = 0

    season_folder = _join(series_folder, "Season %02d" % season_number)
    _ensure_dir(season_folder)
    item_hash = _short_hash(content_id)
    filename = "S%02dE%02d_%s" % (season_number, episode_number, item_hash)
    strm_path = _join(season_folder, filename + ".strm")
    nfo_path = _join(season_folder, filename + ".nfo")

    art = {}
    art["poster"] = make_artwork_url(
        _art_url(episode, "poster"), client.base,
        client.cfg.get("username"), client.cfg.get("profile_id")
    )

    extra = (
        _xml_tag("season", season_number)
        + _xml_tag("episode", episode_number)
        + _xml_tag("aired", _value(episode, "air_date", "first_air_date"))
        + _xml_tag("showtitle", _value(series, "title", "name"))
    )
    nfo = _nfo(episode, "episodedetails", content_id, art, extra)
    uri = _plugin_url(content_id, library_id) + "\n"
    if _write_text(strm_path, uri):
        refresh_paths.add(strm_path)
    if _write_text(nfo_path, nfo):
        refresh_paths.add(nfo_path)
    root = MEDIA_ROOT
    files.add(_relative_to_root(strm_path, root))
    files.add(_relative_to_root(nfo_path, root))
    return True


def _manifest_path(root, scope_key):
    return _join(root, ".scope-" + scope_key + ".json")


def _load_manifest(path):
    try:
        data = json.loads(_read_text(path) or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _relative_safe(path):
    path = str(path or "").replace("\\", "/")
    parts = path.split("/")
    return bool(path and not path.startswith("/") and all(p not in ("", ".", "..") for p in parts))


def _remove_empty_generated_directories(root, removed):
    """Prune empty old layout directories after moving entries to the flat layout.

    Only ancestors of tracked files that this sync actually removed are
    considered. The two category roots and anything containing untracked files
    are always preserved.
    """
    candidates = set()
    for relative in removed or []:
        relative = str(relative or "").replace("\\", "/")
        if not _relative_safe(relative):
            continue
        parts = posixpath.dirname(relative).split("/")
        if not parts or parts[0] not in ("Movies", "TV Shows"):
            continue
        for end in range(2, len(parts) + 1):
            candidate = "/".join(parts[:end])
            if _relative_safe(candidate):
                candidates.add(candidate)

    # Remove deepest folders first so their parents can become empty.
    for relative in sorted(candidates, key=lambda value: (value.count("/"), len(value)), reverse=True):
        _require_enabled()
        path = _join(root, relative)
        try:
            directories, files = xbmcvfs.listdir(path)
            if not directories and not files:
                xbmcvfs.rmdir(path)
        except Exception as exc:
            log("Could not prune empty native-library directory %s: %s" % (relative, exc), xbmc.LOGDEBUG)


def _remove_stale(root, previous_files, current_files):
    _require_enabled()
    removed = []
    for relative in set(previous_files or []) - set(current_files or []):
        _require_enabled()
        if not _relative_safe(relative):
            continue
        # Only remove generated, known file types and known image names.
        name = posixpath.basename(relative.replace("\\", "/")).lower()
        allowed = (
            name.endswith((".strm", ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))
            and (name.startswith(("movie_", "show_", "s", "poster.", "fanart.", "thumb_"))
                 or name == "tvshow.nfo")
        )
        if not allowed:
            continue
        full_path = _join(root, relative)
        if xbmcvfs.exists(full_path) and xbmcvfs.delete(full_path):
            removed.append(relative)
    if removed:
        _remove_empty_generated_directories(root, removed)
    return removed


def _json_rpc(method, params=None):
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }
    response = xbmc.executeJSONRPC(json.dumps(request))
    try:
        data = json.loads(response)
    except (TypeError, ValueError):
        return None
    if data.get("error"):
        log("Kodi JSON-RPC %s failed: %s" % (method, data["error"]), xbmc.LOGDEBUG)
        return None
    return data.get("result")


def _same_path(left, right):
    left = os.path.normcase(os.path.normpath(_translated(left))).rstrip("\\/")
    right = os.path.normcase(os.path.normpath(_translated(right))).rstrip("\\/")
    return left == right


def _is_video_source(path):
    result = _json_rpc("Files.GetSources", {"media": "video"})
    sources = (result or {}).get("sources", [])
    return any(_same_path(source.get("file", ""), path) for source in sources if source.get("file"))


def video_source_exists(path):
    """Public helper used by the setup UI to detect a registered video source."""
    return _is_video_source(path)



def ensure_kodi_video_sources():
    """Register Silo's managed folders as Kodi video sources, Netflix-style.

    Kodi has no supported JSON-RPC method for creating video sources. The
    Netflix add-on uses sources.xml for this setup step. Keep a one-time
    backup before editing an existing file, and only add our own missing paths.
    """
    _require_enabled()
    from xml.dom import minidom
    import shutil

    paths = native_library_paths(special=False)
    expected = (
        ("Silo Movies", paths["movies"]),
        ("Silo TV Shows", paths["tvshows"]),
    )
    sources_xml_path = _translated("special://userdata/sources.xml")
    sources_xml_exists = os.path.isfile(sources_xml_path)

    if sources_xml_exists:
        try:
            document = minidom.parse(sources_xml_path)
        except Exception as exc:
            raise IOError(
                'Could not read Kodi sources.xml. It was not modified; '
                'check the Kodi userdata sources.xml file. Details: %s' % exc
            )
        root = document.documentElement
        if root is None or root.tagName != "sources":
            raise IOError("Kodi sources.xml has an unexpected XML root; it was not modified.")
    else:
        document = minidom.Document()
        root = document.createElement("sources")
        document.appendChild(root)
        for section_name in ("programs", "video", "music", "pictures", "files"):
            section = document.createElement(section_name)
            default = document.createElement("default")
            default.setAttribute("pathversion", "1")
            section.appendChild(default)
            root.appendChild(section)

    def direct_child(parent, tag_name):
        for child in parent.childNodes:
            if child.nodeType == 1 and child.tagName == tag_name:
                return child
        return None

    video_node = direct_child(root, "video")
    if video_node is None:
        video_node = document.createElement("video")
        default = document.createElement("default")
        default.setAttribute("pathversion", "1")
        video_node.appendChild(default)
        root.appendChild(video_node)

    def child_text(parent, tag_name):
        nodes = parent.getElementsByTagName(tag_name)
        if not nodes:
            return ""
        return "".join(
            child.data for child in nodes[0].childNodes
            if child.nodeType in (3, 4)
        ).strip()

    def normalised_path(path):
        value = str(path or "").strip()
        if value.startswith("special://"):
            value = _translated(value)
        try:
            value = xbmcvfs.makeLegalFilename(value)
        except Exception:
            pass
        return os.path.normcase(os.path.normpath(value)).rstrip("\\\\/")

    source_nodes = [
        child for child in video_node.childNodes
        if child.nodeType == 1 and child.tagName == "source"
    ]
    existing_paths = {}
    existing_names = set()
    for source_node in source_nodes:
        name = child_text(source_node, "name")
        if name:
            existing_names.add(name.casefold())
        source_path = child_text(source_node, "path")
        if source_path:
            existing_paths[normalised_path(source_path)] = name

    added = []
    already_present = []
    for preferred_name, source_path in expected:
        legal_path = xbmcvfs.makeLegalFilename(_translated(source_path))
        path_key = normalised_path(legal_path)
        if path_key in existing_paths:
            already_present.append({
                "name": existing_paths[path_key] or preferred_name,
                "path": legal_path,
            })
            continue

        source_name = preferred_name
        suffix = 2
        while source_name.casefold() in existing_names:
            source_name = "%s (%d)" % (preferred_name, suffix)
            suffix += 1

        source_node = document.createElement("source")
        name_node = document.createElement("name")
        name_node.appendChild(document.createTextNode(source_name))
        source_node.appendChild(name_node)

        path_node = document.createElement("path")
        path_node.setAttribute("pathversion", "1")
        path_node.appendChild(document.createTextNode(legal_path))
        source_node.appendChild(path_node)

        sharing_node = document.createElement("allowsharing")
        sharing_node.appendChild(document.createTextNode("true"))
        source_node.appendChild(sharing_node)
        video_node.appendChild(source_node)

        existing_paths[path_key] = source_name
        existing_names.add(source_name.casefold())
        added.append({"name": source_name, "path": legal_path})

    backup_path = ""
    if added:
        # Preserve the user's pre-existing source configuration before replacing
        # it. Never overwrite an earlier backup.
        if sources_xml_exists:
            backup_path = sources_xml_path + ".silo.bak"
            if not os.path.exists(backup_path):
                shutil.copy2(sources_xml_path, backup_path)

        temporary_path = sources_xml_path + ".silo.tmp"
        try:
            serialized = b"\n".join(
                line for line in document.toprettyxml(
                    indent="  ", encoding="utf-8"
                ).splitlines() if line.strip()
            ) + b"\n"
            with open(temporary_path, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, sources_xml_path)
        except Exception:
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except Exception:
                pass
            raise

        log(
            "Registered Silo Kodi video source(s): %s"
            % ", ".join(item["name"] for item in added),
            xbmc.LOGINFO,
        )

    return {
        "added": added,
        "existing": already_present,
        "sources_xml": sources_xml_path,
        "backup": backup_path,
    }

def _scan_category(path, remove_missing=False):
    _require_enabled()
    if remove_missing:
        # Limit cleanup to the add-on-owned source path. Never run a whole-library
        # clean, which could remove unrelated network/offline media.
        translated = _translated(path)
        escaped = translated.replace('"', '\\"')
        xbmc.executebuiltin('CleanLibrary(video, "%s")' % escaped)
    result = _json_rpc(
        "VideoLibrary.Scan",
        {"directory": _translated(path), "showdialogs": False},
    )
    return result is not None


def _wait_for_scan(monitor, timeout=300):
    started = time.time()
    while not monitor.abortRequested() and time.time() - started < timeout:
        if not xbmc.getCondVisibility("Library.IsScanningVideo"):
            return
        if monitor.waitForAbort(2):
            return


def _refresh_changed_nfo(paths):
    """Refresh existing entries from modified local NFO files after scanning."""
    _require_enabled()
    paths = set(os.path.normcase(os.path.normpath(_translated(p))) for p in paths or [])
    if not paths:
        return

    def pages(method, result_key, params):
        start = 0
        while True:
            payload = dict(params)
            payload["limits"] = {"start": start, "end": start + 500}
            data = _json_rpc(method, payload) or {}
            items = data.get(result_key, [])
            if not items:
                break
            for item in items:
                yield item
            limits = data.get("limits") or {}
            total = int(limits.get("total") or 0)
            start += len(items)
            if start >= total or len(items) < 500:
                break

    for item in pages("VideoLibrary.GetMovies", "movies", {"properties": ["file", "title"]}):
        file_path = item.get("file")
        if file_path and os.path.normcase(os.path.normpath(file_path)) in paths:
            _json_rpc("VideoLibrary.RefreshMovie", {"movieid": item.get("movieid"), "ignorenfo": False})

    for show in pages("VideoLibrary.GetTVShows", "tvshows", {"properties": ["file", "title"]}):
        show_path = show.get("file")
        if show_path and os.path.normcase(os.path.normpath(show_path)) in paths:
            _json_rpc("VideoLibrary.RefreshTVShow", {"tvshowid": show.get("tvshowid"), "ignorenfo": False, "refreshepisodes": False})

    episode_paths = {p for p in paths if os.path.splitext(p)[1].lower() == ".nfo"}
    if not episode_paths:
        return
    for show in pages("VideoLibrary.GetTVShows", "tvshows", {"properties": ["title"]}):
        show_id = show.get("tvshowid")
        if show_id is None:
            continue
        for ep in pages("VideoLibrary.GetEpisodes", "episodes", {"tvshowid": show_id, "properties": ["file", "title"]}):
            file_path = ep.get("file")
            if file_path and os.path.normcase(os.path.normpath(file_path)) in episode_paths:
                _json_rpc("VideoLibrary.RefreshEpisode", {"episodeid": ep.get("episodeid"), "ignorenfo": False})


def sync_current_profile(client, monitor=None, progress=None):
    """Sync active Silo libraries only while the feature is enabled."""
    if not _integration_enabled():
        return {
            "skipped": "disabled",
            "movies": 0,
            "shows": 0,
            "episodes": 0,
        }
    paths = ensure_library_directories()
    # The persistent Kodi service normally owns the proxy. Starting it here
    # also supports manual setup when the service has not started yet.
    start_artwork_proxy()
    root = paths["root"]
    movies_root = paths["movies"]
    shows_root = paths["tvshows"]
    if not client.base or not (client.cfg.get("token") or client.cfg.get("refresh_token")):
        return {
            "skipped": "not signed in",
            "movies": 0,
            "shows": 0,
            "episodes": 0,
            "paths": native_library_paths(special=True),
        }

    if progress:
        progress(0, "Connecting to Silo…")
    profile_id = str(client.cfg.get("profile_id") or "")
    server = str(client.cfg.get("server") or client.base)
    username = str(client.cfg.get("username") or "")
    scope_key = _short_hash(server + "|" + username + "|" + profile_id, 16)
    manifest_path = _manifest_path(root, scope_key)
    previous = _load_manifest(manifest_path)
    previous_files = set(previous.get("files") or [])
    old_art_refs = previous.get("art_refs") or {}
    new_art_refs = {}
    files = set()
    refresh_paths = set()
    movie_count = 0
    episode_count = 0
    show_count = 0
    touched = set()
    removals = []

    # Persistent item fingerprints: catalogue cards are cheap, and only changed
    # items need metadata/artwork work. Episode cards let us identify changed
    # seasons without asking every show for every season on every sync.
    worker_cfg = dict(client.cfg)
    worker_identity = (
        str(worker_cfg.get("server") or client.base or ""),
        str(worker_cfg.get("username") or ""),
        str(worker_cfg.get("profile_id") or ""),
    )
    # Manifest v2 changes the directory layout so Kodi sees each movie/show
    # as a direct child of its configured source. Rebuild v1 entries once;
    # keep the old file list so stale generated files/directories are pruned.
    previous_items = (
        (previous.get("items") or {})
        if previous.get("version") == MANIFEST_VERSION else {}
    )
    if not isinstance(previous_items, dict):
        previous_items = {}

    current_item_entries = {}
    series_cards_by_library = {}
    changed_series_by_library = {}
    changed_episode_groups = {}
    library_info_by_id = {}
    pending_futures = set()
    sync_counts = {"movies": 0, "shows": 0, "episodes": 0}
    changed_counts = {"movies": 0, "shows": 0, "episodes": 0}

    def worker_client():
        cached = getattr(_SYNC_LOCAL, "client", None)
        if cached is None or getattr(_SYNC_LOCAL, "identity", None) != worker_identity:
            if cached is not None:
                try:
                    cached.session.close()
                except Exception:
                    pass
            cached = SiloClient()
            cached.cfg.clear()
            cached.cfg.update(worker_cfg)
            cached._caps = client._caps
            _SYNC_LOCAL.client = cached
            _SYNC_LOCAL.identity = worker_identity
        else:
            cached.cfg.clear()
            cached.cfg.update(worker_cfg)
        return cached

    state_fields = {
        "position_seconds", "progress_updated_at", "user_state", "sort_metrics",
        "user_data", "badges", "upcoming_event", "search_diagnostics",
    }

    def stable_value(value, key=""):
        if isinstance(value, dict):
            return {
                k: stable_value(v, str(k))
                for k, v in sorted(value.items())
                if k not in state_fields
            }
        if isinstance(value, list):
            return [stable_value(v, key) for v in value]
        if isinstance(value, str) and (key.endswith("_url") or key.endswith("_urls")):
            return value.split("?", 1)[0]
        return value

    def card_fingerprint(card):
        payload = json.dumps(
            {"artwork_mode": 2, "card": stable_value(card)},
            sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def item_identity(library_id, content_id):
        return str(library_id) + "|" + str(content_id)

    def tracked_files_exist(entry, media_type, card):
        tracked = list(entry.get("files") or [])
        if not tracked:
            return False
        names = [posixpath.basename(str(path).replace("\\", "/")).lower() for path in tracked]
        if media_type in ("movie", "episode"):
            required_exts = (".strm", ".nfo")
            if not all(any(name.endswith(ext) for name in names) for ext in required_exts):
                return False
        elif not any(name == "tvshow.nfo" for name in names):
            return False

        # Check the key files on every item; also verify a representative artwork
        # file when Silo advertises artwork. This catches lost output without
        # doing multiple VFS existence checks for every cached image on every sync.
        for relative in tracked:
            name = posixpath.basename(str(relative).replace("\\", "/")).lower()
            if name.endswith((".strm", ".nfo")):
                if not xbmcvfs.exists(_join(root, relative)):
                    return False
        # Artwork is remote; NFO/STRM existence is sufficient. Local image
        # files from older versions are pruned by manifest reconciliation.
        return True

    def item_needs_write(identity, library_id, media_type, fingerprint, card):
        old = previous_items.get(identity) or {}
        valid = (
            old.get("fingerprint") == fingerprint
            and str(old.get("library_id") or "") == str(library_id)
            and str(old.get("type") or "") == media_type
            and tracked_files_exist(old, media_type, card)
        )
        if valid:
            current_item_entries[identity] = old
            return False
        return True

    def make_entry(identity, library_id, content_id, media_type, fingerprint, item_files, item_refs):
        return {
            "identity": identity,
            "library_id": str(library_id),
            "content_id": str(content_id),
            "type": media_type,
            "fingerprint": fingerprint,
            "files": sorted(item_files),
            "art_refs": dict(item_refs),
        }

    def process_item(item, library_id, library_segment, media_type, fingerprint):
        _require_enabled()
        content_id = _content_id(item)
        identity = item_identity(library_id, content_id)
        item_files, item_refs, item_refresh = set(), {}, set()
        item_kind = "movie" if media_type == "movie" else "show"
        target = movies_root if media_type == "movie" else shows_root
        folder_name = _managed_folder_name(
            item_kind, scope_key, library_id, content_id
        )
        written = _write_entry(
            worker_client(), item, library_id, target,
            item_kind, old_art_refs, item_refs, item_files, item_refresh,
            previous_files, folder_name,
        )
        return {
            "entries": [make_entry(identity, library_id, content_id, media_type,
                                   fingerprint, item_files, item_refs)] if written else [],
            "refresh_paths": item_refresh,
            "touched": {"Movies" if media_type == "movie" else "TV Shows"} if written else set(),
            "changed": (
                {"movies": 1} if media_type == "movie" else {"shows": 1}
            ) if written else {},
        }

    def process_episode_group(library_id, library_segment, series_id, season_number, summaries):
        """Fetch a changed season once; write just its new/changed episodes."""
        _require_enabled()
        item_client = worker_client()
        details = item_client.episodes(
            str(series_id), int(season_number), library_id=str(library_id)
        ) or []
        details_by_id = {_content_id(item): item for item in details if _content_id(item)}
        series = (series_cards_by_library.get(str(library_id)) or {}).get(str(series_id))
        if not series:
            series = {"content_id": str(series_id), "title": ""}
        target = _join(
            shows_root,
            _managed_folder_name("show", scope_key, library_id, series_id),
        )
        entries, item_refresh, touched = [], set(), set()

        for summary in summaries:
            content_id = summary["content_id"]
            episode = details_by_id.get(content_id) or summary["card"]
            episode = dict(episode)
            episode.setdefault("content_id", content_id)
            episode.setdefault("type", "episode")
            episode.setdefault("series_id", str(series_id))
            episode.setdefault("season_number", int(season_number))
            item_files, item_refs, refresh = set(), {}, set()
            if not _write_episode(
                item_client, episode, series, str(library_id), target,
                int(season_number), old_art_refs, item_refs, item_files, refresh,
                previous_files,
            ):
                raise IOError("Could not export episode %s" % content_id)
            entries.append(make_entry(
                summary["identity"], library_id, content_id, "episode",
                summary["fingerprint"], item_files, item_refs,
            ))
            item_refresh.update(refresh)
            touched.add("TV Shows")

        return {
            "entries": entries,
            "refresh_paths": item_refresh,
            "touched": touched,
            "changed": {"episodes": len(entries)} if entries else {},
        }

    def consume_future(future):
        result = future.result()
        for entry in result.get("entries") or []:
            current_item_entries[entry["identity"]] = entry
        refresh_paths.update(result.get("refresh_paths") or set())
        touched.update(result.get("touched") or set())
        for key, value in (result.get("changed") or {}).items():
            changed_counts[key] += value

    def enqueue(executor, fn, *args):
        pending_futures.add(executor.submit(fn, *args))
        while len(pending_futures) >= MAX_PENDING_ITEMS:
            done, remaining = wait(pending_futures, return_when=FIRST_COMPLETED)
            pending_futures.clear()
            pending_futures.update(remaining)
            for future in done:
                consume_future(future)

    try:
        _require_enabled()
        libraries = client.libraries()
        if progress:
            progress(3, "Found %d Silo libraries" % len(libraries))
        library_total = max(1, len(libraries))

        with ThreadPoolExecutor(max_workers=MAX_SYNC_WORKERS) as executor:
            for library_index, library in enumerate(libraries):
                if progress:
                    progress(
                        3 + int(80 * library_index / library_total),
                        "Scanning library %d of %d: %s"
                        % (
                            library_index + 1, len(libraries),
                            library.get("name") or library.get("title") or "Library",
                        ),
                    )
                library_id = str(library.get("id") or library.get("library_id") or "")
                if not library_id:
                    continue
                library_name = str(library.get("name") or library.get("title") or "Library")
                library_info_by_id[library_id] = library
                library_segment = _safe_component(library_name, "Library") + "_" + _short_hash(library_id, 6)
                series_cards_by_library[library_id] = {}
                changed_series_by_library[library_id] = set()

                for requested_type in ("movie", "series", "episode"):
                    _require_enabled()
                    cursor = None
                    while True:
                        _require_enabled()
                        page_items, cursor = client.catalog_page(
                            library_id, cursor=cursor, limit=200,
                            media_type=requested_type,
                        )
                        if not isinstance(page_items, list):
                            raise ValueError(
                                "Silo catalogue returned an invalid %s page for library %s"
                                % (requested_type, library_id)
                            )
                        for raw_card in page_items:
                            content_id = _content_id(raw_card)
                            if not content_id:
                                continue
                            card = dict(raw_card)
                            card["type"] = requested_type
                            identity = item_identity(library_id, content_id)
                            fingerprint = card_fingerprint(card)

                            if requested_type == "movie":
                                sync_counts["movies"] += 1
                                media_type = "movie"
                            elif requested_type == "series":
                                sync_counts["shows"] += 1
                                media_type = "series"
                                series_cards_by_library[library_id][content_id] = card
                            else:
                                sync_counts["episodes"] += 1
                                media_type = "episode"

                            changed = item_needs_write(
                                identity, library_id, media_type, fingerprint, card
                            )
                            if requested_type == "series" and changed:
                                changed_series_by_library[library_id].add(content_id)

                            if requested_type != "episode":
                                if changed:
                                    enqueue(
                                        executor, process_item, card, library_id,
                                        library_segment, media_type, fingerprint,
                                    )
                                continue

                            parent = _value(card, "series_id", "parent_id", "show_id")
                            if not parent and isinstance(card.get("series"), dict):
                                parent = _content_id(card["series"])
                            if not parent:
                                raise ValueError(
                                    "Episode %s has no series_id in the Silo catalogue"
                                    % content_id
                                )

                            # If the parent show changed, its title may also
                            # appear in episode NFOs, so refresh those episodes.
                            if not changed and str(parent) not in changed_series_by_library[library_id]:
                                continue

                            try:
                                season_number = int(
                                    card.get("season_number", card.get("season", 0)) or 0
                                )
                            except (TypeError, ValueError):
                                season_number = 0
                            group_key = (library_id, str(parent), season_number)
                            group = changed_episode_groups.setdefault(group_key, [])
                            group.append({
                                "identity": identity,
                                "content_id": content_id,
                                "fingerprint": fingerprint,
                                "card": card,
                            })

                        if not cursor:
                            break

            # Expand only seasons containing new/changed episodes.
            for (library_id, series_id, season_number), summaries in changed_episode_groups.items():
                library = library_info_by_id.get(library_id) or {}
                library_name = str(library.get("name") or library.get("title") or "Library")
                library_segment = _safe_component(library_name, "Library") + "_" + _short_hash(library_id, 6)
                enqueue(
                    executor, process_episode_group, library_id, library_segment,
                    series_id, season_number, summaries,
                )

            while pending_futures:
                done, remaining = wait(pending_futures, return_when=FIRST_COMPLETED)
                pending_futures.clear()
                pending_futures.update(remaining)
                for future in done:
                    consume_future(future)

        movie_count = sync_counts["movies"]
        show_count = sync_counts["shows"]
        episode_count = sync_counts["episodes"]

        # Rebuild the manifest file list only from items still present in the
        # successfully enumerated catalogs. This allows safe deletion and keeps
        # files/art refs for unchanged items without rewriting them.
        files = set()
        new_art_refs = {}
        for entry in current_item_entries.values():
            files.update(entry.get("files") or [])
            new_art_refs.update(entry.get("art_refs") or {})
    except Exception:
        # Never prune after any failed typed catalogue page or worker operation.
        raise

    _require_enabled()
    if progress:
        progress(94, "Reconciling removed entries…")
    removals = _remove_stale(root, previous_files, files)
    if removals:
        touched.update("Movies" if path.startswith("Movies/") else "TV Shows" for path in removals)

    old_pending = set(previous.get("needs_scan") or [])
    pending = old_pending | touched
    scan_started = False
    clean_categories = set("Movies" if p.startswith("Movies/") else "TV Shows" for p in removals)

    for category, path in (("Movies", movies_root), ("TV Shows", shows_root)):
        if category not in pending:
            continue
        if not _is_video_source(path):
            continue
        if _scan_category(path, remove_missing=(category in clean_categories)):
            scan_started = True
            pending.discard(category)

    _require_enabled()
    if progress:
        progress(97, "Saving sync state…")
    _write_text(
        manifest_path,
        json.dumps({
            "version": MANIFEST_VERSION,
            "scope": scope_key,
            "server": server,
            "username": username,
            "profile_id": profile_id,
            "files": sorted(files),
            "items": current_item_entries,
            "art_refs": new_art_refs,
            "needs_scan": sorted(pending),
            "last_sync": int(time.time()),
        }, indent=2, sort_keys=True),
    )

    if _integration_enabled() and touched and pending and not xbmcvfs.exists(_join(root, ".source-reminder-shown")):
        xbmcgui.Dialog().notification(
            "Silo library integration",
            "Add the generated Movies and TV Shows folders as Kodi video sources to finish setup.",
            xbmcgui.NOTIFICATION_INFO,
            10000,
        )
        _write_text(_join(root, ".source-reminder-shown"), "shown")

    if scan_started and monitor is not None:
        _wait_for_scan(monitor)
        _refresh_changed_nfo(refresh_paths)

    if progress:
        progress(100, "Library sync completed")
    return {
        "movies": movie_count,
        "shows": show_count,
        "episodes": episode_count,
        "removed": len(removals),
        "scanned": scan_started,
        "pending_scan": sorted(pending),
    }
