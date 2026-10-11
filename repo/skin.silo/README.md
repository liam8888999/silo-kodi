# Silo Kodi Skin

A Silo-focused Kodi skin designed for Kodi 21.3 Omega.

## Version 0.1.2

- Fix malformed shared include XML which could prevent the skin from loading.
- Replace the home window with a self-contained Silo-first screen.
- Add a Kodi settings hub, settings-category controls, a settings dialog, a profiles page, system information, and a calibration fallback.
- Use Kodi 21.3's `xbmc.gui` 5.17.0 ABI.

## Install

Install `skin.silo-0.1.2.zip` from Kodi's **Add-ons → Install from zip file** option, then select **Silo** from **Settings → Interface → Skin**. Keep the Silo video add-on installed separately; this skin does not duplicate its authentication or API.

## Known limitations

This is a usability recovery build, not yet a full copy of Estuary. Kodi settings pages have dedicated layouts, but less-common modal dialogs and some advanced windows still need complete skin-specific implementations and runtime testing on Kodi 21.3.
