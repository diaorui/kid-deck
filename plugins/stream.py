import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, time as dtime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import pychromecast
from pychromecast.discovery import CastBrowser, SimpleCastListener
import requests

deno = os.path.expanduser("~/.deno/bin")
if deno not in os.environ.get("PATH", ""):
    os.environ["PATH"] = deno + ":" + os.environ.get("PATH", "")

import yt_dlp

from plugins import Plugin


def _local_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def parse_time(time_str: str) -> dtime:
    parts = time_str.strip().split(":")
    return dtime(int(parts[0]), int(parts[1]))


def parse_youtube_id(value: str) -> str | None:
    """Accept bare 11-char id or common YouTube URL forms."""
    s = (value or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[\w-]{11}", s):
        return s
    m = re.search(
        r"(?:v=|/youtu\.be/|/embed/|/shorts/|youtube\.com/watch\?.*?v=)([\w-]{11})",
        s,
    )
    return m.group(1) if m else None


CHANNEL_IDS = {
    "@likenastyaofficial": "UCJplp5SjeGSdVdwsfb9Q7lQ",
    "@ArtforKidsHub": "UC5XMF3Inoi8R9nSI8ChOsdQ",
    "@annamcnulty": "UCPIavfNa4DTfHXDBaFOHuKA",
    "@RosannaPansino": "UCjwmbv6NE4mOh8Z8VhPUx1Q",
    "@XIAOXINGXING-樂樂TV": "UCtWocEKhgpEffPDlwwozK9Q",
    "@TrinityandBeyond": "UCCryrohClZM8XK4yEYBE_qA",
    "@PrestonYT": "UC70Dib4MvFfT1tU6MqeyHpQ",
    "@KidsDianaShow": "UCk8GzjMOrta8yxDcKfylJYw",
    "@Blippi": "UC5PYHgAzJ1wLEidB58SK6Xw",
    "@BeyondFamily": "UCI3t3ddiv350EgU_Q7EOP9w",
    "@XiaolingToy": "UC1krNaypYa_vx9W2Kvku8Xw",
}


def _resolve_channel_id(handle: str) -> str | None:
    if handle in CHANNEL_IDS:
        return CHANNEL_IDS[handle]
    r = requests.get(
        f"https://www.youtube.com/{handle}/about",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    for p in [
        r'"externalId":"(UC[^"]+)"',
        r'"channelId":"(UC[^"]+)"',
        r'<meta itemprop="channelId" content="(UC[^"]+)"',
    ]:
        m = re.search(p, r.text)
        if m:
            return m.group(1)
    return None


def _fetch_channel_videos(handle: str, ydl: yt_dlp.YoutubeDL) -> list[dict]:
    url = f"https://www.youtube.com/{handle}/videos"
    info = ydl.extract_info(url, download=False)
    entries = info.get("entries", [])
    videos = []
    for entry in entries:
        if not entry or entry.get("media_type") != "video":
            continue
        vid = entry.get("id")
        dur = int(entry.get("duration") or 0)
        ts = entry.get("timestamp")
        now_ts = time.time()
        pub_ts = int(ts) if ts and ts <= now_ts else int(now_ts)
        videos.append(
            {
                "title": entry.get("title", ""),
                "video_id": vid,
                "published": pub_ts,
                "link": f"https://youtube.com/watch?v={vid}",
                "handle": handle,
                "duration": dur,
                "resolved_url": entry.get("url"),
            }
        )
    return videos


def _parse_duration(raw: str) -> int:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def _validate_feed(url: str) -> dict:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code} — server returned error"}
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        return {"ok": False, "error": f"Invalid XML: {e}"}
    tag = root.tag.lower()
    if tag == "rss":
        ch = root.find("channel")
        if ch is None:
            return {"ok": False, "error": "RSS has no <channel> element"}
    elif tag in ("feed",):
        return {"ok": False, "error": "Atom feeds are not supported (only RSS 2.0)"}
    else:
        return {"ok": False, "error": f"Not an RSS feed (root is <{tag}>)"}
    title = ch.findtext("title", "").strip()
    if not title:
        return {"ok": False, "error": "Feed has no title"}
    items = ch.findall("item")
    if not items:
        return {"ok": False, "error": "Feed has no episodes"}
    sample_url = ""
    for item in items:
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        eurl = (enclosure.get("url") or "").strip()
        if eurl:
            sample_url = eurl
            break
    if not sample_url:
        return {"ok": False, "error": "No audio content found in any episode"}
    return {"ok": True, "title": title}


ITUNES_NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
TARGET_LOUDNESS = -16.0


def _measure_loudness(url: str) -> float:
    """Analyze first 60s of audio with ffmpeg loudnorm, return gain_db to hit TARGET_LOUDNESS.
    Returns 0.0 on failure (safe fallback: no adjustment)."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-i", url, "-t", "60",
                "-af", "loudnorm=I=-16:print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=120,
        )
        stderr = proc.stderr
        start = stderr.find("{")
        if start == -1:
            return 0.0
        json_str = stderr[start:]
        depth = 0
        end = -1
        for i, c in enumerate(json_str):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return 0.0
        data = json.loads(json_str[:end])
        input_i = float(data.get("input_i", 0))
        gain_db = TARGET_LOUDNESS - input_i
        return max(-15.0, min(15.0, gain_db))
    except Exception as e:
        logging.getLogger("plugin.stream").warning("loudness measurement failed: %s", e)
        return 0.0


def _fetch_feed(feed_name: str, feed_url: str, keep: int) -> list[dict]:
    r = requests.get(feed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    root = ET.fromstring(r.content)
    ch = root.find("channel")
    items = ch.findall("item")
    result = []
    for item in items:
        title = item.findtext("title", "Untitled")
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        mp3_url = (enclosure.get("url") or "").strip()
        if not mp3_url:
            continue
        pub_raw = item.findtext("pubDate", "")
        pub_ts = int(time.time())
        if pub_raw:
            try:
                dt = parsedate_to_datetime(pub_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                pub_ts = int(dt.timestamp())
            except Exception:
                pass
        dur_raw = item.findtext("duration", "") or item.findtext(
            "itunes:duration", "", ITUNES_NS
        )
        dur = _parse_duration(dur_raw) if dur_raw else 0
        link = item.findtext("link", "")
        result.append(
            {
                "title": title,
                "url": mp3_url,
                "published": pub_ts,
                "duration": dur,
                "feed_name": feed_name,
                "feed_url": feed_url,
                "link": link,
            }
        )
    result.sort(key=lambda e: e["published"], reverse=True)
    if keep > 0:
        result = result[:keep]
    return result


def _normalize_video(v: dict) -> dict:
    vid = v.get("video_id") or ""
    return {
        "id": f"v:{vid}",
        "kind": "video",
        "title": v.get("title") or "",
        "published": int(v.get("published") or 0),
        "duration": int(v.get("duration") or 0),
        "source": v.get("handle") or "",
        "play_url": v.get("resolved_url") or "",
        "video_id": vid,
        "link": v.get("link") or f"https://youtube.com/watch?v={vid}",
    }


def _normalize_audio(e: dict) -> dict:
    url = e.get("url") or ""
    return {
        "id": f"a:{url}",
        "kind": "audio",
        "title": e.get("title") or "",
        "published": int(e.get("published") or 0),
        "duration": int(e.get("duration") or 0),
        "source": e.get("feed_name") or "",
        "play_url": url,
        "link": e.get("link") or "",
        "gain_db": e.get("gain_db", 0.0),
    }


def plan_playlist(
    video_pool: list[dict],
    audio_pool: list[dict],
    screen0: float,
    total0: float,
    ratio: float,
) -> list[dict]:
    """Interleave newest pools by cumulative screen/total ratio."""
    vi = ai = 0
    screen = float(screen0)
    total = float(total0)
    out: list[dict] = []
    while vi < len(video_pool) or ai < len(audio_pool):
        want_video = (total <= 0) or (screen / total < ratio)
        if want_video and vi < len(video_pool):
            item = video_pool[vi]
            vi += 1
            out.append(item)
            d = float(item.get("duration") or 0)
            screen += d
            total += d
        elif ai < len(audio_pool):
            item = audio_pool[ai]
            ai += 1
            out.append(item)
            total += float(item.get("duration") or 0)
        elif vi < len(video_pool):
            item = video_pool[vi]
            vi += 1
            out.append(item)
            d = float(item.get("duration") or 0)
            screen += d
            total += d
        else:
            break
    return out


class StreamPlugin(Plugin):
    name = "stream"
    title = "Stream"
    icon = "📡"
    order = 4

    def __init__(self, controller, config: dict):
        super().__init__(controller, config)

        self.screen_minutes_per_hour: float = float(
            config.get("screen_minutes_per_hour", 12)
        )
        self.max_video_minutes: int = int(config.get("max_video_minutes", 30))
        self.max_audio_minutes: int = int(config.get("max_audio_minutes", 30))
        self.playlist_horizon_hours: float = float(
            config.get("playlist_horizon_hours", 3.5)
        )
        self.yt_fetch_per_channel: int = int(config.get("yt_fetch_per_channel", 8))
        self.podcast_keep_per_feed: int = int(config.get("podcast_keep_per_feed", 10))
        self.feed_interval: int = int(config.get("feed_interval", 60))

        sched = config.get("schedule") or {}
        self.stop_time = parse_time(sched.get("stop_time", "21:00"))
        self.outro_video_url: str = str(config.get("outro_video_url", "") or "")

        self.channel_enabled: dict[str, bool] = dict(
            config.get("channel_enabled", {})
        )
        self.feeds: dict[str, dict] = dict(config.get("feeds", {}))

        self.status = "disconnected"
        self.device_name = ""
        self.playlist: list[dict] = []
        self.current_index = -1
        self.total_played_sec: float = 0.0
        self.screen_played_sec: float = 0.0

        self._cast: pychromecast.Chromecast | None = None
        self._browser: pychromecast.discovery.CastBrowser | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._yt_cache: dict[str, list[dict]] = {}
        self._pc_cache: dict[str, list[dict]] = {}
        self._gain_cache: dict[str, float] = {}
        self._yt_feed_thread: threading.Thread | None = None
        self._pc_feed_thread: threading.Thread | None = None
        self._feed_stop_event = threading.Event()
        self._play_start: float = 0.0
        self._media_duration_local: int = 0
        self._outro_playing: bool = False
        # Separate ydl instances so YT feed thread and play resolve never share one
        self._ydl = yt_dlp.YoutubeDL(
            {
                "format": "18",
                "quiet": True,
                "playlistend": self.yt_fetch_per_channel,
                "socket_timeout": 30,
                "remote_components": ["ejs:github"],
                "extractor_args": {"youtube": ["player_client=android"]},
            }
        )
        self._ydl_play = yt_dlp.YoutubeDL(
            {
                "format": "18",
                "quiet": True,
                "socket_timeout": 30,
                "remote_components": ["ejs:github"],
                "extractor_args": {"youtube": ["player_client=android"]},
            }
        )

    def _ratio(self) -> float:
        return max(0.01, min(0.99, self.screen_minutes_per_hour / 60.0))

    def _video_budget_sec(self) -> float:
        return self.playlist_horizon_hours * self.screen_minutes_per_hour * 60.0

    def _audio_budget_sec(self) -> float:
        audio_min = max(0.0, 60.0 - self.screen_minutes_per_hour)
        return self.playlist_horizon_hours * audio_min * 60.0

    def _past_stop_time(self) -> bool:
        now = datetime.now().time()
        if self.stop_time.hour < 6:
            if now.hour < 6:
                return now >= self.stop_time
            return True
        if now.hour < 6:
            return False
        return now >= self.stop_time

    def _save_config(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        try:
            import yaml

            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            if "plugins" not in cfg:
                cfg["plugins"] = {}
            cfg["plugins"]["stream"] = {
                "screen_minutes_per_hour": self.screen_minutes_per_hour,
                "max_video_minutes": self.max_video_minutes,
                "max_audio_minutes": self.max_audio_minutes,
                "playlist_horizon_hours": self.playlist_horizon_hours,
                "yt_fetch_per_channel": self.yt_fetch_per_channel,
                "podcast_keep_per_feed": self.podcast_keep_per_feed,
                "feed_interval": self.feed_interval,
                "schedule": {
                    "stop_time": self.stop_time.strftime("%H:%M"),
                },
                "outro_video_url": self.outro_video_url,
                "channel_enabled": dict(self.channel_enabled),
                "feeds": {
                    name: {"url": info["url"], "enabled": info.get("enabled", True)}
                    for name, info in self.feeds.items()
                },
            }
            with open(path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log.error("failed to save config: %s", e)

    def _build_preview_playlist(self) -> list[dict]:
        """Live preview from latest cache (as if starting a fresh session). Does not mutate play state."""
        v_pool, a_pool = self._build_pools()
        return plan_playlist(v_pool, a_pool, 0.0, 0.0, self._ratio())

    def _build_pools(self) -> tuple[list[dict], list[dict]]:
        """Newest-first pools sized by horizon budgets. No play-history dedup."""
        max_v = self.max_video_minutes * 60
        max_a = self.max_audio_minutes * 60
        v_budget = self._video_budget_sec()
        a_budget = self._audio_budget_sec()

        videos: list[dict] = []
        for handle, enabled in self.channel_enabled.items():
            if not enabled:
                continue
            for raw in self._yt_cache.get(handle, []):
                item = _normalize_video(raw)
                if not item["video_id"]:
                    continue
                if item["duration"] <= 0 or item["duration"] > max_v:
                    continue
                videos.append(item)
        videos.sort(key=lambda x: x["published"], reverse=True)
        seen: set[str] = set()
        uniq_v: list[dict] = []
        for it in videos:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            uniq_v.append(it)

        audios: list[dict] = []
        for name, info in self.feeds.items():
            if not info.get("enabled", True):
                continue
            for raw in self._pc_cache.get(name, []):
                item = _normalize_audio(raw)
                if not item["play_url"]:
                    continue
                if item["duration"] <= 0 or item["duration"] > max_a:
                    continue
                audios.append(item)
        audios.sort(key=lambda x: x["published"], reverse=True)
        seen_a: set[str] = set()
        uniq_a: list[dict] = []
        for it in audios:
            if it["id"] in seen_a:
                continue
            seen_a.add(it["id"])
            uniq_a.append(it)

        video_pool: list[dict] = []
        acc = 0.0
        for it in uniq_v:
            video_pool.append(it)
            acc += it["duration"]
            if acc >= v_budget:
                break

        audio_pool: list[dict] = []
        acc = 0.0
        for it in uniq_a:
            audio_pool.append(it)
            acc += it["duration"]
            if acc >= a_budget:
                break

        return video_pool, audio_pool

    def _replan_locked(self) -> bool:
        """Rebuild playlist from latest cache. Caller holds lock.
        Continues eye-protection ratio from screen_played/total_played.
        """
        v_pool, a_pool = self._build_pools()
        planned = plan_playlist(
            v_pool,
            a_pool,
            self.screen_played_sec,
            self.total_played_sec,
            self._ratio(),
        )
        self.playlist = planned
        self.current_index = 0 if planned else -1
        self.log.info("replan → %d items (Vpool=%d Apool=%d screen=%.1fm total=%.1fm)",
                       len(planned), len(v_pool), len(a_pool),
                       self.screen_played_sec / 60, self.total_played_sec / 60)
        return bool(planned)

    def _ensure_mixed_playlist_locked(self) -> bool:
        """Ensure we have something to play; replan when list ends or one kind runs out.

        If remaining is missing video or audio while cache still has that kind, rebuild
        so we do not stream only one type for a long tail.
        """
        if self.current_index < 0 or self.current_index >= len(self.playlist):
            return self._replan_locked()

        rest = self.playlist[self.current_index :]
        has_v = any(x.get("kind") == "video" for x in rest)
        has_a = any(x.get("kind") == "audio" for x in rest)
        if has_v and has_a:
            return True

        v_pool, a_pool = self._build_pools()
        need_replan = (not has_v and len(v_pool) > 0) or (not has_a and len(a_pool) > 0)
        if not need_replan and (v_pool or a_pool) and not rest:
            need_replan = True
        if need_replan:
            return self._replan_locked()

        return bool(rest)

    def _yt_feed_loop(self):
        """Background YT channel fetch — independent of podcast loop."""
        first_pass = True
        while not self._feed_stop_event.is_set():
            with self._lock:
                handles = list(self.channel_enabled.keys())
            for handle in handles:
                if self._feed_stop_event.is_set():
                    return
                if not self.channel_enabled.get(handle, True):
                    continue
                try:
                    videos = _fetch_channel_videos(handle, self._ydl)
                    with self._lock:
                        self._yt_cache[handle] = videos
                except Exception as e:
                    self.log.warning("YT feed: failed %s: %s", handle, e)
                delay = 0.5 if first_pass else self.feed_interval
                self._feed_stop_event.wait(delay)
            first_pass = False

    def _pc_feed_loop(self):
        """Background podcast RSS fetch — independent of YT loop."""
        first_pass = True
        while not self._feed_stop_event.is_set():
            with self._lock:
                feed_list = list(self.feeds.items())
            for feed_name, info in feed_list:
                if self._feed_stop_event.is_set():
                    return
                if not info.get("enabled", True):
                    continue
                try:
                    episodes = _fetch_feed(
                        feed_name, info["url"], self.podcast_keep_per_feed
                    )
                    for ep in episodes:
                        if ep.get("duration", 0) >= 60:
                            if ep["url"] not in self._gain_cache:
                                self._gain_cache[ep["url"]] = _measure_loudness(ep["url"])
                            ep["gain_db"] = self._gain_cache[ep["url"]]
                        else:
                            ep["gain_db"] = 0.0
                    with self._lock:
                        self._pc_cache[feed_name] = episodes
                except Exception as e:
                    self.log.warning("podcast feed: failed %s: %s", feed_name, e)
                delay = 0.5 if first_pass else self.feed_interval
                self._feed_stop_event.wait(delay)
            first_pass = False

    def _discover(self) -> pychromecast.Chromecast | None:
        try:
            zc = getattr(self.controller, "zc", None)
            if zc is None:
                return None
            if self._browser is None:
                self._browser = CastBrowser(SimpleCastListener(), zc)
                self._browser.start_discovery()
                time.sleep(30)
            if not self._browser.devices:
                return None
            device = list(self._browser.devices.values())[0]
            cc = pychromecast.get_chromecast_from_cast_info(device, zc)
            cc.wait(timeout=30)
            return cc
        except Exception as e:
            self.log.warning("discovery error: %s", e)
        return None

    def _record_progress_locked(self, item: dict | None):
        if not item or self._play_start <= 0:
            return
        elapsed = max(0.0, time.time() - self._play_start)
        dur = float(item.get("duration") or 0)
        played = min(elapsed, dur) if dur > 0 else elapsed
        self.total_played_sec += played
        if item.get("kind") == "video":
            self.screen_played_sec += played
        self._play_start = 0

    def _play_current(self) -> str | None:
        """Play playlist[current_index]. May replan if needed. Returns error or None."""
        max_attempts = 12
        for attempt in range(max_attempts):
            if self._past_stop_time():
                self.log.info("past stop time, stopping")
                self._do_stop()
                return "past stop time"
            with self._lock:
                if self.current_index < 0 or self.current_index >= len(self.playlist):
                    if not self._ensure_mixed_playlist_locked():
                        self.status = "connected_idle"
                        return "playlist empty"
                    if self.current_index < 0 or self.current_index >= len(self.playlist):
                        self.status = "connected_idle"
                        return "playlist empty"
                item = dict(self.playlist[self.current_index])
                cast = self._cast
            if cast is None:
                return "not connected"
            try:
                if item.get("kind") == "video":
                    video_id = item.get("video_id") or ""
                    self.log.info("attempt %d/%d idx=%d id=%s title=%s kind=video",
                                   attempt + 1, max_attempts, self.current_index,
                                   video_id, item.get("title"))
                    info = self._ydl_play.extract_info(
                        f"https://youtube.com/watch?v={video_id}", download=False
                    )
                    url = info.get("url") or item.get("play_url")
                    dur = int(info.get("duration") or item.get("duration") or 0)
                    if not url:
                        self.log.warning("resolve %s returned no url, fallback to feed url",
                                          video_id)
                        url = item.get("play_url") or ""
                    if not url:
                        raise RuntimeError(f"could not resolve URL for {video_id}")
                    ext = info.get("ext", "?")
                    fmt_id = info.get("format_id", "?")
                    proto = info.get("protocol", "?")
                    has_manifest = "manifest_url" in info or "manifest" in str(info.get("url", ""))
                    self.log.info("resolve OK url_len=%d dur=%d ext=%s fmt=%s proto=%s manifest=%s url=%s",
                                   len(url), dur, ext, fmt_id, proto, has_manifest, url)
                    with self._lock:
                        if self.current_index < len(self.playlist):
                            self.playlist[self.current_index]["duration"] = dur
                            self.playlist[self.current_index]["play_url"] = url
                    mc = cast.media_controller
                    mc.play_media(url, "video/mp4")
                    self.log.info("play_media(video, ct=video/mp4, url_len=%d)", len(url))
                    with self._lock:
                        self._play_start = time.time()
                        self._media_duration_local = dur
                        self.status = "playing"
                    self.log.info("playing video [%d/%d] %s",
                                   self.current_index + 1, len(self.playlist),
                                   item.get("title"))
                    return None
                else:
                    url = item.get("play_url") or ""
                    if not url:
                        raise RuntimeError("missing audio url")
                    gain_db = item.get("gain_db", 0.0)
                    if gain_db != 0.0:
                        local_ip = _local_ip()
                        if not local_ip:
                            raise RuntimeError("cannot apply gain_db: no LAN IP detected")
                        port = self.controller.config.get("server", {}).get("port", 8080)
                        url = (
                            f"http://{local_ip}:{port}/api/stream/audio_proxy"
                            f"?url={quote(url)}&gain_db={gain_db}"
                        )
                    self.log.info("attempt %d/%d idx=%d title=%s kind=audio gain_db=%.1f",
                                   attempt + 1, max_attempts, self.current_index,
                                   item.get("title"), gain_db)
                    mc = cast.media_controller
                    mc.play_media(url, "audio/mpeg")
                    self.log.info("play_media(audio, ct=audio/mpeg, url_len=%d)", len(url))
                    with self._lock:
                        self._play_start = time.time()
                        self._media_duration_local = int(item.get("duration") or 0)
                        self.status = "playing"
                    self.log.info("playing audio [%d/%d] %s",
                                   self.current_index + 1, len(self.playlist),
                                   item.get("title"))
                    return None
            except Exception as e:
                self.log.warning("skipping unavailable %s: %s", item.get("title"), e)
                if attempt < max_attempts - 1:
                    with self._lock:
                        self.status = "retrying"
                    self.log.info("retrying in 2s")
                    time.sleep(2)
        with self._lock:
            self.status = "connected_idle"
        self.log.error("all %d attempts exhausted", max_attempts)
        return "all items unavailable"

    def _advance_and_play(self) -> str | None:
        """After finish/skip of current item: record, advance, maybe replan, play next."""
        with self._lock:
            if self._outro_playing or self.status == "ending":
                self._do_stop()
                return "outro ended"
        if self._past_stop_time():
            # Auto-stop path: prefer outro over hard stop
            self._begin_auto_stop()
            return "past stop time"
        with self._lock:
            cur = None
            if 0 <= self.current_index < len(self.playlist):
                cur = dict(self.playlist[self.current_index])
            self._record_progress_locked(cur)
            self.current_index += 1
            # If remaining lacks both kinds, replan from latest cache before play
            if not self._ensure_mixed_playlist_locked():
                self.status = "connected_idle"
                return "playlist empty"
        return self._play_current()

    def _begin_auto_stop(self):
        """Called when stop_time is reached during normal play (not manual stop)."""
        with self._lock:
            if self._outro_playing or self.status == "ending":
                return
            if self.status != "playing":
                return
            url = (self.outro_video_url or "").strip()
            if not url:
                self.log.info("auto-stop at stop_time (no outro)")
                self._do_stop()
                return
        self.log.info("auto-stop → playing outro")
        err = self._start_outro()
        if err:
            self.log.error("outro failed (%s), full stop", err)
            with self._lock:
                self._do_stop()

    def _start_outro(self) -> str | None:
        """Stop current media (keep cast) and play configured outro video."""
        vid = parse_youtube_id(self.outro_video_url)
        if not vid:
            return "invalid outro url"
        with self._lock:
            cast = self._cast
            if cast is None:
                return "not connected"
            try:
                cast.media_controller.stop()
            except Exception:
                pass
            self._outro_playing = True
            self.status = "ending"
            self.playlist = [
                {
                    "id": f"v:{vid}",
                    "kind": "video",
                    "title": "Goodnight",
                    "published": int(time.time()),
                    "duration": 0,
                    "source": "outro",
                    "play_url": "",
                    "video_id": vid,
                    "link": f"https://youtube.com/watch?v={vid}",
                }
            ]
            self.current_index = 0
            self._play_start = 0
            self._media_duration_local = 0
        try:
            info = self._ydl_play.extract_info(
                f"https://youtube.com/watch?v={vid}", download=False
            )
            url = info.get("url")
            dur = int(info.get("duration") or 0)
            title = info.get("title") or "Goodnight"
            if not url:
                return "could not resolve outro URL"
            cast.media_controller.play_media(url, "video/mp4")
            with self._lock:
                if self.playlist:
                    self.playlist[0]["duration"] = dur
                    self.playlist[0]["title"] = title
                    self.playlist[0]["play_url"] = url
                self._play_start = time.time()
                self._media_duration_local = dur
                self._outro_playing = True
                self.status = "ending"
            self.log.info("outro playing — %s (%ds)", title, dur)
            return None
        except Exception as e:
            return str(e)

    def _monitor_loop(self):
        prev_state = "UNKNOWN"
        prev_reason = None
        stuck_since = 0.0
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    status = self.status
                    outro = self._outro_playing
                    cast = self._cast
                    play_start = self._play_start
                    media_dur = self._media_duration_local
                past = self._past_stop_time()

                # Outro in progress: wait for finish, then full stop
                if outro or status == "ending":
                    if cast is None:
                        with self._lock:
                            self._do_stop()
                        prev_state = "UNKNOWN"
                        self._stop_event.wait(1)
                        continue
                    try:
                        mc = cast.media_controller
                        mc.update_status()
                        state = mc.status.player_state
                        idle_reason = mc.status.idle_reason
                    except Exception as e:
                        self.log.warning("outro monitor error: %s", e)
                        state, idle_reason = "UNKNOWN", None
                    finished = (
                        state == "IDLE"
                        and prev_state in ("PLAYING", "BUFFERING")
                        and idle_reason == "FINISHED"
                    )
                    elapsed = time.time() - play_start if play_start > 0 else 0
                    timed_out = media_dur > 0 and elapsed >= media_dur + 5
                    if finished or timed_out:
                        self.log.info("outro done, full stop")
                        with self._lock:
                            self._do_stop()
                        prev_state = "UNKNOWN"
                    else:
                        if state != prev_state or idle_reason != prev_reason:
                            self.log.debug("outro monitor: state=%s prev=%s reason=%s",
                                            state, prev_state, idle_reason)
                        prev_state = state
                        prev_reason = idle_reason
                    self._stop_event.wait(1)
                    continue

                # Auto-stop time: start outro (or hard stop if none)
                if past and status == "playing":
                    self.log.info("past stop_time → begin auto-stop")
                    self._begin_auto_stop()
                    prev_state = "UNKNOWN"
                    stuck_since = 0.0
                    self._stop_event.wait(1)
                    continue

                if cast is None:
                    self._stop_event.wait(2)
                    continue

                if status != "playing":
                    self._stop_event.wait(2)
                    continue

                mc = cast.media_controller
                mc.update_status()
                state = mc.status.player_state
                idle_reason = mc.status.idle_reason

                should_next = False
                with self._lock:
                    if self.status == "playing" and not self._outro_playing:
                        if (
                            state == "IDLE"
                            and prev_state in ("PLAYING", "BUFFERING")
                            and idle_reason == "FINISHED"
                        ):
                            should_next = True

                if should_next:
                    self.log.info("advance: state=%s prev=%s reason=%s",
                                   state, prev_state, idle_reason)
                    err = self._advance_and_play()
                    if err:
                        self.log.error("advance stopped: %s", err)
                    stuck_since = 0.0
                elif state != prev_state or idle_reason != prev_reason:
                    self.log.debug("monitor: state=%s prev=%s reason=%s",
                                    state, prev_state, idle_reason)

                prev_state = state
                prev_reason = idle_reason
            except Exception as e:
                self.log.exception("monitor error")
                if isinstance(e, pychromecast.error.NotConnected):
                    with self._lock:
                        self._do_stop()
                    prev_state = "UNKNOWN"

            self._stop_event.wait(2)

    def _do_stop(self):
        self._play_start = 0
        self._media_duration_local = 0
        self.current_index = -1
        self.playlist = []
        self.total_played_sec = 0.0
        self.screen_played_sec = 0.0
        self._outro_playing = False
        try:
            if self._cast:
                mc = self._cast.media_controller
                mc.stop()
                time.sleep(0.5)
                self._cast.quit_app()
                time.sleep(1)
                self._cast.disconnect()
        except Exception:
            pass
        self._cast = None
        self.status = "disconnected"
        self.device_name = ""

    def start(self):
        super().start()
        self._feed_stop_event.clear()
        self._yt_feed_thread = threading.Thread(target=self._yt_feed_loop, daemon=True)
        self._pc_feed_thread = threading.Thread(target=self._pc_feed_loop, daemon=True)
        self._yt_feed_thread.start()
        self._pc_feed_thread.start()
        self.log.info("ready (YT + podcast fetch in parallel)")

    def stop(self):
        self._stop_event.set()
        self._feed_stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._yt_feed_thread:
            self._yt_feed_thread.join(timeout=5)
        if self._pc_feed_thread:
            self._pc_feed_thread.join(timeout=5)
        self._do_stop()
        super().stop()
        self.log.info("stopped")

    def register_routes(self, app):
        from fastapi import Request

        @self.router.get("/status")
        async def status():
            with self._lock:
                if self.status in ("playing", "ending"):
                    playlist = [dict(x) for x in self.playlist]
                    curr_idx = self.current_index
                else:
                    # Live preview from latest feed cache (like TV/Podcast tabs)
                    playlist = [dict(x) for x in self._build_preview_playlist()]
                    curr_idx = -1
                current = None
                if 0 <= curr_idx < len(playlist):
                    current = dict(playlist[curr_idx])
                elapsed = (
                    time.time() - self._play_start
                    if self.status in ("playing", "ending") and self._play_start > 0
                    else 0
                )
                return {
                    "status": self.status,
                    "device_name": self.device_name,
                    "playlist": playlist,
                    "current_index": curr_idx,
                    "current": current,
                    "queue_count": len(playlist),
                    "channel_enabled": dict(self.channel_enabled),
                    "feeds": {
                        name: {
                            "url": info.get("url", ""),
                            "enabled": info.get("enabled", True),
                        }
                        for name, info in self.feeds.items()
                    },
                    "stop_time": self.stop_time.strftime("%H:%M"),
                    "past_stop_time": self._past_stop_time(),
                    "outro_video_url": self.outro_video_url,
                    "outro_playing": self._outro_playing,
                    "media_position": elapsed,
                    "media_duration": self._media_duration_local,
                    "screen_played_sec": self.screen_played_sec,
                    "total_played_sec": self.total_played_sec,
                    "params": {
                        "screen_minutes_per_hour": self.screen_minutes_per_hour,
                        "max_video_minutes": self.max_video_minutes,
                        "max_audio_minutes": self.max_audio_minutes,
                        "playlist_horizon_hours": self.playlist_horizon_hours,
                    },
                }

        @self.router.post("/connect")
        async def connect():
            import asyncio

            with self._lock:
                if self._cast:
                    return {"ok": True, "device": self.device_name}
            loop = asyncio.get_event_loop()
            cast = await loop.run_in_executor(None, self._discover)
            if cast is None:
                self.log.warning("/connect: no device found")
                return {"ok": False, "error": "no device found"}
            with self._lock:
                self._cast = cast
                self.device_name = cast.name
                self.status = "connected_idle"
                self.log.info("/connect → device=%s", cast.name)
            return {"ok": True, "device": cast.name}

        @self.router.post("/play")
        async def play():
            import asyncio

            if self._past_stop_time():
                self.log.info("/play rejected: past stop time")
                return {"ok": False, "error": "past stop time"}
            with self._lock:
                if self._cast is None:
                    self.log.info("/play rejected: not connected")
                    return {"ok": False, "error": "not connected"}
                if self.status == "playing":
                    self.log.info("/play rejected: already playing")
                    return {"ok": False, "error": "already playing"}
                self.total_played_sec = 0.0
                self.screen_played_sec = 0.0
                if not self._replan_locked():
                    self.log.warning("/play rejected: empty playlist")
                    return {"ok": False, "error": "playlist empty, try again later"}
                if self._thread is None or not self._thread.is_alive():
                    self._stop_event.clear()
                    self._thread = threading.Thread(
                        target=self._monitor_loop, daemon=True
                    )
                    self._thread.start()
            self.log.info("/play: starting (playlist=%d items)", len(self.playlist))
            loop = asyncio.get_event_loop()
            err = await loop.run_in_executor(None, self._play_current)
            if err:
                with self._lock:
                    self.current_index = -1
                    self.playlist = []
                    if self.status == "playing":
                        self.status = "connected_idle"
                self.log.error("/play failed: %s", err)
                return {"ok": False, "error": err}
            self.log.info("/play OK")
            return {"ok": True}

        @self.router.post("/stop")
        async def stop_route():
            import asyncio

            self.log.info("/stop")
            loop = asyncio.get_event_loop()

            def _locked_stop():
                with self._lock:
                    self._do_stop()

            await loop.run_in_executor(None, _locked_stop)
            return {"ok": True}

        @self.router.post("/skip")
        async def skip():
            import asyncio

            with self._lock:
                if self._outro_playing or self.status == "ending":
                    self.log.info("/skip → outro confirm, full stop")
                    self._do_stop()
                    return {"ok": True}
            if self._past_stop_time():
                self.log.info("/skip → past stop time, auto-stop")
                await asyncio.get_event_loop().run_in_executor(
                    None, self._begin_auto_stop
                )
                return {"ok": True}
            with self._lock:
                if self.status != "playing":
                    self.log.info("/skip rejected: not playing")
                    return {"ok": False, "error": "not playing"}
            self.log.info("/skip: advancing from idx=%d", self.current_index)
            loop = asyncio.get_event_loop()
            if self._cast:
                try:
                    await loop.run_in_executor(
                        None, lambda: self._cast.media_controller.stop()
                    )
                except Exception:
                    pass
            err = await loop.run_in_executor(None, self._advance_and_play)
            if err:
                return {"ok": False, "error": err}
            return {"ok": True}

        @self.router.post("/stop_time")
        async def stop_time_route(request: Request):
            data = await request.json()
            time_str = data.get("time", "")
            if time_str:
                with self._lock:
                    self.stop_time = parse_time(time_str)
                self._save_config()
                if self._past_stop_time():
                    with self._lock:
                        if self.status == "playing":
                            self._do_stop()
            return {"ok": True, "stop_time": self.stop_time.strftime("%H:%M")}

        @self.router.post("/settings")
        async def settings_route(request: Request):
            data = await request.json()
            with self._lock:
                if "screen_minutes_per_hour" in data:
                    v = float(data["screen_minutes_per_hour"])
                    self.screen_minutes_per_hour = max(1.0, min(60.0, v))
                if "max_video_minutes" in data:
                    v = int(data["max_video_minutes"])
                    self.max_video_minutes = max(1, min(180, v))
                if "max_audio_minutes" in data:
                    v = int(data["max_audio_minutes"])
                    self.max_audio_minutes = max(1, min(180, v))
                if "playlist_horizon_hours" in data:
                    v = float(data["playlist_horizon_hours"])
                    self.playlist_horizon_hours = max(0.5, min(24.0, v))
                if "outro_video_url" in data:
                    self.outro_video_url = str(data.get("outro_video_url") or "").strip()
            self._save_config()
            with self._lock:
                return {
                    "ok": True,
                    "params": {
                        "screen_minutes_per_hour": self.screen_minutes_per_hour,
                        "max_video_minutes": self.max_video_minutes,
                        "max_audio_minutes": self.max_audio_minutes,
                        "playlist_horizon_hours": self.playlist_horizon_hours,
                    },
                    "outro_video_url": self.outro_video_url,
                }

        @self.router.post("/toggle_channel")
        async def toggle_channel(request: Request):
            data = await request.json()
            handle = data.get("handle", "")
            enabled = data.get("enabled", True)
            with self._lock:
                if handle in self.channel_enabled:
                    self.channel_enabled[handle] = enabled
            self._save_config()
            return {"ok": True}

        @self.router.post("/add_channel")
        async def add_channel(request: Request):
            import asyncio

            data = await request.json()
            handle = data.get("handle", "").strip()
            if not handle:
                return {"ok": False, "error": "Enter a channel handle"}
            if not handle.startswith("@"):
                handle = "@" + handle
            with self._lock:
                if handle in self.channel_enabled:
                    return {"ok": False, "error": "Channel already added"}
            loop = asyncio.get_event_loop()
            cid = await loop.run_in_executor(None, _resolve_channel_id, handle)
            if cid is None:
                return {"ok": False, "error": "Channel not found — check spelling"}
            with self._lock:
                self.channel_enabled[handle] = True
                self._yt_cache[handle] = []
            self._save_config()
            return {"ok": True, "handle": handle}

        @self.router.post("/toggle_feed")
        async def toggle_feed(request: Request):
            data = await request.json()
            feed_name = data.get("feed_name", "")
            enabled = data.get("enabled", True)
            with self._lock:
                if feed_name in self.feeds:
                    self.feeds[feed_name]["enabled"] = enabled
            self._save_config()
            return {"ok": True}

        @self.router.post("/add_feed")
        async def add_feed(request: Request):
            import asyncio

            data = await request.json()
            url = (data.get("url") or "").strip()
            if not url:
                return {"ok": False, "error": "Enter a feed URL"}
            with self._lock:
                for name, info in self.feeds.items():
                    if info.get("url") == url:
                        return {"ok": False, "error": f'Feed already exists: "{name}"'}
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _validate_feed, url)
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error", "Invalid feed")}
            feed_title = result["title"]
            with self._lock:
                if feed_title in self.feeds:
                    return {
                        "ok": False,
                        "error": f'Feed "{feed_title}" already exists',
                    }
                self.feeds[feed_title] = {"url": url, "enabled": True}
                self._pc_cache[feed_title] = []
            self._save_config()
            return {"ok": True, "feed_name": feed_title}

        @self.router.get("/audio_proxy")
        async def audio_proxy(url: str, gain_db: float = 0.0):
            from fastapi.responses import StreamingResponse, JSONResponse

            proc = subprocess.Popen(
                [
                    "ffmpeg", "-i", url,
                    "-af", f"volume={gain_db}dB",
                    "-f", "mp3", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            def _proxy():
                try:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.kill()
                    proc.wait()

            # Check ffmpeg started successfully before streaming
            import select
            ready, _, _ = select.select([proc.stderr], [], [], 0.5)
            if ready:
                err = proc.stderr.read(1024)
                if b"Error" in err or b"error" in err:
                    proc.kill()
                    proc.wait()
                    self.log.error("audio_proxy ffmpeg error for url=%s gain_db=%.1f: %s",
                                   url[:80], gain_db, err[:200].decode("utf-8", errors="replace"))
                    return JSONResponse(
                        {"ok": False, "error": f"ffmpeg failed: {err[:200].decode('utf-8', errors='replace')}"},
                        status_code=500,
                    )

            return StreamingResponse(_proxy(), media_type="audio/mpeg")

        app.include_router(self.router)

    def ui_section(self) -> str:
        return """
        <div class="plugin-section" id="plugin-stream">
          <div class="yt-status-line" id="st-status">Disconnected</div>
          <div id="st-errmsg" style="display:none; padding:8px 12px; background:#2a1515; border:1px solid #ff4444; border-radius:8px; font-size:13px; color:#ff8888; margin-bottom:12px"></div>

          <div class="auto-stop-row">
            <span class="auto-stop-label">Auto-stop</span>
            <span class="auto-stop-value" id="st-stop-time-display" onclick="stEditStopTime()">--:--</span>
            <input type="time" id="st-stop-time-input" style="display:none" onchange="stSaveStopTime()">
          </div>

          <details class="collapsible" id="st-settings-section">
            <summary>Settings</summary>
            <div style="display:flex;flex-direction:column;gap:10px;padding:4px 0 8px">
              <div class="auto-stop-row" style="margin:0">
                <span class="auto-stop-label">Screen min/hour</span>
                <input type="number" id="st-param-screen" min="1" max="60" step="1"
                  style="width:72px;background:var(--card-hover);border:1px solid #333;border-radius:8px;color:var(--accent);font-size:18px;font-weight:500;padding:6px 8px;text-align:right"
                  onchange="stSaveSettings()">
              </div>
              <div class="auto-stop-row" style="margin:0">
                <span class="auto-stop-label">Max video (min)</span>
                <input type="number" id="st-param-max-video" min="1" max="180" step="1"
                  style="width:72px;background:var(--card-hover);border:1px solid #333;border-radius:8px;color:var(--accent);font-size:18px;font-weight:500;padding:6px 8px;text-align:right"
                  onchange="stSaveSettings()">
              </div>
              <div class="auto-stop-row" style="margin:0">
                <span class="auto-stop-label">Max audio (min)</span>
                <input type="number" id="st-param-max-audio" min="1" max="180" step="1"
                  style="width:72px;background:var(--card-hover);border:1px solid #333;border-radius:8px;color:var(--accent);font-size:18px;font-weight:500;padding:6px 8px;text-align:right"
                  onchange="stSaveSettings()">
              </div>
              <div class="auto-stop-row" style="margin:0">
                <span class="auto-stop-label">Horizon (hours)</span>
                <input type="number" id="st-param-horizon" min="0.5" max="24" step="0.5"
                  style="width:72px;background:var(--card-hover);border:1px solid #333;border-radius:8px;color:var(--accent);font-size:18px;font-weight:500;padding:6px 8px;text-align:right"
                  onchange="stSaveSettings()">
              </div>
              <div style="padding:4px 0 0">
                <div class="auto-stop-label" style="margin-bottom:6px">Outro video (at stop time)</div>
                <input type="text" id="st-param-outro" placeholder="https://youtube.com/watch?v=… or video id"
                  maxlength="200"
                  style="width:100%;box-sizing:border-box;background:var(--card-hover);border:1px solid #333;border-radius:8px;color:var(--text);font-size:13px;padding:10px 12px"
                  onchange="stSaveSettings()">
              </div>
            </div>
          </details>

          <details class="collapsible" id="st-channels-section">
            <summary>Channels</summary>
            <div class="series-pills" id="st-channel-pills"></div>
            <div class="yt-add-channel-row">
              <input type="text" id="st-add-channel-input" placeholder="@channelname" maxlength="100" onkeydown="if(event.key==='Enter')stAddChannel()">
              <button onclick="stAddChannel()">+ Add</button>
            </div>
          </details>

          <details class="collapsible" id="st-feeds-section">
            <summary>Podcasts</summary>
            <div class="series-pills" id="st-feed-pills"></div>
            <div class="yt-add-channel-row">
              <input type="text" id="st-add-feed-input" placeholder="https://example.com/feed.xml" maxlength="500" onkeydown="if(event.key==='Enter')stAddFeed()">
              <button onclick="stAddFeed()">+ Add</button>
            </div>
          </details>

          <div class="transport" id="st-controls" style="gap:12px">
            <button class="transport-btn transport-play" onclick="stPlay()" id="st-play-btn">&#x25B6;</button>
            <button class="transport-btn transport-play stop-btn" onclick="stStop()" id="st-stop-btn" style="display:none">&#x23F9;</button>
            <button class="transport-btn" onclick="stSkip()" id="st-skip-btn" style="display:none">&#x23ED;</button>
          </div>

          <div class="series-picker" id="st-queue-section" style="display:none">
            <div class="series-label" style="display:flex; justify-content:space-between; align-items:center">
              <span>Playlist</span>
              <span id="st-queue-count" style="font-size:12px; color:var(--text-dim)">0</span>
            </div>
            <div id="st-queue-list" style="max-height:360px; overflow-y:auto; margin-top:8px"></div>
          </div>
        </div>
        """

    def ui_js(self) -> str:
        return """
        let stState = {};

        function stShowError(msg) {
          const el = document.getElementById('st-errmsg');
          el.textContent = msg;
          el.style.display = '';
          setTimeout(function(){ el.style.display = 'none'; }, 10000);
        }
        function stHideError() {
          document.getElementById('st-errmsg').style.display = 'none';
        }
        async function stFetch(url, body) {
          try {
            const opts = { method: 'POST' };
            if (body) { opts.body = JSON.stringify(body); opts.headers = { 'Content-Type': 'application/json' }; }
            const ctrl = new AbortController();
            var t = setTimeout(function() { ctrl.abort(); }, 60000);
            opts.signal = ctrl.signal;
            const r = await fetch(url, opts);
            clearTimeout(t);
            return await r.json();
          } catch(e) { console.error(e); return null; }
        }
        function stEsc(s) { return (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
        function stFormatDate(ts) {
          if (!ts) return '';
          var d = new Date(ts * 1000);
          var m = d.getMonth() + 1, day = d.getDate();
          return (m < 10 ? '0' : '') + m + '/' + (day < 10 ? '0' : '') + day;
        }
        function stFmtDur(sec) {
          sec = Math.floor(sec || 0);
          var m = Math.floor(sec / 60), s = sec % 60;
          return m + ':' + (s < 10 ? '0' : '') + s;
        }
        function stApplyScroll(row, text) {
          if (!row) return;
          var el = row.querySelector('.yt-qi-title');
          if (!el) return;
          // Measure after layout (innerHTML rebuild can report 0 width too early)
          requestAnimationFrame(function() {
            var titleEl = row.querySelector('.yt-qi-title');
            if (!titleEl) return;
            if (titleEl.querySelector('.scroll-wrap')) return;
            if (titleEl.scrollWidth <= titleEl.clientWidth) return;
            var e2 = stEsc(text);
            titleEl.innerHTML = '<span class="scroll-wrap" style="--dur:' + Math.max(6, text.length / 6) + 's"><span>' + e2 + '&nbsp;&nbsp;&nbsp;</span><span aria-hidden="true">' + e2 + '&nbsp;&nbsp;&nbsp;</span></span>';
          });
        }

        async function stConnect() {
          stHideError();
          const r = await stFetch('/api/stream/connect');
          if (!r || !r.ok) stShowError(r ? r.error : 'Connect failed');
        }
        async function stPlay() {
          stHideError();
          const btn = document.getElementById('st-play-btn');
          const origHTML = btn.innerHTML;
          btn.disabled = true;
          btn.innerHTML = 'Starting\u2026';
          var statusEl = document.getElementById('st-status');
          if (stState.status === 'disconnected') {
            statusEl.textContent = 'Connecting\u2026';
            const r = await stFetch('/api/stream/connect');
            if (!r || !r.ok) {
              btn.disabled = false; btn.innerHTML = origHTML;
              statusEl.textContent = 'Disconnected';
              stShowError(r ? r.error : 'Connect failed');
              return;
            }
          }
          statusEl.textContent = 'Starting Stream\u2026';
          const r2 = await stFetch('/api/stream/play');
          btn.disabled = false; btn.innerHTML = origHTML;
          if (!r2 || !r2.ok) stShowError(r2 ? r2.error : 'Play failed');
        }
        async function stStop() {
          await stFetch('/api/stream/stop');
        }
        async function stSkip() {
          const r = await stFetch('/api/stream/skip');
          if (r && !r.ok && r.error) stShowError(r.error);
        }
        function stEditStopTime() {
          var input = document.getElementById('st-stop-time-input');
          if (input.showPicker) input.showPicker(); else input.click();
        }
        async function stSaveStopTime() {
          var val = document.getElementById('st-stop-time-input').value;
          if (val) {
            document.getElementById('st-stop-time-display').textContent = val;
            await stFetch('/api/stream/stop_time', { time: val });
          }
        }
        var stParamsFp = '';
        function stFillParams(s) {
          var p = s.params;
          if (!p) return;
          var outro = s.outro_video_url || '';
          var fp = [p.screen_minutes_per_hour, p.max_video_minutes, p.max_audio_minutes, p.playlist_horizon_hours, outro].join('|');
          if (fp === stParamsFp) return;
          // Don't overwrite while user is editing an input
          var ae = document.activeElement;
          if (ae && ae.id && ae.id.indexOf('st-param-') === 0) return;
          stParamsFp = fp;
          document.getElementById('st-param-screen').value = p.screen_minutes_per_hour;
          document.getElementById('st-param-max-video').value = p.max_video_minutes;
          document.getElementById('st-param-max-audio').value = p.max_audio_minutes;
          document.getElementById('st-param-horizon').value = p.playlist_horizon_hours;
          document.getElementById('st-param-outro').value = outro;
        }
        async function stSaveSettings() {
          stHideError();
          var body = {
            screen_minutes_per_hour: parseFloat(document.getElementById('st-param-screen').value),
            max_video_minutes: parseInt(document.getElementById('st-param-max-video').value, 10),
            max_audio_minutes: parseInt(document.getElementById('st-param-max-audio').value, 10),
            playlist_horizon_hours: parseFloat(document.getElementById('st-param-horizon').value),
            outro_video_url: (document.getElementById('st-param-outro').value || '').trim()
          };
          if (isNaN(body.screen_minutes_per_hour) || isNaN(body.max_video_minutes) ||
              isNaN(body.max_audio_minutes) || isNaN(body.playlist_horizon_hours)) {
            stShowError('Invalid settings');
            return;
          }
          const r = await stFetch('/api/stream/settings', body);
          if (!r || !r.ok) {
            stShowError(r ? (r.error || 'Settings failed') : 'Settings failed');
            return;
          }
          stParamsFp = '';
          stFillParams(r);
        }
        async function stToggleChannel(handle, enabled) {
          await stFetch('/api/stream/toggle_channel', { handle: handle, enabled: enabled });
        }
        async function stAddChannel() {
          const input = document.getElementById('st-add-channel-input');
          const handle = (input.value || '').trim();
          if (!handle) return;
          const r = await stFetch('/api/stream/add_channel', { handle: handle });
          if (r && r.ok) input.value = '';
          else stShowError(r ? r.error : 'Failed to add channel');
        }
        async function stToggleFeed(feedName, enabled) {
          await stFetch('/api/stream/toggle_feed', { feed_name: feedName, enabled: enabled });
        }
        async function stAddFeed() {
          const input = document.getElementById('st-add-feed-input');
          const url = (input.value || '').trim();
          if (!url) return;
          const r = await stFetch('/api/stream/add_feed', { url: url });
          if (r && r.ok) input.value = '';
          else stShowError(r ? r.error : 'Failed to add feed');
        }

        var stQueueFingerprint = '';
        var stPrevIndex = -1;
        function stRenderQueue(s) {
          const container = document.getElementById('st-queue-list');
          const section = document.getElementById('st-queue-section');
          const idx = s.current_index;
          const isEmpty = !s.playlist || !s.playlist.length;
          if (isEmpty) {
            stQueueFingerprint = '';
            stPrevIndex = -1;
            section.style.display = 'none';
            return;
          }
          section.style.display = '';
          // Fingerprint ids only — duration may update after YT resolve and must not rebuild (kills marquee)
          var fp = JSON.stringify(s.playlist.map(function(v) { return v.id; }));
          var sameData = fp === stQueueFingerprint;
          var sameIdx = idx === stPrevIndex;
          if (sameData && sameIdx) {
            // Try scroll again — section may have been hidden on first render
            var cr = idx >= 0 && idx < container.children.length ? container.children[idx] : null;
            if (cr && s.playlist[idx]) stApplyScroll(cr, s.playlist[idx].title || '');
          } else if (sameData) {
            var oldIdx = stPrevIndex;
            stPrevIndex = idx;
            var oldRow = oldIdx >= 0 && oldIdx < container.children.length ? container.children[oldIdx] : null;
            if (oldRow) {
              oldRow.className = 'yt-queue-item';
              var oldTitleEl = oldRow.querySelector('.yt-qi-title');
              if (oldTitleEl && s.playlist[oldIdx]) oldTitleEl.textContent = s.playlist[oldIdx].title || '';
            }
            var newRow = idx >= 0 && idx < container.children.length ? container.children[idx] : null;
            if (newRow) {
              var act = (s.status === 'playing' || s.status === 'ending');
              newRow.className = 'yt-queue-item' + (act ? ' playing' : '') + ' current';
              var titleEl = newRow.querySelector('.yt-qi-title');
              if (titleEl && s.playlist[idx]) titleEl.textContent = s.playlist[idx].title || '';
              stApplyScroll(newRow, s.playlist[idx].title || '');
              newRow.scrollIntoView({ block: 'nearest' });
            }
          } else {
            stQueueFingerprint = fp;
            stPrevIndex = idx;
            let html = '';
            for (let i = 0; i < s.playlist.length; i++) {
              const v = s.playlist[i];
              const isCurrent = i === idx;
              const playing = isCurrent && (s.status === 'playing' || s.status === 'ending');
              const cls = 'yt-queue-item' + (playing ? ' playing' : '') + (isCurrent ? ' current' : '');
              const kind = v.kind === 'video' ? 'V' : 'A';
              const dur = v.duration ? stFmtDur(v.duration) : '';
              const title = v.title || '', escaped2 = stEsc(title);
              html += '<div class="' + cls + '">' +
                '<span class="yt-qi-indicator">' + kind + '</span>' +
                '<span class="yt-qi-date">' + (v.published ? stFormatDate(v.published) : '') + '</span>' +
                '<span class="yt-qi-handle">' + stEsc((v.source || '').replace('@', '')).substring(0, 14) + '</span>' +
                '<span class="yt-qi-title">' + escaped2 + '</span>' +
                '<span class="yt-qi-dur">' + dur + '</span>' +
                '</div>';
            }
            container.innerHTML = html;
            if (idx >= 0 && idx < s.playlist.length) {
              var el = container.children[idx];
              if (el) {
                el.scrollIntoView({ block: 'nearest' });
                stApplyScroll(el, s.playlist[idx].title || '');
              }
            }
          }
          // Progress: update in place like TV/Podcast (do not rebuild title)
          var oldBars = container.querySelectorAll('.yt-queue-item:not(.playing) .qi-progress');
          oldBars.forEach(function(el){ el.remove(); });
          if ((s.status === 'playing' || s.status === 'ending') && s.media_duration > 0 && idx >= 0) {
            var row = container.querySelector('.yt-queue-item.playing') || container.children[idx];
            if (row) {
              var pct = Math.min(100, Math.max(0, (s.media_position || 0) / s.media_duration * 100));
              var fill = row.querySelector('.qi-progress-fill');
              if (fill) {
                fill.style.width = pct + '%';
              } else {
                var bar = document.createElement('div');
                bar.className = 'qi-progress';
                bar.innerHTML = '<div class="qi-progress-fill" style="width:' + pct + '%"></div>';
                row.appendChild(bar);
              }
            }
          }
          var totalSec = 0;
          (s.playlist || []).forEach(function(v){ totalSec += (v.duration || 0); });
          document.getElementById('st-queue-count').textContent =
            (s.playlist || []).length + ' · ' + stFmtDur(totalSec);
        }

        function stRenderChannels(s) {
          const container = document.getElementById('st-channel-pills');
          if (!s.channel_enabled) return;
          const handles = Object.keys(s.channel_enabled);
          container.innerHTML = handles.map(function(h) {
            const checked = s.channel_enabled[h];
            const label = h.replace('@', '');
            return '<label class="yt-ch-label' + (checked ? ' active' : '') + '" ' +
              'onclick="stToggleChannel(\\'' + h + '\\', ' + (!checked) + ')">' +
              stEsc(label) + '</label>';
          }).join('');
        }
        function stRenderFeeds(s) {
          const container = document.getElementById('st-feed-pills');
          if (!s.feeds) return;
          const names = Object.keys(s.feeds);
          container.innerHTML = names.map(function(n) {
            const checked = s.feeds[n].enabled;
            return '<label class="yt-ch-label' + (checked ? ' active' : '') + '" ' +
              'onclick="stToggleFeed(decodeURIComponent(\\'' + encodeURIComponent(n) + '\\'), ' + (!checked) + ')">' +
              stEsc(n).substring(0, 18) + '</label>';
          }).join('');
        }

        document.getElementById('st-queue-list').addEventListener('click', function(e) {
          var row = e.target.closest('.yt-queue-item');
          if (!row) return;
          var idx = Array.prototype.indexOf.call(row.parentNode.children, row);
          if (row.classList.contains('current')) return;
          var isSelected = row.classList.toggle('selected');
          if (isSelected) {
            for (var i = 0; i < row.parentNode.children.length; i++) {
              var child = row.parentNode.children[i];
              if (child !== row && child.classList.contains('selected')) {
                child.classList.remove('selected');
                if (stState.playlist && stState.playlist[i]) {
                  var pt = child.querySelector('.yt-qi-title');
                  if (pt) pt.textContent = stState.playlist[i].title || '';
                }
              }
            }
            if (stState.playlist && stState.playlist[idx])
              stApplyScroll(row, stState.playlist[idx].title || '');
          } else {
            if (stState.playlist && stState.playlist[idx]) {
              var titleEl = row.querySelector('.yt-qi-title');
              if (titleEl) titleEl.textContent = stState.playlist[idx].title || '';
            }
          }
        });

        async function stPoll() {
          try {
            const r = await fetch('/api/stream/status');
            const s = await r.json();
            stState = s;
            var statusEl = document.getElementById('st-status');
            if (s.status === 'disconnected') {
              statusEl.textContent = 'Disconnected';
            } else if (s.status === 'connected_idle') {
              statusEl.textContent = 'Connected: ' + (s.device_name || '');
            } else if (s.status === 'ending') {
              statusEl.textContent = 'Ending 🎵 ' + ((s.current && s.current.title) || 'Goodnight');
            } else if (s.status === 'retrying') {
              statusEl.textContent = 'Retrying\u2026';
            } else if (s.status === 'playing' && s.current) {
              var k = s.current.kind === 'video' ? '▶' : '🎧';
              statusEl.textContent = k + ' ' + (s.current.title || '');
            } else {
              statusEl.textContent = s.status;
            }
            if (s.past_stop_time && s.status !== 'ending') {
              statusEl.textContent += ' · past stop time';
            }
            document.getElementById('st-stop-time-display').textContent = s.stop_time || '--:--';
            document.getElementById('st-stop-time-input').value = s.stop_time || '';
            var active = (s.status === 'playing' || s.status === 'ending');
            document.getElementById('st-play-btn').style.display = active ? 'none' : '';
            document.getElementById('st-stop-btn').style.display = active ? '' : 'none';
            document.getElementById('st-skip-btn').style.display = active ? '' : 'none';
            stFillParams(s);
            stRenderQueue(s);
            stRenderChannels(s);
            stRenderFeeds(s);
          } catch(e) { console.error(e); }
        }
        setInterval(stPoll, 1500);
        stPoll();
        """
