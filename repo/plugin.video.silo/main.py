"""Kodi plugin entry point for Silo Server.

Responsibilities in this file:
    * Build Kodi's directory UI.
    * Browse Silo libraries, series, seasons and episodes.
    * Select a playable Silo file/version.
    * Ask Silo for the freshest resume position when Play is selected.
    * Start Silo playback at that server position.
    * Report Kodi's live playback position back to Silo.

Important resume behaviour:
    Silo is the source of truth. The catalog supplies the watched flag, while a
    lightweight status=in_progress progress request supplies detailed partial
    positions. This avoids downloading the full progress history every time a
    library is opened while keeping Kodi's partial-watch indicators accurate.

    When Play is selected, play() still performs a SECOND fresh /api/v2/progress
    lookup immediately before playback. That fresh server value is put on the
    resolved Kodi ListItem, allowing Kodi to show its normal single Resume/Play
    prompt using the current Silo position.

    Kodi -> Silo progress reporting is unchanged, so partial playback is still
    stored on the Silo server for other clients to see.

Large Kodi directory lists are sent with xbmcplugin.addDirectoryItems(), which
Kodi documents as more efficient for large lists than one addDirectoryItem() call
at a time. Pagination is handled internally by SiloClient and is never shown
to the user.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin

from resources.lib.silo import SiloClient, SiloError, log


# Kodi supplies a numeric handle for the current plugin directory.
HANDLE = int(sys.argv[1])

# Base plugin URL supplied by Kodi.
BASE_URL = sys.argv[0]

# Media types that this addon can send directly to Kodi's VideoPlayer.
PLAYABLE = (
    "movie",
    "episode",
    "video",
)

# Maximum number of entries shown in one Kodi directory page.
DIRECTORY_PAGE_SIZE = 200


# Build a Kodi plugin URL containing the action and any required IDs.
def paginate_directory(items, page):
    """Return one 200-item slice and whether another page exists."""
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    start = (page_number - 1) * DIRECTORY_PAGE_SIZE
    end = start + DIRECTORY_PAGE_SIZE

    return items[start:end], page_number > 1, end < len(items)


def add_previous_page(library_id=None, series_id=None, season_number=None,
                      action=None, page=1):
    """Add a Previous Page folder when the current directory is past page 1."""
    try:
        page_number = int(page or 1)
    except (TypeError, ValueError):
        page_number = 1

    if page_number <= 1:
        return

    params = {
        "action": action,
        "page": page_number - 1,
    }

    if library_id:
        params["library_id"] = library_id
    if series_id:
        params["series_id"] = series_id
    if season_number is not None:
        params["season_number"] = season_number

    item = xbmcgui.ListItem(label="Previous Page")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(**params),
        item,
        True,
    )


def add_next_page(library_id=None, series_id=None, season_number=None,
                  action=None, page=1):
    """Add a Next Page folder when another 200-item slice exists."""
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    params = {
        "action": action,
        "page": page_number + 1,
    }

    if library_id:
        params["library_id"] = library_id
    if series_id:
        params["series_id"] = series_id
    if season_number is not None:
        params["season_number"] = season_number

    item = xbmcgui.ListItem(label="Next Page")
    item.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(**params),
        item,
        True,
    )


def build_url(**params):
    return BASE_URL + "?" + urlencode(params)


# Display a short informational notification in Kodi.
def notify(message):
    xbmcgui.Dialog().notification(
        "Silo",
        message,
        xbmcgui.NOTIFICATION_INFO,
        3000,
    )


def format_position(seconds):
    """Convert a number of seconds into a simple human-readable timestamp."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0

    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)

    return "%d:%02d" % (minutes, seconds)


def fetch_detail_metadata(client, items, library_id, max_workers=8):
    """Fetch extended metadata concurrently before rendering Kodi items.

    Catalog data is fast and contains most metadata. The detail endpoint adds
    cast, crew and full file stream information. Each request runs in a worker
    with its own SiloClient/session so Kodi only waits for the slowest detail
    request rather than every request serially.
    """
    content_ids = []
    seen = set()

    for item in items:
        content_id = get_content_id(item)
        if not content_id:
            continue

        key = str(content_id)
        if key not in seen:
            seen.add(key)
            content_ids.append(content_id)

    if not content_ids:
        return {}

    def fetch_one(content_id):
        try:
            worker_client = SiloClient()
            worker_client.cfg.update(client.cfg)

            return content_id, worker_client.item_detail(
                content_id,
                library_id,
            )
        except SiloError as exc:
            log(
                "Unable to retrieve detail metadata for %s: %s" % (
                    content_id,
                    exc,
                ),
                xbmc.LOGWARNING,
            )
            return content_id, None

    details = {}
    worker_count = max(
        1,
        min(int(max_workers or 8), len(content_ids)),
    )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, content_id)
            for content_id in content_ids
        ]

        for future in as_completed(futures):
            content_id, detail = future.result()
            if detail:
                details[str(content_id)] = detail

    return details


def get_runtime_seconds(item):
    """Convert Silo's catalog runtime (minutes) into Kodi seconds."""
    if not item:
        return 0.0

    try:
        runtime = float(item.get("runtime", 0) or 0)
    except (TypeError, ValueError):
        runtime = 0.0

    return max(0.0, runtime * 60.0)


def get_progress_position(progress):
    """Safely read a Silo progress position and duration."""
    if not progress:
        return 0.0, 0.0

    try:
        position = float(progress.get("position_seconds", 0) or 0)
    except (TypeError, ValueError):
        position = 0.0

    try:
        duration = float(progress.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0

    position = max(0.0, position)
    duration = max(0.0, duration)

    # Protect against malformed data where Silo reports a position beyond duration.
    if duration > 0:
        position = min(position, duration)

    return position, duration


def _art_url(client, value):
    """Return an artwork URL from either a string or a small artwork dict."""
    if not value:
        return ""

    if isinstance(value, dict):
        value = (
            value.get("url")
            or value.get("src")
            or value.get("path")
        )

    if not value:
        return ""

    return client.abs_url(str(value))


def set_art(list_item, client, poster=None, backdrop=None, logo=None, still=None):
    """Apply Silo artwork to the Kodi ListItem.

    Current Silo CatalogItem responses expose poster_url, backdrop_url and
    logo_url. Older field names and dictionary-style artwork values are also
    accepted as fallbacks so artwork remains compatible with older servers.
    """
    poster_url = _art_url(client, poster)
    backdrop_url = _art_url(client, backdrop)
    logo_url = _art_url(client, logo)
    still_url = _art_url(client, still)

    art = {}

    if poster_url:
        art.update({
            "thumb": poster_url,
            "poster": poster_url,
            "icon": poster_url,
        })

    if backdrop_url:
        art["fanart"] = backdrop_url

    if logo_url:
        art["clearlogo"] = logo_url

    # Episode stills are useful as thumbnails when no poster is available.
    if still_url and not poster_url:
        art.update({
            "thumb": still_url,
            "icon": still_url,
        })

    if art:
        list_item.setArt(art)


def catalog_progress(item):
    """Convert Silo's catalog-level viewer state into our common progress shape.

    The Silo CatalogItem schema contains:
        user_state.played
        position_seconds
        duration_seconds

    Using these fields avoids a full /api/v2/progress download just to draw
    watched/resume markers in the library. The detailed progress endpoint is
    still queried fresh when the user actually starts playback.
    """
    if not item:
        return None

    user_state = item.get("user_state") or {}

    # A profile-scoped catalog should normally contain user_state. If it is
    # absent, return None rather than guessing the watch state.
    if not isinstance(user_state, dict) or not user_state:
        return None

    # Silo's catalog exposes the current resume position directly.
    position = item.get("position_seconds", 0)
    duration = item.get("duration_seconds", 0)

    try:
        position = max(0.0, float(position or 0))
    except (TypeError, ValueError):
        position = 0.0

    try:
        duration = max(0.0, float(duration or 0))
    except (TypeError, ValueError):
        duration = 0.0

    # Catalog runtime is in minutes; progress duration is in seconds.
    # Unwatched items may not have a progress duration, so use the catalog
    # runtime as the Kodi duration in that case.
    if duration <= 0:
        duration = get_runtime_seconds(item)

    return {
        "completed": bool(user_state.get("played", False)),
        "position_seconds": position,
        "duration_seconds": duration,
        "updated_at": item.get("progress_updated_at") or "",
    }


def set_catalog_metadata(list_item, item, client):
    """Apply metadata that Silo includes directly in CatalogItem responses.

    This deliberately uses only catalog data so opening a large library does
    not trigger one detail request per movie or episode.
    """
    if not item:
        return

    tag = list_item.getVideoInfoTag()
    media_type = (item.get("type") or item.get("media_type") or "").lower()

    # Kodi media types map Silo's catalog types to the native video types.
    kodi_media_type = {
        "movie": "movie",
        "series": "tvshow",
        "season": "season",
        "episode": "episode",
        "video": "video",
    }.get(media_type, "video")

    tag.setMediaType(kodi_media_type)

    title = item.get("title") or item.get("name")
    if title:
        tag.setTitle(title)

    # Silo normally supplies year on CatalogItem, but some TV series
    # can have a missing/zero year while their first-air/release date exists.
    # Derive the year from that date so Kodi does not lose it.
    year = item.get("year")

    try:
        year = int(year or 0)
    except (TypeError, ValueError):
        year = 0

    if year <= 0:
        date_candidates = []

        if media_type == "series":
            date_candidates.extend([
                item.get("first_air_date"),
            ])

        date_candidates.extend([
            item.get("release_date"),
            item.get("first_air_date"),
            item.get("air_date"),
        ])

        for date_value in date_candidates:
            if date_value:
                try:
                    year = int(str(date_value)[:4])
                except (TypeError, ValueError):
                    year = 0

                if year > 0:
                    break

    if year > 0:
        tag.setYear(year)

    genres = [str(value) for value in (item.get("genres") or []) if value]
    if genres:
        tag.setGenres(genres)

    studios = [str(value) for value in (item.get("studios") or []) if value]
    if studios:
        tag.setStudios(studios)

    countries = [str(value) for value in (item.get("countries") or []) if value]
    if countries:
        tag.setCountries(countries)

    keywords = [str(value) for value in (item.get("keywords") or []) if value]
    if keywords:
        tag.setTags(keywords)

    plot = item.get("overview") or item.get("plot")
    if plot:
        tag.setPlot(plot)
        tag.setPlotOutline(plot)

    if item.get("tagline"):
        tag.setTagLine(item["tagline"])

    if item.get("content_rating"):
        tag.setMpaa(item["content_rating"])

    if item.get("original_language"):
        try:
            tag.setOriginalLanguage(item["original_language"])
        except Exception:
            # Keep the catalog value available even when Kodi cannot recognise
            # the language code supplied by the server.
            list_item.setProperty(
                "Silo.OriginalLanguage",
                str(item["original_language"]),
            )

    if item.get("series_title"):
        tag.setTvShowTitle(item["series_title"])

    if item.get("show_status"):
        tag.setTvShowStatus(item["show_status"])

    if item.get("season_number") is not None:
        try:
            tag.setSeason(int(item["season_number"]))
        except (TypeError, ValueError):
            pass

    if item.get("episode_number") is not None:
        try:
            tag.setEpisode(int(item["episode_number"]))
        except (TypeError, ValueError):
            pass

    release_date = item.get("release_date")
    if release_date:
        if media_type == "episode":
            tag.setFirstAired(str(release_date))
        else:
            tag.setPremiered(str(release_date))

    if item.get("runtime"):
        try:
            duration_seconds = int(round(float(item["runtime"]) * 60))
        except (TypeError, ValueError):
            duration_seconds = 0

        if duration_seconds > 0:
            tag.setDuration(duration_seconds)

    # Preserve every rating Silo exposes. Kodi supports multiple named rating
    # types; IMDb is preferred as the default when it exists.
    ratings = {}
    rating_map = (
        ("imdb", item.get("rating_imdb")),
        ("tmdb", item.get("rating_tmdb")),
        ("rotten_tomatoes_critic", item.get("rating_rt_critic")),
        ("rotten_tomatoes_audience", item.get("rating_rt_audience")),
    )

    for rating_type, value in rating_map:
        if value is None:
            continue
        try:
            ratings[rating_type] = (float(value), 0)
        except (TypeError, ValueError):
            pass

    if ratings:
        default_rating = "imdb" if "imdb" in ratings else next(iter(ratings))
        try:
            tag.setRatings(ratings, default_rating)
        except AttributeError:
            # Kodi versions before the InfoTagVideo rating API can still
            # receive named ratings through ListItem.
            for rating_type, (value, votes) in ratings.items():
                list_item.setRating(
                    rating_type,
                    value,
                    votes,
                    rating_type == default_rating,
                )

    # Store identifiers that are useful to Kodi and to skins/addons.
    unique_ids = {}
    for key in ("imdb_id", "tmdb_id", "tvdb_id"):
        value = item.get(key)
        if value:
            unique_ids[key.replace("_id", "")] = str(value)

    if unique_ids:
        default_id = (
            "imdb" if "imdb" in unique_ids
            else "tmdb" if "tmdb" in unique_ids
            else "tvdb"
        )
        try:
            tag.setUniqueIDs(unique_ids, default_id)
        except AttributeError:
            list_item.setUniqueIDs(unique_ids, default_id)

        for key, value in unique_ids.items():
            list_item.setProperty("Silo.%sID" % key.upper(), value)

    # The catalog has a few useful fields with no dedicated Kodi video-info
    # field. Expose them as ListItem properties so skins can still access them.
    if item.get("networks"):
        list_item.setProperty(
            "Silo.Networks",
            " / ".join(str(value) for value in item["networks"] if value),
        )

    for key in (
        "status",
        "item_source",
        "work_id",
        "work_title",
    ):
        value = item.get(key)
        if value:
            list_item.setProperty("Silo.%s" % key.title(), str(value))

    overlay = item.get("overlay_summary") or {}
    if isinstance(overlay, dict):
        for key in (
            "resolution",
            "hdr",
            "audio",
            "audio_channels",
            "video_codec",
            "container",
            "aspect_ratio",
            "release_type",
            "edition",
            "multi_audio",
            "multi_sub",
        ):
            value = overlay.get(key)
            if value not in (None, "", False):
                list_item.setProperty(
                    "Silo.%s" % "".join(part.title() for part in key.split("_")),
                    str(value),
                )


def _detail_version(detail, file_id=None):
    """Select the Silo file version Kodi should use for pre-play details."""
    versions = detail.get("versions") or []

    if file_id is not None:
        wanted = str(file_id)

        for version in versions:
            if str(version.get("file_id") or version.get("id")) == wanted:
                return version

    # The detail endpoint tells us which version its library/playback
    # presentation resolved to. Prefer that instead of assuming versions[0].
    effective_resolution = detail.get("effective_version_resolution")
    effective_hdr = detail.get("effective_version_hdr")
    effective_codec = detail.get("effective_version_codec_video")
    effective_edition = detail.get("effective_version_edition_key")

    best = None
    best_score = -1

    for version in versions:
        score = 0

        if effective_resolution and str(version.get("resolution") or "") == str(effective_resolution):
            score += 4

        if effective_hdr is not None and bool(version.get("hdr")) == bool(effective_hdr):
            score += 2

        if effective_codec and str(version.get("codec_video") or "") == str(effective_codec):
            score += 2

        if effective_edition and str(version.get("edition_key") or "") == str(effective_edition):
            score += 2

        if score > best_score:
            best = version
            best_score = score

    return best or (versions[0] if versions else {})


def _aspect_ratio(value):
    """Convert Silo's aspect ratio into Kodi's numeric float form."""
    if value in (None, ""):
        return 0.0

    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    text = str(value).strip()

    if ":" in text:
        parts = text.split(":", 1)
        try:
            width = float(parts[0])
            height = float(parts[1])
            if height:
                return width / height
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return 0.0


def set_stream_details(list_item, version):
    """Populate Kodi's pre-playback video, audio and subtitle stream details."""
    if not version:
        return

    tag = list_item.getVideoInfoTag()
    video_tracks = version.get("video_tracks") or []
    audio_tracks = version.get("audio_tracks") or []
    subtitle_tracks = version.get("subtitle_tracks") or []
    duration = int(version.get("duration") or 0)

    for track in video_tracks:
        aspect = _aspect_ratio(track.get("aspect_ratio"))

        info = {}
        if track.get("codec"):
            info["codec"] = track["codec"]
        if track.get("width"):
            info["width"] = int(track["width"])
        if track.get("height"):
            info["height"] = int(track["height"])
        if aspect > 0:
            info["aspect"] = aspect
        if duration > 0:
            info["duration"] = duration
        if track.get("language"):
            info["language"] = track["language"]

        hdr = (
            track.get("dolby_vision")
            or ("dolbyvision" if track.get("dv_profile") else "")
            or (
                "hdr10"
                if str(
                    track.get("video_range_type", "")
                ).upper().startswith("HDR10")
                else ""
            )
            or (
                "hlg"
                if str(
                    track.get("video_range_type", "")
                ).upper().startswith("HLG")
                else ""
            )
        )

        if hdr:
            info["hdrtype"] = hdr

        # Preserve additional probed values for skins/addons even where Kodi's
        # native stream API has no corresponding setter.
        for key in (
            "profile",
            "level",
            "bitrate",
            "frame_rate",
            "bit_depth",
            "color_space",
            "color_transfer",
            "color_primaries",
            "pixel_format",
        ):
            value = track.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Video.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

        if info:
            try:
                list_item.addStreamInfo("video", info)
            except Exception:
                pass

        try:
            stream = xbmc.VideoStreamDetail(
                int(track.get("width") or 0),
                int(track.get("height") or 0),
                aspect,
                duration,
                str(track.get("codec") or ""),
                "",
                str(track.get("language") or ""),
                str(hdr or ""),
            )
            tag.addVideoStream(stream)
        except Exception:
            pass

    # Fallback for detail responses containing only version-level video data.
    if not video_tracks and (
        version.get("codec_video") or version.get("resolution")
    ):
        resolution = str(version.get("resolution") or "")
        width = height = 0

        if "x" in resolution.lower():
            try:
                width, height = [
                    int(v)
                    for v in resolution.lower().split("x", 1)
                ]
            except (TypeError, ValueError):
                pass
        elif resolution.lower().endswith("p"):
            try:
                height = int(resolution[:-1])
            except ValueError:
                pass

        info = {
            "codec": version.get("codec_video") or "",
            "duration": duration,
        }

        if width:
            info["width"] = width
        if height:
            info["height"] = height

        try:
            list_item.addStreamInfo("video", info)
        except Exception:
            pass

        try:
            tag.addVideoStream(
                xbmc.VideoStreamDetail(
                    width,
                    height,
                    _aspect_ratio(version.get("aspect_ratio")),
                    duration,
                    str(version.get("codec_video") or ""),
                )
            )
        except Exception:
            pass

    for track in audio_tracks:
        info = {}

        if track.get("codec"):
            info["codec"] = track["codec"]
        if track.get("language"):
            info["language"] = track["language"]
        if track.get("channels"):
            info["channels"] = int(track["channels"])

        for key in (
            "title",
            "profile",
            "layout",
            "bitrate",
            "sample_rate",
            "bit_depth",
        ):
            value = track.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Audio.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

        if info:
            try:
                list_item.addStreamInfo("audio", info)
            except Exception:
                pass

        try:
            tag.addAudioStream(
                xbmc.AudioStreamDetail(
                    int(track.get("channels") or 0),
                    str(track.get("codec") or ""),
                    str(track.get("language") or ""),
                )
            )
        except Exception:
            pass

    for track in subtitle_tracks:
        language = str(
            track.get("language")
            or track.get("title")
            or ""
        )

        if language:
            try:
                list_item.addStreamInfo(
                    "subtitle",
                    {"language": language},
                )
            except Exception:
                pass

            try:
                tag.addSubtitleStream(
                    xbmc.SubtitleStreamDetail(language)
                )
            except Exception:
                pass


def set_detail_metadata(list_item, detail, client, file_id=None):
    """Apply Silo detail-only metadata such as cast, crew and stream tracks."""
    if not detail:
        return

    # CatalogItemDetail embeds the complete CatalogItem. Apply those
    # fields here too because detail-only responses contain IDs, countries and
    # other values that are not present on the browse card.
    set_catalog_metadata(
        list_item,
        detail,
        client,
    )

    tag = list_item.getVideoInfoTag()

    if detail.get("sort_title"):
        try:
            tag.setSortTitle(detail["sort_title"])
        except Exception:
            pass

    if detail.get("original_title"):
        try:
            tag.setOriginalTitle(detail["original_title"])
        except Exception:
            pass

    if detail.get("first_air_date"):
        try:
            tag.setFirstAired(str(detail["first_air_date"]))
        except Exception:
            pass

    if detail.get("air_date"):
        try:
            tag.setPremiered(str(detail["air_date"]))
        except Exception:
            pass

    cast = []
    cast_info = []

    for person in detail.get("cast") or []:
        name = person.get("name")
        if not name:
            continue

        role = str(person.get("character") or "")
        thumbnail = client.abs_url(person.get("photo_url") or "")
        try:
            order = int(person.get("order") or 0)
        except (TypeError, ValueError):
            order = 0

        # Kodi 20+ InfoTagVideo.setCast() expects xbmc.Actor objects.
        # Keep the older dictionary representation as a fallback for Kodi
        # builds/skins that still use ListItem.setCast().
        try:
            cast.append(
                xbmc.Actor(
                    str(name),
                    role,
                    order,
                    thumbnail,
                )
            )
        except Exception:
            pass

        actor_info = {"name": str(name)}
        if role:
            actor_info["role"] = role
        if thumbnail:
            actor_info["thumbnail"] = thumbnail
        if order:
            actor_info["order"] = order
        cast_info.append(actor_info)

    if cast:
        try:
            tag.setCast(cast)
        except Exception:
            pass

    if cast_info and not cast:
        try:
            list_item.setCast(cast_info)
        except Exception:
            pass

    directors = []
    writers = []
    credits = []

    for person in detail.get("crew") or []:
        name = person.get("name")
        job = str(person.get("job") or "").strip()
        if not name:
            continue

        name = str(name)
        if job:
            credits.append(name)

        job_lower = job.lower()
        if job_lower == "director" or job_lower == "directors":
            directors.append(name)

        if any(word in job_lower for word in (
            "writer",
            "screenplay",
            "screenwriter",
            "story",
        )):
            writers.append(name)

    try:
        if directors:
            tag.setDirectors(directors)
        if writers:
            tag.setWriters(writers)
    except Exception:
        # Keep each crew category independent so a single unsupported setter
        # cannot prevent the other metadata from being stored.
        try:
            if directors:
                tag.setDirectors(directors)
        except Exception:
            pass
        try:
            if writers:
                tag.setWriters(writers)
        except Exception:
            pass

    # Kodi does not expose a separate native "all crew" field through the
    # current InfoTagVideo setter API. Writers/directors are the useful native
    # categories; preserve the complete role/name list as a Silo property.
    if credits:
        list_item.setProperty("Silo.Crew", " / ".join(credits))

    version = _detail_version(detail, file_id)
    set_stream_details(list_item, version)

    # Full-detail runtime is the actual selected file duration in seconds.
    if version.get("duration"):
        try:
            duration = int(version["duration"])
            tag.setDuration(duration)
            list_item.setInfo("video", {"duration": duration})
        except (TypeError, ValueError):
            pass


def set_watch_state(list_item, progress, content_type=None):
    """Apply Silo's current watched/resume state to a Kodi ListItem.

    Kodi uses the VideoInfoTag methods setPlaycount() and setResumePoint().
    The exact capitalization matters: it is setPlaycount, not setPlayCount.

    This state is primarily for Kodi's library UI. play() still performs a
    fresh server lookup immediately before playback, so the displayed value is
    never trusted as the final resume position.
    """
    if not progress:
        return

    completed = bool(progress.get("completed", False))
    position, duration = get_progress_position(progress)
    tag = list_item.getVideoInfoTag()

    # Runtime is independent of resume state. Use setInfo() as well as
    # VideoInfoTag.setDuration() so Kodi's directory views receive the duration.
    if duration > 0:
        duration_int = int(round(duration))
        tag.setDuration(duration_int)

        info = {"duration": duration_int}
        if content_type:
            info["mediatype"] = str(content_type)
        list_item.setInfo("video", info)

    if completed:
        # Silo says the item is fully watched.
        tag.setPlaycount(1)
        return

    # Anything incomplete is explicitly unwatched/in progress.
    tag.setPlaycount(0)

    # Store the server resume marker so Kodi/skins can show the item as
    # partially played. Kodi documents setResumePoint(time, totalTime) for this.
    if position > 0 and duration > 0:
        tag.setResumePoint(position, duration)


def get_content_id(item):
    """Return a Silo catalog item's content ID."""
    return item.get("content_id") or item.get("id")


def add_catalog_item(client, item, library_id):
    """Convert one Silo catalog object into a Kodi ListItem."""
    content_id = get_content_id(item)

    if not content_id:
        log("Skipping catalog item with no content ID", xbmc.LOGWARNING)
        return

    title = item.get("title") or item.get("name") or "Unknown"
    media_type = (item.get("type") or item.get("media_type") or "").lower()

    list_item = xbmcgui.ListItem(label=title)
    tag = list_item.getVideoInfoTag()
    tag.setTitle(title)

    set_catalog_metadata(list_item, item, client)

    # Copy basic metadata that Kodi can display.
    if item.get("year"):
        try:
            tag.setYear(int(item["year"]))
        except (TypeError, ValueError):
            pass

    if item.get("plot"):
        tag.setPlot(item["plot"])

    set_art(
        list_item,
        client,
        # Current Silo v2 artwork fields.
        poster=(
            item.get("poster_url")
            or item.get("poster")
            or item.get("image")
            or item.get("artwork")
            or item.get("thumbnail")
        ),
        backdrop=item.get("backdrop_url"),
        logo=item.get("logo_url"),
    )

    # Use the watched/resume state already included in the catalog response.
    # This avoids an extra full progress-table request for every library load.
    set_watch_state(
        list_item,
        catalog_progress(item),
        media_type,
    )

    is_playable = media_type in PLAYABLE
    play_content_id = item.get("play_content_id") or content_id

    if is_playable:
        list_item.setProperty("IsPlayable", "true")

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="play",
                content_id=play_content_id,
                library_id=library_id,
            ),
            list_item,
            False,
        )
        return

    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(
            action="seasons",
            series_id=content_id,
            library_id=library_id,
        ),
        list_item,
        True,
    )


def list_root(client, page=None):
    """Display the initial screen or the logged-in Silo libraries.

    The addon deliberately does not start the login dialogue automatically.
    When no account is authenticated, Kodi shows a simple Login button and the
    user must select it before any server/account information is requested.
    """

    # --------------------------------------------------------------
    # NOT LOGGED IN
    # --------------------------------------------------------------
    # This restores the original addon behaviour: merely opening the addon
    # does not immediately ask for credentials.
    if not client.cfg.get("token"):
        login_item = xbmcgui.ListItem(label="Login")

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="login"),
            login_item,
            False,
        )

        xbmcplugin.setContent(HANDLE, "files")
        xbmcplugin.endOfDirectory(HANDLE)
        return

    # --------------------------------------------------------------
    # LOGGED IN
    # --------------------------------------------------------------
    libraries = client.libraries()
    page_items, has_previous, has_next = paginate_directory(
        libraries,
        page,
    )

    add_previous_page(
        action="root",
        page=page,
    )

    for library in page_items:
        library_id = library.get("id")
        if not library_id:
            continue

        title = library.get("name") or library.get("title") or "Library"
        item = xbmcgui.ListItem(label=title)

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(action="library", library_id=library_id),
            item,
            True,
        )

    # Let the user change the active Silo household profile.
    profile_item = xbmcgui.ListItem(label="Switch Profile")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="switch_profile"),
        profile_item,
        False,
    )

    # Clear local authentication/profile state and require a fresh login next time.
    logout_item = xbmcgui.ListItem(label="Logout")
    xbmcplugin.addDirectoryItem(
        HANDLE,
        build_url(action="logout"),
        logout_item,
        False,
    )

    if has_next:
        add_next_page(
            action="root",
            page=page,
        )

    xbmcplugin.setContent(HANDLE, "files")
    xbmcplugin.endOfDirectory(HANDLE)


def list_library(client, library_id, cursor=None):
    """Display every item in a Silo library as efficiently as possible.

    The catalog supplies the viewer's watched flag. A small in-progress-only
    progress request supplies detailed partial positions; the full progress
    history remains reserved for the fresh playback check when Play is pressed.

    Kodi's addDirectoryItems() is used in batches because Kodi documents it as
    more efficient for large lists than repeatedly calling addDirectoryItem().
    """
    if not library_id:
        raise SiloError("No library ID was supplied.")

    # Load one 200-item Silo catalog page. Larger libraries use the cursor below.
    items, next_cursor = client.catalog_page(library_id, cursor=cursor, limit=200)

    # The normal catalog tells us whether an item is played, but the detailed
    # partial position is not guaranteed to be present on every catalog row.
    # Fetch ONLY currently in-progress records so Kodi can display accurate
    # resume bars without downloading the user's entire progress history.
    try:
        in_progress_map = client.in_progress_map(
            library_id=library_id
        )
    except SiloError as exc:
        # A progress-display failure must never stop the library from loading.
        log(
            "Unable to retrieve in-progress Silo records: %s" % exc,
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    xbmcplugin.setContent(HANDLE, "movies")

    # Fetch cast, crew and full stream details before Kodi receives the list.
    # Requests run concurrently so the entire library still renders once.
    detail_map = fetch_detail_metadata(
        client,
        items,
        library_id,
    )

    # Build Kodi entries first, then send them in batches. A batch size keeps
    # memory usage reasonable for very large libraries while still avoiding
    # thousands of individual Kodi plugin calls.
    batch = []
    batch_size = 500

    for catalog_item in items:
        content_id = get_content_id(catalog_item)
        if not content_id:
            log("Skipping catalog item with no content ID", xbmc.LOGWARNING)
            continue

        title = catalog_item.get("title") or catalog_item.get("name") or "Unknown"
        media_type = (
            catalog_item.get("type")
            or catalog_item.get("media_type")
            or ""
        ).lower()

        list_item = xbmcgui.ListItem(label=title)
        tag = list_item.getVideoInfoTag()
        tag.setTitle(title)

        set_catalog_metadata(list_item, catalog_item, client)

        if catalog_item.get("year"):
            try:
                tag.setYear(int(catalog_item["year"]))
            except (TypeError, ValueError):
                pass

        if catalog_item.get("plot") or catalog_item.get("overview"):
            tag.setPlot(catalog_item.get("plot") or catalog_item.get("overview"))

        # Silo's current CatalogItem fields are poster_url/backdrop_url/logo_url.
        # Older names remain as fallbacks.
        set_art(
            list_item,
            client,
            poster=(
                catalog_item.get("poster_url")
                or catalog_item.get("poster")
                or catalog_item.get("image")
                or catalog_item.get("artwork")
                or catalog_item.get("thumbnail")
            ),
            backdrop=catalog_item.get("backdrop_url"),
            logo=catalog_item.get("logo_url"),
        )

        # Apply the extended metadata fetched concurrently above.
        detail = detail_map.get(str(content_id))
        if detail:
            set_detail_metadata(list_item, detail, client)

        # Start with the fast catalog snapshot. For an in-progress item, use
        # the dedicated server progress record because it contains the detailed
        # position and duration required for Kodi's partial-watch indicator.
        display_progress = catalog_progress(catalog_item)
        server_progress = in_progress_map.get(str(content_id))

        if server_progress:
            display_progress = server_progress

        set_watch_state(
            list_item,
            display_progress,
            media_type,
        )

        if media_type in PLAYABLE:
            list_item.setProperty("IsPlayable", "true")
            url = build_url(
                action="play",
                content_id=catalog_item.get("play_content_id") or content_id,
                library_id=library_id,
                duration_seconds=catalog_item.get("duration_seconds") or "",
            )
            batch.append((url, list_item, False))
        else:
            url = build_url(
                action="seasons",
                series_id=content_id,
                library_id=library_id,
            )
            batch.append((url, list_item, True))

        # Flush a batch so extremely large libraries do not require the entire
        # Kodi list to remain in one Python tuple list at once.
        if len(batch) >= batch_size:
            xbmcplugin.addDirectoryItems(
                HANDLE,
                batch,
                totalItems=len(items),
            )
            batch = []

    if batch:
        xbmcplugin.addDirectoryItems(
            HANDLE,
            batch,
            totalItems=len(items) + (1 if next_cursor else 0),
        )

    if next_cursor:
        next_url = build_url(
            action="library",
            library_id=library_id,
            cursor=next_cursor,
        )
        next_item = xbmcgui.ListItem(label="Next Page")
        next_item.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(HANDLE, next_url, next_item, True)

    xbmcplugin.endOfDirectory(HANDLE)

def list_seasons(client, series_id, library_id, page=None):
    """Display all seasons belonging to a series."""
    if not series_id:
        raise SiloError("No series ID was supplied.")

    seasons = client.seasons(series_id, library_id)
    page_items, has_previous, has_next = paginate_directory(
        seasons,
        page,
    )

    xbmcplugin.setContent(HANDLE, "seasons")

    add_previous_page(
        series_id=series_id,
        library_id=library_id,
        action="seasons",
        page=page,
    )

    for season in page_items:
        season_number = season.get("season_number", season.get("number"))
        if season_number is None:
            continue

        title = season.get("title") or "Season %s" % season_number
        item = xbmcgui.ListItem(label=title)

        xbmcplugin.addDirectoryItem(
            HANDLE,
            build_url(
                action="season",
                series_id=series_id,
                season_number=season_number,
                library_id=library_id,
            ),
            item,
            True,
        )

    if has_next:
        add_next_page(
            series_id=series_id,
            library_id=library_id,
            action="seasons",
            page=page,
        )

    xbmcplugin.endOfDirectory(HANDLE)


def list_episodes(client, series_id, season_number, library_id, page=None):
    """Display all episodes for a season and apply their current watched state."""
    if not series_id:
        raise SiloError("No series ID was supplied.")

    if season_number is None:
        raise SiloError("No season number was supplied.")

    all_episodes = client.episodes(series_id, season_number, library_id)
    episodes, has_previous, has_next = paginate_directory(
        all_episodes,
        page,
    )

    # Fetch only currently in-progress records for accurate episode resume
    # markers. Completed state comes from each catalog item's user_state.played
    # field, so we do not need the full progress history here.
    try:
        in_progress_map = client.in_progress_map(
            library_id=library_id
        )
    except SiloError as exc:
        log(
            "Unable to retrieve in-progress Silo records: %s" % exc,
            xbmc.LOGWARNING,
        )
        in_progress_map = {}

    xbmcplugin.setContent(HANDLE, "episodes")

    add_previous_page(
        series_id=series_id,
        season_number=season_number,
        library_id=library_id,
        action="season",
        page=page,
    )

    # Fetch extended episode metadata concurrently before Kodi receives the list.
    detail_map = fetch_detail_metadata(
        client,
        episodes,
        library_id,
    )

    # The episode endpoint returns CatalogItem objects as well, including the
    # viewer's watched flag. The in-progress map supplies detailed positions.
    batch = []
    batch_size = 500

    for episode in episodes:
        content_id = get_content_id(episode)
        if not content_id:
            continue

        title = episode.get("title") or episode.get("name") or "Episode"
        item = xbmcgui.ListItem(label=title)
        tag = item.getVideoInfoTag()
        tag.setTitle(title)

        set_catalog_metadata(item, episode, client)

        if episode.get("episode_number") is not None:
            try:
                tag.setEpisode(int(episode["episode_number"]))
            except (TypeError, ValueError):
                pass

        if episode.get("season_number") is not None:
            try:
                tag.setSeason(int(episode["season_number"]))
            except (TypeError, ValueError):
                pass

        if episode.get("plot"):
            tag.setPlot(episode["plot"])

        set_art(
            item,
            client,
            # Current Silo v2 artwork fields.
            poster=(
                episode.get("poster_url")
                or episode.get("poster")
                or episode.get("image")
                or episode.get("thumbnail")
            ),
            backdrop=episode.get("backdrop_url"),
            logo=episode.get("logo_url"),
            # Episode still is retained as a thumbnail fallback.
            still=(
                episode.get("still_url")
                or episode.get("still")
            ),
        )

        # Apply the extended metadata fetched concurrently above.
        detail = detail_map.get(str(content_id))
        if detail:
            set_detail_metadata(item, detail, client)

        # Start with the catalog snapshot and prefer the dedicated in-progress
        # server record when Silo has one for this episode.
        display_progress = catalog_progress(episode)
        server_progress = in_progress_map.get(str(content_id))

        if server_progress:
            display_progress = server_progress

        set_watch_state(
            item,
            display_progress,
            "episode",
        )

        item.setProperty("IsPlayable", "true")

        # Reuse an already-known single file when the episode exposes one.
        files = episode.get("files") or []
        params = {
            "action": "play",
            "content_id": content_id,
            "library_id": library_id,
        }

        if len(files) == 1:
            file_id = files[0].get("id") or files[0].get("file_id")
            if file_id:
                params["file_id"] = file_id

        if episode.get("duration_seconds") is not None:
            params["duration_seconds"] = episode.get("duration_seconds")

        batch.append((
            build_url(**params),
            item,
            False,
        ))

        if len(batch) >= batch_size:
            xbmcplugin.addDirectoryItems(
                HANDLE,
                batch,
                totalItems=len(episodes),
            )
            batch = []

    if batch:
        xbmcplugin.addDirectoryItems(
            HANDLE,
            batch,
            totalItems=len(episodes),
        )

    if has_next:
        add_next_page(
            series_id=series_id,
            season_number=season_number,
            library_id=library_id,
            action="season",
            page=page,
        )

    xbmcplugin.endOfDirectory(HANDLE)

def choose_file(client, content_id, library_id):
    """Return the file/version selected by the user."""
    versions = client.versions(content_id, library_id)

    if not versions:
        raise SiloError("Silo returned no playable versions.")

    if len(versions) == 1:
        return versions[0].get("id") or versions[0].get("file_id")

    labels = []

    for version in versions:
        labels.append(
            version.get("name")
            or version.get("title")
            or version.get("filename")
            or version.get("file_name")
            or str(version.get("id") or version.get("file_id"))
        )

    selected = xbmcgui.Dialog().select("Select version", labels)

    if selected < 0:
        return None

    version = versions[selected]
    return version.get("id") or version.get("file_id")


def apply_fresh_resume_to_resolved_item(list_item, progress, fallback_duration=0.0):
    """Put the freshly retrieved Silo resume state onto the resolved item.

    Kodi itself owns the resume dialog. We deliberately do not show our own
    yes/no dialog because Kodi will ask once using this freshly supplied
    resume point when its normal "Ask if resumable" behaviour is enabled.

    We also do not use StartOffset here. StartOffset would bypass the normal
    Kodi resume decision. The native Kodi resume system should perform the
    seek after the user chooses Resume.
    """
    tag = list_item.getVideoInfoTag()

    try:
        fallback_duration = max(0.0, float(fallback_duration or 0))
    except (TypeError, ValueError):
        fallback_duration = 0.0

    if not progress:
        # An unwatched item may have no progress record, but the catalog still
        # supplies its runtime. Pass that runtime to Kodi independently.
        if fallback_duration > 0:
            duration_int = int(round(fallback_duration))
            tag.setDuration(duration_int)
            list_item.setInfo("video", {"duration": duration_int})

        tag.setPlaycount(0)
        tag.setResumePoint(0.0, 0.0)
        return

    completed = bool(progress.get("completed", False))
    position, duration = get_progress_position(progress)

    # Prefer the fresh progress duration when available; otherwise use the
    # catalog duration carried through the plugin URL.
    if duration <= 0:
        duration = fallback_duration

    if duration > 0:
        duration_int = int(round(duration))
        tag.setDuration(duration_int)
        list_item.setInfo("video", {"duration": duration_int})

    if completed:
        # A completed item must not be offered as resumable.
        tag.setPlaycount(1)
        tag.setResumePoint(0.0, 0.0)
        return

    tag.setPlaycount(0)

    if position > 0 and duration > 0:
        # This is the fresh server position. Kodi's own resume prompt will use
        # this value and perform the seek if the user selects Resume.
        tag.setResumePoint(position, duration)
    else:
        # Incomplete but with no usable resume position.
        tag.setResumePoint(0.0, 0.0)


def play(client, content_id, file_id, library_id, duration_seconds=None):
    """Play media using one fresh Silo resume check and Kodi's native prompt.

    Playback order:
        1. Resolve the file/version.
        2. Query Silo progress again immediately before playback.
        3. Ask Silo for the normal stream/session at position zero.
        4. Put the fresh Silo resume point on the resolved Kodi ListItem.
        5. Use setResolvedUrl(), letting Kodi show its normal single Resume/Play
           prompt and perform the seek itself.
        6. Report Kodi's actual playback position back to Silo.

    There is intentionally NO custom resume dialog here.
    """
    if not content_id:
        raise SiloError("No content ID was supplied for playback.")

    # Resolve the exact file/version to play.
    if not file_id:
        file_id = choose_file(client, content_id, library_id)

    if not file_id:
        return

    # Fetch the extended item detail only when the item is actually played.
    # The library/episode listing remains fast because it uses CatalogItem
    # metadata directly. The detail response contains cast, crew and complete
    # video/audio/subtitle track descriptors.
    try:
        detail = client.item_detail(
            content_id,
            library_id,
            file_id,
        )
    except SiloError as exc:
        detail = None
        log(
            "Unable to retrieve extended metadata for %s: %s" % (
                content_id,
                exc,
            ),
            xbmc.LOGWARNING,
        )

    # --------------------------------------------------------------
    # FRESH SERVER PROGRESS CHECK
    # --------------------------------------------------------------
    # This is deliberately performed after the user selects Play, rather than
    # trusting the progress snapshot that was used to build the directory.
    latest_progress = None

    try:
        latest_progress = client.get_progress(
            content_id,
            library_id,
        )
    except SiloError as exc:
        # Playback should still work if Silo's progress endpoint is temporarily
        # unavailable. In that case Kodi receives no resume point.
        log(
            "Fresh progress lookup failed; continuing without Silo resume: %s" % exc,
            xbmc.LOGWARNING,
        )

    if latest_progress:
        fresh_position, fresh_duration = get_progress_position(latest_progress)
        log(
            "Fresh Silo state before playback: content=%s position=%.3f duration=%.3f completed=%s"
            % (
                content_id,
                fresh_position,
                fresh_duration,
                latest_progress.get("completed", False),
            )
        )
    else:
        log(
            "No Silo progress record found immediately before playback for content %s"
            % content_id
        )

    # --------------------------------------------------------------
    # START THE SILO PLAYBACK SESSION
    # --------------------------------------------------------------
    # Silo provides the stream URL/session here, but Kodi is responsible for
    # performing the actual resume seek after its native Resume/Play choice.
    # Therefore start_position MUST remain zero to avoid a double seek.
    info = client.start_playback(
        file_id,
        start_position=0.0,
    )

    if not info.get("url"):
        raise SiloError("Silo did not provide a playback URL.")

    # --------------------------------------------------------------
    # RESOLVED KODI LIST ITEM
    # --------------------------------------------------------------
    # setResolvedUrl() is important here. Kodi can use the original directory
    # item's metadata/artwork while replacing its path with this resolved URL.
    # This also lets Kodi's normal native resume mechanism handle the ONE resume
    # prompt instead of us running a second dialog ourselves.
    resolved_item = xbmcgui.ListItem(path=info["url"])

    # Reapply the same extended metadata to the resolved playback item so
    # Kodi retains cast/crew and stream information after resolution.
    if detail:
        try:
            set_catalog_metadata(resolved_item, detail, client)
            set_detail_metadata(
                resolved_item,
                detail,
                client,
                file_id=file_id,
            )
        except Exception as exc:
            log(
                "Unable to apply extended playback metadata for %s: %s" % (
                    content_id,
                    exc,
                ),
                xbmc.LOGWARNING,
            )

    # Apply the freshly retrieved Silo resume point to the resolved item.
    # Do not set StartOffset: Kodi should decide whether to resume or start over.
    apply_fresh_resume_to_resolved_item(
        resolved_item,
        latest_progress,
        fallback_duration=duration_seconds,
    )

    # Keep the resolved item playable.
    resolved_item.setProperty("IsPlayable", "true")

    # Tell Kodi that the plugin URL has been resolved to the actual Silo stream.
    # Kodi now handles the normal single Resume/Play prompt itself.
    xbmcplugin.setResolvedUrl(
        HANDLE,
        True,
        resolved_item,
    )

    # --------------------------------------------------------------
    # KODI -> SILO LIVE PROGRESS REPORTING
    # --------------------------------------------------------------
    session_id = info.get("session_id")

    if session_id:
        track_progress(
            client,
            session_id,
        )
    else:
        log(
            "Silo playback started without a session ID; "
            "playback progress cannot be reported.",
            xbmc.LOGWARNING,
        )


def track_progress(client, session_id):
    """Monitor Kodi playback and periodically report its position to Silo."""
    player = xbmc.Player()
    monitor = xbmc.Monitor()

    # Wait for Kodi to actually begin playing the resolved stream.
    for _ in range(60):
        if player.isPlaying():
            break

        if monitor.abortRequested():
            return

        xbmc.sleep(500)

    if not player.isPlaying():
        log(
            "Kodi playback did not start; Silo playback session will not be tracked.",
            xbmc.LOGWARNING,
        )
        return

    sequence = 0
    last_position = 0.0

    # Report roughly every ten seconds while Kodi is playing.
    while player.isPlaying():
        if monitor.abortRequested():
            break

        try:
            position = float(player.getTime())
        except Exception:
            position = last_position

        last_position = max(0.0, position)

        paused = xbmc.getCondVisibility("Player.Paused")

        sequence += 1

        try:
            client.report_progress(
                session_id,
                sequence,
                last_position,
                paused,
            )
        except SiloError as exc:
            # Never interrupt the video because a progress update failed.
            log(
                "Unable to report playback progress: %s" % exc,
                xbmc.LOGWARNING,
            )

        # Sleep in small chunks so playback can stop/abort without making the
        # addon wait a full five seconds before noticing it.
        for _ in range(50):
            if not player.isPlaying() or monitor.abortRequested():
                break
            xbmc.sleep(100)

    # Try one final position read while Kodi still has player state available.
    try:
        if player.isPlaying():
            last_position = max(0.0, float(player.getTime()))
    except Exception:
        pass

    # The final DELETE gets its own sequence number.
    sequence += 1

    try:
        client.stop_playback(
            session_id,
            sequence,
            last_position,
        )
    except SiloError as exc:
        log(
            "Unable to stop Silo playback session: %s" % exc,
            xbmc.LOGWARNING,
        )


def router(client):
    """Route Kodi's current plugin request to the appropriate addon action."""
    query = sys.argv[2]

    if query.startswith("?"):
        query = query[1:]

    params = dict(
        parse_qsl(
            query,
            keep_blank_values=True,
        )
    )

    action = params.get("action")

    if not action:
        list_root(client, params.get("page"))
        return

    if action == "root":
        list_root(
            client,
            params.get("page"),
        )
        return

    if action == "login":
        # A Login button starts the complete authentication flow.
        # This asks for server, username and password, then selects/verifies
        # the Silo profile before returning to the library screen.
        client.login_full()
        xbmc.executebuiltin("Container.Refresh")
        return

    if action == "library":
        list_library(
            client,
            params.get("library_id"),
            params.get("cursor"),
        )
        return

    if action == "seasons":
        list_seasons(
            client,
            params.get("series_id"),
            params.get("library_id"),
            params.get("page"),
        )
        return

    if action == "season":
        list_episodes(
            client,
            params.get("series_id"),
            params.get("season_number"),
            params.get("library_id"),
            params.get("page"),
        )
        return

    if action == "play":
        play(
            client,
            params.get("content_id"),
            params.get("file_id"),
            params.get("library_id"),
            params.get("duration_seconds"),
        )
        return

    if action == "switch_profile":
        client.select_profile()
        xbmc.executebuiltin("Container.Refresh")
        return

    if action == "logout":
        client.logout()
        notify("Logged out of Silo")
        xbmc.executebuiltin("Container.Refresh")
        return

    log(
        "Unknown Kodi plugin action: %s" % action,
        xbmc.LOGWARNING,
    )


def main():
    """Create the Silo client and process Kodi's current plugin request."""
    client = SiloClient()

    try:
        router(client)

    except SiloError as exc:
        log(
            "Silo error: %s" % exc,
            xbmc.LOGERROR,
        )

        xbmcgui.Dialog().notification(
            "Silo",
            str(exc),
            xbmcgui.NOTIFICATION_ERROR,
            5000,
        )

    except Exception as exc:
        log(
            "Unexpected addon error: %s" % exc,
            xbmc.LOGERROR,
        )

        xbmcgui.Dialog().notification(
            "Silo",
            "Unexpected error: %s" % exc,
            xbmcgui.NOTIFICATION_ERROR,
            5000,
        )


# Kodi executes main.py as the addon entry point.
main()
