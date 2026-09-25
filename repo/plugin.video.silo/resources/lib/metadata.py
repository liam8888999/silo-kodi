"""Silo-to-Kodi metadata, artwork, ratings, and watch-state handling."""

from resources.lib import runtime
from resources.lib.common import *
from resources.lib.utils import *

def fetch_detail_metadata(client, items, library_id, max_workers=2, per_item_library=False):
    """Fetch extended metadata concurrently and retry transient failures.

    Catalog data is fast and contains most metadata. The detail endpoint adds
    cast, crew and full file stream information. Requests run concurrently,
    while transient timeouts, connection failures and server throttling/errors
    are retried before an item is considered unavailable.

    When per_item_library is true, every supplied card occurrence is fetched
    separately using its source library ID. This is required by merged Home
    sections because the same content ID can exist in multiple libraries and
    each occurrence can have a different added_at timestamp.
    """
    requests = []
    seen = set()

    for index, item in enumerate(items):
        content_id = get_content_id(item)
        if not content_id:
            continue

        item_library_id = library_id
        if per_item_library:
            item_library_id = (
                item.get("_silo_home_source_library_id")
                or item_library_id
            )

        if per_item_library:
            # Preserve one request for each card occurrence/library pair.
            request_key = "%s|%s|%s" % (
                str(content_id),
                str(item_library_id) if item_library_id is not None else "",
                index,
            )
        else:
            request_key = str(content_id)

        if request_key in seen:
            continue
        seen.add(request_key)
        requests.append(
            (content_id, item_library_id, request_key)
        )

    if not requests:
        return {}

    def fetch_one(request):
        content_id, request_library_id, request_key = request
        attempts = 3

        for attempt in range(attempts):
            try:
                # Use a separate session per worker. requests.Session should not
                # be shared across concurrent requests.
                worker_client = SiloClient()
                worker_client.cfg.update(client.cfg)

                detail = worker_client.item_detail(
                    content_id,
                    request_library_id,
                )

                if detail:
                    # Silo's v2 item detail embeds CatalogItem, including
                    # added_at. The merged Recently Added sorter consumes this
                    # value from the occurrence-specific detail below.
                    return request_key, detail

                # An empty document is unusual but should get one retry.
                if attempt < attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
                    continue

                return request_key, None

            except SiloError as exc:
                status = getattr(exc, "status", None)

                # Retry transient HTTP failures and network errors. For 429,
                # Silo supplies the authoritative Retry-After delay.
                transient = (
                    status is None
                    or status == 408
                    or status == 429
                    or status >= 500
                )

                if attempt < attempts - 1 and transient:
                    retry_after = getattr(exc, "retry_after", None)

                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 0.0

                    if status == 429 and delay <= 0:
                        delay = 2.0
                    elif delay <= 0:
                        delay = 0.5 * (attempt + 1)

                    # Give the server a little breathing room before the next
                    # attempt, especially after a rate-limit response.
                    time.sleep(max(0.25, delay))
                    continue

                log(
                    "Unable to retrieve detail metadata for %s: %s"
                    % (content_id, exc),
                    xbmc.LOGWARNING,
                )
                return request_key, None

        return request_key, None

    worker_count = max(
        1,
        min(int(max_workers or 2), len(requests)),
    )

    details = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, request)
            for request in requests
        ]

        for future in as_completed(futures):
            request_key, detail = future.result()
            if detail:
                details[request_key] = detail

    return details


def fetch_home_added_at_by_section(client, section, card_items):
    """Return added_at values only for cards already present in a Home row.

    The Home-card endpoint limits the displayed items but omits added_at.
    Silo's section-backed catalog endpoint exposes added_at, so use it only
    as a metadata lookup and never add its extra catalogue items to Kodi.
    """
    if not card_items:
        return {}

    section_id = section.get("id") or section.get("section_id")
    if not section_id:
        return {}

    wanted = set()
    for item in card_items:
        content_id = get_content_id(item)
        if content_id:
            wanted.add(str(content_id))

    if not wanted:
        return {}

    try:
        data = client.home_section_catalog_items(
            section_id,
            image_size="medium",
            limit=200,
        ) or {}
    except SiloError as exc:
        log(
            "Unable to retrieve added_at lookup for Home section %s: %s"
            % (section_id, exc),
            xbmc.LOGINFO,
        )
        return {}

    result = {}
    for catalog_item in data.get("items") or []:
        content_id = get_content_id(catalog_item)
        if not content_id or str(content_id) not in wanted:
            continue

        added_at = catalog_item.get("added_at")
        if added_at:
            result[str(content_id)] = added_at

    log(
        "Home added_at lookup section=%s matched=%d/%d"
        % (section_id, len(result), len(wanted)),
        xbmc.LOGINFO,
    )
    return result



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



def has_usable_resume(progress):
    """Return whether a progress record contains a real resume position."""
    if not progress or bool(progress.get("completed", False)):
        return False

    position, duration = get_progress_position(progress)
    return position > 0 and duration > 0



def normalize_watch_rollup(user_data, fallback_episode_count=0):
    """Normalize Silo's aggregate season/series viewer state."""
    if not isinstance(user_data, dict):
        user_data = {}

    def as_int(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    watched_count = as_int(user_data.get("watched_count"))
    unplayed_count = as_int(user_data.get("unplayed_count"))
    in_progress_count = as_int(user_data.get("in_progress_count"))

    if watched_count + unplayed_count <= 0:
        unplayed_count = as_int(fallback_episode_count)

    total_count = watched_count + unplayed_count

    return {
        "played": bool(user_data.get("played", False)) or (
            total_count > 0 and watched_count >= total_count
        ),
        "watched_count": min(watched_count, total_count),
        "unplayed_count": max(0, total_count - watched_count),
        "in_progress_count": min(
            in_progress_count,
            max(0, total_count - watched_count),
        ),
        "total_count": total_count,
    }



def merge_season_watch_rollup(existing, candidate):
    """Keep the richest aggregate row when the same season is seen twice."""
    if existing is None:
        return candidate

    existing_score = (
        existing.get("total_count", 0),
        existing.get("watched_count", 0),
        existing.get("in_progress_count", 0),
    )
    candidate_score = (
        candidate.get("total_count", 0),
        candidate.get("watched_count", 0),
        candidate.get("in_progress_count", 0),
    )

    return candidate if candidate_score > existing_score else existing



def fetch_series_watch_data(client, items, library_id=None, max_workers=4):
    """Fetch Silo's season rollups and aggregate them to each series."""
    series_ids = []
    seen = set()

    for item in items:
        media_type = (
            item.get("type")
            or item.get("media_type")
            or ""
        ).lower()

        if media_type == "series":
            series_id = get_content_id(item)
        elif media_type == "season":
            series_id = item.get("series_id")
        else:
            continue

        if not series_id:
            continue

        key = str(series_id)
        if key not in seen:
            seen.add(key)
            series_ids.append(series_id)

    if not series_ids:
        return {}, {}

    if library_id:
        library_ids = [library_id]
    else:
        try:
            library_ids = [
                library.get("id")
                for library in client.libraries()
                if library.get("id")
            ]
        except SiloError as exc:
            log(
                "Unable to retrieve libraries for series watch-state lookup: %s"
                % exc,
                xbmc.LOGWARNING,
            )
            return {}, {}

    if not library_ids:
        return {}, {}

    def fetch_one(series_id):
        season_map = {}

        for candidate_library_id in library_ids:
            try:
                seasons = client.seasons(
                    series_id,
                    candidate_library_id,
                    suppress_not_found=True,
                ) or []
            except SiloError as exc:
                log(
                    "Unable to retrieve seasons for series %s in library %s: %s"
                    % (series_id, candidate_library_id, exc),
                    xbmc.LOGDEBUG,
                )
                continue

            for season in seasons:
                season_content_id = get_content_id(season)
                season_number = season.get(
                    "season_number",
                    season.get("number"),
                )
                rollup = normalize_watch_rollup(
                    season.get("user_data"),
                    season.get("episode_count") or 0,
                )
                candidate = {
                    "content_id": (
                        str(season_content_id)
                        if season_content_id
                        else ""
                    ),
                    "series_id": str(series_id),
                    "season_number": season_number,
                    **rollup,
                }
                key = (
                    str(season_content_id)
                    if season_content_id
                    else "%s:%s" % (series_id, season_number)
                )
                season_map[key] = merge_season_watch_rollup(
                    season_map.get(key),
                    candidate,
                )

        watched_count = 0
        unplayed_count = 0
        in_progress_count = 0

        for season in season_map.values():
            watched_count += season.get("watched_count", 0)
            unplayed_count += season.get("unplayed_count", 0)
            in_progress_count += season.get("in_progress_count", 0)

        total_count = watched_count + unplayed_count

        return (
            str(series_id),
            {
                "played": (
                    total_count > 0
                    and watched_count >= total_count
                ),
                "watched_count": watched_count,
                "unplayed_count": unplayed_count,
                "in_progress_count": in_progress_count,
                "total_count": total_count,
                "season_count": len(season_map),
            },
            season_map,
        )

    worker_count = max(
        1,
        min(int(max_workers or 4), len(series_ids)),
    )
    series_map = {}
    season_map = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, series_id)
            for series_id in series_ids
        ]

        for future in as_completed(futures):
            series_key, series_rollup, series_seasons = future.result()
            series_map[series_key] = series_rollup

            for key, season in series_seasons.items():
                season_map[key] = merge_season_watch_rollup(
                    season_map.get(key),
                    season,
                )

    return series_map, season_map



def set_container_watch_state(list_item, rollup):
    """Apply Silo's aggregate watch state to a series or season folder."""
    if not rollup:
        return

    watched_count = max(0, int(rollup.get("watched_count", 0) or 0))
    unplayed_count = max(0, int(rollup.get("unplayed_count", 0) or 0))
    in_progress_count = max(
        0,
        int(rollup.get("in_progress_count", 0) or 0),
    )
    total_count = max(
        0,
        int(
            rollup.get("total_count")
            or watched_count + unplayed_count
            or 0
        ),
    )

    if total_count <= 0:
        return

    completed = watched_count >= total_count
    partial = (
        not completed
        and (watched_count > 0 or in_progress_count > 0)
    )
    unwatched_count = max(0, total_count - watched_count)

    # Populate Kodi's native TV-show/season episode-count properties so
    # skins can display the same watched/total information they use for items
    # from Kodi's own video database.
    watched_percent = (
        (watched_count * 100.0) / float(total_count)
        if total_count > 0
        else 0.0
    )

    properties = {
        "totalepisodes": str(total_count),
        "numepisodes": str(total_count),
        "watchedepisodes": str(watched_count),
        "unwatchedepisodes": str(unwatched_count),
        "inprogressepisodes": str(in_progress_count),
        "watchedepisodepercent": str(int(round(watched_percent))),
        "WatchedEpisodes": str(watched_count),
        "UnWatchedEpisodes": str(unwatched_count),
        "UnwatchedEpisodes": str(unwatched_count),
        "InProgressEpisodes": str(in_progress_count),
        "InProgressCount": str(in_progress_count),
        "TotalEpisodes": str(total_count),
        "Silo.EpisodeCount": str(total_count),
        "Silo.WatchedEpisodes": str(watched_count),
        "Silo.UnwatchedEpisodes": str(unwatched_count),
        "Silo.InProgressEpisodes": str(in_progress_count),
        "Silo.WatchedEpisodePercent": str(int(round(watched_percent))),
        "Silo.TotalEpisodes": str(total_count),
        "Silo.PartiallyWatched": "true" if partial else "false",
    }

    season_count = rollup.get("season_count")
    if season_count is not None:
        properties.update({
            "totalseasons": str(int(season_count)),
            "numseasons": str(int(season_count)),
            "Silo.SeasonCount": str(int(season_count)),
        })

    for key, value in properties.items():
        list_item.setProperty(key, value)

    tag = list_item.getVideoInfoTag()

    if completed:
        tag.setPlaycount(1)
        tag.setResumePoint(0.0, 0.0)
        return

    tag.setPlaycount(0)

    if partial:
        fraction = (
            watched_count + (0.5 * in_progress_count)
        ) / float(total_count)
        fraction = min(0.999, max(0.001, fraction))
        tag.setResumePoint(fraction, 1.0)
    else:
        tag.setResumePoint(0.0, 0.0)



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

    # Catalog listings expose viewer flags as user_state, while the
    # series-season and season-episode endpoints expose the same watched
    # state as user_data. Accept both shapes.
    user_state = item.get("user_state")
    user_data = item.get("user_data")

    if not isinstance(user_state, dict) or not user_state:
        user_state = user_data if isinstance(user_data, dict) else {}

    if not user_state:
        return None

    # Silo exposes the current resume position directly when available.
    # Dedicated in-progress records are still preferred by the callers that
    # need an exact position.
    position = (
        item.get("position_seconds")
        if item.get("position_seconds") is not None
        else user_data.get("position_seconds", 0)
        if isinstance(user_data, dict)
        else 0
    )
    duration = (
        item.get("duration_seconds")
        if item.get("duration_seconds") is not None
        else user_data.get("duration_seconds", 0)
        if isinstance(user_data, dict)
        else 0
    )

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

    # Preserve catalog viewer state that has no direct VideoInfoTag setter.
    user_state = item.get("user_state")
    if isinstance(user_state, dict):
        for key in (
            "played",
            "is_favorite",
            "in_watchlist",
        ):
            if key in user_state:
                list_item.setProperty(
                    "Silo.UserState.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    "true" if bool(user_state[key]) else "false",
                )

    play_content_id = item.get("play_content_id")
    if play_content_id:
        list_item.setProperty("Silo.PlayContentID", str(play_content_id))

    series_id = item.get("series_id")
    if series_id:
        list_item.setProperty("Silo.SeriesID", str(series_id))

    content_id = item.get("content_id") or item.get("id")
    if content_id:
        list_item.setProperty("Silo.ContentID", str(content_id))

    # Keep the server's catalog progress fields available even when the
    # native watch marker is applied separately by set_watch_state().
    for key in (
        "position_seconds",
        "duration_seconds",
        "progress_updated_at",
    ):
        value = item.get(key)
        if value not in (None, ""):
            list_item.setProperty(
                "Silo.%s" % "".join(
                    part.title() for part in key.split("_")
                ),
                str(value),
            )

    if item.get("added_at"):
        try:
            tag.setDateAdded(str(item["added_at"]))
        except Exception:
            list_item.setProperty(
                "Silo.AddedAt",
                str(item["added_at"]),
            )

    for key in (
        "poster_thumbhash",
        "backdrop_thumbhash",
    ):
        value = item.get(key)
        if value:
            list_item.setProperty(
                "Silo.%s" % "".join(
                    part.title() for part in key.split("_")
                ),
                str(value),
            )

    badges = item.get("badges") or []
    if badges:
        list_item.setProperty(
            "Silo.Badges",
            " / ".join(str(value) for value in badges if value),
        )

    sort_metrics = item.get("sort_metrics")
    if isinstance(sort_metrics, dict):
        for key in (
            "release_date",
            "runtime_minutes",
            "resolution",
            "bitrate_kbps",
            "progress_ratio",
            "viewed_at",
            "play_count",
            "author",
            "narrator",
            "series_name",
        ):
            value = sort_metrics.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Sort.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

    upcoming = item.get("upcoming_event")
    if isinstance(upcoming, dict):
        for key in (
            "type",
            "air_date",
            "air_time",
            "episode_title",
            "season_number",
            "episode_number",
        ):
            value = upcoming.get(key)
            if value not in (None, ""):
                list_item.setProperty(
                    "Silo.Upcoming.%s" % "".join(
                        part.title() for part in key.split("_")
                    ),
                    str(value),
                )

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
    cast_names = []
    cast_and_roles = []

    for person in detail.get("cast") or []:
        name = person.get("name")
        if not name:
            continue

        name = str(name)
        role = str(person.get("character") or "")
        thumbnail = client.abs_url(person.get("photo_url") or "")

        try:
            order = int(person.get("order") or 0)
        except (TypeError, ValueError):
            order = 0

        # Kodi 20+ uses xbmc.Actor objects for InfoTagVideo.setCast().
        try:
            cast.append(
                xbmc.Actor(
                    name,
                    role,
                    order,
                    thumbnail,
                )
            )
        except Exception:
            pass

        actor_info = {"name": name}
        if role:
            actor_info["role"] = role
        if thumbnail:
            actor_info["thumbnail"] = thumbnail
        if order:
            actor_info["order"] = order

        cast_info.append(actor_info)
        cast_names.append(name)

        if role:
            cast_and_roles.append((name, role))
        else:
            cast_and_roles.append((name, ""))

    if cast:
        try:
            tag.setCast(cast)
        except Exception:
            pass

    # Keep Kodi's older ListItem representation as well. It is deprecated in
    # Kodi 20+, but remains supported and some skins/add-ons still consume it.
    if cast_info:
        # InfoTagVideo.setCast() above is the current Kodi API and retains the
        # actor thumbnail supplied by Silo.
        list_item.setProperty(
            "Silo.CastNames",
            " / ".join(cast_names),
        )

    directors = []
    writers = []
    credits = []

    for person in detail.get("crew") or []:
        name = person.get("name")
        job = str(person.get("job") or "").strip()

        if not name:
            continue

        name = str(name)
        job_lower = job.lower()

        if job:
            credits.append(name)

        # Silo can return detailed crew job labels, not only the bare "Director"
        # or "Writer" values.
        if (
            job_lower == "director"
            or job_lower.endswith(" director")
            or "director" in job_lower
        ):
            directors.append(name)

        if any(
            word in job_lower
            for word in (
                "writer",
                "screenplay",
                "screenwriter",
                "story",
                "novel",
            )
        ):
            writers.append(name)

    # Remove duplicates while preserving Silo's order.
    directors = list(dict.fromkeys(directors))
    writers = list(dict.fromkeys(writers))
    credits = list(dict.fromkeys(credits))

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

    if credits:
        list_item.setProperty(
            "Silo.Crew",
            " / ".join(credits),
        )

    # Keep complete crew names/jobs available without requiring JSON in the
    # Kodi runtime. Thumbnail-bearing crew data remains in the structured
    # Silo.CrewDetails property only when a skin specifically needs it.
    crew_details = []
    for person in detail.get("crew") or []:
        if not person.get("name"):
            continue
        crew_details.append(
            "%s (%s)" % (
                str(person.get("name")),
                str(person.get("job") or ""),
            )
        )

    if crew_details:
        list_item.setProperty(
            "Silo.CrewDetails",
            " / ".join(crew_details),
        )

    # Detail-only viewer and series information.
    if detail.get("user_rating") is not None:
        try:
            tag.setUserRating(int(detail["user_rating"]))
        except (TypeError, ValueError):
            pass

    for key in (
        "season_count",
        "episode_count",
        "air_time",
        "air_timezone",
        "effective_subtitle_language",
        "effective_subtitle_mode",
        "effective_version_resolution",
        "effective_version_codec_video",
        "effective_version_edition_key",
    ):
        value = detail.get(key)
        if value not in (None, ""):
            list_item.setProperty(
                "Silo.%s" % "".join(
                    part.title() for part in key.split("_")
                ),
                str(value),
            )

    # Marker data remains available as a simple property. Complex marker JSON
    # is intentionally not serialised here because it is not required by Kodi
    # for pre-playback metadata.
    for marker_name in ("intro", "credits", "recap", "preview"):
        marker = detail.get(marker_name)
        if marker:
            list_item.setProperty(
                "Silo.Marker.%s" % marker_name.title(),
                str(marker),
            )

    # Native date-added and ID infolabels are still consumed by some skins.
    added_at = detail.get("added_at")
    if added_at:
        try:
            tag.setDateAdded(str(added_at))
        except Exception:
            pass

    unique_ids = {}
    if detail.get("imdb_id"):
        unique_ids["imdb"] = str(detail["imdb_id"])
    if detail.get("tmdb_id"):
        unique_ids["tmdb"] = str(detail["tmdb_id"])
    if detail.get("tvdb_id"):
        unique_ids["tvdb"] = str(detail["tvdb_id"])

    if unique_ids:
        try:
            default_id = "imdb" if "imdb" in unique_ids else next(iter(unique_ids))
            tag.setUniqueIDs(
                unique_ids,
                default_id,
            )
        except Exception:
            for key, value in unique_ids.items():
                try:
                    tag.setUniqueID(value, key, key == "imdb")
                except Exception:
                    pass

    version = _detail_version(detail, file_id)
    set_stream_details(list_item, version)

    # Full-detail runtime is the actual selected file duration in seconds.
    if version.get("duration"):
        try:
            duration = int(version["duration"])
            tag.setDuration(duration)
        except (TypeError, ValueError):
            pass



def set_season_metadata(list_item, season, client, series_id=None):
    """Populate a Kodi season ListItem from Silo's season response."""
    if not isinstance(season, dict):
        return

    tag = list_item.getVideoInfoTag()
    tag.setMediaType("season")

    season_number = season.get(
        "season_number",
        season.get("number"),
    )

    if season_number is not None:
        try:
            tag.setSeason(int(season_number))
        except (TypeError, ValueError):
            pass

    title = season.get("title")
    if title:
        tag.setTitle(str(title))

    overview = season.get("overview")
    if overview:
        try:
            tag.setPlot(str(overview))
            tag.setPlotOutline(str(overview))
        except Exception:
            pass

    air_date = season.get("air_date")
    if air_date:
        try:
            tag.setPremiered(str(air_date))
        except Exception:
            pass

    episode_count = season.get("episode_count")
    try:
        episode_count = max(0, int(episode_count or 0))
    except (TypeError, ValueError):
        episode_count = 0

    if episode_count > 0:
        # Keep the standard Kodi episode-count fields available to skins.
        for key, value in (
            ("totalepisodes", episode_count),
            ("numepisodes", episode_count),
        ):
            list_item.setProperty(key, str(value))
        list_item.setProperty("Silo.EpisodeCount", str(episode_count))

    content_id = get_content_id(season)
    if content_id:
        list_item.setProperty("Silo.ContentID", str(content_id))

    play_content_id = season.get("play_content_id")
    if play_content_id:
        list_item.setProperty(
            "Silo.PlayContentID",
            str(play_content_id),
        )

    if series_id:
        list_item.setProperty("Silo.SeriesID", str(series_id))

    list_item.setProperty(
        "Silo.IsSpecials",
        "true" if bool(season.get("is_specials", False)) else "false",
    )

    poster_url = season.get("poster_url")
    poster_thumbhash = season.get("poster_thumbhash")

    if poster_url:
        set_art(
            list_item,
            client,
            poster=poster_url,
        )

    if poster_thumbhash:
        list_item.setProperty(
            "Silo.PosterThumbhash",
            str(poster_thumbhash),
        )

    # Preserve every season field supplied by the endpoint so skins/add-ons
    # can access the server data even where Kodi has no native setter.
    for key in (
        "content_id",
        "play_content_id",
        "series_id",
        "season_number",
        "title",
        "overview",
        "air_date",
        "episode_count",
        "is_specials",
    ):
        value = season.get(key)
        if value not in (None, ""):
            property_name = "Silo.Season.%s" % "".join(
                part.title() for part in key.split("_")
            )
            list_item.setProperty(property_name, str(value))



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

    # Runtime is independent of resume state. Use Kodi's native
    # VideoInfoTag duration field so directory views receive the duration.
    if duration > 0:
        duration_int = int(round(duration))
        tag.setDuration(duration_int)

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
            runtime.HANDLE,
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
        runtime.HANDLE,
        build_url(
            action="seasons",
            series_id=content_id,
            library_id=library_id,
        ),
        list_item,
        True,
    )




def display_title_for_catalog_item(catalog_item):
    """Build the same visible label used by Search for catalog items."""
    title = (
        catalog_item.get("title")
        or catalog_item.get("name")
        or "Unknown"
    )
    media_type = (
        catalog_item.get("type")
        or catalog_item.get("media_type")
        or ""
    ).lower()

    if media_type == "movie":
        return "[Movie] %s" % title

    if media_type == "series":
        return "[TV Show] %s" % title

    if media_type == "episode":
        series_title = (
            catalog_item.get("series_title")
            or catalog_item.get("series_name")
            or ""
        )
        season_number = catalog_item.get("season_number")
        episode_number = catalog_item.get("episode_number")

        episode_code = ""
        if season_number is not None and episode_number is not None:
            try:
                episode_code = "S%02dE%02d" % (
                    int(season_number),
                    int(episode_number),
                )
            except (TypeError, ValueError):
                episode_code = ""

        if series_title and episode_code:
            return "[Episode] %s - %s - %s" % (
                series_title,
                episode_code,
                title,
            )

        if series_title:
            return "[Episode] %s - %s" % (
                series_title,
                title,
            )

        if episode_code:
            return "[Episode] %s - %s" % (
                episode_code,
                title,
            )

        return "[Episode] %s" % title

    return title



def build_catalog_list_item(client, catalog_item, detail=None, progress=None, series_rollup=None, season_rollup=None):
    """Build a Kodi ListItem using the same metadata/watch pipeline everywhere."""
    content_id = get_content_id(catalog_item)
    title = catalog_item.get("title") or catalog_item.get("name") or "Unknown"
    media_type = (catalog_item.get("type") or catalog_item.get("media_type") or "").lower()

    item = xbmcgui.ListItem(label=display_title_for_catalog_item(catalog_item))
    item.getVideoInfoTag().setTitle(title)

    set_catalog_metadata(item, catalog_item, client)
    set_art(
        item,
        client,
        poster=(catalog_item.get("poster_url") or catalog_item.get("poster")
                or catalog_item.get("image") or catalog_item.get("artwork")
                or catalog_item.get("thumbnail")),
        backdrop=catalog_item.get("backdrop_url"),
        logo=catalog_item.get("logo_url"),
        still=catalog_item.get("still_url") or catalog_item.get("still"),
    )

    if detail:
        set_detail_metadata(item, detail, client)

    display_progress = progress if progress is not None else catalog_progress(catalog_item)
    set_watch_state(item, display_progress, media_type)

    if media_type == "series":
        set_container_watch_state(item, series_rollup)
    elif media_type == "season":
        set_container_watch_state(item, season_rollup)

    return item, media_type, content_id, title, display_progress



__all__ = ["fetch_detail_metadata","fetch_home_added_at_by_section","get_runtime_seconds","get_progress_position","has_usable_resume","normalize_watch_rollup","merge_season_watch_rollup","fetch_series_watch_data","set_container_watch_state","_art_url","set_art","catalog_progress","set_catalog_metadata","_detail_version","_aspect_ratio","set_stream_details","set_detail_metadata","set_season_metadata","set_watch_state","add_catalog_item","display_title_for_catalog_item","build_catalog_list_item"]
