"""Background worker for the opt-in Silo native Kodi library integration."""

import time

import xbmc
import xbmcaddon

from resources.lib.silo import SiloClient, log
from resources.lib.artwork_proxy import start_artwork_proxy
from resources.lib.library_sync import (
    LibrarySyncDisabled,
    sync_current_profile,
)


ADDON = xbmcaddon.Addon()
SETTING_ENABLED = "native_library_enabled"
SETTING_INTERVAL = "native_library_interval_minutes"
DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 1440


def _enabled():
    return str(ADDON.getSetting(SETTING_ENABLED) or "").strip().lower() in (
        "true", "1", "yes"
    )


def _interval_seconds():
    try:
        minutes = int(float(ADDON.getSetting(SETTING_INTERVAL) or DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_INTERVAL_MINUTES
    minutes = max(MIN_INTERVAL_MINUTES, min(minutes, MAX_INTERVAL_MINUTES))
    return minutes * 60


def run():
    monitor = xbmc.Monitor()
    # Keep the authenticated artwork endpoint alive for Kodi's native video
    # library, even between catalogue syncs (and when sync is paused).
    start_artwork_proxy()
    last_sync = 0
    last_enabled = _enabled()
    last_interval = _interval_seconds()
    log("Native library integration service started (enabled=%s)" % last_enabled, xbmc.LOGINFO)

    while not monitor.abortRequested():
        enabled = _enabled()
        interval = _interval_seconds()

        # Enabling the feature or changing its schedule requests an immediate
        # sync. Disabling it never deletes files; it only stops future work.
        setting_changed = enabled != last_enabled or interval != last_interval
        if enabled and (not last_enabled or interval != last_interval):
            last_sync = 0
            log("Native library settings changed; requesting immediate sync", xbmc.LOGINFO)

        last_enabled = enabled
        last_interval = interval

        if enabled and time.time() - last_sync >= interval:
            try:
                log("Native library sync starting", xbmc.LOGINFO)
                client = SiloClient()
                # Never trigger an interactive login from a background service.
                if client.base and (
                    client.cfg.get("token") or client.cfg.get("refresh_token")
                ):
                    stats = sync_current_profile(client, monitor=monitor)
                    log("Native library sync finished: %s" % stats, xbmc.LOGINFO)
                else:
                    log(
                        "Native library sync skipped: no saved Silo session. server=%r username=%r"
                        % (client.base, client.cfg.get("username")),
                        xbmc.LOGWARNING,
                    )
            except LibrarySyncDisabled:
                log("Native library sync stopped because the setting was disabled", xbmc.LOGINFO)
            except Exception as exc:
                log("Native library sync failed; existing Kodi entries were kept: %s" % exc, xbmc.LOGERROR)
            last_sync = time.time()

        # Poll settings promptly so enabling starts a sync without waiting for
        # the previous interval. Also lets Kodi stop the service promptly.
        if monitor.waitForAbort(2):
            break

    log("Native library integration service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    run()
