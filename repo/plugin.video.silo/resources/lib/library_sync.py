"""Generate Kodi-native STRM/NFO entries from the active Silo profile.

Only files inside this add-on's managed native-library directory are written or
removed. The source folders themselves are user-added to Kodi once, with
Local information only selected as their content provider.
"""

import hashlib
import json
import os
import re
import time
from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape

import xbmc
import xbmcgui
import xbmcvfs

from resources.lib.silo import log


ADDON_ID = "plugin.video.silo"
MEDIA_ROOT = "special://profile/addon_data/plugin.video.silo/native-library"
MANIFEST_VERSION = 1
MAX_ART_BYTES = 20 * 1024 * 1024


def _translated(path):
    return xbmcvfs.translatePath(path)


def _join(root, *parts):
    return os.path.join(root, *parts)


def _ensure_dir(path):
    if xbmcvfs.exists(path):
        return True
    xbmcvfs.mkdirs(path)
    if not xbmcvfs.exists(path):
        raise IOError("Kodi could not create directory: %s" % path)
    return True


def native_library_paths(special=True):
    """Return the managed folder paths in Kodi-special or local form."""
    if special:
        root = MEDIA_ROOT
        return {
            "root": root,
            "movies": root.rstrip("/") + "/Movies",
            "tvshows": root.rstrip("/") + "/TV Shows",
        }
    root = _translated(MEDIA_ROOT)
    return {
        "root": root,
        "movies": _join(root, "Movies"),
        "tvshows": _join(root, "TV Shows"),
    }


def ensure_library_directories():
    """Create the managed directories, even when no account is signed in."""
    paths = native_library_paths(special=False)
    for path in paths.values():
        _ensure_dir(path)
    log(
        "Native library folders ready: Movies=%s; TV Shows=%s"
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
    parent = os.path.dirname(path)
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
    parent = os.path.dirname(path)
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
    thumbs = ""
    if art.get("poster"):
        thumbs += _xml_tag("thumb", art["poster"])
    else:
        poster = _value(item, "poster_url", "poster", "image", "artwork", "thumbnail")
        if poster:
            thumbs += _xml_tag("thumb", poster)
    fanart = ""
    if art.get("fanart"):
        fanart = "<fanart>%s</fanart>" % _xml_tag("thumb", art["fanart"])
    else:
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
        ("poster_url", "poster", "image", "artwork", "thumbnail")
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
    try:
        absolute = client.abs_url(source)
        canonical = _canonical_url(absolute)
        key_prefix = os.path.relpath(folder, _translated(MEDIA_ROOT)).replace("\\", "/")
        ref_key = key_prefix + "/" + prefix
        previous = old_art_refs.get(ref_key) or ""
        if previous == canonical:
            for rel in (previous_files or set()):
                if rel.startswith(key_prefix + "/" + prefix + "."):
                    candidate = _join(_translated(MEDIA_ROOT), rel.replace("/", os.sep))
                    if xbmcvfs.exists(candidate):
                        new_art_refs[ref_key] = canonical
                        files.add(rel)
                        return os.path.basename(candidate)

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
        rel = os.path.relpath(path, _translated(MEDIA_ROOT)).replace("\\", "/")
        files.add(rel)
        new_art_refs[ref_key] = canonical
        return os.path.basename(path)
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
                 previous_files=None):
    content_id = _content_id(item)
    if not content_id:
        return False

    item_hash = _short_hash(content_id)
    title = str(_value(item, "title", "name") or content_id)
    art = {}
    folder = base_folder

    if item_kind == "movie":
        folder = _join(base_folder, "movie_" + item_hash)
        filename = "movie_" + item_hash
        nfo_tag = "movie"
    elif item_kind == "show":
        folder = _join(base_folder, "show_" + item_hash)
        filename = ""
        nfo_tag = "tvshow"
    else:
        return False

    _ensure_dir(folder)
    art_root = _translated(MEDIA_ROOT)
    art_poster = _download_art(
        client, _art_url(item, "poster"), folder, "poster",
        old_art_refs, new_art_refs, files, previous_files
    )
    if art_poster:
        art["poster"] = art_poster
    art_fanart = _download_art(
        client, _art_url(item, "fanart"), folder, "fanart",
        old_art_refs, new_art_refs, files, previous_files
    )
    if art_fanart:
        art["fanart"] = art_fanart

    if item_kind == "movie":
        strm_path = _join(folder, filename + ".strm")
        nfo_path = _join(folder, filename + ".nfo")
        uri = _plugin_url(content_id, library_id) + "\n"
        nfo = _nfo(item, nfo_tag, content_id, art)
        for path, value in ((strm_path, uri), (nfo_path, nfo)):
            if _write_text(path, value):
                refresh_paths.add(path)
            files.add(os.path.relpath(path, art_root).replace("\\", "/"))
    else:
        nfo_path = _join(folder, "tvshow.nfo")
        nfo = _nfo(item, nfo_tag, content_id, art)
        if _write_text(nfo_path, nfo):
            refresh_paths.add(nfo_path)
        files.add(os.path.relpath(nfo_path, art_root).replace("\\", "/"))

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
    poster = _download_art(
        client, _art_url(episode, "poster"), season_folder, "thumb_" + item_hash,
        old_art_refs, new_art_refs, files, previous_files
    )
    if poster:
        art["poster"] = poster

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
    root = _translated(MEDIA_ROOT)
    files.add(os.path.relpath(strm_path, root).replace("\\", "/"))
    files.add(os.path.relpath(nfo_path, root).replace("\\", "/"))
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


def _remove_stale(root, previous_files, current_files):
    removed = []
    for relative in set(previous_files or []) - set(current_files or []):
        if not _relative_safe(relative):
            continue
        # Only remove generated, known file types and known image names.
        name = os.path.basename(relative).lower()
        allowed = (
            name.endswith((".strm", ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))
            and (name.startswith(("movie_", "show_", "s", "poster.", "fanart.", "thumb_"))
                 or name == "tvshow.nfo")
        )
        if not allowed:
            continue
        full_path = _join(root, *relative.split("/"))
        if xbmcvfs.exists(full_path) and xbmcvfs.delete(full_path):
            removed.append(relative)
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


def _scan_category(path, remove_missing=False):
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
    paths = set(os.path.normcase(os.path.normpath(p)) for p in paths or [])
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
    """Sync active Silo libraries to persistent STRM/NFO files. Return stats.

    Directories are created before authentication is checked so users can
    configure Kodi's video sources as part of first-time setup.
    """
    paths = ensure_library_directories()
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

    try:
        libraries = client.libraries()
        if progress:
            progress(3, "Found %d Silo libraries" % len(libraries))
        library_total = max(1, len(libraries))
        for library_index, library in enumerate(libraries):
            if progress:
                progress(
                    3 + int(88 * library_index / library_total),
                    "Syncing library %d of %d: %s"
                    % (
                        library_index + 1,
                        len(libraries),
                        library.get("name") or library.get("title") or "Library",
                    ),
                )
            library_id = library.get("id") or library.get("library_id")
            library_name = str(library.get("name") or library.get("title") or "Library")
            if library_id in (None, ""):
                continue
            library_segment = _safe_component(library_name, "Library") + "_" + _short_hash(library_id, 6)
            cursor = None
            while True:
                items, cursor = client.catalog_page(str(library_id), cursor=cursor, limit=200)
                for item in items or []:
                    media_type = _media_type(item)
                    content_id = _content_id(item)
                    if not content_id:
                        continue

                    if media_type in ("movie", "video"):
                        target = _join(movies_root, scope_key, library_segment)
                        before = len(files)
                        _write_entry(
                            client, item, str(library_id), target, "movie",
                            old_art_refs, new_art_refs, files, refresh_paths,
                            previous_files,
                        )
                        movie_count += 1
                        touched.add("Movies")
                    elif media_type in ("series", "tvshow", "show"):
                        target = _join(shows_root, scope_key, library_segment)
                        if _write_entry(
                            client, item, str(library_id), target, "show",
                            old_art_refs, new_art_refs, files, refresh_paths,
                            previous_files,
                        ):
                            show_count += 1
                            touched.add("TV Shows")
                        series_hash = _short_hash(content_id)
                        series_folder = _join(target, "show_" + series_hash)
                        for season in client.seasons(content_id, library_id=str(library_id)) or []:
                            season_number = season.get("season_number", season.get("number", 0))
                            try:
                                season_number = int(season_number or 0)
                            except (TypeError, ValueError):
                                season_number = 0
                            for episode in client.episodes(content_id, season_number, library_id=str(library_id)) or []:
                                if _write_episode(
                                    client, episode, item, str(library_id), series_folder,
                                    season_number, old_art_refs, new_art_refs, files, refresh_paths,
                                    previous_files,
                                ):
                                    episode_count += 1
                                    touched.add("TV Shows")
                if not cursor:
                    break
    except Exception:
        # An incomplete API traversal must never prune files, because an empty
        # partial response could otherwise look like a mass deletion.
        raise

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
            "art_refs": new_art_refs,
            "needs_scan": sorted(pending),
            "last_sync": int(time.time()),
        }, indent=2, sort_keys=True),
    )

    if touched and pending and not xbmcvfs.exists(_join(root, ".source-reminder-shown")):
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
