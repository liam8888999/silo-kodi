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


# Central logging helper so every log line identifies this addon.
def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[plugin.video.silo] %s" % msg, level)


# Window property used to keep the login loading indicator visible while the
# refreshed root directory is loading after a successful login.
_LOGIN_LOADING_PROPERTY = "Silo.LoginLoading"


def _show_login_loading():
    """Show Kodi's native non-cancelable busy spinner for the login flow."""
    xbmcgui.Window(10000).setProperty(_LOGIN_LOADING_PROPERTY, "true")

    # DialogBusy was removed as a usable Python class in Kodi 18+. The
    # supported workaround is to activate the non-cancelable busy-dialog
    # window explicitly.
    xbmc.executebuiltin("ActivateWindow(busydialognocancel)")

    # Allow Kodi's GUI thread to process and display the dialog before the
    # synchronous network request immediately following this call starts.
    xbmc.sleep(100)


def _hide_login_loading():
    """Close the login busy spinner and clear its refresh-state marker."""
    xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
    xbmc.sleep(50)
    xbmcgui.Window(10000).clearProperty(_LOGIN_LOADING_PROPERTY)


# Load saved server/login/device/profile settings from Kodi's addon profile.
# Kodi's add-on settings are the persistent configuration store.
# The cfg dictionary remains an in-memory convenience for the rest of the
# client; it is never persisted to config.json.
_INTERNAL_SETTINGS = (
    "device_id",
    "token",
    "refresh_token",
    "profile_id",
    "profile_token",
)


def _setting(key, default=""):
    value = ADDON.getSetting(key)
    return value if value not in (None, "") else default


def _set_setting(key, value):
    ADDON.setSetting(key, "" if value is None else str(value))


def _clear_account_settings():
    """Clear editable login/profile settings and authentication state."""
    for key in (
        "server",
        "username",
        "device_id",
        "token",
        "refresh_token",
        "profile_id",
        "profile_token",
        "start_overrides",
    ):
        _set_setting(key, "")


def load_config():
    """Load all persistent add-on state from Kodi settings."""
    cfg = {}

    server = _setting("server").strip().rstrip("/")
    username = _setting("username").strip()
    profile = ""

    # Support username#profile in the username field.
    if "#" in username:
        username, inline_profile = username.split("#", 1)
        username = username.strip()
        profile = inline_profile.strip()

    if server:
        cfg["server"] = server
    if username:
        cfg["username"] = username
    if profile:
        cfg["profile_name"] = profile

    raw_items = _setting("items_per_page")
    try:
        cfg["items_per_page"] = max(20, min(int(raw_items or 200), 200))
    except (TypeError, ValueError):
        cfg["items_per_page"] = 200

    for key in _INTERNAL_SETTINGS:
        value = _setting(key)

        if not value:
            continue

        cfg[key] = value

    return cfg


def save_config(cfg):
    """Persist only hidden/internal runtime state into Kodi settings.

    Server and username are read-only settings and are already persisted by Kodi
    itself. Pagination is also handled directly by Kodi. These are deliberately
    not written here.
    """
    for key in _INTERNAL_SETTINGS:
        if key not in cfg:
            _set_setting(key, "")
            continue

        _set_setting(key, cfg[key])


# Custom exception used for errors that should be shown/logged by Kodi.
class SiloError(Exception):
    def __init__(self, msg, status=None, problem=None, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.problem = problem or {}
        self.retry_after = retry_after


class SiloClient:
    """Small authenticated client for Silo's v2 API."""

    # Create the API client, load saved settings and create a stable Kodi device ID.
    def __init__(self):
        self.cfg = load_config()

        if not self.cfg.get("device_id"):
            self.cfg["device_id"] = "kodi-" + uuid.uuid4().hex[:16]
            _set_setting("device_id", self.cfg["device_id"])

        self.session = requests.Session()
        self._caps = None
        self.sync_settings()
        # Detail responses are reused when the same item is later played.
        self._details = {}

    # ------------------------------------------------------------ settings

    def sync_settings(self):
        """Refresh editable connection/profile settings from Kodi."""
        raw_username = ADDON.getSetting("username").strip()
        server = ADDON.getSetting("server").strip().rstrip("/")
        profile = ""

        username = raw_username

        if "#" in raw_username:
            username, inline_profile = raw_username.split("#", 1)
            username = username.strip()
            profile = inline_profile.strip()

        old_identity = (
            self.cfg.get("server", ""),
            self.cfg.get("username", ""),
            self.cfg.get("profile_name", ""),
        )
        new_identity = (
            server,
            username,
            profile,
        )

        if new_identity != old_identity:
            for key in ("token", "refresh_token", "profile_id", "profile_token"):
                self.cfg.pop(key, None)
            self._caps = None

        self.cfg["server"] = server if server else self.cfg.get("server", "")
        self.cfg["username"] = username if username else self.cfg.get("username", "")

        if profile:
            self.cfg["profile_name"] = profile
        else:
            self.cfg.pop("profile_name", None)

        # Server, username and profile are already persisted by Kodi's
        # settings store. Do not call save_config() here: doing so could clear
        # an internal token if Kodi has not yet exposed that setting value to
        # this Addon instance.



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
        # Kodi's settings store is authoritative. Read the live values
        # directly instead of relying on a potentially stale in-memory cfg.
        device_id = _setting("device_id")
        token = _setting("token")
        profile_id = _setting("profile_id")
        profile_token = _setting("profile_token")

        if not device_id:
            device_id = "kodi-" + uuid.uuid4().hex[:16]
            _set_setting("device_id", device_id)

        self.cfg["device_id"] = device_id

        h = {
            "Accept": "application/json",
            "X-Device-ID": device_id,
            "X-Client-Name": "kodi-silo",
            "X-Client-Version": ADDON_VERSION,
            "X-Client-Platform": "kodi",
        }

        if token:
            h["Authorization"] = "Bearer " + token
            self.cfg["token"] = token

        if profile_id:
            h["X-Profile-Id"] = str(profile_id)
            self.cfg["profile_id"] = str(profile_id)

        if profile_token:
            h["X-Profile-Token"] = str(profile_token)
            self.cfg["profile_token"] = str(profile_token)

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

        if not _setting("token"):
            self.cfg.pop("token", None)
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
        server = server.rstrip("/")
        user = user.strip()

        ADDON.setSetting("server", server)
        ADDON.setSetting("username", user)

        if "#" in user:
            username, profile_name = user.split("#", 1)
            username = username.strip()
            profile_name = profile_name.strip()

            if not username or not profile_name:
                ADDON.setSetting("server", "")
                ADDON.setSetting("username", "")
                raise SiloError(
                    "Use username#profile, for example liam1#liam2"
                )

            ADDON.setSetting("username", username)
            self.cfg["username"] = username
            self.cfg["profile_name"] = profile_name
            self._requested_profile_name = profile_name
        else:
            self.cfg["username"] = user
            self.cfg.pop("profile_name", None)
            self._requested_profile_name = ""

        self.cfg["server"] = server
        save_config(self.cfg)



    # Store the access/refresh token pair returned by Silo.
    def _store_tokens(self, data):
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or "")

        if not access_token or not refresh_token:
            raise SiloError("Silo login did not return authentication tokens")

        self.cfg["token"] = access_token
        self.cfg["refresh_token"] = refresh_token

        # Persist authentication immediately in Kodi's settings store.
        _set_setting("token", access_token)
        _set_setting("refresh_token", refresh_token)

        log("Silo authentication tokens saved to Kodi settings")

    # Authenticate directly with /auth/login.
    #
    # This deliberately does NOT call _send(), because _send() calls login()
    # when a token is missing. Calling _send() here would recurse forever.
    def login(self, show_loading=False):
        if not self.base or not self.cfg.get("username"):
            self._prompt_account()

        # Never leave the busy dialog covering an input prompt. The loading
        # indicator starts only after the password has been submitted.
        _hide_login_loading()

        pw = xbmcgui.Dialog().input(
            "Password for %s" % self.cfg["username"],
            option=xbmcgui.ALPHANUM_HIDE_INPUT,
        )

        if not pw:
            raise SiloError("Login cancelled")

        # Start Kodi's native busy spinner immediately after the password
        # prompt closes, before the network authentication request begins.
        if show_loading:
            _show_login_loading()

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
            if show_loading:
                _hide_login_loading()
            raise SiloError("Cannot reach server: %s" % e)

        if not r.ok:
            if show_loading:
                _hide_login_loading()
            raise SiloError("Login failed - " + self._problem(r))

        try:
            self._store_tokens(r.json())
        except Exception:
            if show_loading:
                _hide_login_loading()
            raise

    # Complete interactive login used by the Kodi Login button.
    #
    # This intentionally performs the whole initial-login sequence in one
    # place: server/username/password, token storage, profile selection and
    # profile PIN verification when required.
    def login_full(self):
        # Explicit login starts with no busy spinner so the server, username
        # and password prompts are unobstructed.
        _hide_login_loading()

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
        for key in ("token", "refresh_token", "profile_id", "profile_token"):
            _set_setting(key, "")
        save_config(self.cfg)

        try:
            # login() now asks for server URL, username and password from
            # scratch because no account fields were retained above.
            self.login(show_loading=True)

            # Keep the spinner active when the account can select its
            # profile automatically. select_profile() pauses it only when Kodi
            # actually needs to display a profile or PIN prompt.
            self.select_profile()

        except SiloError:
            # Always close the loading indicator on a failed or cancelled login.
            _hide_login_loading()

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
            for key in ("token", "refresh_token", "profile_id", "profile_token"):
                _set_setting(key, "")
            save_config(self.cfg)
            raise

    # Exchange the saved refresh token for a new access token.
    def refresh(self):
        rt = _setting("refresh_token") or self.cfg.get("refresh_token")
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
        # Clear the editable account settings from Kodi itself as well as the
        # in-memory client state. Keeping these values would cause the next
        # addon launch to use the settings-first login path and ask only for
        # the password.
        for key in (
            "server",
            "username",
            "token",
            "refresh_token",
            "profile_id",
            "profile_token",
        ):
            self.cfg.pop("server" if key == "server" else key, None)
            _set_setting(key, "")

        # Keep the stable device ID, but discard account/profile-specific state.
        self.cfg.pop("profile_name", None)
        self._caps = None
        self._requested_profile_name = ""

        log("Silo account settings and authentication state cleared")

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
            # There is no profile chooser to display, so leave the login
            # spinner active and select the only profile automatically.
            chosen = profiles[0]
        else:
            # A real profile-selection dialog needs to be visible to the user,
            # so temporarily close the login spinner while Kodi displays it.
            _hide_login_loading()

            idx = xbmcgui.Dialog().select(
                "Who's watching?",
                [p.get("name", "Profile") for p in profiles],
            )

            if idx < 0:
                raise SiloError("No profile selected")

            chosen = profiles[idx]

            # Resume the existing login spinner once profile selection is done.
            _show_login_loading()

        self.cfg["profile_id"] = str(chosen["id"])
        self.cfg.pop("profile_token", None)

        # Persist profile selection directly in Kodi's settings store so a
        # new plugin invocation can immediately reuse the selected profile.
        _set_setting("profile_id", self.cfg["profile_id"])
        _set_setting("profile_token", "")

        # The selected profile ID is stored internally. The visible settings
        # page intentionally has no editable profile-name field.

        if chosen.get("has_pin"):
            # The PIN prompt must be visible, so pause the spinner for it and
            # resume the same login spinner afterwards.
            _hide_login_loading()
            try:
                self.verify_profile(chosen["id"])
            finally:
                _show_login_loading()

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
        _set_setting("profile_token", self.cfg["profile_token"])

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
            "start_position": float(start_position),
            "playback_attempt_id": uuid.uuid4().hex,
            "quality_preference": "original",
            "subtitle_fidelity_preference": "preserve",
            "metered": False,
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
        body = self._start_body(file_id, start_position)

        data = self._json(
            "POST",
            "/api/v2/playback/start",
            body=body,
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
