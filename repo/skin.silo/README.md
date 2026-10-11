# Silo Kodi Skin

A Kodi skin add-on focused on the Silo Media Server add-on.

## Status

Early first pass. Version 0.1.1 corrects the Kodi 21 Omega skin ABI declaration and removes a hard dependency on a specific version of `plugin.video.silo`. This is not yet a complete replacement for every Kodi window: directory browsing, video information, playback OSD, dialogs, settings, and watch-party flows still need dedicated layouts and testing on Kodi 21.

## Install for development

1. Install `skin.silo-0.1.1.zip` from the compatible package, or copy the `skin.silo` directory into Kodi's `addons` directory.
2. Ensure `plugin.video.silo` is installed and configured to use the Silo features.
3. In Kodi, open **Settings → Interface → Skin** and select **Silo**.

## Design

- Dark navy canvas with Silo mint accent.
- A simple, remote-friendly home screen with large focus targets.
- Entry points into Silo and Silo settings.
- No duplicate login or server API code; Silo-Kodi remains responsible for server communication.

## Compatibility notes

Kodi 21.3 Omega requires `xbmc.gui` 5.17.0. The skin manifest now declares that requirement. The action routes for search, watchlist, and history remain placeholders until aligned with the current Silo-Kodi router; Browse Silo and Silo Settings are the initial known-safe paths.
