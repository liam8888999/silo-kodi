# Silo Kodi Skin

A Silo-focused Kodi skin designed for Kodi 21.3 Omega.

## Version 0.1.2

- Fix malformed shared include XML which could prevent the skin from loading.
- Add a self-contained Silo-first home screen and a Silo directory view.
- Add a Kodi settings hub, settings-category templates, profile/system information pages, and a dedicated settings dialog.
- Add keyboard, selection, confirmation, and busy dialogs for common Silo and Kodi actions.
- Declare Kodi 21.3's `xbmc.gui` 5.17.0 ABI and the required `font13` definition.

## Install

Install `skin.silo-0.1.2.zip` using Kodi's **Add-ons → Install from zip file** option, then select **Silo** under **Settings → Interface → Skin**. Keep the Silo video add-on installed separately; this skin does not duplicate its authentication or API.

## Known limitations

This is a recovery/usability build, not yet a complete skin replacement. Some less-common dialogs and advanced windows still need skin-specific layouts. The XML and ZIP structure have been validated, but the skin has not yet been smoke-tested in a running Kodi 21.3 session.
