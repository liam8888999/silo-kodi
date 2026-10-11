# Silo Kodi Skin

A Kodi skin add-on focused on the Silo Media Server add-on.

## Status

Early first pass. This establishes the standalone `skin.silo` add-on identity and a Silo-branded home screen. It is not yet a complete replacement for every Kodi window: directory browsing, video information, playback OSD, dialogs, settings, and watch-party flows still need dedicated skin layouts and testing on Kodi 21.

## Install for development

1. Copy `skin.silo` into Kodi's `addons` directory, or package this directory as `skin.silo-0.1.0.zip` with the folder at the ZIP root.
2. Ensure `plugin.video.silo` is installed and configured first.
3. In Kodi, open **Settings → Interface → Skin** and select **Silo**.

## Design

- Dark navy canvas with Silo mint accent.
- A simple, remote-friendly home screen with large focus targets.
- Direct entry points into Silo and Silo settings.
- No duplicate login or server API code; Silo-Kodi remains responsible for server communication.

## Compatibility notes

This skin is intended for Kodi 21 / Python 3. The action routes for search, watchlist, and history must be aligned with the current Silo-Kodi router before those shortcuts are considered complete. The Browse Silo and Silo Settings entry points are the initial safe paths.
