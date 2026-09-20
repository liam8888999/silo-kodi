"""Silo /api/v2 client for Kodi.

This module is responsible for communicating with the Silo Server API.
Kodi UI/playback decisions live in main.py so the two responsibilities stay
separate and the code remains easier to maintain.

Important playback behaviour:
    * Silo is the source of truth for resume position.
    * main.py performs a fresh progress lookup immediately before playback.
    * The selected resume position is sent to POST /api/v2/playback/start as
      start_position so Kodi does not have to perform a second seek.
    * Kodi -> Silo progress is reported throughout playback.

Pagination is deliberately handled inside this client. Kodi never receives
or displays a "Next Page" item. The catalog and progress requests use the
maximum page size documented by the current Silo API where supported, reducing
the number of HTTP round trips for large libraries.

Endpoints used by this addon:
    POST /api/v2/auth/login
    POST /api/v2/auth/refresh
    GET  /api/v2/profiles
    POST /api/v2/profiles/{id}/verify-pin
    GET  /api/v2/user/libraries
    GET  /api/v2/catalog
    GET  /api/v2/catalog/series/{id}/seasons[/{n}/episodes]
    GET  /api/v2/catalog/items/{id}/versions
    GET  /api/v2/progress
    GET  /api/v2/playback/capabilities
    POST /api/v2/playback/start
    POST /api/v2/playback/{sid}/progress
    DELETE /api/v2/playback/{sid}
"""

# Standard library modules used for configuration, UUID generation and URL handling.
import json
import os
import uuid
from urllib.parse import quote

# requests is used for all API communication made by the addon itself.
import requests
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs


# Read addon metadata and determine where Kodi should store persistent configuration.
ADDON = xbmcaddon.Addon()
ADDON_VERSION = ADDON.getAddonInfo("version")
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
CONFIG_PATH = os.path.join(PROFILE_DIR, "config.json")


# Central logging helper so every log line identifies this addon.
def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[plugin.video.silo] %s" % msg, level)


# Load saved server/login/device/profile settings from Kodi's addon profile.
def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# Save configuration changes such as tokens, profile selection and playback settings.
def save_config(cfg):
    xbmcvfs.mkdirs(PROFILE_DIR)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


# Custom exception used for errors that should be shown/logged by Kodi.
class SiloError(Exception):
    def __init__(self, msg, status=None, problem=None, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.problem = problem or {}
        self.retry_after = retry_after


# Playback-start fields whose accepted strings are not fully described by the
# OpenAPI schema. If Silo returns a validation error for one of these fields,
# the addon tries the next known candidate automatically.
_VOCAB = {
    "subtitle_fidelity_preference": (
        ("subtitle_fidelity_preference",),
        [
            "preserve", "auto", "prefer_fidelity", "fidelity", "native",
            "prefer_native", "exact", "compatible", "compatibility", "balanced",
            "best_effort", "any", "default", "none", "off", "sidecar", "text",
            "convert", "original", "high", "strict", "lossless", ""
        ]
    ),
    "quality_preference": (
        ("quality_preference",),
        ["original", "auto", "1080p", "2160p", "720p", "direct", ""]
    ),
    "video_evidence": (
        ("client_capabilities", "video_evidence"),
        [
            "declared", "probed", "reported", "measured", "observed",
            "platform", "api", "runtime", "static", "assumed", "heuristic",
            "inferred", "unknown", "none", ""
        ]
    ),
    "audio_evidence": (
        ("client_capabilities", "audio_evidence"),
        [
            "declared", "probed", "reported", "measured", "observed",
            "platform", "api", "runtime", "static", "assumed", "heuristic",
            "inferred", "unknown", "none", ""
        ]
    ),
    "form_factor": (
        ("client_playback_context", "form_factor"),
        [
            "desktop", "tv", "phone", "tablet", "web", "laptop", "set_top_box",
            "console", "other", "unknown", "stb", "htpc"
        ]
    ),
}


# Set a nested dictionary value, creating missing dictionaries along the way.
def _set_path(obj, path, value):
    for key in path[:-1]:
        obj = obj.setdefault(key, {})
    obj[path[-1]] = value


# Merge nested playback overrides without replacing unrelated configuration fields.
def _deep_merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class SiloClient:
    """Small authenticated client for Silo's v2 API."""

    # Create the API client, load saved settings and create a stable Kodi device ID.
    def __init__(self):
        self.cfg = load_config()

        if not self.cfg.get("device_id"):
            self.cfg["device_id"] = "kodi-" + uuid.uuid4().hex[:16]
            save_config(self.cfg)

        self.session = requests.Session()
        self._caps = None
        self.sync_settings()
        # Detail responses are reused when the same item is later played.
        self._details = {}

    # ------------------------------------------------------------ settings

    def sync_settings(self):
        """Sync editable Kodi connection settings into the saved config."""
        server = ADDON.getSetting("server").strip().rstrip("/")
        username = ADDON.getSetting("username").strip()
        profile = ADDON.getSetting("profile").strip()

        changed = (
            server != self.cfg.get("server", "")
            or username != self.cfg.get("username", "")
            or profile != self.cfg.get("profile_name", "")
        )

        if changed:
            for key in ("token", "refresh_token", "profile_id", "profile_token"):
                self.cfg.pop(key, None)
            self._caps = None

        if server:
            self.cfg["server"] = server
        else:
            self.cfg.pop("server", None)
        if username:
            self.cfg["username"] = username
        else:
            self.cfg.pop("username", None)
        if profile:
            self.cfg["profile_name"] = profile
        else:
            self.cfg.pop("profile_name", None)

        save_config(self.cfg)

    # ------------------------------------------------------------ helpers

    # Base server URL used for every Silo API request.
    @property
    def base(self):
        return self.cfg.get("server", "").rstrip("/")

    # Convert relative Silo URLs such as /api/... into absolute URLs.
    def abs_url(self, url):
        if not url:
            return ""
        return self.base + url if url.startswith("/") else url

    # Build authentication/client headers required by Silo API endpoints.
    def _headers(self):
        h = {
            "Accept": "application/json",
            "X-Device-ID": self.cfg["device_id"],
            "X-Client-Name": "kodi-silo",
            "X-Client-Version": ADDON_VERSION,
            "X-Client-Platform": "kodi",
        }

        if self.cfg.get("token"):
            h["Authorization"] = "Bearer " + self.cfg["token"]

        if self.cfg.get("profile_id"):
            h["X-Profile-Id"] = str(self.cfg["profile_id"])

        if self.cfg.get("profile_token"):
            h["X-Profile-Token"] = self.cfg["profile_token"]

        return h

    @staticmethod
    def _problem(r):
        """Create a readable message from a Silo problem+json response."""
        try:
            p = r.json()
        except ValueError:
            return "HTTP %s: %s" % (r.status_code, (r.text or r.reason)[:300])

        errors = p.get("errors") or []
        fields = [
            "%s [%s] %s" % (
                e.get("location", "?"),
                e.get("code", ""),
                e.get("detail", ""),
            )
            for e in errors[:8]
        ]

        head = p.get("detail") or p.get("title") or r.reason

        return "HTTP %s: %s%s" % (
            r.status_code,
            head,
            ("\n" + "\n".join(fields)) if fields else "",
        )

    # Send an authenticated API request and handle access-token/profile-token retries.
    def _send(self, method, path, params=None, body=None, need_profile=True, retry=True):
        if not self.base:
            self._prompt_account()

        if not self.cfg.get("token"):
            self.login()

        if need_profile and not self.cfg.get("profile_id"):
            self.select_profile()

        try:
            r = self.session.request(
                method,
                self.base + path,
                headers=self._headers(),
                params=params,
                json=body,
                timeout=30,
            )
        except requests.RequestException as e:
            raise SiloError("Cannot reach server: %s" % e)

        # A 401 may mean the access token has expired. Refresh it and retry once.
        if r.status_code == 401 and retry:
            if not self.refresh():
                self.cfg.pop("token", None)
                self.cfg.pop("refresh_token", None)
                save_config(self.cfg)
                self.login()

            return self._send(method, path, params, body, need_profile, False)

        # A locked profile may need a fresh profile-verification token.
        if r.status_code == 403 and retry and "profile_verification" in r.text:
            self.cfg.pop("profile_token", None)
            self.verify_profile(self.cfg.get("profile_id"))
            return self._send(method, path, params, body, need_profile, False)

        if not r.ok:
            log(
                "%s %s -> %s" % (method, path, r.text[:1200]),
                xbmc.LOGWARNING,
            )

            try:
                problem = r.json()
            except ValueError:
                problem = {}

            retry_after = r.headers.get("Retry-After")
            raise SiloError(
                self._problem(r),
                r.status_code,
                problem,
                retry_after,
            )

        return r

    # Convenience wrapper that sends a request and decodes the JSON response.
    def _json(self, *args, **kwargs):
        r = self._send(*args, **kwargs)
        return r.json() if r.content else None

    # --------------------------------------------------------------- auth

    # Ask for the server and username the first time the addon is configured.
    def _prompt_account(self):
        dlg = xbmcgui.Dialog()

        server = dlg.input(
            "Silo server URL (e.g. http://host:8090)",
            defaultt="http://",
        )

        if not server:
            raise SiloError("Login cancelled")

        user = dlg.input(
            "Silo username",
            defaultt="",
        )

        if not user:
            raise SiloError("Login cancelled")

        # A '#' is optional. Without it, retain the normal profile-selection
        # dialog. With it, use the part before '#' as the account username and
        # the part after '#' as the profile name.
        if "#" in user:
            username, profile_name = user.split("#", 1)
            username = username.strip()
            profile_name = profile_name.strip()

            if not username or not profile_name:
                raise SiloError(
                    "Use username#profile, for example liam1#liam2"
                )

            self.cfg["username"] = username
            self._requested_profile_name = profile_name
        else:
            self.cfg["username"] = user.strip()
            self._requested_profile_name = self.cfg.get("profile_name", "")

        self.cfg["server"] = server.rstrip("/")
        save_config(self.cfg)

    # Store the access/refresh token pair returned by Silo.
    def _store_tokens(self, data):
        self.cfg["token"] = data["access_token"]
        self.cfg["refresh_token"] = data["refresh_token"]
        save_config(self.cfg)

    # Authenticate directly with /auth/login.
    #
    # This deliberately does NOT call _send(), because _send() calls login()
    # when a token is missing. Calling _send() here would recurse forever.
    def login(self):
        if not self.base or not self.cfg.get("username"):
            self._prompt_account()

        pw = xbmcgui.Dialog().input(
            "Password for %s" % self.cfg["username"],
            option=xbmcgui.ALPHANUM_HIDE_INPUT,
        )

        if not pw:
            raise SiloError("Login cancelled")

        try:
            r = self.session.post(
                self.base + "/api/v2/auth/login",
                timeout=20,
                headers={
                    "Accept": "application/json",
                    "X-Device-ID": self.cfg["device_id"],
                    "X-Client-Name": "kodi-silo",
                    "X-Client-Version": ADDON_VERSION,
                    "X-Client-Platform": "kodi",
                },
                json={
                    "username": self.cfg["username"],
                    "password": pw,
                    "provider": "",
                },
            )
        except requests.RequestException as e:
            raise SiloError("Cannot reach server: %s" % e)

        if not r.ok:
            raise SiloError("Login failed - " + self._problem(r))

        self._store_tokens(r.json())

    # Complete interactive login used by the Kodi Login button.
    #
    # This intentionally performs the whole initial-login sequence in one
    # place: server/username/password, token storage, profile selection and
    # profile PIN verification when required.
    def login_full(self):
        # Every explicit Kodi login starts as a completely fresh attempt.
        # Do not reuse a previously entered server, username, token or profile
        # after a failed/cancelled login; this ensures the next attempt always
        # starts at the server URL prompt.
        for key in (
            "server",
            "username",
            "token",
            "refresh_token",
            "profile_id",
            "profile_token",
        ):
            self.cfg.pop(key, None)

        self._caps = None
        self._requested_profile_name = ""
        save_config(self.cfg)

        try:
            # login() now asks for server URL, username and password from
            # scratch because no account fields were retained above.
            self.login()

            # login() only authenticates the account. Select the household
            # profile afterwards so subsequent profile-scoped API calls have
            # everything they need.
            self.select_profile()

        except SiloError:
            # A bad password, unknown user/server, cancelled prompt, cancelled
            # profile selection, or cancelled PIN must leave no partial login
            # state behind. The next Login selection will start at server URL.
            for key in (
                "server",
                "username",
                "token",
                "refresh_token",
                "profile_id",
                "profile_token",
            ):
                self.cfg.pop(key, None)

            self._caps = None
            self._requested_profile_name = ""
            save_config(self.cfg)
            raise

    # Exchange the saved refresh token for a new access token.
    def refresh(self):
        rt = self.cfg.get("refresh_token")
        if not rt or not self.base:
            return False

        try:
            r = self.session.post(
                self.base + "/api/v2/auth/refresh",
                headers={
                    "Accept": "application/json",
                    "X-Device-ID": self.cfg["device_id"],
                    "X-Client-Name": "kodi-silo",
                    "X-Client-Version": ADDON_VERSION,
                    "X-Client-Platform": "kodi",
                },
                json={"refresh_token": rt},
                timeout=20,
            )
        except requests.RequestException:
            return False

        if not r.ok:
            return False

        self._store_tokens(r.json())
        return True

    # Fully log the account out locally.
    #
    # Server and username are deliberately removed as well as the tokens and
    # profile information. This makes the next use a completely fresh login,
    # allowing the user to enter a different server and/or username instead of
    # being forced to reuse the previous account. The stable Kodi device ID is
    # intentionally retained so Silo still recognises this Kodi installation
    # as the same device after logging into another account.
    def logout(self):
        for key in (
            "server",
            "username",
            "token",
            "refresh_token",
            "profile_id",
            "profile_token",
        ):
            self.cfg.pop(key, None)

        # Playback capabilities can be profile/account dependent, so discard
        # the cached copy and fetch it again after the next login.
        self._caps = None
        save_config(self.cfg)

    # ----------------------------------------------------------- profiles

    # Load household profiles and remember the selected profile.
    def select_profile(self):
        data = self._json(
            "GET",
            "/api/v2/profiles",
            need_profile=False,
        )

        profiles = (data or {}).get("items", [])

        if not profiles:
            raise SiloError("This account has no profiles")

        requested_name = str(
            getattr(self, "_requested_profile_name", "")
            or self.cfg.get("profile_name", "")
            or ""
        ).strip()

        if requested_name:
            chosen = next(
                (
                    profile
                    for profile in profiles
                    if str(profile.get("name", "")).strip().casefold()
                    == requested_name.casefold()
                ),
                None,
            )

            if chosen is None:
                raise SiloError(
                    "Profile '%s' was not found. Use username#profile."
                    % requested_name
                )
        elif len(profiles) == 1:
            chosen = profiles[0]
        else:
            idx = xbmcgui.Dialog().select(
                "Who's watching?",
                [p.get("name", "Profile") for p in profiles],
            )

            if idx < 0:
                raise SiloError("No profile selected")

            chosen = profiles[idx]

        self.cfg["profile_id"] = str(chosen["id"])
        self.cfg.pop("profile_token", None)
        save_config(self.cfg)

        if chosen.get("has_pin"):
            self.verify_profile(chosen["id"])

    # Verify a PIN-locked profile and store its temporary verification token.
    def verify_profile(self, profile_id):
        pin = xbmcgui.Dialog().input(
            "Profile PIN",
            type=xbmcgui.INPUT_NUMERIC,
            option=xbmcgui.ALPHANUM_HIDE_INPUT,
        )

        if not pin:
            raise SiloError("PIN required")

        data = self._json(
            "POST",
            "/api/v2/profiles/%s/verify-pin" % profile_id,
            body={"pin": pin},
            need_profile=False,
        )

        if not data or not data.get("valid"):
            raise SiloError("Wrong PIN")

        self.cfg["profile_token"] = data.get("profile_token", "")
        save_config(self.cfg)

    # ------------------------------------------------------------ browsing

    @staticmethod
    def _next(data):
        """Return the next API cursor, or None when the current page is final."""
        page = (data or {}).get("page") or {}
        return page.get("next_cursor") if page.get("has_more") else None

    # Return every library visible to the current profile. API pagination is hidden from Kodi.
    def libraries(self):
        libraries = []
        cursor = None

        while True:
            data = self._json(
                "GET",
                "/api/v2/user/libraries",
                params={"cursor": cursor} if cursor else None,
            ) or {}

            libraries.extend(data.get("items", []))

            cursor = self._next(data)
            if not cursor:
                return libraries


    # Search the profile-visible catalog across all accessible libraries.
    # Silo performs the search server-side, so the addon does not need to
    # download and scan every library itself.
    def search_catalog(self, query, limit=100, offset=0):
        """Return one page of Silo's server-side catalog search results."""
        query = str(query or "").strip()
        if not query:
            return {"items": [], "has_more": False, "total": 0}

        try:
            limit = max(1, min(int(limit or 100), 100))
        except (TypeError, ValueError):
            limit = 100

        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0

        return self._json(
            "GET",
            "/api/v1/catalog",
            params={
                "source": "query",
                "q": query,
                "limit": limit,
                "offset": offset,
                "include_total": "false",
            },
        ) or {}

    # Return every catalog item in a library while handling pagination internally.
    # Silo's current API documents a maximum catalog page size of 200, so use
    # that maximum to reduce the number of HTTP round trips for large libraries.
    def catalog_page(self, library_id, cursor=None, limit=200):
        """Return one Silo catalog page and its continuation cursor.

        Silo's shared catalog limit supports up to 200 items per request.
        Pagination is exposed to the Kodi UI so large libraries do not force
        every item and its extended metadata to load before the first page.
        """
        limit = max(1, min(int(limit or 200), 200))

        params = {
            "library_id": library_id,
            "limit": limit,
            "skip_total": "true",
            # Use practical library artwork sizes while keeping responses small.
            "image_size": "medium",
        }

        if cursor:
            params["cursor"] = cursor

        data = self._json(
            "GET",
            "/api/v2/catalog",
            params=params,
        ) or {}

        return (
            data.get("items", []),
            self._next(data),
        )

    # Return every library item. Kept for callers that explicitly need the
    # complete collection; normal Kodi library browsing uses catalog_page().
    def catalog(self, library_id, limit=200):
        items = []
        cursor = None

        while True:
            page_items, cursor = self.catalog_page(
                library_id,
                cursor=cursor,
                limit=limit,
            )

            items.extend(page_items)

            if not cursor:
                return items

    # Return all seasons for a series.
    def seasons(self, series_id, library_id=None):
        data = self._json(
            "GET",
            "/api/v2/catalog/series/%s/seasons" % quote(series_id, safe=":"),
            params={"library_id": library_id} if library_id else None,
        ) or {}

        return data.get("items", [])

    # Return all episodes for one series season.
    def episodes(self, series_id, season_number, library_id=None):
        data = self._json(
            "GET",
            "/api/v2/catalog/series/%s/seasons/%s/episodes"
            % (quote(series_id, safe=":"), season_number),
            params={"library_id": library_id} if library_id else None,
        ) or {}

        return data.get("items", [])

    # Return the complete detail document for one catalog item.
    #
    # Unlike the browse/catalog card, this endpoint includes cast, crew and
    # full file-version track metadata. Results are cached for the lifetime of
    # this Kodi directory request so play() can reuse the same detail document.
    def item_detail(self, content_id, library_id=None, file_id=None):
        key = (
            str(content_id),
            str(library_id) if library_id is not None else "",
            str(file_id) if file_id is not None else "",
        )

        if key in self._details:
            return self._details[key]

        # The detail endpoint also prepares cast/crew artwork. Kodi only
        # needs small thumbnails for these person images, which keeps the
        # metadata response substantially smaller for large libraries.
        params = {"image_size": "small"}

        if library_id:
            params["library_id"] = library_id
        if file_id:
            params["file_id"] = file_id

        data = self._json(
            "GET",
            "/api/v2/catalog/items/%s" % quote(content_id, safe=":"),
            params=params or None,
        ) or {}

        self._details[key] = data
        return data

    # Return the playable versions/files for one catalog item.
    def versions(self, content_id, library_id=None):
        data = self._json(
            "GET",
            "/api/v2/catalog/items/%s/versions" % quote(content_id, safe=":"),
            params={"library_id": library_id} if library_id else None,
        ) or {}

        return data.get("items", [])

    # ------------------------------------------------------------ progress

    # Retrieve every server-side progress record, hiding pagination from Kodi.
    def progress(self, library_id=None, status=None):
        records = []
        cursor = None

        while True:
            # The current Silo progress endpoint supports up to 200 records
            # per page, which reduces round trips when the fresh playback check
            # scans the server's progress collection.
            params = {"limit": 200}

            if library_id:
                params["library_id"] = library_id

            if status:
                params["status"] = status

            if cursor:
                params["cursor"] = cursor

            data = self._json(
                "GET",
                "/api/v2/progress",
                params=params,
            ) or {}

            records.extend(data.get("items", []))

            cursor = self._next(data)
            if not cursor:
                return records

    # Return only in-progress records for a library.
    #
    # The catalog already tells us which items are fully played. To display
    # accurate partial-watch markers we only need the smaller in_progress subset
    # rather than downloading the entire progress history.
    def in_progress(self, library_id=None):
        return self.progress(
            library_id=library_id,
            status="in_progress",
        )

    # Build a lookup map containing the newest in-progress record for each item.
    def in_progress_map(self, library_id=None):
        result = {}

        for record in self.in_progress(library_id=library_id):
            media_id = record.get("media_item_id")
            if media_id is None:
                continue

            key = str(media_id)
            existing = result.get(key)

            if (
                not existing
                or (record.get("updated_at") or "")
                > (existing.get("updated_at") or "")
            ):
                result[key] = record

        return result

    # Return the newest Silo progress record for one catalog media item.
    def get_progress(self, content_id, library_id=None):
        matches = [
            record
            for record in self.progress(library_id=library_id)
            if str(record.get("media_item_id")) == str(content_id)
        ]

        if not matches:
            return None

        # Silo returns newest-change-first, but sorting explicitly means this
        # method remains correct even if the API ordering changes later.
        matches.sort(
            key=lambda record: record.get("updated_at") or "",
            reverse=True,
        )

        return matches[0]

    # Build a single lookup map containing the newest record for each media item.
    def progress_map(self, library_id=None):
        result = {}

        for record in self.progress(library_id=library_id):
            media_id = record.get("media_item_id")
            if media_id is None:
                continue

            key = str(media_id)
            existing = result.get(key)

            if not existing or (record.get("updated_at") or "") > (existing.get("updated_at") or ""):
                result[key] = record

        return result

    # ------------------------------------------------------------ playback

    # Fetch and cache the playback capabilities advertised by Silo.
    def playback_caps(self):
        if self._caps is None:
            self._caps = self._json(
                "GET",
                "/api/v2/playback/capabilities",
                need_profile=False,
            ) or {}

        return self._caps

    # Identify this Kodi installation in playback requests.
    def _installation_id(self):
        return self.playback_caps().get("installation_id") or self.cfg["device_id"]

    # Pick the highest protocol version supported by both the server metadata and our fallback.
    def _protocol_version(self):
        versions = self.playback_caps().get("protocol_versions") or [3]

        try:
            return max(int(version) for version in versions)
        except (TypeError, ValueError):
            return 3

    # Build a protocol-v3 playback/start request.
    def _start_body(self, file_id, start_position=0.0):
        caps = self.playback_caps()
        pv = self._protocol_version()

        video = ["h264", "hevc", "vp9", "av1", "mpeg2video", "mpeg4", "vc1"]
        audio = ["aac", "ac3", "eac3", "dts", "truehd", "flac", "opus", "mp3", "vorbis", "pcm"]
        containers = ["mkv", "mp4", "avi", "ts", "webm", "mov"]

        # Describe the HTTP/direct-play capability of this Kodi client.
        delivery_template = {
            "enabled": True,
            "supported_on_device": True,
            "containers": containers,
            "video_codecs": video,
            "audio_decode_codecs": audio,
            "audio_passthrough_codecs": [],
            "subtitles": {
                "embedded_text": True,
                "sidecar_text": True,
                "ass_styling": True,
                "embedded_bitmap": True,
                "sidecar_bitmap": True,
                "font_attachments": True,
            },
            "features": [],
            "auth_header_refresh": False,
            "validated_claims": [],
            "transformations": [],
        }

        # The capabilities response can contain a dict of delivery names.
        # Preserve those actual names instead of accidentally iterating a dict
        # as though it were a list of delivery objects.
        server_deliveries = caps.get("deliveries") or {}

        if isinstance(server_deliveries, dict):
            deliveries = {
                name: dict(value) if isinstance(value, dict) else dict(delivery_template)
                for name, value in server_deliveries.items()
            }
        else:
            deliveries = {
                name: dict(delivery_template)
                for name in server_deliveries
            }

        return {
            "installation_id": self._installation_id(),
            "protocol_version": pv,
            "client_features": [],
            "file_id": str(file_id),
            "profile_id": str(self.cfg["profile_id"]),
            "playback_attempt_id": uuid.uuid4().hex,
            "quality_preference": "original",
            # This field is required by the Silo v2 schema. Its actual accepted
            # vocabulary is learned by the probing logic below when necessary.
            "subtitle_fidelity_preference": "preserve",
            "metered": False,
            # This is the IMPORTANT server-side resume position. main.py fills
            # this with the fresh position obtained immediately before playback.
            "start_position": float(start_position),
            "progress_persistence": "server",
            "client_capabilities": {
                "video_evidence": "declared",
                "audio_evidence": "declared",
                "codecs_video": video,
                "codecs_video_hardware": [],
                "codecs_audio": audio,
                "containers": containers,
                "hdr": True,
                "max_resolution": "2160p",
            },
            "client_playback_context": {
                "protocol_version": pv,
                "form_factor": "desktop",
                "app_version": ADDON_VERSION,
                "device": {
                    "platform": "kodi",
                    "manufacturer": "",
                    "model": "",
                    "os_version": xbmc.getInfoLabel("System.OSVersionInfo"),
                },
                "output": {},
                "deliveries": deliveries,
            },
        }

    # Find playback fields mentioned by Silo's validation error.
    @staticmethod
    def _invalid_fields(err):
        errors = err.problem.get("errors") or []
        text = " ".join(
            "%s %s" % (
                e.get("location", ""),
                e.get("detail", ""),
            )
            for e in errors
        ).lower()

        return [
            field
            for field in _VOCAB
            if field.lower() in text
        ]

    # Ask Silo for a playable stream URL using the supplied server-authoritative start position.
    def start_playback(self, file_id, start_position=0.0):
        # A value learned on an earlier run can speed up playback-start.
        saved_overrides = self.cfg.get("start_overrides") or {}
        chosen = {}

        # Reset the local subtitle override if it is known to be one of the
        # values that the current server rejected. The probing loop below will
        # learn a valid value again and save it.
        if isinstance(saved_overrides.get("subtitle_fidelity_preference"), str):
            saved_subtitle = saved_overrides.get("subtitle_fidelity_preference")
            if saved_subtitle not in _VOCAB["subtitle_fidelity_preference"][1]:
                saved_overrides.pop("subtitle_fidelity_preference", None)
                save_config(self.cfg)

        last_error = None

        for _ in range(60):
            body = _deep_merge(
                self._start_body(file_id, start_position),
                saved_overrides,
            )

            # Values already discovered during this request have priority over
            # values remembered from an earlier request.
            for field, index in chosen.items():
                path, candidates = _VOCAB[field]
                _set_path(body, path, candidates[index])

            try:
                data = self._json(
                    "POST",
                    "/api/v2/playback/start",
                    body=body,
                )
                break

            except SiloError as exc:
                last_error = exc

                bad_fields = self._invalid_fields(exc) if exc.status == 422 else []

                if not bad_fields:
                    log(
                        "playback/start body was: %s" % json.dumps(body)[:4000],
                        xbmc.LOGWARNING,
                    )
                    raise

                # Advance every rejected vocabulary field to the next candidate.
                for field in bad_fields:
                    next_index = chosen.get(field, -1) + 1
                    candidates = _VOCAB[field][1]

                    if next_index >= len(candidates):
                        raise SiloError(
                            "No accepted value found for '%s'. Set it under "
                            "start_overrides in config.json." % field
                        )

                    chosen[field] = next_index

                xbmc.sleep(150)

        else:
            raise last_error or SiloError("Gave up probing playback field values")

        # Remember values accepted by this server so future starts can be immediate.
        if chosen:
            saved = self.cfg.setdefault("start_overrides", {})

            for field, index in chosen.items():
                path, candidates = _VOCAB[field]
                _set_path(saved, path, candidates[index])

            save_config(self.cfg)

            log(
                "learned playback values: %s" % {
                    field: _VOCAB[field][1][index]
                    for field, index in chosen.items()
                }
            )

        plan = data.get("playback_plan")

        if not plan:
            raise SiloError(
                "Server refused playback: %s"
                % json.dumps(data.get("terminal") or data.get("outcome"))[:300]
            )

        stream = plan.get("stream") or {}
        url = self.abs_url(stream.get("url"))

        if not url:
            raise SiloError("Silo returned a playback plan without a stream URL.")

        # Kodi's VideoPlayer uses its own HTTP client rather than the Python
        # requests.Session above. Include both stream-specific headers from Silo
        # and the account/profile headers needed to open the stream endpoint.
        headers = dict(stream.get("headers") or {})
        auth_headers = self._headers()

        for key in ("Authorization", "X-Profile-Id", "X-Profile-Token"):
            value = auth_headers.get(key)
            if value and key not in headers:
                headers[key] = value

        # Kodi supports per-URL HTTP headers with |Header=value&Header2=value.
        if headers:
            url += "|" + "&".join(
                "%s=%s" % (key, quote(str(value), safe=""))
                for key, value in headers.items()
            )

        log(
            "delivery=%s protocol=%s outcome=%s start_position=%.3f"
            % (
                plan.get("delivery"),
                stream.get("protocol"),
                data.get("outcome"),
                float(start_position),
            )
        )

        return {
            "url": url,
            "session_id": data.get("session_id") or plan.get("session_id"),
        }

    # Send the current Kodi playback position to Silo.
    def report_progress(self, session_id, sequence, position, paused):
        self._send(
            "POST",
            "/api/v2/playback/%s/progress" % session_id,
            body={
                "installation_id": self._installation_id(),
                "sequence": int(sequence),
                "position": float(position),
                "is_paused": bool(paused),
            },
        )

    # Tell Silo that the playback session ended.
    def stop_playback(self, session_id, sequence, position):
        # Silo requires stop_id to be a canonical UUID, not uuid.uuid4().hex.
        stop_id = str(uuid.uuid4())

        self._send(
            "DELETE",
            "/api/v2/playback/%s" % session_id,
            body={
                "installation_id": self._installation_id(),
                "stop_id": stop_id,
                "sequence": int(sequence),
                "position": float(position),
                "is_paused": False,
            },
        )
