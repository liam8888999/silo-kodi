"""Background worker for the opt-in Silo native Kodi library integration."""

import time

import xbmc
import xbmcaddon

from resources.lib.silo import SiloClient, log
from resources.lib.library_sync import sync_current_profile


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
    last_sync = 0
    log("Native library integration service started", xbmc.LOGINFO)

    while not monitor.abortRequested():
        if not _enabled():
            if monitor.waitForAbort(10):
                break
            continue

        interval = _interval_seconds()
        if time.time() - last_sync >= interval:
            try:
                client = SiloClient()
                # Do not trigger interactive login prompts from a background
                # service. Sync only when a saved authenticated session exists.
                if client.base and (
                    client.cfg.get("token") or client.cfg.get("refresh_token")
                ):
                    stats = sync_current_profile(client, monitor=monitor)
                    log("Native library sync finished: %s" % stats, xbmc.LOGINFO)
                else:
                    log("Native library sync skipped: sign in through the Silo add-on first", xbmc.LOGDEBUG)
            except Exception as exc:
                log("Native library sync failed; existing Kodi entries were kept: %s" % exc, xbmc.LOGWARNING)
            last_sync = time.time()

        # Sleep in short intervals so Kodi can stop the service promptly.
        remaining = max(1, interval - (time.time() - last_sync))
        if monitor.waitForAbort(min(10, remaining)):
            break

    log("Native library integration service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    run()
