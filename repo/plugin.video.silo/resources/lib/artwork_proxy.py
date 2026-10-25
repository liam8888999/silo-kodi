"""Serve authenticated Silo artwork to Kodi's native library over localhost.

Kodi's local NFO files cannot attach the add-on's Bearer/profile headers to an
ordinary remote image URL. This tiny loopback-only proxy keeps credentials in
the add-on and lets Kodi fetch artwork with a normal HTTP URL. Tokens are never
written to NFOs or included in the proxy URL.
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

import xbmc
import xbmcaddon

from resources.lib.silo import SiloClient, log


HOST = "127.0.0.1"
PORT = 17653
BASE_URL = "http://%s:%d" % (HOST, PORT)
MAX_ART_BYTES = 20 * 1024 * 1024

_SERVER = None
_SERVER_LOCK = threading.Lock()

# Query-string credentials must never be persisted in Kodi's database/NFOs.
_SECRET_QUERY_KEYS = {
    "token", "access_token", "refresh_token", "profile_token",
    "authorization", "auth", "api_key", "apikey", "signature", "sig",
    "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "x-goog-signature",
}


def _normalise_server(value):
    parts = urlsplit(str(value or "").strip().rstrip("/"))
    return (
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
    )


def _clean_query(query):
    pairs = parse_qsl(query, keep_blank_values=True)
    safe = [(key, value) for key, value in pairs
            if key.lower() not in _SECRET_QUERY_KEYS]
    return urlencode(safe, doseq=True)


def _safe_direct_url(source):
    """Allow public HTTP(S) CDN URLs, but never retain signed/token URLs."""
    parts = urlsplit(str(source or ""))
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(key.lower() in _SECRET_QUERY_KEYS for key, _ in pairs):
        return ""
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(pairs, doseq=True), ""
    ))


def make_artwork_url(source, server, username, profile_id):
    """Return a stable local proxy URL for same-server artwork.

    Public external image URLs stay direct. Authenticated Silo URLs contain
    only the server identity and path in this URL-safe payload, never tokens.
    """
    source = str(source or "").strip()
    server = str(server or "").strip().rstrip("/")
    if not source or not server:
        return ""

    parsed_source = urlsplit(source)
    if parsed_source.scheme.lower() in ("http", "https") and parsed_source.netloc:
        server_parts = urlsplit(server)
        if (parsed_source.scheme.lower(), parsed_source.netloc.lower()) != (
            server_parts.scheme.lower(), server_parts.netloc.lower()
        ):
            return _safe_direct_url(source)
        path = parsed_source.path or "/"
        query = parsed_source.query
    elif source.startswith("//"):
        # Protocol-relative URLs are not safe to treat as authenticated routes.
        return _safe_direct_url("https:" + source)
    else:
        parsed_source = urlsplit(source)
        path = parsed_source.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        query = parsed_source.query

    if not path.startswith("/") or path.startswith("//"):
        return ""
    if "\\" in path or any(part in (".", "..") for part in path.split("/")):
        return ""

    payload = {
        "server": server,
        "username": str(username or ""),
        "profile_id": str(profile_id or ""),
        "path": path,
        "query": _clean_query(query),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return BASE_URL + "/artwork/" + token


def _decode_payload(token):
    padding = "=" * ((4 - len(token) % 4) % 4)
    raw = base64.urlsafe_b64decode((token + padding).encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid artwork payload")
    path = str(payload.get("path") or "")
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(part in (".", "..") for part in path.split("/"))
        or not str(payload.get("server") or "")
    ):
        raise ValueError("Invalid artwork path")
    # URLs must not carry credentials; the proxy adds the current Silo headers.
    query = str(payload.get("query") or "")
    if any(key.lower() in _SECRET_QUERY_KEYS
           for key, _ in parse_qsl(query, keep_blank_values=True)):
        raise ValueError("Artwork URL contains a credential")
    return payload, path + (("?" + query) if query else "")


def _sniff_image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return ""


class _ArtworkHandler(BaseHTTPRequestHandler):
    server_version = "SiloArtworkProxy/1"

    def log_message(self, fmt, *args):
        # Do not write artwork URLs, encoded route data, or request metadata to
        # Kodi's HTTP server log.
        return

    def _send(self, status, content_type, data=b"", send_body=True, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if send_body and data:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        self._serve(send_body=False)

    def do_GET(self):
        self._serve(send_body=True)

    def _serve(self, send_body=True):
        request_path = urlsplit(self.path).path
        if request_path == "/health":
            self._send(200, "text/plain; charset=utf-8", b"ok", send_body, "no-store")
            return
        prefix = "/artwork/"
        if not request_path.startswith(prefix):
            self._send(404, "text/plain; charset=utf-8", b"not found", send_body)
            return

        try:
            payload, target_path = _decode_payload(request_path[len(prefix):])
        except Exception:
            self._send(400, "text/plain; charset=utf-8", b"invalid artwork request", send_body)
            return

        client = SiloClient()
        if (
            _normalise_server(payload.get("server")) != _normalise_server(client.base)
            or str(payload.get("username") or "").casefold()
                != str(client.cfg.get("username") or "").casefold()
            or str(payload.get("profile_id") or "")
                != str(client.cfg.get("profile_id") or "")
        ):
            # The NFO must not make the current account's credentials available
            # to a different server or profile. Switch to the matching Silo
            # server/profile in the add-on to access its library artwork.
            self._send(403, "text/plain; charset=utf-8", b"select matching Silo profile", send_body)
            return

        addon = xbmcaddon.Addon()
        if not (
            addon.getSetting("token")
            or addon.getSetting("refresh_token")
        ):
            self._send(503, "text/plain; charset=utf-8", b"Silo sign-in required", send_body)
            return

        try:
            response = client._send(
                "GET",
                target_path,
                timeout=25,
                extra_headers={
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                },
            )
            data = response.content or b""
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not data:
                self._send(404, "text/plain; charset=utf-8", b"empty artwork", send_body)
                return
            if len(data) > MAX_ART_BYTES:
                self._send(413, "text/plain; charset=utf-8", b"artwork too large", send_body)
                return
            if not content_type.startswith("image/"):
                content_type = _sniff_image_type(data)
            if not content_type.startswith("image/"):
                self._send(502, "text/plain; charset=utf-8", b"upstream did not return an image", send_body)
                return
            # Kodi performs its own artwork caching; the URL remains stable and
            # contains no access token.
            self._send(200, content_type, data, send_body, "public, max-age=3600")
        except Exception as exc:
            log("Native library artwork proxy request failed: %s" % type(exc).__name__, xbmc.LOGDEBUG)
            self._send(502, "text/plain; charset=utf-8", b"artwork unavailable", send_body)


def start_artwork_proxy():
    """Start the loopback proxy, or reuse an already-running service instance."""
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is not None:
            return True
        try:
            server = ThreadingHTTPServer((HOST, PORT), _ArtworkHandler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="SiloArtworkProxy",
                daemon=True,
            )
            thread.start()
            _SERVER = server
            log("Native library artwork proxy listening on %s:%d" % (HOST, PORT), xbmc.LOGINFO)
            return True
        except OSError:
            # The addon plugin and service may be separate Python contexts. If
            # the service already owns the port, a tiny health request confirms
            # that it is the expected Silo proxy.
            try:
                with urlopen(BASE_URL + "/health", timeout=1) as response:
                    if response.status == 200 and response.read(16) == b"ok":
                        return True
            except Exception:
                pass
            log("Native library artwork proxy could not bind its loopback port", xbmc.LOGERROR)
            return False
