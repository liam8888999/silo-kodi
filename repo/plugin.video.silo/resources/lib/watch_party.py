"""Watch Party networking, synchronization, and lobby handling."""

from resources.lib import runtime
from resources.lib.common import *
from resources.lib.utils import *
from resources.lib.metadata import *
from resources.lib.playback import *

def _watch_party_socket_url(client, room_id):
    """Build the room WebSocket URL like Silo's web client."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(client.base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = (
        "/api/v2/watch-together/rooms/%s/ws"
        % quote(str(room_id), safe="")
    )
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))



def _watch_party_http_origin(client):
    """Return the HTTP origin Silo expects on native WebSocket handshakes."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(client.base)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "",
            "",
            "",
            "",
        )
    )



class _WatchPartyUiState:
    """Thread-safe Watch Party state shared by the socket monitor and UI."""

    def __init__(self, status):
        self._lock = threading.Lock()
        self.status = str(status)
        self.lobby = True
        self.finished = False
        self.ended = False

    def update(self, status=None, lobby=None, finished=None, ended=None):
        with self._lock:
            if status is not None:
                self.status = str(status)
            if lobby is not None:
                self.lobby = bool(lobby)
            if finished is not None:
                self.finished = bool(finished)
            if ended is not None:
                self.ended = bool(ended)

    def snapshot(self):
        with self._lock:
            return (
                self.status,
                self.lobby,
                self.finished,
                self.ended,
            )



def _watch_party_window():
    return xbmcgui.Window(10000)



def _watch_party_set_members(members):
    """Store the server Watch Party member readiness snapshot."""
    window = _watch_party_window()
    normalized = []

    for member in members or []:
        if not isinstance(member, dict):
            continue

        normalized.append(
            {
                "display_name": str(
                    member.get("display_name") or "Participant"
                ),
                "is_host": bool(member.get("is_host")),
                "is_self": bool(member.get("is_self")),
                "connected": bool(member.get("connected")),
                "is_ready": bool(member.get("is_ready")),
                "is_buffering": bool(member.get("is_buffering")),
                "is_syncing": bool(member.get("is_syncing")),
                "lobby_ready": bool(member.get("lobby_ready")),
            }
        )

    try:
        window.setProperty(
            "Silo.WatchParty.Members",
            json.dumps(
                normalized,
                separators=(",", ":"),
            ),
        )
    except Exception:
        window.setProperty("Silo.WatchParty.Members", "[]")



def _watch_party_clear_properties(window=None):
    window = window or _watch_party_window()
    for property_name in (
        "Silo.WatchParty.RoomID",
        "Silo.WatchParty.RoomToken",
        "Silo.WatchParty.Code",
        "Silo.WatchParty.Status",
        "Silo.WatchParty.Lobby",
        "Silo.WatchParty.Finished",
        "Silo.WatchParty.Ended",
        "Silo.WatchParty.LeaveRequested",
        "Silo.WatchParty.Members",
        "Silo.WatchParty.LobbyVisible",
    ):
        try:
            window.clearProperty(property_name)
        except Exception:
            try:
                window.setProperty(property_name, "")
            except Exception:
                pass



def _watch_party_reset_lobby():
    """Reset the Watch Party folder to its disconnected/joinable state."""
    window = _watch_party_window()

    # Clear room credentials and stale finished/playing state together so a
    # refreshed lobby cannot continue displaying the previous room.
    _watch_party_clear_properties(window)

    window.setProperty(
        "Silo.WatchParty.Status",
        "Not connected to a Watch Party.",
    )
    window.setProperty("Silo.WatchParty.Lobby", "true")
    window.setProperty("Silo.WatchParty.Finished", "false")
    window.setProperty("Silo.WatchParty.Ended", "false")
    window.setProperty("Silo.WatchParty.Members", "[]")
    window.setProperty("Silo.WatchParty.LobbyVisible", "true")



def _watch_party_refresh_lobby():
    """Refresh the live Watch Party lobby directory."""
    try:
        window = _watch_party_window()
        if window.getProperty("Silo.WatchParty.LobbyVisible").lower() != "true":
            return

        # The lobby directory is deliberately non-cacheable. A normal Kodi
        # container refresh can therefore invoke the plugin again and rebuild
        # the visible items from the latest Watch Party window properties.
        xbmc.executebuiltin("Container.Refresh")
    except Exception as exc:
        log(
            "Unable to refresh Watch Party lobby directory: %s" % exc,
            xbmc.LOGDEBUG,
        )


def _watch_party_lobby_display_key():
    """Return the state that can actually change the visible lobby directory."""
    window = _watch_party_window()
    return (
        window.getProperty("Silo.WatchParty.Status"),
        window.getProperty("Silo.WatchParty.Lobby"),
        window.getProperty("Silo.WatchParty.Finished"),
        window.getProperty("Silo.WatchParty.Ended"),
        window.getProperty("Silo.WatchParty.Code"),
        window.getProperty("Silo.WatchParty.Members"),
    )


def _watch_party_update_window_state(ui_state):
    """Mirror monitor state into Kodi global window properties."""
    status, lobby, finished, ended = ui_state.snapshot()
    window = _watch_party_window()

    window.setProperty("Silo.WatchParty.Status", status)
    window.setProperty("Silo.WatchParty.Lobby", "true" if lobby else "false")
    window.setProperty("Silo.WatchParty.Finished", "true" if finished else "false")
    window.setProperty("Silo.WatchParty.Ended", "true" if ended else "false")

    return status, lobby, finished, ended



def list_watch_party_lobby():
    """Display the current Watch Party state and participants."""
    window = _watch_party_window()

    # This function itself is what renders the visible lobby, including when
    # Kodi reached it through the watch_party_join action. Mark it visible here
    # so the background monitor can refresh the same directory periodically.
    window.setProperty("Silo.WatchParty.LobbyVisible", "true")
    status = window.getProperty("Silo.WatchParty.Status") or "Connecting to Watch Party..."
    room_code = window.getProperty("Silo.WatchParty.Code") or ""
    lobby = window.getProperty("Silo.WatchParty.Lobby").lower() == "true"
    finished = window.getProperty("Silo.WatchParty.Finished").lower() == "true"
    ended = window.getProperty("Silo.WatchParty.Ended").lower() == "true"

    xbmcplugin.setPluginCategory(runtime.HANDLE, "Watch Party")
    xbmcplugin.setContent(runtime.HANDLE, "files")

    if not room_code and not finished:
        info_item = xbmcgui.ListItem(label=status)
        info_item.setArt({"icon": "DefaultInfo.png"})
        info_item.setInfo(
            "video",
            {
                "title": "Watch Party",
                "plot": status,
            },
        )
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(action="watch_party_join"),
            info_item,
            True,
        )

        join_item = xbmcgui.ListItem(label="Join Watch Party")
        join_item.setArt({"icon": "DefaultFolder.png"})
        join_item.setInfo(
            "video",
            {
                "title": "Join Watch Party",
                "plot": "Enter a Watch Party room code.",
            },
        )
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(action="watch_party_join"),
            join_item,
            True,
        )

    elif finished:
        message = (
            "The Watch Party has ended."
            if ended
            else "Disconnected from the Watch Party."
        )
        info_item = xbmcgui.ListItem(label=message)
        info_item.setArt({"icon": "DefaultInfo.png"})
        info_item.setInfo(
            "video",
            {
                "title": "Watch Party",
                "plot": message,
            },
        )
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(action="watch_party_join"),
            info_item,
            True,
        )

    else:
        # There is one explicit Leave action for every connected room state.
        # Kodi's normal '..' parent entry remains available independently.
        leave_item = xbmcgui.ListItem(label="Leave Watch Party")
        leave_item.setArt({"icon": "DefaultFolder.png"})
        leave_item.setInfo(
            "video",
            {
                "title": "Leave Watch Party",
                "plot": "Leave the room and return to Silo.",
            },
        )
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(action="watch_party_leave"),
            leave_item,
            False,
        )

        state_label = status
        if not lobby:
            state_label = "%s (RESUME)" % state_label
        if room_code:
            state_label = "%s — Room %s" % (state_label, room_code)

        state_item = xbmcgui.ListItem(label=state_label)
        state_item.setArt({
            "icon": "DefaultVideo.png" if not lobby else "DefaultInfo.png"
        })
        state_item.setInfo(
            "video",
            {
                "title": "Watch Party",
                "plot": status,
            },
        )
        xbmcplugin.addDirectoryItem(
            runtime.HANDLE,
            build_url(
                action=(
                    "watch_party_show_player"
                    if not lobby
                    else "watch_party_lobby"
                )
            ),
            state_item,
            False,
        )

        try:
            members = json.loads(
                window.getProperty("Silo.WatchParty.Members") or "[]"
            )
        except (TypeError, ValueError):
            members = []

        if members:
            if lobby:
                ready_count = sum(
                    1
                    for member in members
                    if member.get("lobby_ready")
                )
                ready_label = "lobby ready"
            else:
                ready_count = sum(
                    1
                    for member in members
                    if member.get("is_ready")
                )
                ready_label = "ready"

            header = xbmcgui.ListItem(
                label="Participants — %d/%d %s"
                % (ready_count, len(members), ready_label)
            )
            header.setArt({"icon": "DefaultInfo.png"})
            header.setInfo(
                "video",
                {
                    "title": "Watch Party Participants",
                    "plot": "%d of %d participants are %s."
                    % (ready_count, len(members), ready_label),
                },
            )
            xbmcplugin.addDirectoryItem(
                runtime.HANDLE,
                build_url(action="watch_party_lobby"),
                header,
                False,
            )

            for member in members:
                name = str(member.get("display_name") or "Participant")
                if member.get("is_self"):
                    name += " (You)"
                if member.get("is_host"):
                    name += " (Host)"

                if not member.get("connected"):
                    member_state = "Disconnected"
                elif lobby:
                    member_state = (
                        "Lobby ready"
                        if member.get("lobby_ready")
                        else "Not ready"
                    )
                elif member.get("is_buffering"):
                    member_state = "Buffering"
                elif member.get("is_syncing"):
                    member_state = "Syncing"
                elif member.get("is_ready"):
                    member_state = "Ready"
                else:
                    member_state = "Not ready"

                member_item = xbmcgui.ListItem(
                    label="%s — %s" % (name, member_state)
                )
                member_item.setArt({"icon": "DefaultInfo.png"})
                member_item.setInfo(
                    "video",
                    {
                        "title": name,
                        "plot": "Watch Party status: %s." % member_state,
                    },
                )
                xbmcplugin.addDirectoryItem(
                    runtime.HANDLE,
                    build_url(action="watch_party_lobby"),
                    member_item,
                    False,
                )

    # The Watch Party lobby is live state, not a cacheable media directory.
    # Disable Kodi's directory cache so Container.Refresh rebuilds it from the
    # current server/member state.
    xbmcplugin.endOfDirectory(
        runtime.HANDLE,
        succeeded=True,
        updateListing=False,
        cacheToDisc=False,
    )



def _watch_party_connection_active():
    """Return whether this Kodi session still has an active Watch Party connection."""
    window = _watch_party_window()

    room_id = window.getProperty("Silo.WatchParty.RoomID")
    room_token = window.getProperty("Silo.WatchParty.RoomToken")
    finished = window.getProperty("Silo.WatchParty.Finished").lower() == "true"
    leaving = window.getProperty("Silo.WatchParty.LeaveRequested").lower() == "true"

    return bool(room_id and room_token and not finished and not leaving)



def _watch_party_join(client):
    """Open the existing Watch Party or join a new one by room code."""
    if _watch_party_connection_active():
        # The background monitor remains alive when the user navigates away
        # from the lobby. Reuse that connection instead of creating another
        # monitor or asking for the room code again.
        list_watch_party_lobby()
        return
    code = xbmcgui.Dialog().input(
        "Watch Party code",
        type=xbmcgui.INPUT_ALPHANUM,
    ).strip().upper()

    if not code:
        xbmcplugin.endOfDirectory(runtime.HANDLE)
        return

    response = client.watch_party_join(code=code)
    room = response.get("room") or {}
    room_token = response.get("room_access_token")
    room_id = room.get("room_id")

    if not room_id or not room_token:
        raise SiloError("Silo did not return Watch Party room credentials.")

    room_code = str(room.get("code") or code)
    window = _watch_party_window()

    # Keep room proof only in Kodi's current window/session. It is not written
    # to addon settings or the config file.
    window.setProperty("Silo.WatchParty.RoomID", str(room_id))
    window.setProperty("Silo.WatchParty.RoomToken", str(room_token))
    window.setProperty("Silo.WatchParty.Code", room_code)
    window.setProperty(
        "Silo.WatchParty.Status",
        "Joined Watch Party %s. Waiting for the host to start playback."
        % room_code,
    )
    window.setProperty("Silo.WatchParty.Lobby", "true")
    window.setProperty("Silo.WatchParty.Finished", "false")
    window.setProperty("Silo.WatchParty.Ended", "false")
    window.clearProperty("Silo.WatchParty.LeaveRequested")

    stop_event = threading.Event()
    state = _WatchPartyUiState(
        "Joined Watch Party %s. Waiting for the host to start playback."
        % room_code
    )
    result = {"error": None}

    def monitor_runner():
        try:
            _watch_party_monitor(
                client,
                room_id,
                room_token,
                stop_event=stop_event,
                ui_state=state,
            )
        except Exception as exc:
            result["error"] = exc
            state.update(
                status="Watch Party disconnected: %s" % exc,
                lobby=False,
                finished=True,
            )
            _watch_party_update_window_state(state)
            _watch_party_reset_lobby()
            _watch_party_refresh_lobby()

    thread = threading.Thread(
        target=monitor_runner,
        name="SiloWatchParty",
    )
    # The monitor owns the room WebSocket. Keeping it non-daemon prevents
    # Kodi from ending the plugin invocation immediately after the lobby
    # directory is returned, which otherwise leaves playback running with no
    # Watch Party transport connection.
    thread.daemon = False
    thread.start()

    # Finish the current join action with the normal Kodi directory
    # contents. The monitor remains alive in the background. Later refreshes
    # are limited to an already-open lobby container, so they never re-open
    # the room-code prompt.
    list_watch_party_lobby()



def _watch_party_show_player():
    """Bring the active Watch Party video player to the foreground."""
    try:
        player = xbmc.Player()
        if player.isPlaying():
            xbmc.executebuiltin("ActivateWindow(fullscreenvideo)")
        else:
            log(
                "Watch Party player foreground requested, but Kodi is not "
                "currently playing video.",
                xbmc.LOGDEBUG,
            )
    except Exception as exc:
        log(
            "Unable to bring Watch Party player to the foreground: %s" % exc,
            xbmc.LOGWARNING,
        )



def _watch_party_leave():
    """Leave the Watch Party, stop its local playback, and leave the lobby."""
    window = _watch_party_window()

    if not window.getProperty("Silo.WatchParty.RoomID"):
        _watch_party_clear_properties(window)
        xbmc.executebuiltin("Container.Back")
        return

    # LeaveRequested must remain set until the background monitor handles it.
    # Do not call _watch_party_reset_lobby() here because that clears the flag
    # before the monitor can close the room connection and playback session.
    window.setProperty("Silo.WatchParty.LeaveRequested", "true")
    window.setProperty("Silo.WatchParty.Status", "Leaving Watch Party...")
    window.setProperty("Silo.WatchParty.Lobby", "false")
    window.setProperty("Silo.WatchParty.Finished", "true")
    window.setProperty("Silo.WatchParty.Ended", "false")

    # Explicitly stop the Kodi player as part of leaving. The Watch Party
    # player's onPlayBackStopped callback will see the leave request and
    # perform the normal room/session shutdown as well. The monitor also polls
    # LeaveRequested, so this remains safe when nothing is currently playing.
    try:
        player = xbmc.Player()
        if player.isPlaying():
            log(
                "Stopping Kodi playback because the user left the Watch Party.",
                xbmc.LOGINFO,
            )
            player.stop()
    except Exception as exc:
        log(
            "Unable to stop Kodi playback while leaving Watch Party: %s"
            % exc,
            xbmc.LOGWARNING,
        )

    xbmcgui.Dialog().notification(
        "Watch Party",
        "Left Watch Party.",
        xbmcgui.NOTIFICATION_INFO,
        2000,
    )

    # Navigate back out of the dedicated Watch Party lobby rather than
    # rendering a disconnected state inside it.
    xbmc.executebuiltin("Container.Back")





class _SiloWebSocket:
    """Minimal RFC 6455 WebSocket client for Silo's room protocol."""

    def __init__(self, url, protocols, timeout=15, origin=None):
        self.url = url
        self.protocols = list(protocols or [])
        self.timeout = float(timeout or 15)
        self.origin = str(origin or "").strip()
        self.sock = None
        self._buffer = b""

    def connect(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise SiloError("Invalid Watch Party WebSocket URL.")

        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        host = parsed.hostname
        host_header = host
        if ":" in host and not host.startswith("["):
            host_header = "[%s]" % host

        if (parsed.scheme == "ws" and port != 80) or (
            parsed.scheme == "wss" and port != 443
        ):
            host_header = "%s:%d" % (host_header, port)

        try:
            sock = socket.create_connection(
                (host, port),
                timeout=self.timeout,
            )
        except Exception as exc:
            log(
                "Watch Party WebSocket TCP connection failed to %s:%d: %s"
                % (host, port, exc),
                xbmc.LOGERROR,
            )
            raise SiloError(
                "Watch Party WebSocket TCP connection failed: %s" % exc
            )

        log(
            "Watch Party WebSocket TCP connection established to %s:%d"
            % (host, port),
            xbmc.LOGDEBUG,
        )

        if parsed.scheme == "wss":
            try:
                context = ssl.create_default_context()
                sock = context.wrap_socket(
                    sock,
                    server_hostname=host,
                )
            except Exception as exc:
                try:
                    sock.close()
                except Exception:
                    pass

                log(
                    "Watch Party WebSocket TLS handshake failed: %s"
                    % exc,
                    xbmc.LOGERROR,
                )
                raise SiloError(
                    "Watch Party WebSocket TLS handshake failed: %s" % exc
                )

            log(
                "Watch Party WebSocket TLS connection established",
                xbmc.LOGDEBUG,
            )

        self.sock = sock
        self.sock.settimeout(self.timeout)

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Origin: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: %s\r\n"
            "\r\n"
        ) % (
            path,
            host_header,
            self.origin,
            key,
            ", ".join(self.protocols),
        )

        try:
            self.sock.sendall(request.encode("ascii"))
        except Exception as exc:
            self.close()
            log(
                "Watch Party WebSocket upgrade request failed to send: %s"
                % exc,
                xbmc.LOGERROR,
            )
            raise SiloError(
                "Watch Party WebSocket upgrade request failed: %s" % exc
            )

        log(
            "Watch Party WebSocket upgrade request sent to %s"
            % path,
            xbmc.LOGDEBUG,
        )

        try:
            response = self._read_http_headers()
        except socket.timeout:
            self.close()
            log(
                "Watch Party WebSocket handshake timed out waiting for HTTP 101",
                xbmc.LOGERROR,
            )
            raise SiloError(
                "Watch Party WebSocket handshake timed out waiting for HTTP 101"
            )
        except Exception:
            self.close()
            raise

        lines = response.decode("latin-1").split("\r\n")
        status = lines[0] if lines else ""
        headers = {}

        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        log(
            "Watch Party WebSocket handshake response received: %s"
            % status,
            xbmc.LOGDEBUG,
        )

        if not status.startswith("HTTP/1.1 101"):
            self.close()
            raise SiloError(
                "Silo rejected the Watch Party WebSocket handshake: %s"
                % status
            )

        expected = base64.b64encode(
            hashlib.sha1(
                (
                    key
                    + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                ).encode("ascii")
            ).digest()
        ).decode("ascii")

        if headers.get("sec-websocket-accept", "").strip() != expected:
            self.close()
            raise SiloError("Invalid Watch Party WebSocket handshake response.")

        if headers.get("sec-websocket-protocol", "").strip() != "silo.room.v2":
            self.close()
            raise SiloError(
                "Silo did not select the expected Watch Party socket protocol."
            )

        # Keep the room monitor responsive enough to immediately undo
        # unauthorized Kodi pause/seek actions.
        self.sock.settimeout(0.20)
        return self

    def _read_http_headers(self):
        data = b""

        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SiloError("Watch Party WebSocket closed during handshake.")
            data += chunk

            if len(data) > 32768:
                raise SiloError("Watch Party WebSocket handshake is too large.")

        separator = data.index(b"\r\n\r\n") + 4
        self._buffer = data[separator:]
        return data[:separator]

    def _recv_exact(self, size):
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise SiloError("Watch Party WebSocket connection closed.")
            self._buffer += chunk

        value = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return value

    def recv(self):
        """Return one complete text message, or None on a short read timeout."""
        while True:
            try:
                first, second = self._recv_exact(2)
                fin = bool(first & 0x80)
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                length = second & 0x7F

                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(8))[0]

                if length > 8 * 1024 * 1024:
                    raise SiloError("Watch Party WebSocket frame is too large.")

                mask = self._recv_exact(4) if masked else None
                payload = self._recv_exact(length) if length else b""

            except socket.timeout:
                return None

            if mask:
                payload = bytes(
                    value ^ mask[index % 4]
                    for index, value in enumerate(payload)
                )

            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue

            if opcode == 0xA:
                continue

            if opcode == 0x8:
                try:
                    self._send_frame(0x8, payload[:125])
                except Exception:
                    pass
                raise SiloError("Watch Party WebSocket was closed by Silo.")

            if opcode == 0x0 or not fin:
                raise SiloError(
                    "Watch Party sent an unsupported fragmented WebSocket message."
                )

            if opcode != 0x1:
                continue

            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                raise SiloError("Watch Party sent invalid UTF-8 data.")

    def send(self, message):
        self._send_frame(
            0x1,
            json.dumps(
                message,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def _send_frame(self, opcode, payload=b""):
        if self.sock is None:
            raise SiloError("Watch Party WebSocket is not connected.")

        payload = payload or b""
        length = len(payload)
        mask = os.urandom(4)

        if length <= 125:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 65535:
            header = struct.pack(
                "!BBH",
                0x80 | opcode,
                0x80 | 126,
                length,
            )
        else:
            header = struct.pack(
                "!BBQ",
                0x80 | opcode,
                0x80 | 127,
                length,
            )

        masked = bytes(
            value ^ mask[index % 4]
            for index, value in enumerate(payload)
        )

        self.sock.sendall(header + mask + masked)

    def close(self):
        sock = self.sock
        self.sock = None

        if sock is None:
            return

        try:
            payload = b""
            mask = os.urandom(4)
            header = struct.pack("!BB", 0x88, 0x80)
            masked = bytes(
                value ^ mask[index % 4]
                for index, value in enumerate(payload)
            )
            sock.sendall(header + mask + masked)
        except Exception:
            pass

        try:
            sock.close()
        except Exception:
            pass


def _watch_party_monitor(
    client,
    room_id,
    room_token,
    stop_event=None,
    ui_state=None,
):
    """Follow a host-controlled Watch Party as a guest."""

    url = _watch_party_socket_url(client, room_id)
    socket = None

    def connect_watch_party_socket():
        """Mint a fresh ticket and establish a new room WebSocket connection."""
        ticket = client.watch_party_socket_ticket(room_id, room_token)
        ticket_value = ticket["ticket"]

        return _SiloWebSocket(
            url,
            [
                "silo.room.v2",
                "silo.ticket.%s" % ticket_value,
            ],
            timeout=15,
            origin=_watch_party_http_origin(client),
        ).connect()

    try:
        socket = connect_watch_party_socket()
    except Exception as exc:
        log(
            "Watch Party WebSocket connection failed: %s (%s)"
            % (exc, type(exc).__name__),
            xbmc.LOGERROR,
        )
        raise SiloError(
            "Unable to connect to the Watch Party: %s"
            % exc
        )

    try:
        socket.send({"type": "lobby_ready", "ready": True})
        log(
            "Watch Party lobby readiness sent after room connection.",
            xbmc.LOGDEBUG,
        )
    except Exception as exc:
        log(
            "Unable to send initial Watch Party lobby readiness: %s" % exc,
            xbmc.LOGWARNING,
        )

    monitor = xbmc.Monitor()
    if stop_event is None:
        stop_event = threading.Event()
    if ui_state is None:
        ui_state = _WatchPartyUiState("Connected to Watch Party.")
    session_id = None
    attached = False
    current_selection_revision = None
    last_command_id = None
    last_ping = 0.0
    last_state_report = 0.0
    playback_sequence = 0
    last_reported_paused = None
    next_progress_report_at = time.time()
    server_time_offset = 0.0
    last_ready_report = 0.0
    last_lobby_ready_report = 0.0
    last_lobby_display_key = None
    self_member_ready = False
    waiting_command_id = None

    # The room is authoritative for guest transport. Snapshots and host
    # commands update this state; it is then enforced continuously whenever
    # this profile is not allowed to control transport locally.
    room_phase = None
    room_playback_state = None
    room_can_control_transport = False
    room_target_position = 0.0
    room_target_updated_at = time.time()
    room_transport_known = False
    transport_sync_hold_until = 0.0
    transport_guard_until = 0.0
    remote_stop_until = 0.0
    was_room_playing = False
    last_transport_enforcement = 0.0
    last_transport_offset_log = 0.0
    send_lock = threading.Lock()

    # Kodi invokes onPlayBackStopped for an explicit user Stop as well as for
    # normal media replacement. Ignore the callback briefly while switching to
    # a new host-selected item, but treat a genuine stop as an immediate room
    # disconnect.
    disconnect_requested = threading.Event()
    switching_media_until = 0.0
    watch_party_input_lock_enabled = False

    # Kodi keymaps run before xbmc.Player callbacks. Those callbacks can undo
    # a seek or speed change after it happens, but they cannot make the original
    # physical button/hotkey press a true no-op. Watch Party therefore installs
    # a temporary keymap while playback is active and maps common seek/FF/RW
    # inputs to Kodi's documented noop action.
    watch_party_keymap_path = os.path.join(
        xbmcvfs.translatePath("special://profile"),
        "keymaps",
        "silo_watch_party.xml",
    )
    watch_party_keymap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<keymap>
  <FullscreenVideo>
    <keyboard>
      <p>noop</p>
      <space>noop</space>
      <f>noop</f>
      <r>noop</r>
      <period>noop</period>
      <comma>noop</comma>
      <quote>noop</quote>
      <opensquarebracket>noop</opensquarebracket>
      <closesquarebracket>noop</closesquarebracket>
      <pageup>noop</pageup>
      <pagedown>noop</pagedown>
      <channel_up>noop</channel_up>
      <channel_down>noop</channel_down>
      <fastforward>noop</fastforward>
      <rewind>noop</rewind>
      <left mod="ctrl">noop</left>
      <right mod="ctrl">noop</right>
    </keyboard>
    <remote>
      <play>noop</play>
      <pause>noop</pause>
      <play_pause>noop</play_pause>
      <forward>noop</forward>
      <reverse>noop</reverse>
      <skipplus>noop</skipplus>
      <skipminus>noop</skipminus>
    </remote>
  </FullscreenVideo>
  <FullscreenInfo>
    <keyboard>
      <p>noop</p>
      <space>noop</space>
      <f>noop</f>
      <r>noop</r>
      <period>noop</period>
      <comma>noop</comma>
      <quote>noop</quote>
      <opensquarebracket>noop</opensquarebracket>
      <closesquarebracket>noop</closesquarebracket>
      <pageup>noop</pageup>
      <pagedown>noop</pagedown>
      <channel_up>noop</channel_up>
      <channel_down>noop</channel_down>
      <fastforward>noop</fastforward>
      <rewind>noop</rewind>
      <left mod="ctrl">noop</left>
      <right mod="ctrl">noop</right>
    </keyboard>
    <remote>
      <play>noop</play>
      <pause>noop</pause>
      <play_pause>noop</play_pause>
      <forward>noop</forward>
      <reverse>noop</reverse>
      <skipplus>noop</skipplus>
      <skipminus>noop</skipminus>
    </remote>
  </FullscreenInfo>
</keymap>
"""

    def set_watch_party_input_lock(enabled):
        """Temporarily make Watch Party seek/FF/RW inputs true no-ops."""
        nonlocal watch_party_input_lock_enabled

        enabled = bool(enabled)
        if enabled == watch_party_input_lock_enabled:
            return True

        try:
            keymap_dir = os.path.dirname(watch_party_keymap_path)

            if enabled:
                if not xbmcvfs.exists(keymap_dir):
                    xbmcvfs.mkdirs(keymap_dir)

                handle = xbmcvfs.File(watch_party_keymap_path, "w")
                try:
                    handle.write(watch_party_keymap_xml)
                finally:
                    handle.close()
            else:
                if xbmcvfs.exists(watch_party_keymap_path):
                    xbmcvfs.delete(watch_party_keymap_path)

            xbmc.executebuiltin("ReloadKeymaps")
            watch_party_input_lock_enabled = enabled

            log(
                "Watch Party input lock %s."
                % ("enabled" if enabled else "disabled"),
                xbmc.LOGDEBUG,
            )
            return True
        except Exception as exc:
            log(
                "Unable to %s Watch Party input keymap: %s"
                % ("enable" if enabled else "disable", exc),
                xbmc.LOGWARNING,
            )
            return False

    def clear_watch_party_state():
        window = xbmcgui.Window(10000)
        for property_name in (
            "Silo.WatchParty.RoomID",
            "Silo.WatchParty.RoomToken",
            "Silo.WatchParty.Code",
        ):
            try:
                window.clearProperty(property_name)
            except Exception:
                try:
                    window.setProperty(property_name, "")
                except Exception:
                    pass

    def close_watch_party_playback_session(reason):
        """Stop Kodi media and explicitly terminate its Silo playback session."""
        nonlocal session_id, attached, playback_sequence

        active_session_id = session_id
        active_player = player

        try:
            last_position = player_position(active_player)
        except Exception:
            last_position = 0.0

        # A permanent Watch Party shutdown must always stop the Kodi player,
        # even if the playback session ID was already cleared by a room
        # transition. This prevents direct media from continuing independently
        # after the Watch Party itself has disappeared.
        try:
            if active_player.isPlaying():
                log(
                    "Stopping local Watch Party playback: %s" % reason,
                    xbmc.LOGINFO,
                )
                active_player.stop()
        except Exception as exc:
            log(
                "Unable to stop local Watch Party playback: %s"
                % exc,
                xbmc.LOGWARNING,
            )

        if active_session_id:
            # Stop Kodi first so the stream consumer closes as well. Then tell
            # Silo explicitly that this Watch Party playback attempt is over.
            try:
                stop_sequence = max(1, playback_sequence + 1)
                client.stop_playback(
                    active_session_id,
                    stop_sequence,
                    last_position,
                )
                log(
                    "Stopped Silo Watch Party playback session %s"
                    % active_session_id,
                    xbmc.LOGDEBUG,
                )
            except SiloError as exc:
                log(
                    "Unable to stop Silo Watch Party playback session %s: %s"
                    % (active_session_id, exc),
                    xbmc.LOGWARNING,
                )
            except Exception as exc:
                log(
                    "Unexpected error stopping Silo Watch Party playback session %s: %s"
                    % (active_session_id, exc),
                    xbmc.LOGWARNING,
                )

        session_id = None
        attached = False

    def request_watch_party_disconnect(reason):
        nonlocal switching_media_until
        if (
            switching_media_until > time.time()
            and reason == "Kodi playback stopped"
        ):
            return

        if disconnect_requested.is_set():
            return

        log(
            "Watch Party participant disconnecting: %s" % reason,
            xbmc.LOGINFO,
        )
        set_watch_party_input_lock(False)
        disconnect_requested.set()
        close_watch_party_playback_session(reason)
        _watch_party_reset_lobby()
        _watch_party_refresh_lobby()
        update_ui(
            status="Disconnected from Watch Party.",
            lobby=False,
            finished=True,
        )
        clear_watch_party_state()

        try:
            socket.close()
        except Exception:
            pass

    def update_ui(status=None, lobby=None, finished=None, ended=None):
        ui_state.update(
            status=status,
            lobby=lobby,
            finished=finished,
            ended=ended,
        )
        _watch_party_update_window_state(ui_state)

    def notify(status, level=xbmcgui.NOTIFICATION_INFO, ms=3000):
        try:
            xbmcgui.Dialog().notification(
                "Watch Party",
                status,
                level,
                ms,
            )
        except Exception:
            pass

    def send(message):
        try:
            with send_lock:
                socket.send(message)
            return True
        except Exception:
            return False

    def player_paused(player):
        try:
            return int(player.getPlaySpeed()) == 0
        except Exception:
            return bool(xbmc.getCondVisibility("Player.Paused"))

    def player_position(player):
        try:
            return max(0.0, float(player.getTime()))
        except Exception:
            return 0.0

    def parse_server_timestamp(value):
        try:
            return datetime.datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    def authoritative_position(now=None):
        now = time.time() if now is None else now

        if (
            room_transport_known
            and room_playback_state == "playing"
        ):
            return max(
                0.0,
                room_target_position
                + max(0.0, now - room_target_updated_at),
            )

        return max(0.0, room_target_position)

    def set_transport_guard(seconds=1.0):
        nonlocal transport_guard_until
        transport_guard_until = max(
            transport_guard_until,
            time.time() + max(0.0, float(seconds or 0.0)),
        )

    def update_authoritative_room_state(room):
        nonlocal room_phase, room_playback_state, room_can_control_transport
        nonlocal room_target_position, room_target_updated_at
        nonlocal room_transport_known

        room_phase = room.get("phase")
        room_playback_state = room.get("playback_state")
        room_can_control_transport = bool(
            room.get("self_can_control_transport")
        )

        try:
            room_target_position = max(
                0.0,
                float(room.get("anchor_position_seconds") or 0),
            )
        except (TypeError, ValueError):
            room_target_position = 0.0

        # Anchor the snapshot to the local receive time. The server's anchor
        # timestamp uses the server clock and may not yet have a calibrated
        # client offset during startup.
        room_target_updated_at = time.time()

        room_transport_known = (
            room_phase == "playing"
            and room_playback_state in ("playing", "paused", "waiting")
        )

    def handle_local_transport_event(action, player):
        """Correct a local Kodi play/pause change back to the room state.

        Watch Party playback is server-authoritative for this client even when
        Silo reports that the profile is allowed to control transport.
        """
        if action not in ("play", "pause"):
            return False

        if not (
            session_id
            and attached
            and room_transport_known
        ):
            return False

        log(
            "Ignoring local Watch Party %s control; restoring server state."
            % action,
            xbmc.LOGDEBUG,
        )
        enforce_guest_transport(player)
        return True


    def reconcile_guest_transport(player, now):
        """Keep Kodi on the room's authoritative transport state."""
        if (
            not session_id
            or not attached
            or not room_transport_known
            or now < transport_guard_until
            or now < transport_sync_hold_until
            or room_playback_state == "waiting"
        ):
            return

        enforce_guest_transport(player)

    def enforce_guest_transport(player):
        """Undo unauthorized local pause/seek/play changes immediately."""

        nonlocal last_transport_enforcement

        if (
            not session_id
            or not attached
            or not room_transport_known
            or not player.isPlaying()
            or time.time() < transport_guard_until
        ):
            return

        now = time.time()
        target_position = authoritative_position(now)
        target_paused = room_playback_state in ("paused", "waiting")
        current_position = player_position(player)
        current_paused = player_paused(player)

        position_drift = abs(current_position - target_position)
        needs_seek = position_drift > 0.50
        needs_pause_change = current_paused != target_paused

        if not needs_seek and not needs_pause_change:
            return

        if position_drift >= 3.0:
            log(
                "Large Watch Party transport drift detected: "
                "kodi=%s server=%s delta=%+.3fs"
                % (
                    format_position(current_position),
                    format_position(target_position),
                    current_position - target_position,
                ),
                xbmc.LOGWARNING,
            )

        if now - last_transport_enforcement >= 1.0:
            log(
                "Enforcing locked Watch Party transport: local=%s%s "
                "target=%s%s"
                % (
                    format_position(current_position),
                    " paused" if current_paused else " playing",
                    format_position(target_position),
                    " paused" if target_paused else " playing",
                ),
                xbmc.LOGDEBUG,
            )
            last_transport_enforcement = now

        set_transport_guard(2.0 if needs_seek else 1.0)
        _watch_party_apply_transport_state(
            player,
            target_position,
            target_paused,
        )

    class _WatchPartyPlayer(xbmc.Player):
        """Kodi callbacks for immediate enforcement of locked room transport.

        The transport guard suppresses callbacks generated by our own
        corrective play/pause/seek operations, preventing those corrections
        from recursively triggering another correction.
        """

        def _transport_locked(self):
            return (
                session_id
                and attached
                and room_transport_known
                and room_phase == "playing"
            )

        def onPlayBackPaused(self):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            # A local pause is never allowed to change the Watch Party state.
            # Restore the room's authoritative playing state immediately.
            if room_playback_state == "playing":
                set_transport_guard()
                _kodi_set_watch_party_play_state(self, True)

        def onPlayBackResumed(self):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            # A local resume is never allowed to change the Watch Party state.
            # Restore the room's authoritative paused state immediately,
            # including the temporary "waiting" state used during resync.
            if room_playback_state in ("paused", "waiting"):
                set_transport_guard()
                _kodi_set_watch_party_play_state(self, False)

        def onPlayBackSpeedChanged(self, speed):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            try:
                current_speed = int(speed)
            except (TypeError, ValueError):
                return

            # Fast-forward and rewind are implemented by Kodi as playback
            # speed changes. Guests are not allowed to control transport, so
            # immediately return to normal playback speed.
            if current_speed in (0, 1):
                return

            log(
                "Ignoring local Watch Party playback speed change: %s"
                % current_speed,
                xbmc.LOGDEBUG,
            )
            set_transport_guard(1.0)

            try:
                # PlayerControl(Play) returns FF/RW to normal 1x playback.
                xbmc.executebuiltin("PlayerControl(Play)")
            except Exception:
                pass

            # If the room is paused/waiting, the Play control above may have
            # resumed Kodi as part of leaving FF/RW. Restore the authoritative
            # paused state immediately.
            if room_playback_state in ("paused", "waiting"):
                set_transport_guard(1.0)
                _kodi_set_watch_party_play_state(self, False)

        def onPlayBackSeek(self, time_value, seek_offset):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            target = authoritative_position()
            try:
                local_seek = max(0.0, float(time_value))
            except (TypeError, ValueError):
                local_seek = target

            # Pull Kodi straight back to the room position after a native
            # skip or timeline seek. Pause during the correction so the player
            # cannot advance while the authoritative position is restored.
            if abs(local_seek - target) > 0.75:
                set_transport_guard(2.0)
                _watch_party_apply_transport_state(
                    self,
                    target,
                    room_playback_state in ("paused", "waiting"),
                )

        def onPlayBackSeekChapter(self, chapter):
            if not self._transport_locked() or time.time() < transport_guard_until:
                return

            # Chapter/previous/next seek actions are also local seeks and are
            # not permitted for Watch Party guests.
            set_transport_guard(2.0)
            _watch_party_apply_transport_state(
                self,
                authoritative_position(),
                room_playback_state in ("paused", "waiting"),
            )

        def onPlayBackStarted(self):
            if not self._transport_locked():
                return

            # Starting the media is allowed, but the room's paused state is
            # still authoritative once Kodi has a playable item.
            if room_playback_state in ("paused", "waiting"):
                set_transport_guard()
                try:
                    self.pause()
                except Exception:
                    pass

        def onPlayBackStopped(self):
            # A host stopping room playback also causes Kodi to stop its local
            # player. That is a remote room event, not a request to leave the
            # Watch Party, so only a genuine local Stop disconnects the guest.
            if time.time() < remote_stop_until:
                return

            # Kodi also fires this for host-driven media replacement; the
            # replacement window prevents those transitions being mistaken for
            # a deliberate local Stop.
            request_watch_party_disconnect("Kodi playback stopped")

    player = _WatchPartyPlayer()

    try:
        while (
            not monitor.abortRequested()
            and not disconnect_requested.is_set()
            and not stop_event.is_set()
        ):
            now = time.time()

            if (
                _watch_party_window().getProperty(
                    "Silo.WatchParty.LeaveRequested"
                ).lower()
                == "true"
            ):
                request_watch_party_disconnect("user left Watch Party lobby")
                break

            # Kodi does not expose a reliable folder-navigation callback
            # from a background plugin thread. Leaving the lobby is therefore
            # handled by the explicit Leave Watch Party item, while the socket
            # remains connected through idle lobby time.
            if (
                room_phase == "lobby"
                and now - last_lobby_ready_report >= 0.5
            ):
                send({
                    "type": "lobby_ready",
                    "ready": True,
                })
                last_lobby_ready_report = now

            if now - last_ping >= 15:
                send({
                    "type": "ping",
                    "client_sent_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                })
                last_ping = now

            try:
                raw = socket.recv()
            except Exception as exc:
                if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                    raw = None
                else:
                    # Silo intentionally limits each room WebSocket to five
                    # minutes. That is a socket credential lifetime, not a room
                    # lifetime. Reconnect with a freshly minted ticket instead
                    # of treating the normal socket expiry as leaving the room.
                    log(
                        "Watch Party WebSocket ended; reconnecting with a fresh "
                        "socket credential: %s" % exc,
                        xbmc.LOGINFO,
                    )
                    try:
                        socket.close()
                    except Exception:
                        pass

                    reconnected = False
                    reconnect_delay = 0.5

                    while (
                        not monitor.abortRequested()
                        and not disconnect_requested.is_set()
                        and not stop_event.is_set()
                    ):
                        try:
                            socket = connect_watch_party_socket()
                            reconnected = True

                            # The previous WebSocket connection's attachment
                            # does not carry over to the new connection. Keep
                            # the existing playback session ID, but explicitly
                            # attach that session again after reconnecting.
                            attached = False
                            last_state_report = 0.0

                            log(
                                "Watch Party WebSocket reconnected successfully; "
                                "reattaching playback session.",
                                xbmc.LOGINFO,
                            )
                            break
                        except SiloError as reconnect_exc:
                            log(
                                "Watch Party reconnect attempt failed: %s"
                                % reconnect_exc,
                                xbmc.LOGWARNING,
                            )

                            if reconnect_exc.status in (403, 404, 410):
                                disconnect_reason = (
                                    "Watch Party room is no longer available"
                                )
                                request_watch_party_disconnect(
                                    disconnect_reason
                                )
                                reconnected = False
                                break

                            xbmc.sleep(int(reconnect_delay * 1000))
                            reconnect_delay = min(5.0, reconnect_delay * 2.0)

                        except Exception as reconnect_exc:
                            log(
                                "Watch Party reconnect attempt failed: %s"
                                % reconnect_exc,
                                xbmc.LOGWARNING,
                            )
                            xbmc.sleep(int(reconnect_delay * 1000))
                            reconnect_delay = min(5.0, reconnect_delay * 2.0)

                    if not reconnected:
                        break

                    # The room coordinator keeps playback state/session
                    # attachment in shared runtime. The first post-reconnect
                    # snapshot lets us converge back to that authoritative
                    # state; no new playback session is created here.
                    raw = None

            if raw:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    message = {}

                message_type = message.get("type")

                if message_type == "snapshot":
                    room = message.get("room") or {}
                    previous_room_phase = room_phase
                    update_authoritative_room_state(room)
                    members = room.get("members") or []
                    _watch_party_set_members(members)
                    phase = room.get("phase")
                    selection_revision = room.get("selection_revision")
                    selected_content_id = room.get("selected_content_id")
                    selected_file_id = room.get("selected_file_id")
                    selected_library_id = room.get("selected_library_id")

                    if phase == "playing":
                        was_room_playing = True
                        set_watch_party_input_lock(True)

                        # The room is already playing as soon as Silo reports
                        # phase=playing. Kodi may still be opening/buffering the
                        # local stream, so never describe this as "starting"
                        # merely because player.isPlaying() is not true yet.
                        update_ui(
                            status="Playing with the Watch Party host.",
                            lobby=False,
                        )


                        if (
                            selected_content_id
                            and selection_revision != current_selection_revision
                        ):
                            current_selection_revision = selection_revision
                            switching_media_until = time.time() + 5.0
                            session_id = _start_watch_party_guest_playback(
                                client,
                                selected_content_id,
                                selected_file_id,
                                selected_library_id,
                                player=player,
                            )
                            playback_sequence = 0
                            last_reported_paused = None
                            next_progress_report_at = time.time()

                            # Kodi starts the stream asynchronously. Keep it
                            # paused while the initial Watch Party position is
                            # being synchronised so the guest cannot run ahead
                            # of the room while the seek is applied.
                            set_transport_guard(3.0)
                            transport_sync_hold_until = time.time() + 5.0
                            if _wait_for_watch_party_player(player, timeout=15.0):
                                _kodi_set_watch_party_play_state(player, False)

                                sync_position = authoritative_position()
                                current_position = player_position(player)

                                if abs(current_position - sync_position) > 0.25:
                                    try:
                                        player.seekTime(sync_position)
                                        xbmc.sleep(75)
                                    except Exception as exc:
                                        log(
                                            "Unable to apply initial Watch Party "
                                            "sync seek to %.3fs: %s"
                                            % (sync_position, exc),
                                            xbmc.LOGWARNING,
                                        )

                                # Resume only after the authoritative starting
                                # position has been applied.
                                if room_playback_state == "playing":
                                    set_transport_guard(0.75)
                                    _kodi_set_watch_party_play_state(player, True)

                            attached = False
                            last_command_id = None
                            last_state_report = 0.0
                            set_transport_guard(0.75)
                            update_ui(
                                status="Playing with the Watch Party host.",
                                lobby=False,
                            )

                    elif phase == "lobby":
                        # Silo's host "Stop Playback" operation returns the
                        # still-open room directly from playing -> lobby.
                        # Detect that transition from the previous snapshot
                        # instead of relying on a mutable readiness flag.
                        stopped_from_playback = (
                            previous_room_phase == "playing"
                            or was_room_playing
                        )

                        if stopped_from_playback:
                            was_room_playing = False

                            # Mark the stop as remote before calling stop(),
                            # because Kodi may invoke onPlayBackStopped from
                            # the stop operation itself.
                            remote_stop_until = time.time() + 5.0

                            close_watch_party_playback_session(
                                "Host stopped Watch Party playback"
                            )
                            set_watch_party_input_lock(False)
                            session_id = None
                            attached = False
                            last_command_id = None
                            current_selection_revision = selection_revision

                        else:
                            was_room_playing = False
                            set_watch_party_input_lock(False)

                        ready_count = sum(
                            1
                            for member in members
                            if isinstance(member, dict)
                            and bool(member.get("lobby_ready"))
                        )
                        participant_count = len(
                            [member for member in members if isinstance(member, dict)]
                        )

                        if participant_count:
                            if stopped_from_playback:
                                lobby_status = (
                                    "Watch Party %s — %d/%d participants ready. "
                                    "Playback stopped; waiting for the host."
                                    % (
                                        _watch_party_window().getProperty(
                                            "Silo.WatchParty.Code"
                                        ) or "lobby",
                                        ready_count,
                                        participant_count,
                                    )
                                )
                            else:
                                lobby_status = (
                                    "Watch Party %s — %d/%d participants ready. "
                                    "Waiting for the host to start playback."
                                    % (
                                        _watch_party_window().getProperty(
                                            "Silo.WatchParty.Code"
                                        ) or "lobby",
                                        ready_count,
                                        participant_count,
                                    )
                                )
                        else:
                            lobby_status = (
                                "Watch Party %s — waiting for participants."
                                % (
                                    _watch_party_window().getProperty(
                                        "Silo.WatchParty.Code"
                                    ) or "lobby",
                                )
                            )

                        update_ui(
                            status=lobby_status,
                            lobby=True,
                        )

                        if stopped_from_playback:
                            # close_watch_party_playback_session() normally
                            # stops Kodi. Keep one final guarded stop for any
                            # player that is still alive during callback teardown.
                            try:
                                if player.isPlaying():
                                    log(
                                        "Host stopped Watch Party playback; "
                                        "stopping local Kodi player.",
                                        xbmc.LOGINFO,
                                    )
                                    player.stop()
                            except Exception as exc:
                                log(
                                    "Unable to stop Kodi playback after remote "
                                    "Watch Party stop: %s" % exc,
                                    xbmc.LOGWARNING,
                                )

                    
                    elif phase == "paused":
                        was_room_playing = True
                        set_watch_party_input_lock(True)
                        update_ui(
                            status="Playback paused with the Watch Party host.",
                            lobby=False,
                        )

                    # Server snapshots can arrive frequently for transport
                    # state even when nothing visible in the lobby has changed.
                    # Rebuild the Kodi directory only when its visible state
                    # actually changes.
                    lobby_display_key = _watch_party_lobby_display_key()
                    if lobby_display_key != last_lobby_display_key:
                        _watch_party_refresh_lobby()
                        last_lobby_display_key = lobby_display_key

                elif message_type == "transport_command":
                    command = message.get("command") or {}
                    command_id = command.get("command_id")

                    if not command_id or command_id == last_command_id:
                        continue

                    if not session_id:
                        continue

                    action = command.get("action")
                    if action not in ("play", "pause", "seek"):
                        continue

                    try:
                        position = max(
                            0.0,
                            float(command.get("position_seconds") or 0),
                        )
                    except (TypeError, ValueError):
                        position = 0.0

                    execute_at = command.get("execute_at")
                    if action != "seek" and execute_at:
                        target = parse_server_timestamp(execute_at)
                        if target is not None:
                            delay = max(
                                0.0,
                                target - (time.time() + server_time_offset),
                            )
                            if delay > 0:
                                xbmc.sleep(int(delay * 1000))

                    # A seek command can arrive behind the
                    # snapshot for a newer seek. The room's waiting anchor is
                    # the latest authoritative target, so never apply an older
                    # command after that newer target is already known.
                    if (
                        action == "seek"
                        and room_playback_state == "waiting"
                        and abs(position - room_target_position) > 0.75
                    ):
                        log(
                            "Ignoring stale Watch Party seek command %s at %.3fs; "
                            "current waiting target is %.3fs."
                            % (
                                command_id,
                                position,
                                room_target_position,
                            ),
                            xbmc.LOGDEBUG,
                        )
                        continue

                    command_playback_state = (
                        command.get("playback_state") or "playing"
                    )

                    # The command is itself authoritative. Establish its
                    # position/state before touching Kodi so callbacks and the
                    # enforcement loop cannot mistake our correction for a
                    # prohibited local action.
                    room_target_position = position
                    room_target_updated_at = time.time()
                    room_playback_state = command_playback_state
                    waiting_command_id = (
                        command_id
                        if command_playback_state == "waiting"
                        else None
                    )
                    last_ready_report = 0.0
                    self_member_ready = False
                    room_transport_known = (
                        room_phase == "playing"
                        and command_playback_state in (
                            "playing",
                            "paused",
                            "waiting",
                        )
                    )
                    # Ignore local transport callbacks while Kodi settles
                    # this server-scheduled command.
                    set_transport_guard(
                        2.0 if action == "seek" else 0.75
                    )

                    applied = _apply_watch_party_guest_command(
                        action,
                        position,
                        player,
                        command_playback_state,
                    )

                    if applied:
                        transport_sync_hold_until = max(
                            transport_sync_hold_until,
                            time.time() + 3.0,
                        )

                    if not applied:
                        log(
                            "Watch Party command %s was not applied yet."
                            % command_id,
                            xbmc.LOGWARNING,
                        )
                        # The next snapshot/repeated command can retry once the
                        # player is actually available. Do not acknowledge it.
                        continue

                    last_command_id = command_id

                    # A guest acknowledges only after the player actually
                    # matches the requested transport state.
                    xbmc.sleep(100)
                    actual_position = player_position(player)
                    actual_paused = player_paused(player)
                    target_position = authoritative_position()
                    target_paused = command_playback_state in ("paused", "waiting")

                    if (
                        abs(actual_position - target_position) <= 1.0
                        and actual_paused == target_paused
                    ):
                        if command_playback_state == "waiting":
                            self_member_ready = True
                        send({
                            "type": "ready",
                            "session_id": session_id,
                            "command_id": command_id,
                            "position_seconds": actual_position,
                            "is_paused": actual_paused,
                        })
                    else:
                        log(
                            "Watch Party command %s applied locally but did "
                            "not settle at the authoritative state yet."
                            % command_id,
                            xbmc.LOGDEBUG,
                        )

                elif message_type == "pong":
                    sent = parse_server_timestamp(message.get("client_sent_at"))
                    server_received = parse_server_timestamp(
                        message.get("server_received_at")
                    )
                    server_sent = parse_server_timestamp(
                        message.get("server_sent_at")
                    )
                    received = time.time()
                    if (
                        sent is not None
                        and server_received is not None
                        and server_sent is not None
                    ):
                        server_time_offset = (
                            server_received
                            - sent
                            + server_sent
                            - received
                        ) / 2.0

                elif message_type == "room_closed":
                    # The host has ended the Watch Party itself, rather than
                    # merely stopping playback. Permanently close both the
                    # Watch Party connection and the Silo playback session.
                    disconnect_requested.set()
                    remote_stop_until = time.time() + 5.0
                    close_watch_party_playback_session("Watch Party ended remotely")
                    clear_watch_party_state()

                    xbmcgui.Dialog().notification(
                        "Watch Party",
                        "The Watch Party has ended.",
                        xbmcgui.NOTIFICATION_INFO,
                        4000,
                    )

                    try:
                        socket.close()
                    except Exception:
                        pass
                    break

                elif message_type == "connection_replaced":
                    disconnect_requested.set()
                    remote_stop_until = time.time() + 5.0
                    close_watch_party_playback_session(
                        "Watch Party connection replaced"
                    )
                    clear_watch_party_state()
                    xbmcgui.Dialog().notification(
                        "Watch Party",
                        "This profile joined the Watch Party somewhere else.",
                        xbmcgui.NOTIFICATION_WARNING,
                        5000,
                    )
                    break

            # Kodi play/pause is always locked while Watch Party playback
            # is active. Polling is retained as a fallback for remotes/skins
            # that do not reliably emit the native callbacks; it never sends a
            # transport request back to Silo.
            if (
                session_id
                and attached
                and room_transport_known
                and player.isPlaying()
                and time.time() >= transport_guard_until
                and time.time() >= transport_sync_hold_until
            ):
                if player_paused(player) != (room_playback_state in ("paused", "waiting")):
                    enforce_guest_transport(player)


            # Attach the current Silo playback session exactly once. The
            # session ID must remain unchanged for all later state reports.
            if session_id and not attached:
                if send({
                    "type": "attach_session",
                    "session_id": session_id,
                }):
                    attached = True

            # Enforce the current room transport after applying any new socket
            # message, so an unauthorized local pause/seek is corrected against
            # the newest host state before the next guest report.
            reconcile_guest_transport(player, now)

            # Persistent Silo progress is independent from Watch
            # Party room attachment and transport synchronization. This keeps
            # resume tracking alive while the room socket is reattaching and
            # allows a server-terminated playback session to be detected from
            # its authoritative progress response.
            if session_id and player.isPlaying():
                actual_paused = player_paused(player)
                current_position = player_position(player)

                pause_state_changed = (
                    last_reported_paused is not None
                    and bool(actual_paused) != bool(last_reported_paused)
                )

                if now >= next_progress_report_at or pause_state_changed:
                    playback_sequence += 1
                    try:
                        client.report_progress(
                            session_id,
                            playback_sequence,
                            current_position,
                            actual_paused,
                        )
                        last_reported_paused = bool(actual_paused)
                        next_progress_report_at = now + 5.0
                        log(
                            "Watch Party Silo progress reported: "
                            "sequence=%d position=%.3f paused=%s"
                            % (
                                playback_sequence,
                                current_position,
                                bool(actual_paused),
                            ),
                            xbmc.LOGDEBUG,
                        )
                    except SiloError as exc:
                        if playback_session_was_terminated(exc):
                            log(
                                "Silo Watch Party playback session was terminated "
                                "by the server; stopping Kodi playback.",
                                xbmc.LOGWARNING,
                            )
                            disconnect_requested.set()
                            remote_stop_until = time.time() + 5.0
                            try:
                                if player.isPlaying():
                                    player.stop()
                            except Exception as stop_exc:
                                log(
                                    "Unable to stop Kodi Watch Party playback "
                                    "after server termination: %s"
                                    % stop_exc,
                                    xbmc.LOGWARNING,
                                )

                            update_ui(
                                status="Playback was terminated by the server.",
                                lobby=False,
                                finished=True,
                            )
                            _watch_party_reset_lobby()
                            _watch_party_refresh_lobby()
                            try:
                                socket.close()
                            except Exception:
                                pass
                            break

                        next_progress_report_at = now + 5.0
                        log(
                            "Unable to report Watch Party playback progress: %s"
                            % exc,
                            xbmc.LOGWARNING,
                        )

            # Room synchronization state is separate from persistent progress.
            if (
                session_id
                and attached
                and player.isPlaying()
                and now - last_state_report >= 1.5
            ):
                current_position = player_position(player)
                actual_paused = player_paused(player)
                send({
                    "type": "state_report",
                    "session_id": session_id,
                    "position_seconds": current_position,
                    "is_paused": actual_paused,
                })
                last_state_report = now

            xbmc.sleep(25)

    except Exception as exc:
        log(
            "Watch Party monitor crashed: %s (%s)"
            % (exc, type(exc).__name__),
            xbmc.LOGERROR,
        )
        raise

    finally:
        set_watch_party_input_lock(False)
        if not disconnect_requested.is_set():
            disconnect_requested.set()
            close_watch_party_playback_session(
                "Watch Party monitor closed unexpectedly"
            )

        update_ui(
            finished=True,
            lobby=False,
        )
        clear_watch_party_state()
        try:
            socket.close()
        except Exception:
            pass



def _wait_for_watch_party_player(player, timeout=20.0):
    """Wait for Kodi to finish opening Watch Party media before transport changes."""
    deadline = time.time() + max(0.0, float(timeout or 0.0))

    while time.time() < deadline:
        try:
            if player.isPlaying():
                return True
        except Exception:
            pass

        xbmc.sleep(50)

    try:
        return bool(player.isPlaying())
    except Exception:
        return False



def _kodi_set_watch_party_play_state(player, should_play):
    """Set the current Kodi video player's play state explicitly."""
    if not player.isPlaying():
        return False

    try:
        response = xbmc.executeJSONRPC(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "Player.PlayPause",
                    "params": {
                        "playerid": 1,
                        "play": bool(should_play),
                    },
                    "id": "silo-watch-party-play-state",
                },
                separators=(",", ":"),
            )
        )
        payload = json.loads(response or "{}")
        result = payload.get("result") or {}
        speed = result.get("speed")
        if speed is not None:
            return (int(speed) != 0) == bool(should_play)
    except Exception as exc:
        log(
            "Kodi JSON-RPC Watch Party play-state change failed: %s"
            % exc,
            xbmc.LOGDEBUG,
        )

    # Fall back to Kodi's Player API if JSON-RPC did not return a usable state.
    try:
        if bool(player_paused(player)) == (not bool(should_play)):
            player.pause()
            xbmc.sleep(50)
    except Exception:
        pass

    try:
        return bool(player_paused(player)) == (not bool(should_play))
    except Exception:
        return False



def _watch_party_apply_transport_state(player, position, paused):
    """Force Kodi onto one authoritative Watch Party transport state.

    Seek corrections preserve the room's authoritative transport whenever
    possible. A playing room is allowed to seek while Kodi keeps playing;
    paused/waiting rooms pause before the seek and remain paused afterwards.
    This avoids repeatedly pausing a guest when the server is still playing.
    """
    if not player.isPlaying():
        return False

    try:
        current = max(0.0, float(player.getTime()))
    except Exception:
        current = 0.0

    needs_seek = abs(current - position) > 0.75

    if needs_seek:
        try:
            currently_paused = int(player.getPlaySpeed()) == 0
        except Exception:
            currently_paused = bool(xbmc.getCondVisibility("Player.Paused"))

        # Only pause during the seek when the authoritative target is
        # actually paused. For a playing room, seeking while already playing
        # lets Kodi land on the target without creating an artificial pause
        # that the server immediately has to undo.
        if paused and not currently_paused:
            if not _kodi_set_watch_party_play_state(player, False):
                log(
                    "Unable to pause Kodi before Watch Party resynchronisation.",
                    xbmc.LOGWARNING,
                )
                return False

        try:
            player.seekTime(position)
            xbmc.sleep(75)
        except Exception as exc:
            log(
                "Unable to apply Watch Party seek to %.3fs: %s"
                % (position, exc),
                xbmc.LOGWARNING,
            )
            return False

    try:
        currently_paused = int(player.getPlaySpeed()) == 0
    except Exception:
        currently_paused = bool(xbmc.getCondVisibility("Player.Paused"))

    if paused:
        if not currently_paused:
            if not _kodi_set_watch_party_play_state(player, False):
                log(
                    "Watch Party pause command did not leave Kodi paused.",
                    xbmc.LOGWARNING,
                )
                return False
    else:
        if currently_paused:
            if not _kodi_set_watch_party_play_state(player, True):
                log(
                    "Watch Party play command did not leave Kodi playing.",
                    xbmc.LOGWARNING,
                )
                return False

    return True



def _apply_watch_party_guest_command(
    action,
    position,
    player,
    playback_state="playing",
):
    """Apply a host transport command after Kodi has opened the media."""
    if action not in ("seek", "play", "pause"):
        return False

    if not player.isPlaying():
        if not _wait_for_watch_party_player(player):
            log(
                "Watch Party command deferred because Kodi did not start "
                "the selected media within the startup window.",
                xbmc.LOGWARNING,
            )
            return False

    return _watch_party_apply_transport_state(
        player,
        position,
        playback_state in ("paused", "waiting"),
    )



def _start_watch_party_guest_playback(
    client,
    content_id,
    file_id=None,
    library_id=None,
    player=None,
):
    """Start the host-selected item using the normal Silo Kodi playback path."""
    library_id = resolve_playback_library_id(client, content_id, library_id)

    if not file_id:
        file_id = choose_file(client, content_id, library_id)

    if not file_id:
        raise SiloError("The Watch Party item has no playable version.")

    info = client.start_playback(
        file_id,
        start_position=0.0,
        direct_play_only=direct_play_only_enabled(),
    )

    if not info.get("url"):
        raise SiloError("Silo did not provide a Watch Party playback URL.")

    item = xbmcgui.ListItem(path=info["url"])
    item.setProperty("OverrideInfotag", "true")

    try:
        detail = client.item_detail(content_id, library_id, file_id)
        if detail:
            set_catalog_metadata(item, detail, client)
            set_detail_metadata(item, detail, client, file_id=file_id)
            set_art(
                item,
                client,
                poster=detail.get("poster_url") or detail.get("poster"),
                backdrop=detail.get("backdrop_url") or detail.get("backdrop"),
                logo=detail.get("logo_url") or detail.get("logo"),
                still=detail.get("still_url") or detail.get("still"),
            )
    except Exception as exc:
        log(
            "Unable to apply Watch Party playback metadata for %s: %s"
            % (content_id, exc),
            xbmc.LOGWARNING,
        )

    # Watch Party playback is initiated from the non-playable "Join Watch Party"
    # action, so there is no Kodi playable-item resolution context for
    # setResolvedUrl() to hand back to the VideoPlayer. Start the resolved media
    # explicitly through xbmc.Player() instead.
    if player is None:
        player = xbmc.Player()
    player.play(item=info["url"], listitem=item)
    log(
        "Started Watch Party guest playback through Kodi Player: %s"
        % content_id,
        xbmc.LOGDEBUG,
    )
    return info.get("session_id")



__all__ = ["_watch_party_socket_url","_watch_party_http_origin","_WatchPartyUiState","_watch_party_window","_watch_party_set_members","_watch_party_clear_properties","_watch_party_reset_lobby","_watch_party_refresh_lobby","_watch_party_lobby_display_key","_watch_party_update_window_state","list_watch_party_lobby","_watch_party_connection_active","_watch_party_join","_watch_party_show_player","_watch_party_leave","_SiloWebSocket","_watch_party_monitor","_wait_for_watch_party_player","_kodi_set_watch_party_play_state","_watch_party_apply_transport_state","_apply_watch_party_guest_command","_start_watch_party_guest_playback"]
