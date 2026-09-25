"""Silo playback resolution, session control, and progress reporting."""

from resources.lib import runtime
from resources.lib.common import *
from resources.lib.utils import *
from resources.lib.metadata import *

def resolve_playback_library_id(client, content_id, library_id=None):
    """Resolve a playable item's library using the same pattern as search."""
    if library_id not in (None, "", "None", "null"):
        return library_id

    libraries = client.libraries()

    # Search does not always know which library a result came from. Mirror its
    # resolution strategy here: test the item against each accessible library
    # until Silo accepts one and returns the item's detail document.
    for library in libraries:
        candidate_library_id = library.get("id")
        if candidate_library_id in (None, ""):
            continue

        try:
            detail = client.item_detail(
                content_id,
                candidate_library_id,
            )
        except SiloError as exc:
            log(
                "Unable to check playback item %s in library %s: %s"
                % (content_id, candidate_library_id, exc),
                xbmc.LOGDEBUG,
            )
            continue

        if detail:
            log(
                "Resolved playback library for %s: %s"
                % (content_id, candidate_library_id),
            )
            return candidate_library_id

    raise SiloError(
        "Unable to determine the Silo library for this item."
    )



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



def play(
    client,
    content_id,
    file_id,
    library_id,
    duration_seconds=None,
    resume=False,
    resume_available=False,
):
    """Play media using Kodi's native Resume/Start-over choice.

    Kodi passes resume:true when the user chose Resume and resume:false when
    the user chose Start from beginning. A fresh Silo progress lookup is made
    immediately before playback. Silo always starts the transport at zero;
    when Resume was chosen, the fresh Silo position is placed on the resolved
    item so Kodi performs the seek to the server-authoritative position.
    """
    if not content_id:
        raise SiloError("No content ID was supplied for playback.")

    # Home and global-search items do not always carry a library ID. Resolve
    # the item against the same accessible-library set used by search before
    # asking Silo for versions or starting playback.
    library_id = resolve_playback_library_id(
        client,
        content_id,
        library_id,
    )

    use_kodi_resume_cache = use_kodi_resume_cache_enabled()
    kodi_cached_resume = (
        get_kodi_cached_resume_position()
        if use_kodi_resume_cache
        else 0.0
    )

    if not file_id:
        file_id = choose_file(client, content_id, library_id)

    if not file_id:
        return

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

    latest_progress = None

    if not use_kodi_resume_cache:
        try:
            latest_progress = client.get_progress(
                content_id,
                library_id,
            )
        except SiloError as exc:
            log(
                "Fresh progress lookup failed; continuing without Silo resume: %s" % exc,
                xbmc.LOGWARNING,
            )
    else:
        log(
            "Using Kodi cached resume position %.3fs for content %s"
            % (kodi_cached_resume, content_id)
        )

    # Silo starts the transport at zero in both modes. This is important:
    # Resume is implemented by Kodi seeking the resolved item to the fresh
    # Silo position, while Start from beginning receives no resume point.
    direct_play_only = direct_play_only_enabled()

    info = client.start_playback(
        file_id,
        start_position=0.0,
        direct_play_only=direct_play_only,
    )

    if not info.get("url"):
        raise SiloError("Silo did not provide a playback URL.")

    resolved_item = xbmcgui.ListItem(path=info["url"])

    # The item Kodi originally clicked may already contain a locally cached
    # resume point. Kodi normally merges the resolved ListItem's metadata with
    # that original item, which means an old local resume point can survive when
    # Silo now reports no resume data. Override the original video InfoTag so the
    # fresh server state below completely replaces Kodi's cached resume state.
    # This is also what lets a server-side reset to position 0 actually take
    # effect instead of falling back to Kodi's stale local bookmark.
    resolved_item.setProperty("OverrideInfotag", "true")

    if detail:
        try:
            set_catalog_metadata(resolved_item, detail, client)
            set_detail_metadata(
                resolved_item,
                detail,
                client,
                file_id=file_id,
            )

            # Episodes normally have their own artwork. When they do not have
            # a logo/banner, inherit the parent series logo so Kodi's player
            # can show the same visual title used by the movie playback path.
            playback_logo = detail.get("logo_url") or detail.get("logo")
            media_type = (
                detail.get("type")
                or detail.get("media_type")
                or ""
            ).lower()

            if not playback_logo and media_type == "episode":
                series_id = detail.get("series_id")
                if series_id:
                    try:
                        series_detail = client.item_detail(
                            series_id,
                            library_id,
                        )
                        playback_logo = (
                            series_detail.get("logo_url")
                            or series_detail.get("logo")
                        )
                        if playback_logo:
                            resolved_item.setProperty(
                                "Silo.SeriesLogo",
                                client.abs_url(str(playback_logo)),
                            )
                            log(
                                "Inherited series logo for episode %s from %s"
                                % (content_id, series_id),
                                xbmc.LOGDEBUG,
                            )
                    except SiloError as exc:
                        log(
                            "Unable to retrieve parent series artwork for episode %s: %s"
                            % (content_id, exc),
                            xbmc.LOGDEBUG,
                        )

            # Apply the same Silo artwork to the actual playback ListItem.
            # The directory item already has artwork, but Kodi can create/use
            # this separate resolved item for playback, so copy all available
            # poster/backdrop/logo/still artwork here as well.
            set_art(
                resolved_item,
                client,
                poster=(
                    detail.get("poster_url")
                    or detail.get("poster")
                    or detail.get("image")
                    or detail.get("artwork")
                    or detail.get("thumbnail")
                ),
                backdrop=detail.get("backdrop_url") or detail.get("backdrop"),
                logo=playback_logo,
                still=detail.get("still_url") or detail.get("still"),
            )
        except Exception as exc:
            log(
                "Unable to apply extended playback metadata for %s: %s" % (
                    content_id,
                    exc,
                ),
                xbmc.LOGWARNING,
            )

    if use_kodi_resume_cache:
        if kodi_cached_resume > 0:
            apply_fresh_resume_to_resolved_item(
                resolved_item,
                {
                    "completed": False,
                    "position_seconds": kodi_cached_resume,
                    "duration_seconds": duration_seconds or 0,
                },
                fallback_duration=duration_seconds,
            )
        elif duration_seconds:
            try:
                resolved_item.getVideoInfoTag().setDuration(
                    int(round(float(duration_seconds)))
                )
            except (TypeError, ValueError):
                pass
    else:
        fresh_server_resume = has_usable_resume(latest_progress)

        if (
            latest_progress
            and (
                resume
                or (
                    not resume
                    and not resume_available
                    and fresh_server_resume
                )
            )
        ):
            apply_fresh_resume_to_resolved_item(
                resolved_item,
                latest_progress,
                fallback_duration=duration_seconds,
            )
            if resume:
                log(
                    "Kodi requested Resume; applied fresh Silo resume position "
                    "to the resolved item for content %s" % content_id
                )
            else:
                fresh_position, _fresh_duration = get_progress_position(
                    latest_progress
                )
                resolved_item.setProperty(
                    "StartOffset",
                    "%.3f" % fresh_position,
                )
                log(
                    "Kodi had no resume prompt, but Silo now has a fresh resume "
                    "position of %.3fs for content %s"
                    % (fresh_position, content_id)
                )
        elif resume:
            try:
                resume_duration = max(0.0, float(duration_seconds or 0))
            except (TypeError, ValueError):
                resume_duration = 0.0

            if resume_duration <= 0 and detail:
                version = _detail_version(detail, file_id)
                try:
                    resume_duration = max(0.0, float(version.get("duration") or 0))
                except (TypeError, ValueError):
                    resume_duration = 0.0
                if resume_duration <= 0:
                    resume_duration = get_runtime_seconds(detail)

            synthetic_progress = {
                "completed": False,
                "position_seconds": 0.1,
                "duration_seconds": resume_duration,
            }
            apply_fresh_resume_to_resolved_item(
                resolved_item,
                synthetic_progress,
                fallback_duration=resume_duration,
            )
        else:
            try:
                tag = resolved_item.getVideoInfoTag()
                tag.setPlaycount(0)
                tag.setResumePoint(0.0, 0.0)
            except Exception:
                pass

    resolved_item.setProperty("IsPlayable", "true")

    xbmcplugin.setResolvedUrl(
        runtime.HANDLE,
        True,
        resolved_item,
    )

    session_id = info.get("session_id")

    if session_id:
        track_progress(
            client,
            session_id,
            playback_info=info,
            resolved_item=resolved_item,
            force_start_zero=False,
        )
    else:
        log(
            "Silo playback started without a session ID; "
            "playback progress cannot be reported.",
            xbmc.LOGWARNING,
        )




def playback_session_was_terminated(exc):
    """Return True only for Silo's exact progress-session-not-found response.

    After an admin terminates playback, the progress endpoint returns this
    response because the playback session is no longer active. The admin UI's
    human-readable "Playback authority revoked" message is not sent to Kodi.
    """
    problem = getattr(exc, "problem", {}) or {}
    return (
        getattr(exc, "status", None) == 404
        and problem.get("type")
        == "https://siloserver.org/docs/api/v2/problems/not_found"
        and problem.get("detail") == "Playback session not found"
    )


def track_progress(
    client,
    session_id,
    playback_info=None,
    resolved_item=None,
    force_start_zero=False,
):
    """Monitor Kodi playback, report progress, and learn a stable quality.

    Silo publishes the authoritative quality ladder in playback_plan.available_qualities.
    Kodi does not invent its own bitrate ladder. On sustained buffering we move one
    published rung downward. Upward changes are treated as probes: a higher rung must
    survive a full confirmation period before it becomes the new trusted quality.

    Each quality that causes a real sustained playback failure is remembered for this
    video session. Its retry cooldown increases after repeated failures, which prevents
    the controller from repeatedly bouncing between the same two rungs.

    "original" is Silo's source-preserving ceiling. It remains the highest rung, but
    it is not repeatedly retried simply because playback has been healthy for 90 seconds.
    """
    player = xbmc.Player()
    monitor = xbmc.Monitor()

    direct_play_only = direct_play_only_enabled()

    playback_info = dict(playback_info or {})
    playback_info["session_id"] = session_id

    if direct_play_only:
        log("Direct-play-only mode enabled; adaptive quality switching is disabled.")

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

    # Kodi can apply a resume bookmark from its local database after the
    # resolved URL has been opened. If Silo says there is no resume state but
    # Kodi nevertheless invoked this request with resume=true, the only
    # reliable point at which we can override that local seek is after the
    # player is actually running. Force the player to time zero a few times
    # during the initial startup window so a late Kodi bookmark cannot win.
    if force_start_zero:
        for attempt in range(4):
            if not player.isPlaying() or monitor.abortRequested():
                break

            try:
                before = float(player.getTime())
            except Exception:
                before = 0.0

            try:
                player.seekTime(0.0)
                xbmc.sleep(150)
                after = float(player.getTime())
            except Exception as exc:
                log(
                    "Unable to force Kodi playback to the beginning "
                    "(attempt %d): %s" % (attempt + 1, exc),
                    xbmc.LOGWARNING,
                )
                break

            log(
                "Forced Kodi playback to 0.000s because Silo has no "
                "resume data (attempt %d; before=%.3f after=%.3f)"
                % (attempt + 1, before, after)
            )

    def quality_ladder(plan):
        """Return Silo's published quality ladder in server order."""
        entries = []
        seen = set()

        for quality in (plan or {}).get("available_qualities") or []:
            if not isinstance(quality, dict):
                continue

            label = str(quality.get("label") or "").strip()
            if not label or label in seen:
                continue

            seen.add(label)

            try:
                height = int(quality.get("height") or 0)
            except (TypeError, ValueError):
                height = 0

            try:
                bitrate = int(quality.get("bitrate_kbps") or 0)
            except (TypeError, ValueError):
                bitrate = 0

            entries.append({
                "label": label,
                "height": height,
                "bitrate_kbps": bitrate,
            })

        # Silo's v3 contract publishes original first. Preserve that order
        # instead of inventing a local ordering of the server's rungs.
        return entries

    def detect_quality_label(info):
        """Identify the current rung from the last adopted plan."""
        plan = (info or {}).get("playback_plan") or {}
        ladder = quality_ladder(plan)

        preferred = str((info or {}).get("adaptive_quality") or "").strip()
        labels = [entry["label"] for entry in ladder]

        if preferred and preferred in labels:
            return preferred

        recipe = plan.get("effective_recipe") or {}

        try:
            current_height = int(recipe.get("height") or 0)
        except (TypeError, ValueError):
            current_height = 0

        try:
            current_bitrate = int(recipe.get("bitrate_kbps") or 0)
        except (TypeError, ValueError):
            current_bitrate = 0

        # Prefer an exact height/bitrate match. This also lets an initial plan
        # that was capped by Silo identify its actual rung instead of assuming
        # that "original" was necessarily the active recipe.
        if current_height > 0 or current_bitrate > 0:
            for entry in ladder:
                if (
                    entry["height"] == current_height
                    and entry["bitrate_kbps"] == current_bitrate
                ):
                    return entry["label"]

        if labels:
            return labels[0]

        return ""


    # Per-video-session quality memory. A quality that actually caused a sustained
    # stall is not immediately retried as an upward probe. Repeated failures extend
    # its cooldown so the controller can settle on the highest quality that has
    # demonstrated stable playback for this video.
    quality_failures = {}
    quality_failure_cooldowns = {}

    # The currently trusted quality is the quality playback should fall back to
    # if a higher probe fails. This starts as the actual initial server selection.
    known_good_quality = ""
    known_good_since = 0.0

    # An upward switch is a probe until it remains healthy for the full
    # confirmation interval. While a probe is active, another upward switch is
    # forbidden and a stall returns directly to known_good_quality.
    probe_quality = None

    # A higher quality must prove itself for five minutes before becoming the
    # trusted quality. Failed rungs get an increasing 10/20/40/60 minute cooldown.
    quality_confirmation_threshold = 300.0
    quality_failure_cooldown_base = 600.0
    quality_failure_cooldown_max = 3600.0


    def next_quality_label(plan, current_label, direction):
        """Return the adjacent published rung.

        direction=-1 moves upward toward the source/original ceiling.
        direction=1 moves downward toward lower quality.
        """
        ladder = quality_ladder(plan)

        if len(ladder) < 2:
            return None

        labels = [entry["label"] for entry in ladder]

        if current_label in labels:
            index = labels.index(current_label)
        else:
            # The plan normally starts at original, so unknown state safely
            # falls back to the highest published rung.
            index = 0

        target_index = index + direction

        if target_index < 0 or target_index >= len(labels):
            return None

        return labels[target_index]

    def switch_stream(new_info, position, target_label):
        """Adopt an Silo replan without visibly jumping back to zero."""
        new_url = new_info.get("url")
        if not new_url:
            return False

        new_plan = new_info.get("playback_plan") or {}
        timeline = new_plan.get("timeline") or {}

        # Silo's player_start_seconds is the position the client should use
        # inside the newly planned stream. This is deliberately not always the
        # same number as the source/media position: a transcode can begin from
        # a seek anchor and expose a shorter player-relative timeline.
        try:
            start_offset = float(
                timeline.get("player_start_seconds")
            )
        except (TypeError, ValueError):
            start_offset = max(0.0, float(position or 0.0))

        start_offset = max(0.0, start_offset)

        log(
            "Adapting playback to quality=%s at position=%.3f "
            "(player_start_seconds=%.3f)"
            % (
                target_label,
                position,
                start_offset,
            )
        )

        # Reuse the original resolved ListItem so Kodi keeps the movie's
        # title, artwork, video info and other metadata across an adaptive
        # quality change. Only its playback path is replaced.
        list_item = resolved_item
        if list_item is None:
            # Defensive fallback for callers that do not provide the original
            # item; normal plugin playback always does.
            list_item = xbmcgui.ListItem(path=new_url)

        try:
            list_item.setPath(new_url)
        except Exception:
            # Older Kodi builds may not expose setPath() on ListItem. In that
            # case fall back to a new item rather than failing the quality
            # switch entirely.
            list_item = xbmcgui.ListItem(path=new_url)

        list_item.setProperty("IsPlayable", "true")
        list_item.setProperty(
            "StartOffset",
            "%.3f" % start_offset,
        )

        player.play(new_url, list_item)

        # Kodi can report isPlaying() while the previous HLS input is still
        # being torn down. Wait until Kodi reports the replacement URL as the
        # actual current playing file, otherwise the adaptive monitor can begin
        # measuring the old stream and immediately issue another replan.
        attached = False
        expected_url = str(new_url).split("?", 1)[0]
        for _ in range(120):
            if monitor.abortRequested():
                break

            if player.isPlaying():
                try:
                    playing_url = str(player.getPlayingFile() or "")
                except Exception:
                    playing_url = ""

                playing_base = playing_url.split("?", 1)[0]

                if (
                    playing_url == str(new_url)
                    or playing_base == expected_url
                ):
                    attached = True
                    break

            xbmc.sleep(250)

        if not attached:
            log(
                "Kodi did not attach the adaptive stream for quality=%s"
                % target_label,
                xbmc.LOGWARNING,
            )
            return False

        playback_info.clear()
        playback_info.update(new_info)
        playback_info["session_id"] = session_id
        playback_info["adaptive_quality"] = target_label

        return True

    # Determine the initial active quality from the server plan.
    playback_info["adaptive_quality"] = detect_quality_label(playback_info)
    known_good_quality = playback_info["adaptive_quality"]
    known_good_since = time.time()

    sequence = 0
    last_position = 0.0
    last_progress_position = None
    last_progress_change_at = time.time()
    last_reported_paused = None
    last_paused = None
    progress_report_interval = 5.0
    next_progress_report_at = time.time()
    progress_confirmed = True
    stall_started_at = None
    caching_started_at = None
    post_switch_grace_until = 0.0

    # Downward changes happen relatively quickly once sustained buffering is
    # detected. Upward changes require a longer healthy period and must also pass
    # the per-quality failure cooldown before a higher rung is probed.
    last_down_replan_at = 0.0
    last_up_replan_at = 0.0
    last_upshift_at = 0.0
    downshift_retry_at = 0.0
    downshift_retry_count = 0
    downshift_retry_exhausted = False
    down_cooldown = 10.0
    up_cooldown = 90.0
    stall_threshold = 8.0
    # Five minutes of healthy playback is required before a higher rung can be
    # probed. A successful probe has to satisfy the same full interval before
    # becoming the new trusted quality.
    healthy_recovery_threshold = quality_confirmation_threshold
    upshift_downshift_grace = 30.0
    adaptive_retry_delay = 5.0
    adaptive_retry_limit = 1

    healthy_since = time.time()

    # Kodi can briefly report isPlaying() == false while it replaces an HLS
    # input during an adaptive quality switch. Do not treat that short teardown
    # window as the end of the user's playback session.
    not_playing_since = None
    playback_teardown_grace = 120.0

    while True:
        if monitor.abortRequested():
            break

        now = time.time()

        if not player.isPlaying():
            # A true stop removes the media from Kodi. A temporary false
            # isPlaying() state during an adaptive handoff normally retains
            # Player.HasMedia, so preserve the existing handoff grace only for
            # that case.
            has_media = xbmc.getCondVisibility("Player.HasMedia")

            if not has_media:
                log(
                    "Kodi reports playback stopped; ending adaptive monitor "
                    "and closing the Silo playback session.",
                    xbmc.LOGDEBUG,
                )
                break

            if not_playing_since is None:
                not_playing_since = now
                log(
                    "Kodi temporarily reports playback stopped; keeping "
                    "adaptive monitor alive during stream handoff.",
                    xbmc.LOGDEBUG,
                )

            not_playing_for = now - not_playing_since

            if not_playing_for >= playback_teardown_grace:
                log(
                    "Adaptive monitor ending after Kodi reported no active "
                    "playback for %.1fs." % not_playing_for,
                    xbmc.LOGWARNING,
                )
                break

            xbmc.sleep(100)
            continue

        not_playing_since = None

        try:
            position = float(player.getTime())
        except Exception:
            position = last_position

        position = max(0.0, position)
        paused = bool(xbmc.getCondVisibility("Player.Paused"))

        if paused:
            # Paused time is not playback health and must never count as a
            # buffering stall or toward the healthy recovery timer.
            if last_paused is not True:
                log(
                    "Kodi playback paused; suspending adaptive quality timers.",
                    xbmc.LOGDEBUG,
                )

            caching_started_at = None
            stall_started_at = None
            healthy_since = 0.0
        elif last_paused is True:
            # Start a completely fresh health/stall measurement when playback
            # resumes. Without this reset, a pause longer than stall_threshold
            # could make the unchanged paused position look like buffering.
            last_progress_position = position
            last_progress_change_at = now
            caching_started_at = None
            stall_started_at = None
            healthy_since = now
            log(
                "Kodi playback resumed; resetting adaptive quality timers.",
                xbmc.LOGDEBUG,
            )

        last_paused = paused

        if not paused:
            # A probe becomes trusted only after Kodi has actually resumed
            # advancing on the replacement stream and maintained healthy
            # playback for the full confirmation threshold.
            if (
                probe_quality
                and progress_confirmed
                and healthy_since > 0
                and now - healthy_since >= quality_confirmation_threshold
            ):
                known_good_quality = probe_quality
                known_good_since = now

                # A long successful probe clears the quality's previous failure
                # history, because the current network conditions have now
                # demonstrated that the higher rung is sustainable.
                quality_failures.pop(probe_quality, None)
                quality_failure_cooldowns.pop(probe_quality, None)

                log(
                    "Adaptive quality probe confirmed: %s is now the "
                    "trusted quality after %.0fs of healthy playback"
                    % (
                        probe_quality,
                        now - healthy_since,
                    )
                )

                probe_quality = None

                # Start a fresh recovery interval so another upward probe is
                # never launched immediately after confirming one.
                healthy_since = now

            # Give Kodi a short handoff window after changing streams. During
            # this period the old player state may still be visible even though
            # the replacement URL has already been requested.
            in_post_switch_grace = now < post_switch_grace_until

            if in_post_switch_grace:
                caching_started_at = None
                stall_started_at = None
                stalled_for = 0.0
            else:
                # Player.Caching catches Kodi's internal rebuffering state while
                # position movement catches stalls where Kodi does not expose
                # the caching flag for the whole duration.
                caching = xbmc.getCondVisibility("Player.Caching")

                if caching:
                    if caching_started_at is None:
                        caching_started_at = now
                else:
                    caching_started_at = None

                position_stalled = (
                    last_progress_position is not None
                    and now - last_progress_change_at >= stall_threshold
                )
                caching_stalled = (
                    caching_started_at is not None
                    and now - caching_started_at >= stall_threshold
                )

                if position_stalled or caching_stalled:
                    if stall_started_at is None:
                        healthy_since = 0.0
                        stall_started_at = (
                            caching_started_at
                            if caching_stalled and caching_started_at is not None
                            else last_progress_change_at
                        )

                    stalled_for = now - stall_started_at
                else:
                    if last_progress_position is None:
                        last_progress_position = position
                        last_progress_change_at = now
                    elif position > last_progress_position + 0.25:
                        last_progress_position = position
                        last_progress_change_at = now

                        # Do not start the healthy recovery clock until the
                        # replacement stream has actually advanced.
                        if not progress_confirmed:
                            progress_confirmed = True
                            healthy_since = now
                            log(
                                "Adaptive replacement stream confirmed progressing "
                                "at position=%.3f" % position,
                                xbmc.LOGDEBUG,
                            )

                    if stall_started_at is not None:
                        if progress_confirmed:
                            healthy_since = now
                        stall_started_at = None

                    if healthy_since <= 0 and progress_confirmed:
                        healthy_since = now

                    # A new healthy playback interval starts a fresh adaptive
                    # retry budget for the next independent stall.
                    if downshift_retry_exhausted:
                        downshift_retry_exhausted = False
                        downshift_retry_count = 0
                        downshift_retry_at = 0.0
                        log(
                            "Adaptive retry budget reset after playback recovery",
                            xbmc.LOGDEBUG,
                        )

                    stalled_for = 0.0

            # -------------------------------------------------- downshift
            if (
                not direct_play_only
                and stall_started_at is not None
                and stalled_for >= stall_threshold
                and now - last_down_replan_at >= down_cooldown
                and now >= downshift_retry_at
                and not downshift_retry_exhausted
                and (
                    # A failed upward probe is allowed to fall back immediately;
                    # the normal grace still protects ordinary post-switch playback
                    # from false-positive stalls.
                    probe_quality == detect_quality_label(playback_info)
                    or last_upshift_at <= 0
                    or now - last_upshift_at >= upshift_downshift_grace
                )
            ):
                try:
                    plan = playback_info.get("playback_plan") or {}
                    current_label = detect_quality_label(playback_info)

                    # If the current rung is an upward probe, return directly
                    # to the last trusted quality. This avoids testing another
                    # rung in the same direction after a failed probe.
                    if (
                        probe_quality
                        and current_label == probe_quality
                        and known_good_quality
                        and known_good_quality != current_label
                    ):
                        target_label = known_good_quality
                    else:
                        target_label = next_quality_label(
                            plan,
                            current_label,
                            1,
                        )

                    if target_label is None:
                        # We are already at the lowest published rung. Do not
                        # repeatedly ask Silo for another recovery plan.
                        last_down_replan_at = now
                        downshift_retry_at = 0.0
                        downshift_retry_count = 0
                        downshift_retry_exhausted = False
                    else:
                        recipe = plan.get("effective_recipe") or {}

                        try:
                            current_bitrate = int(
                                recipe.get("bitrate_kbps") or 0
                            )
                        except (TypeError, ValueError):
                            current_bitrate = 0

                        estimated_bandwidth = max(
                            100,
                            int(current_bitrate * 0.60)
                            if current_bitrate > 0
                            else 1500,
                        )

                        # quality_change names the exact next published rung.
                        # This starts a fresh intent replan chain rather than
                        # permanently exhausting the playback recovery chain.
                        new_info = client.replan_playback(
                            playback_info,
                            position,
                            estimated_bandwidth,
                            quality_preference=target_label,
                            operation="quality_change",
                        )

                        new_plan = new_info.get("playback_plan") or {}
                        new_label = (
                            str(
                                (new_info or {}).get("adaptive_quality") or ""
                            ).strip()
                            or target_label
                        )

                        if (
                            new_info.get("url")
                            and new_label == target_label
                            and switch_stream(
                                new_info,
                                position,
                                target_label,
                            )
                        ):
                            switch_time = time.time()

                            # A successful downshift proves that the previous
                            # quality was not sustainable under the conditions
                            # that caused the stall. Remember this quality so it
                            # is not immediately selected again as an upshift.
                            failure_count = quality_failures.get(
                                current_label,
                                0,
                            ) + 1
                            quality_failures[current_label] = failure_count

                            cooldown = min(
                                quality_failure_cooldown_max,
                                quality_failure_cooldown_base
                                * (2 ** min(failure_count - 1, 3)),
                            )
                            quality_failure_cooldowns[current_label] = (
                                switch_time + cooldown
                            )

                            log(
                                "Adaptive quality %s marked unstable after "
                                "sustained stall #%d; upward probe blocked for %.0fs"
                                % (
                                    current_label,
                                    failure_count,
                                    cooldown,
                                )
                            )

                            # The selected lower rung becomes the trusted
                            # fallback. Any active upward probe is abandoned.
                            known_good_quality = target_label
                            known_good_since = switch_time
                            probe_quality = None

                            last_down_replan_at = switch_time
                            last_up_replan_at = 0.0
                            downshift_retry_at = 0.0
                            downshift_retry_count = 0
                            downshift_retry_exhausted = False
                            last_progress_position = None
                            last_progress_change_at = last_down_replan_at
                            progress_confirmed = False
                            healthy_since = 0.0
                            post_switch_grace_until = (
                                last_down_replan_at + 15.0
                            )
                            stall_started_at = None

                            log(
                                "Adaptive downshift complete: %s -> %s"
                                % (current_label, target_label)
                            )
                        else:
                            last_down_replan_at = now
                            log(
                                "Silo returned no exact adaptive downshift for "
                                "%s -> %s (delivery=%s)"
                                % (
                                    current_label,
                                    target_label,
                                    new_plan.get("delivery"),
                                ),
                                xbmc.LOGWARNING,
                            )

                except SiloError as exc:
                    retryable = bool(
                        isinstance(exc.problem, dict)
                        and exc.problem.get("retryable") is True
                    )

                    if retryable and downshift_retry_count < adaptive_retry_limit:
                        downshift_retry_count += 1
                        downshift_retry_at = now + adaptive_retry_delay
                        # The retry timer, rather than the normal cooldown, controls
                        # the next attempt for a retryable server-side startup failure.
                        last_down_replan_at = now - down_cooldown
                        log(
                            "Adaptive downshift failed with retryable Silo error; "
                            "retry %d/%d in %.1fs: %s"
                            % (
                                downshift_retry_count,
                                adaptive_retry_limit,
                                adaptive_retry_delay,
                                exc,
                            ),
                            xbmc.LOGWARNING,
                        )
                    else:
                        last_down_replan_at = now
                        downshift_retry_at = 0.0
                        if retryable:
                            # A retryable startup failure gets one immediate retry
                            # for this stall episode. If that also fails, wait for
                            # playback to recover before allowing another retry cycle.
                            downshift_retry_exhausted = True
                            downshift_retry_count = 0
                            log(
                                "Adaptive downshift retry failed; waiting for "
                                "playback recovery before trying again: %s" % exc,
                                xbmc.LOGWARNING,
                            )
                        else:
                            downshift_retry_count = 0
                            downshift_retry_exhausted = False
                            log(
                                "Adaptive downshift failed: %s" % exc,
                                xbmc.LOGWARNING,
                            )
                except Exception as exc:
                    last_down_replan_at = now
                    log(
                        "Unexpected adaptive downshift error: %s" % exc,
                        xbmc.LOGWARNING,
                    )

            # ---------------------------------------------------- upshift
            if (
                not direct_play_only
                and stall_started_at is None
                and healthy_since > 0
                and probe_quality is None
                and now - healthy_since >= healthy_recovery_threshold
                and now - last_up_replan_at >= up_cooldown
            ):
                try:
                    plan = playback_info.get("playback_plan") or {}
                    current_label = detect_quality_label(playback_info)
                    target_label = next_quality_label(
                        plan,
                        current_label,
                        -1,
                    )

                    if target_label is None:
                        # Already at the source/original ceiling. There is
                        # nothing higher to request until a future downshift.
                        last_up_replan_at = now
                        healthy_since = now
                    elif now < quality_failure_cooldowns.get(target_label, 0.0):
                        # This rung previously caused a sustained stall. Do not
                        # immediately probe it again just because the current
                        # quality has been healthy for five minutes.
                        remaining = max(
                            0.0,
                            quality_failure_cooldowns[target_label] - now,
                        )

                        last_up_replan_at = now

                        log(
                            "Adaptive upward probe held at %s: target quality "
                            "%s remains on failure cooldown for %.0fs"
                            % (
                                current_label,
                                target_label,
                                remaining,
                            ),
                            xbmc.LOGDEBUG,
                        )
                    else:
                        log(
                            "Adaptive upshift eligible: %s -> %s after %.1fs healthy playback"
                            % (
                                current_label,
                                target_label,
                                now - healthy_since,
                            )
                        )

                        recipe = plan.get("effective_recipe") or {}

                        try:
                            current_bitrate = int(
                                recipe.get("bitrate_kbps") or 0
                            )
                        except (TypeError, ValueError):
                            current_bitrate = 0

                        estimated_bandwidth = max(
                            1500,
                            int(current_bitrate * 1.35)
                            if current_bitrate > 0
                            else 8000,
                        )

                        # Never send "auto" for an adaptive quality recovery.
                        # Silo expects the label of the exact ladder rung wanted.
                        new_info = client.replan_playback(
                            playback_info,
                            position,
                            estimated_bandwidth,
                            quality_preference=target_label,
                            operation="quality_change",
                        )

                        new_plan = new_info.get("playback_plan") or {}
                        new_recipe = new_plan.get("effective_recipe") or {}

                        # quality_change is an explicit selection from Silo's
                        # published ladder. Once Silo accepts it and returns a
                        # replacement stream, do not second-guess the response
                        # with a local quality detector. The previous detector
                        # could mislabel a valid rung as "original" and leave
                        # the server on the new plan while Kodi kept the old
                        # stream, causing subsequent 409 stale-plan conflicts.
                        if (
                            new_info.get("url")
                            and switch_stream(
                                new_info,
                                position,
                                target_label,
                            )
                        ):
                            now = time.time()
                            last_up_replan_at = now
                            last_upshift_at = now
                            last_down_replan_at = 0.0
                            last_progress_position = None
                            last_progress_change_at = now
                            progress_confirmed = False
                            healthy_since = 0.0
                            post_switch_grace_until = now + 15.0

                            # The higher rung is now a probe. It must actually
                            # advance and then remain healthy for the full
                            # confirmation interval before replacing the current
                            # trusted quality.
                            probe_quality = target_label

                            log(
                                "Adaptive quality probe started: %s -> %s; "
                                "target must remain healthy for %.0fs before "
                                "becoming trusted"
                                % (
                                    current_label,
                                    target_label,
                                    quality_confirmation_threshold,
                                )
                            )

                            log(
                                "Adaptive upshift complete: %s -> %s "
                                "(server recipe height=%s bitrate=%s)"
                                % (
                                    current_label,
                                    target_label,
                                    new_recipe.get("height"),
                                    new_recipe.get("bitrate_kbps"),
                                )
                            )
                        else:
                            last_up_replan_at = now
                            healthy_since = now

                            log(
                                "Kodi could not adopt the Silo adaptive "
                                "upshift %s -> %s (delivery=%s)"
                                % (
                                    current_label,
                                    target_label,
                                    new_plan.get("delivery"),
                                ),
                                xbmc.LOGWARNING,
                            )

                except SiloError as exc:
                    last_up_replan_at = now
                    healthy_since = now

                    # A failed replan request is not enough evidence that the
                    # target quality itself is unstable. The server may simply
                    # have failed to create the replacement transport.
                    log(
                        "Adaptive quality probe request failed for %s: %s"
                        % (target_label, exc),
                        xbmc.LOGWARNING,
                    )
                except Exception as exc:
                    last_up_replan_at = now
                    healthy_since = now
                    log(
                        "Unexpected adaptive quality recovery error: %s"
                        % exc,
                        xbmc.LOGWARNING,
                    )

        last_position = max(last_position, position)
        pause_state_changed = (
            last_reported_paused is not None
            and bool(paused) != bool(last_reported_paused)
        )
        should_report_progress = (
            now >= next_progress_report_at or pause_state_changed
        )

        if should_report_progress:
            sequence += 1
            try:
                client.report_progress(
                    session_id,
                    sequence,
                    last_position,
                    paused,
                )
                last_reported_paused = bool(paused)
                next_progress_report_at = now + progress_report_interval
            except SiloError as exc:
                if playback_session_was_terminated(exc):
                    log(
                        "Silo playback session was terminated by the server; "
                        "stopping Kodi playback.",
                        xbmc.LOGWARNING,
                    )
                    try:
                        player.stop()
                    except Exception as stop_exc:
                        log(
                            "Unable to stop Kodi playback after server termination: %s"
                            % stop_exc,
                            xbmc.LOGWARNING,
                        )
                    break

                log(
                    "Unable to report playback progress: %s" % exc,
                    xbmc.LOGWARNING,
                )
                # Keep the next scheduled report based on the current time so
                # a slow/unreachable server does not alter adaptive monitoring.
                next_progress_report_at = now + progress_report_interval

        for _ in range(50):
            if not player.isPlaying() or monitor.abortRequested():
                break

            xbmc.sleep(100)

    try:
        if player.isPlaying():
            last_position = max(
                0.0,
                float(player.getTime()),
            )
    except Exception:
        pass

    sequence += 1

    def finish_session():
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
        except Exception as exc:
            log(
                "Unexpected error stopping Silo playback session: %s" % exc,
                xbmc.LOGWARNING,
            )

    log(
        "Adaptive playback monitor ended for session %s at quality=%s"
        % (
            session_id,
            playback_info.get("adaptive_quality") or "unknown",
        ),
        xbmc.LOGDEBUG,
    )

    cleanup_thread = threading.Thread(
        target=finish_session,
        name="SiloPlaybackCleanup",
    )
    cleanup_thread.daemon = True
    cleanup_thread.start()


def kodi_requested_resume():
    """Return Kodi's native resume choice for the current plugin request.

    Kodi passes this to plugin scripts as the fourth argument:
        resume:true  -> the user chose Resume
        resume:false -> the user chose Start from beginning
    """
    if len(sys.argv) < 4:
        return False

    value = str(sys.argv[3] or "").strip().lower()

    if value.startswith("resume:"):
        value = value.split(":", 1)[1]

    return value == "true"



__all__ = ["resolve_playback_library_id","choose_file","apply_fresh_resume_to_resolved_item","play","playback_session_was_terminated","track_progress","kodi_requested_resume"]
