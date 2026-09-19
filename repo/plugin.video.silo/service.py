# -*- coding: utf-8 -*-
"""Resident background metadata service for the Silo Kodi add-on."""
import hashlib
import json

import xbmc
import xbmcgui

from resources.lib.silo import SiloClient, SiloError

REQUEST_PROPERTY = "Silo.MetadataRequest"
READY_PREFIX = "Silo.MetadataReady."
DATA_PREFIX = "Silo.MetadataData."


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[plugin.video.silo] %s" % message, level)


def metadata_key(content_id, library_id):
    value = "v1:%s:%s" % (str(library_id or ""), str(content_id or ""))
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def main():
    monitor = xbmc.Monitor()
    window = xbmcgui.Window(10000)
    client = None

    while not monitor.abortRequested():
        raw = window.getProperty(REQUEST_PROPERTY)

        if raw:
            window.clearProperty(REQUEST_PROPERTY)

            try:
                request = json.loads(raw)
            except (TypeError, ValueError):
                request = {}

            library_id = request.get("library_id")
            content_ids = request.get("content_ids") or []

            if library_id and content_ids:
                try:
                    if client is None:
                        client = SiloClient()

                    details = {}

                    for content_id in content_ids:
                        if monitor.abortRequested():
                            break

                        try:
                            detail = client.item_detail(
                                content_id,
                                library_id,
                            )
                        except SiloError as exc:
                            log(
                                "Background metadata lookup failed for %s: %s"
                                % (content_id, exc),
                                xbmc.LOGWARNING,
                            )
                            continue

                        if detail:
                            window.setProperty(
                                DATA_PREFIX
                                + metadata_key(content_id, library_id),
                                json.dumps(
                                    detail,
                                    separators=(",", ":"),
                                ),
                            )
                            details[str(content_id)] = True

                    if details:
                        window.setProperty(
                            READY_PREFIX + str(library_id),
                            json.dumps(
                                list(details.keys()),
                                separators=(",", ":"),
                            ),
                        )

                except Exception as exc:
                    log(
                        "Background metadata service failed: %s" % exc,
                        xbmc.LOGWARNING,
                    )

        if monitor.waitForAbort(0.25):
            break


if __name__ == "__main__":
    main()
