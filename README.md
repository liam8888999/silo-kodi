# Silo Media Server client for Kodi

A community-developed Kodi client compatible with [Silo Server](https://github.com/Silo-Server/silo-server).

<p align="center"><img src="repo/plugin.video.silo/resources/icon.png" alt="Media Bridge for Kodi icon" width="192"></p>

**Independent community project:** this add-on is not affiliated with, endorsed by, sponsored by, or an official product of Silo Media L.L.C. or the Kodi Foundation. **No media, streams, or subscriptions are included or supplied.** You must configure access to your own Silo Server and provide your own media access.

---

## Discord Server

join for help or feedback

https://discord.gg/dyae2tv7Qs

---

## Kodi Repository Address

https://liam8888999.github.io/silo-kodi/

---

## ✨ Features

### Libraries, browsing and discovery

* Browse movies, TV series, seasons and episodes from your Silo libraries.
* Search your library, including people, and browse person-related media where supported by the server.
* Browse Silo Home sections and Discover recommendations.
* View personal lists including Watchlist, Favourites and history, and access personal collections.
* Browse large libraries with automatic API pagination rather than manually navigating page numbers.
* Inspect alternate file versions before playback, including available media/quality details.

### Playback

* Play media through Kodi using Silo's playback API, including server-planned direct play and transcoding.
* Adaptive quality switching uses the quality options published by Silo Server and can step down when sustained buffering is detected.
* Optional **Only allow direct play** setting disables adaptive quality switching and rejects playback plans that require transcoding or HLS adaptation.
* Report Kodi playback progress to Silo Server approximately every five seconds, so progress is shared with other clients.
* Use the latest server-side resume position with Kodi's native Resume / Start from beginning prompt.
* Report playback capabilities to Silo Server, including supported video/audio and display HDR details where Kodi exposes them, to help the server plan playback.
* Skip available intro, recap and credits segments. You can choose prompts or enable automatic skipping separately for intros, recaps and credits in the add-on settings.
* Support server-side playback termination, which stops the active Kodi stream when Silo Server revokes or terminates its session.

### Accounts, profiles and connectivity

* Sign in using a username/password or device code when supported by the server.
* Switch between PIN-protected profiles; the active profile is shown in the root menu, and closing the profile chooser keeps the current profile.
* Save multiple Silo servers/accounts, switch between them, and reconnect after logging out without re-entering saved account details.
* Show a server's custom name in the server switcher when available, falling back to its URL for the default server name.
* Join an existing Silo Watch Party and synchronise playback with the room.

## 📝 Changelog

The release history below records the features and fixes introduced in the versions for which notes are available. Some older releases are omitted because no notes were supplied for them.

*** V1.1.20 ***
* Add support for adding/removing items from Favourites and Watchlist, and marking items watched/unwatched on the server (native Kodi buttons may still appear)
* Improve episode metadata
* Add a "View parent item" context-menu option where applicable, such as opening a show's root from an episode in Continue Watching
* Add directory images where available
* Add a Discover section
* Add preliminary support for skipping credits
* Add multi-server support

*** V1.1.10 ***
* Add login by device code

*** V1.1.9 ***
* Add detailed information to the version picker

*** V1.1.8 ***
* Support removal of direct-play availability by the server while retaining the add-on's direct-play-only setting
* Improve support for HDR content

*** V1.1.6 ***
* Reduce unnecessary adaptive quality switching
* Stop identifying the add-on as a Jellyfin app in the server activity dashboard

*** V1.1.5 ***
* Group library contents by letter
* Optimise API usage to improve responsiveness
* Improve session refresh to reduce unexpected logouts and repeated password prompts

*** V1.1.1 ***
* Fix "library not found" playback errors for Home category items
* Add an option to use Kodi's cached resume position (disabled by default)
* Add Your Stuff: Favourites, Watchlist, Collections and History
* Add view-only Watch Party support

*** V1.1.0 ***
* Add Home categories such as Continue Watching, Recently Released and Recommended
* Add an option to combine similar Home categories across libraries
* Show banner artwork instead of a text title in the player

*** V1.0.12 ***
* When Kodi requests Resume but the server has no resume position, start at 0.1 seconds
* When Kodi has no resume point but the server does, automatically resume from the server position
* Use server watched states in Search, matching library views
* Add episode counts and season artwork to show libraries and Search
* Improve metadata and add episode numbers when a skin does not already display them

*** V1.0.11 ***
* Fix playback position when the server no longer has a resume point, returning to the beginning instead of using Kodi's stale resume point

*** V1.0.10 ***
* Prevent adaptive quality from rapidly switching up and down; find a suitable quality and prefer direct play
* Add a setting to always use direct play from the server
* Respect stream-termination requests from the server

*** V1.0.9 ***
* Add transcoding support (the author noted this had not been personally tested at the time)
* Add adaptive quality switching

*** V1.0.8 ***
* Show TV shows in Search
* Add a login loading indicator

*** V1.0.7 ***
* Add extra metadata, with information intended to be sourced from the server
* Sync resume points in both directions: check the server when playback starts and update Kodi if the server position changed since the item was loaded
* Add Search
* Add configurable settings (an additional login may be required after upgrading from an older version)
* Add artwork for libraries

## 📋 Requirements

You will need:

* [Kodi](https://kodi.tv/) installed on your device
* A running **Silo Server** instance
* Network connectivity between Kodi and Silo Server

Silo Server is available here:

https://github.com/Silo-Server/silo-server

---

# 🚀 Installation

The Silo repository can be installed directly from Kodi without manually downloading the repository ZIP.

## 1. Enable Unknown Sources

Kodi needs permission to install add-ons from ZIP files.

From the Kodi home screen, go to:

```text
Settings
  → System
    → Add-ons
      → Unknown sources
```

Enable **Unknown sources** and confirm the warning.

You only need to do this once.

---

## 2. Add the Silo Repository to Kodi's File Manager

Open:

```text
Settings
  → File Manager
```

Select:

```text
Add source
```

Enter the following address:

```text
https://liam8888999.github.io/silo-kodi/
```

For the name, enter:

```text
Silo Repository
```

Then select **OK**.

You should now have a file source called:

```text
Silo Repository
```

in Kodi's File Manager.

---

## 3. Install the Repository ZIP

Return to:

```text
Add-ons
  → Add-on browser
  → Install from zip file
```

Select:

```text
Silo Repository
```

You should see the repository ZIP file.

Select the ZIP file, for example:

```text
repository.silo-kodi-1.0.2.zip
```

Kodi will install the **Silo Kodi Repository** directly from the hosted repository.

> **You do not need to download or extract the ZIP file manually.**

---

## 4. Install the Silo Add-on

Once the repository has been installed, go to:

```text
Add-ons
  → Add-on browser
  → Install from repository
```

Select:

```text
Silo Kodi Repository
```

Then select the **Silo** add-on and choose:

```text
Install
```

Kodi will install the Silo add-on and any required dependencies.

---

# ⚙️ Configuration

After installing the add-on, open the Silo and login. 

First it will ask for your server address

For example:

```text
http://192.168.1.100:8080
```

Replace this with the address and port of your own Silo Server.

**Important:** Use the Silo app/API URL, not the server's Jellyfin-compatibility URL.

### Local Silo installation

If Silo Server is running on the same device as Kodi, you may be able to use:

```text
http://localhost:8080
```

If Silo is running on another device, use that device's IP address or hostname instead.

For example:

```text
http://192.168.1.50:8080
```

Follow the sign-in prompts. Depending on the server, you can sign in with a device code or a username and password, then choose your profile and enter its PIN if prompted.

---

# 🎬 Using Silo

Once configured, open **Silo** from Kodi's add-ons.

The add-on communicates with your Silo Server and displays the media available through it.

Browse your available media and select a movie or episode to begin playback.

The basic architecture is:

```text
                    ┌─────────────────┐
                    │      Kodi       │
                    │                 │
                    │   Silo Add-on   │
                    └────────┬────────┘
                             │
                             │ API
                             ▼
                    ┌─────────────────┐
                    │   Silo Server   │
                    │                 │
                    │  Media Server   │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Media Sources  │
                    └─────────────────┘
```

Kodi provides the interface and playback, while Silo provides access to the media.

---

# 🔄 Updates

The Silo add-on is distributed through a Kodi repository, allowing new versions to be delivered through Kodi without manually reinstalling the add-on.

To manually check for updates:

```text
Add-ons
  → My add-ons
  → Silo
  → Check for updates
```

If automatic add-on updates are enabled in Kodi, updates may be installed automatically.

The repository is hosted at:

https://liam8888999.github.io/silo-kodi/

---

# 🛠️ Troubleshooting

## Repository will not install

Make sure the File Manager source was added correctly:

```text
https://liam8888999.github.io/silo-kodi/
```

The source should be named:

```text
Silo Repository
```

Then check:

```text
Add-ons
  → Add-on browser
  → Install from zip file
  → Silo Repository
```

The repository ZIP should be visible there.

---

## Silo does not appear in the repository

Check that:

1. The Silo repository installed successfully.
2. You selected **Install from repository** after installing it.
3. Kodi has network access.
4. The repository ZIP is the current version.

Restarting Kodi can also force Kodi to refresh its add-on information.

---

## Kodi cannot connect to Silo Server

Check that Silo Server is running and accessible from the Kodi device.

For example, if Silo is running at:

```text
http://192.168.1.100:8080
```

make sure the Kodi device can reach that address.

Common causes include:

* Incorrect Silo Server address
* Incorrect port
* Silo Server is not running
* Firewall blocking the connection
* Kodi and Silo being on isolated networks
* Incorrect hostname or IP address

---

## Media appears but will not play

If the library can be browsed but playback fails, first verify that the media can be accessed successfully through Silo Server.

If it still wont play go to settings and enable "only allow direct play" under the play back settings which will disable transcoding completely and hopefully give a better experience

If that still doesnt work then check the Kodi log for additional information.

When reporting a playback problem, include the relevant Kodi log output where possible.

---

## The addon is taking a long time to load my libraries

Sometimes if connection to your library is a bit slow it can take kodi longer to load the libraries into the addon, one way to help improve this is to set pagination to a smaller number in the addon settings

---

# 🧑‍💻 Development

The Silo Kodi add-on is contained in:

```text
plugin.video.silo/
```

A typical add-on structure is:

```text
plugin.video.silo/
├── addon.xml
├── addon.py
└── ...
```

The repository contains the metadata required for Kodi to discover and install the add-on.

---

# 📦 Kodi Repository

The repository is hosted using GitHub Pages:

**https://liam8888999.github.io/silo-kodi/**

Kodi uses the repository to:

* Discover the Silo add-on
* Determine the available version
* Download the add-on
* Download required dependencies
* Receive future updates

---

# 🐛 Issues & Feature Requests

If you encounter a bug or have an idea for improving the add-on, please open an issue on GitHub.

When reporting a problem, include as much useful information as possible:

* Kodi version
* Operating system
* Silo Server version
* Silo add-on version
* Description of the problem
* Relevant Kodi log output

Please remove passwords, API keys, private URLs and other sensitive information before posting logs.

---

# 🔗 Related Projects

### Silo Server

The server used by this add-on to provide access to your media.

https://github.com/Silo-Server/silo-server

---

## ⭐ Support the Project

If you find the Silo Kodi add-on useful, consider giving the project a ⭐ on GitHub.

Bug reports, feature requests and contributions are welcome.
