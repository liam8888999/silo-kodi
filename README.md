# Silo for Kodi

A Kodi add-on for browsing and playing media from a [Silo Server](https://github.com/Silo-Server/silo-server) instance.

Silo acts as the media server, while this add-on provides a Kodi interface for accessing the media available through it.

---

## Discord Server

join for help or feedback

https://discord.gg/fNrAH8BG6

---

## Kodi Repository Address

https://liam8888999.github.io/silo-kodi/

---

## ✨ Features

* 🎬 Browse movies and TV shows through Silo
* 📺 Browse TV series and episodes
* ▶️ Play media directly through Kodi
* 🔌 Connect to a Silo Server instance
* 🌐 Access media from your Silo Server directly through Kodi
*  Supports searching the library using requests directly to the server so you can quickly find your favourites
*  Transcode Support
*  Adaptive quality switching if you have a bad network connection or something cant be played
*  Option to only allow direct plays from the server (always preferred over transcoding anyway)
*  Automatically send watch progress back to the server (updated roughly every 5 seconds for those curious ones)
*  Supports the terminate stream button on the server and will stop playing when that signal is recieved (usually within a couple of seconds)
*  Automatically makes kodi reflect resume points on your server incase you go off and start using another client when you come back and click to play on the video it may offer a different time to resume from (quite annoyingly kodi caches it so i cant change this) but it will in the background jump to wherever your server reports you are up to
  
---

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

You will then need to put in your username and password

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
