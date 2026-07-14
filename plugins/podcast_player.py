import json
import logging
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import pychromecast
from pychromecast.discovery import CastBrowser, SimpleCastListener
import requests

from plugins import Plugin


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
    audio_count = 0
    sample_url = ""
    for item in items:
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        etype = (enclosure.get("type") or "").lower()
        eurl = (enclosure.get("url") or "").strip()
        if not eurl:
            continue
        if etype.startswith("audio/") or etype in ("", "application/octet-stream"):
            if etype.startswith("audio/"):
                audio_count += 1
            if not sample_url:
                sample_url = eurl
    if sample_url:
        ok = False
        try:
            hr = requests.head(sample_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            ok = 200 <= hr.status_code < 400
        except requests.RequestException:
            pass
        if not ok:
            try:
                rg = requests.get(sample_url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"}, timeout=10)
                ok = rg.status_code in (200, 206)
            except requests.RequestException as e:
                return {"ok": False, "error": f"Audio file unreachable: {e}"}
        if not ok:
            try:
                rf = requests.get(sample_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, stream=True)
                if rf.status_code == 200:
                    head = rf.raw.read(20)
                    ok = head[:3] in (b"ID3", b"RIFF") or b"ftyp" in head[:12] or "audio" in (rf.headers.get("content-type") or "")
            except requests.RequestException:
                pass
        if not ok:
            return {"ok": False, "error": "Audio file not accessible"}
    else:
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
        logging.getLogger("plugin.podcast_player").warning("loudness measurement failed: %s", e)
        return 0.0


def _fetch_feed(feed_name: str, feed_url: str) -> list[dict]:
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
        dur_raw = item.findtext("duration", "") or item.findtext("itunes:duration", "", ITUNES_NS)
        dur = _parse_duration(dur_raw) if dur_raw else 0
        link = item.findtext("link", "")
        result.append({
            "title": title,
            "url": mp3_url,
            "published": pub_ts,
            "duration": dur,
            "feed_name": feed_name,
            "feed_url": feed_url,
            "link": link,
        })
    return result


class PodcastPlugin(Plugin):
    name = "podcast_player"
    title = "Podcasts"
    icon = "🎙️"
    order = 3

    def __init__(self, controller, config: dict):
        super().__init__(controller, config)

        self.status = "disconnected"
        self.device_name = ""
        self.queue: list[dict] = []
        self.current_index = -1
        self.feed_interval: int = config.get("feed_interval", 60)
        self.feeds: dict[str, dict] = dict(config.get("feeds", {}))

        self._cast: pychromecast.Chromecast | None = None
        self._browser: pychromecast.discovery.CastBrowser | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._feed_cache: dict[str, list[dict]] = {}
        self._gain_cache: dict[str, float] = {}
        self._feed_thread: threading.Thread | None = None
        self._feed_stop_event = threading.Event()
        self._play_start: float = 0
        self._media_duration_local: int = 0

    def _discover(self) -> pychromecast.Chromecast | None:
        try:
            zc = getattr(self.controller, "zc", None)
            if zc is None:
                return None
            if self._browser is None:
                self._browser = CastBrowser(SimpleCastListener(), zc)
                self._browser.start_discovery()
                time.sleep(3)
            if not self._browser.devices:
                return None
            device = list(self._browser.devices.values())[0]
            cc = pychromecast.get_chromecast_from_cast_info(device, zc)
            cc.wait(timeout=10)
            return cc
        except Exception as e:
            self.log.warning("discovery error: %s", e)
        return None

    def _play_next(self) -> str | None:
        max_attempts = 10
        for _ in range(max_attempts):
            with self._lock:
                if self.current_index < 0 or self.current_index >= len(self.queue):
                    self.status = "connected_idle"
                    return "queue exhausted"
                episode = self.queue[self.current_index]
                title = episode["title"]
                mp3_url = episode["url"]
                gain_db = episode.get("gain_db", 0.0)
            if gain_db != 0.0:
                server = self.controller.config.get("server", {})
                host = server.get("host", "0.0.0.0")
                port = server.get("port", 8080)
                if host and host != "0.0.0.0":
                    mp3_url = (
                        f"http://{host}:{port}/api/podcast_player/audio_proxy"
                        f"?url={quote(mp3_url)}&gain_db={gain_db}"
                    )
            try:
                mc = self._cast.media_controller
                mc.play_media(mp3_url, "audio/mpeg")
                self._play_start = time.time()
                self._media_duration_local = episode.get("duration", 0)
                self.log.info("playing [%d/%d] %s", self.current_index + 1, len(self.queue), title)
                return None
            except Exception as e:
                self.log.warning("skipping unavailable %s: %s", title, e)
                with self._lock:
                    self.current_index += 1
        with self._lock:
            self.status = "connected_idle"
        return "all episodes in queue are unavailable"

    def _build_preview(self) -> list[dict]:
        items = []
        for name, info in self.feeds.items():
            if info.get("enabled", True):
                items.extend(self._feed_cache.get(name, []))
        items.sort(key=lambda v: v["published"], reverse=True)
        return items[:30]

    def _feed_fetch_loop(self):
        first_pass = True
        while not self._feed_stop_event.is_set():
            with self._lock:
                feed_list = list(self.feeds.keys())
            for feed_name in feed_list:
                if self._feed_stop_event.is_set():
                    return
                with self._lock:
                    info = self.feeds.get(feed_name)
                if info is None:
                    continue
                if not info.get("enabled", True):
                    continue
                try:
                    episodes = _fetch_feed(feed_name, info["url"])
                    for ep in episodes:
                        if ep.get("duration", 0) >= 60:
                            if ep["url"] not in self._gain_cache:
                                self._gain_cache[ep["url"]] = _measure_loudness(ep["url"])
                            ep["gain_db"] = self._gain_cache[ep["url"]]
                        else:
                            ep["gain_db"] = 0.0
                    with self._lock:
                        self._feed_cache[feed_name] = episodes
                except Exception as e:
                    self.log.warning("feed: failed to fetch %s: %s", feed_name, e)
                delay = 0.5 if first_pass else self.feed_interval
                self._feed_stop_event.wait(delay)
            first_pass = False

    def _save_config(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        try:
            import yaml
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            if "plugins" not in cfg:
                cfg["plugins"] = {}
            cfg["plugins"]["podcast_player"] = {
                "feeds": {name: {"url": info["url"], "enabled": info["enabled"]} for name, info in self.feeds.items()},
            }
            with open(path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log.error("failed to save config: %s", e)

    def _monitor_loop(self):
        prev_state = "UNKNOWN"
        prev_reason = None

        while not self._stop_event.is_set():
            try:
                cast = self._cast
                if cast is None:
                    self._stop_event.wait(2)
                    continue

                mc = cast.media_controller
                mc.update_status()
                state = mc.status.player_state
                idle_reason = mc.status.idle_reason

                should_play_next = False
                with self._lock:
                    if self.status == "playing":
                        if state == "IDLE" and prev_state in ("PLAYING", "BUFFERING") and idle_reason == "FINISHED":
                            next_idx = self.current_index + 1
                            if next_idx < len(self.queue):
                                self.current_index = next_idx
                                should_play_next = True
                                self.log.info("advance: state=%s prev=%s reason=%s",
                                               state, prev_state, idle_reason)
                            else:
                                self.log.info("queue exhausted")
                                self._do_stop()

                if should_play_next:
                    self._play_next()
                elif state != prev_state or idle_reason != prev_reason:
                    self.log.debug("monitor: state=%s prev=%s reason=%s",
                                    state, prev_state, idle_reason)

                prev_state = state
                prev_reason = idle_reason
            except Exception as e:
                self.log.exception("monitor error")

            self._stop_event.wait(2)

    def _do_stop(self):
        self.current_index = -1
        self.queue = []
        self._play_start = 0
        self._media_duration_local = 0
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

    def _do_disconnect(self):
        self._do_stop()

    def start(self):
        super().start()
        self._feed_stop_event.clear()
        self._feed_thread = threading.Thread(target=self._feed_fetch_loop, daemon=True)
        self._feed_thread.start()
        self.log.info("ready")

    def stop(self):
        self._stop_event.set()
        self._feed_stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._feed_thread:
            self._feed_thread.join(timeout=5)
        self._do_disconnect()
        super().stop()
        self.log.info("stopped")

    def register_routes(self, app):
        from fastapi import Request

        @self.router.get("/status")
        async def status():
            with self._lock:
                current = None
                if self.status == "playing":
                    queue = self.queue
                    curr_idx = self.current_index
                else:
                    queue = self._build_preview()
                    curr_idx = -1
                if curr_idx >= 0 and curr_idx < len(queue):
                    current = dict(queue[curr_idx])
                src_queue = self.queue if self.status == "playing" else queue
                queue_out = [dict(v) for v in src_queue]
                elapsed = time.time() - self._play_start if self.status == "playing" and self._play_start > 0 else 0
                return {
                    "status": self.status,
                    "device_name": self.device_name,
                    "queue": queue_out,
                    "current_index": curr_idx,
                    "current": current,
                    "queue_count": len(queue_out),
                    "feeds": {name: dict(info) for name, info in self.feeds.items()},
                    "media_position": elapsed,
                    "media_duration": self._media_duration_local,
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
                return {"ok": False, "error": "no device found"}
            with self._lock:
                self._cast = cast
                self.device_name = cast.name
                self.status = "connected_idle"
            return {"ok": True, "device": cast.name}

        @self.router.post("/disconnect")
        async def disconnect():
            with self._lock:
                self._do_disconnect()
            return {"ok": True}

        @self.router.post("/refresh")
        async def refresh():
            import asyncio
            loop = asyncio.get_event_loop()

            def _do_refresh():
                for feed_name, info in self.feeds.items():
                    try:
                        episodes = _fetch_feed(feed_name, info["url"])
                        for ep in episodes:
                            if ep.get("duration", 0) >= 60:
                                if ep["url"] not in self._gain_cache:
                                    self._gain_cache[ep["url"]] = _measure_loudness(ep["url"])
                                ep["gain_db"] = self._gain_cache[ep["url"]]
                            else:
                                ep["gain_db"] = 0.0
                        with self._lock:
                            self._feed_cache[feed_name] = episodes
                    except Exception as e:
                        self.log.warning("refresh failed for %s: %s", feed_name, e)

            await loop.run_in_executor(None, _do_refresh)
            with self._lock:
                preview = self._build_preview()
            return {"ok": True, "queue_count": len(preview)}

        @self.router.post("/play")
        async def play():
            import asyncio
            with self._lock:
                if self._cast is None:
                    return {"ok": False, "error": "not connected"}
                if self.status == "playing":
                    return {"ok": False, "error": "already playing"}
                queue = self._build_preview()
                if not queue:
                    return {"ok": False, "error": "queue empty, try again later"}
                self.queue = queue
                self.current_index = 0
                if self._thread is None or not self._thread.is_alive():
                    self._stop_event.clear()
                    self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
                    self._thread.start()
            loop = asyncio.get_event_loop()
            err = await loop.run_in_executor(None, self._play_next)
            if err:
                with self._lock:
                    self.current_index = -1
                    self.queue = []
                return {"ok": False, "error": err}
            with self._lock:
                self.status = "playing"
            return {"ok": True, "episode": self.queue[0] if self.queue else None}

        @self.router.post("/stop")
        async def stop_route():
            import asyncio
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
                if self.status != "playing":
                    return {"ok": False, "error": "not playing"}
                next_idx = self.current_index + 1
                if next_idx >= len(self.queue):
                    self._do_stop()
                    return {"ok": False, "error": "no more episodes"}
                self.current_index = next_idx
            loop = asyncio.get_event_loop()
            err = await loop.run_in_executor(None, self._play_next)
            if err:
                with self._lock:
                    self.current_index = -1
                    self.status = "connected_idle"
                return {"ok": False, "error": err}
            return {"ok": True}

        @self.router.post("/seek")
        async def seek(request: Request):
            import asyncio
            data = await request.json()
            index = int(data.get("index", 0))
            loop = asyncio.get_event_loop()
            with self._lock:
                queue = self.queue if self.status == "playing" else self._build_preview()
                if index < 0 or index >= len(queue):
                    return {"ok": False, "error": "out of range"}
                if self.status == "playing" and self._cast:
                    await loop.run_in_executor(None, lambda: self._cast.media_controller.stop())
                self.current_index = index
                self.queue = list(queue)
                if self._thread is None or not self._thread.is_alive():
                    self._stop_event.clear()
                    self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
                    self._thread.start()
            err = await loop.run_in_executor(None, self._play_next)
            if err:
                with self._lock:
                    self.current_index = -1
                    self.queue = []
                    self.status = "connected_idle"
                return {"ok": False, "error": err}
            with self._lock:
                self.status = "playing"
            return {"ok": True}

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
            data = await request.json()
            url = data.get("url", "").strip()
            if not url:
                return {"ok": False, "error": "Enter a feed URL"}
            if not url.startswith("http://") and not url.startswith("https://"):
                return {"ok": False, "error": "URL must start with http:// or https://"}
            with self._lock:
                for name, info in self.feeds.items():
                    if info["url"] == url:
                        return {"ok": False, "error": f"Feed already added as \"{name}\""}

            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _validate_feed, url)
            if not result["ok"]:
                return {"ok": False, "error": result["error"]}
            feed_title = result["title"]

            with self._lock:
                if feed_title in self.feeds:
                    return {"ok": False, "error": f"Feed \"{feed_title}\" already exists"}
                self.feeds[feed_title] = {"url": url, "enabled": True}
                self._feed_cache[feed_title] = []
            self._save_config()
            return {"ok": True, "feed_name": feed_title}

        @self.router.get("/audio_proxy")
        async def audio_proxy(url: str, gain_db: float = 0.0):
            from fastapi.responses import StreamingResponse

            def _proxy():
                proc = subprocess.Popen(
                    [
                        "ffmpeg", "-i", url,
                        "-af", f"volume={gain_db}dB",
                        "-f", "mp3", "-",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.kill()
                    proc.wait()

            return StreamingResponse(_proxy(), media_type="audio/mpeg")

        app.include_router(self.router)

    def ui_section(self) -> str:
        return """
        <div class="plugin-section" id="plugin-podcast_player">
          <div class="yt-status-line" id="pc-status">Disconnected</div>
          <div id="pc-errmsg" style="display:none; padding:8px 12px; background:#2a1515; border:1px solid #ff4444; border-radius:8px; font-size:13px; color:#ff8888; margin-bottom:12px"></div>

          <details class="collapsible" id="pc-feeds-section">
            <summary>Feeds</summary>
            <div class="series-pills" id="pc-feed-pills"></div>
            <div class="yt-add-channel-row">
              <input type="text" id="pc-add-input" placeholder="https://example.com/feed.xml" maxlength="500" onkeydown="if(event.key==='Enter')pcAddFeed()">
              <button onclick="pcAddFeed()">+ Add</button>
            </div>
          </details>

          <div class="transport" id="pc-controls">
            <button class="transport-btn transport-play" onclick="pcPlay()" id="pc-play-btn">&#x25B6;</button>
            <button class="transport-btn transport-play stop-btn" onclick="pcStop()" id="pc-stop-btn" style="display:none">&#x23F9;</button>
            <button class="transport-btn" onclick="pcSkip()" id="pc-skip-btn" style="display:none">&#x23ED;</button>
          </div>

          <div class="series-picker" id="pc-queue-section" style="display:none">
            <div class="series-label" style="display:flex; justify-content:space-between; align-items:center">
              <span>Queue</span>
              <span id="pc-queue-count" style="font-size:12px; color:var(--text-dim)">0 episodes</span>
            </div>
            <div id="pc-queue-list" style="max-height:320px; overflow-y:auto; margin-top:8px"></div>
          </div>
        </div>
        """

    def ui_js(self) -> str:
        return """
        let pcState = {};

        function pcShowError(msg) {
          const el = document.getElementById('pc-errmsg');
          el.textContent = msg;
          el.style.display = '';
          setTimeout(function(){ el.style.display = 'none'; }, 10000);
        }

        function pcHideError() {
          document.getElementById('pc-errmsg').style.display = 'none';
        }

        async function pcFetch(url, body) {
          try {
            const ctrl = new AbortController();
            var t = setTimeout(function() { ctrl.abort(); }, 30000);
            const opts = { method: 'POST', signal: ctrl.signal };
            if (body) { opts.body = JSON.stringify(body); opts.headers = { 'Content-Type': 'application/json' }; }
            const r = await fetch(url, opts);
            clearTimeout(t);
            return await r.json();
          } catch(e) { console.error(e); return null; }
        }

        async function pcPlay() {
          pcHideError();
          const btn = document.getElementById('pc-play-btn');
          const origHTML = btn.innerHTML;
          btn.disabled = true;
          btn.innerHTML = 'Starting\u2026';

          document.getElementById('pc-status').textContent = 'Connecting\u2026';
          const r = await pcFetch('/api/podcast_player/connect');
          if (!r || !r.ok) {
            btn.disabled = false; btn.innerHTML = origHTML;
            document.getElementById('pc-status').textContent = 'Disconnected';
            pcShowError(r ? r.error : 'Connection failed');
            return;
          }

          document.getElementById('pc-status').textContent = 'Starting\u2026';
          const r3 = await pcFetch('/api/podcast_player/play');
          btn.disabled = false; btn.innerHTML = origHTML;
          pcPoll();
          if (!r3 || !r3.ok) { pcShowError(r3 ? r3.error : 'Play failed'); }
        }

        async function pcStop() {
          pcHideError();
          await pcFetch('/api/podcast_player/stop');
          pcPoll();
        }

        async function pcSkip() {
          pcHideError();
          const r = await pcFetch('/api/podcast_player/skip');
          pcPoll();
          if (!r || !r.ok) { pcShowError(r ? r.error : 'Skip failed'); }
        }

        async function pcToggleFeed(feedName, enabled) {
          pcHideError();
          const r = await pcFetch('/api/podcast_player/toggle_feed', { feed_name: feedName, enabled: enabled });
          pcPoll();
          if (!r || !r.ok) { pcShowError(r ? r.error : 'Toggle failed'); }
        }

        async function pcAddFeed() {
          pcHideError();
          const input = document.getElementById('pc-add-input');
          const btn = document.querySelector('#pc-feeds-section .yt-add-channel-row button');
          const url = input.value.trim();
          if (!url) return;
          btn.disabled = true;
          btn.textContent = 'Adding\u2026';
          const r = await pcFetch('/api/podcast_player/add_feed', { url: url });
          btn.disabled = false;
          btn.textContent = '+ Add';
          if (r && r.ok) {
            input.value = '';
            pcPoll();
          } else {
            pcShowError(r ? r.error : 'Failed to add feed');
          }
        }

        function pcFormatDate(ts) {
          var d = new Date(ts * 1000);
          return (d.getMonth()+1).toString().padStart(2,'0') + '/' + d.getDate().toString().padStart(2,'0');
        }
        function pcEsc(s) { return (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
        function pcApplyScroll(row, text) {
          var el = row.querySelector('.yt-qi-title');
          if (!el || el.scrollWidth <= el.clientWidth) return;
          var e2 = pcEsc(text);
          el.innerHTML = '<span class="scroll-wrap" style="--dur:' + Math.max(6, text.length / 6) + 's"><span>' + e2 + '&nbsp;&nbsp;&nbsp;</span><span aria-hidden="true">' + e2 + '&nbsp;&nbsp;&nbsp;</span></span>';
        }
        var pcQueueFingerprint = '';
        var pcPrevIndex = -1;
        function pcRenderQueue(s) {
          const container = document.getElementById('pc-queue-list');
          const idx = s.current_index;
          const isEmpty = !s.queue || !s.queue.length;
          if (isEmpty) {
            pcQueueFingerprint = '';
            pcPrevIndex = -1;
            container.innerHTML =
              '<div style="color:var(--text-dim);font-size:13px;padding:16px 0;text-align:center">Loading episodes\u2026</div>';
            return;
          }
          var fp = JSON.stringify(s.queue.map(function(v) { return v.url; }));
          var sameData = fp === pcQueueFingerprint;
          var sameIdx = idx === pcPrevIndex;
          if (sameData && sameIdx) return;

          if (sameData) {
            var oldIdx = pcPrevIndex;
            pcPrevIndex = idx;
            var oldRow = oldIdx >= 0 && oldIdx < container.children.length
              ? container.children[oldIdx] : null;
            if (oldRow) {
              oldRow.className = 'yt-queue-item';
              oldRow.querySelector('.yt-qi-indicator').innerHTML = '&#x25B6;';
              var oldTitleEl = oldRow.querySelector('.yt-qi-title');
              if (oldTitleEl && s.queue[oldIdx]) oldTitleEl.innerHTML = pcEsc(s.queue[oldIdx].title || '');
            }
            var newRow = idx >= 0 && idx < container.children.length
              ? container.children[idx] : null;
            if (newRow) {
              newRow.className = 'yt-queue-item' + (s.status === 'playing' ? ' playing' : '') + ' current';
              newRow.querySelector('.yt-qi-indicator').innerHTML = '&#x25B6;';
              var titleEl = newRow.querySelector('.yt-qi-title');
              if (titleEl && s.queue[idx]) {
                titleEl.textContent = s.queue[idx].title || '';
              }
              var durEl = newRow.querySelector('.yt-qi-dur');
              if (durEl && !durEl.textContent && s.queue[idx] && s.queue[idx].duration) {
                var min = Math.floor(s.queue[idx].duration / 60);
                var sec = s.queue[idx].duration % 60;
                durEl.textContent = min + ':' + (sec < 10 ? '0' : '') + sec;
              }
              pcApplyScroll(newRow, s.queue[idx].title || '');
            }
            return;
          }

          pcQueueFingerprint = fp;
          pcPrevIndex = idx;

          let html = '';
          for (let i = 0; i < s.queue.length; i++) {
            const v = s.queue[i];
            const isCurrent = i === idx;
            const playing = isCurrent && s.status === 'playing';
            const cls = 'yt-queue-item' + (playing ? ' playing' : '') + (isCurrent ? ' current' : '');
            const indicator = '&#x25B6;';
            let dur = '';
            if (v.duration) {
              const m = Math.floor(v.duration / 60);
              const sec = v.duration % 60;
              dur = m + ':' + (sec < 10 ? '0' : '') + sec;
            }
            var title = v.title || '', escaped2 = pcEsc(title);
            var titleHtml = escaped2;
            html += '<div class="' + cls + '">' +
              '<span class="yt-qi-indicator">' + indicator + '</span>' +
              '<span class="yt-qi-date">' + (v.published ? pcFormatDate(v.published) : '') + '</span>' +
              '<span class="yt-qi-handle">' + pcEsc(v.feed_name || '').substring(0, 14) + '</span>' +
              '<span class="yt-qi-title">' + titleHtml + '</span>' +
              '<span class="yt-qi-dur">' + dur + '</span>' +
              '</div>';
          }
          container.innerHTML = html;
          if (idx >= 0 && idx < s.queue.length) {
            var el = container.children[idx];
            if (el) {
              el.scrollIntoView({ block: 'nearest' });
              pcApplyScroll(el, s.queue[idx].title || '');
            }
          }
        }

        function pcRenderFeeds(s) {
          const container = document.getElementById('pc-feed-pills');
          if (!s.feeds) return;
          const names = Object.keys(s.feeds);
          container.innerHTML = names.map(function(n) {
            const checked = s.feeds[n].enabled;
            return '<label class="yt-ch-label' + (checked ? ' active' : '') + '" ' +
              'onclick="pcToggleFeed(\\'' + n.replace(/'/g, "\\\\'") + '\\', ' + (!checked) + ')" ' +
              'title="' + (s.feeds[n].url || '') + '">' +
              n.substring(0, 20) + '</label>';
          }).join('');
        }

        async function pcPoll() {
          try {
            const r = await fetch('/api/podcast_player/status');
            const s = await r.json();
            pcState = s;

            var statusEl = document.getElementById('pc-status');
            if (s.status === 'disconnected') statusEl.innerHTML = 'Disconnected';
            else if (s.status === 'connected_idle')
              statusEl.innerHTML = '&#x25CF; Connected to ' + (s.device_name || 'Speaker');
            else if (s.status === 'playing') {
              var txt = '&#x25B6; Playing';
              if (s.current) txt += ' (' + (s.current_index + 1) + '/' + s.queue_count + ')';
              statusEl.innerHTML = txt;
            }

            document.getElementById('pc-queue-section').style.display = '';

            pcRenderFeeds(s);
            document.getElementById('pc-queue-count').textContent = (s.queue_count || 0) + ' episodes';
            pcRenderQueue(s);
            document.getElementById('pc-queue-list').classList.toggle('playing-mode', s.status === 'playing');

            const playing = s.status === 'playing';
            const playBtn = document.getElementById('pc-play-btn');
            playBtn.style.display = playing ? 'none' : '';
            if (!playing && playBtn.disabled) {
            } else {
              playBtn.innerHTML = '\u25B6';
            }
            document.getElementById('pc-stop-btn').style.display = playing ? '' : 'none';
            document.getElementById('pc-skip-btn').style.display = playing ? '' : 'none';

            var ql = document.getElementById('pc-queue-list');
            var oldBars = ql.querySelectorAll('.yt-queue-item:not(.playing) .qi-progress');
            oldBars.forEach(function(el){ el.remove(); });
            if (s.status === 'playing' && s.media_duration > 0 && s.current_index >= 0) {
              var row = ql.querySelector('.yt-queue-item.playing');
              if (row) {
                var pct = Math.min(100, Math.max(0, (s.media_position || 0) / s.media_duration * 100));
                var fill = row.querySelector('.qi-progress-fill');
                if (fill) { fill.style.width = pct + '%'; } else {
                  var bar = document.createElement('div');
                  bar.className = 'qi-progress';
                  bar.innerHTML = '<div class="qi-progress-fill" style="width:' + pct + '%"></div>';
                  row.appendChild(bar);
                }
              }
            }
          } catch(e) { console.error('Podcast poll failed', e); }
        }

        setInterval(pcPoll, 3000);
        pcPoll();
        document.getElementById('pc-queue-list').addEventListener('click', function(e) {
          var ind = e.target.closest('.yt-qi-indicator');
          var row = e.target.closest('.yt-queue-item');
          if (!row) return;
          var idx = Array.prototype.indexOf.call(row.parentNode.children, row);

          if (ind && pcState.status === 'playing' && idx >= 0) {
            pcFetch('/api/podcast_player/seek', { index: idx });
            return;
          }

          if (row.classList.contains('current')) return;

          var isSelected = row.classList.toggle('selected');
          if (isSelected) {
            for (var i = 0; i < row.parentNode.children.length; i++) {
              var child = row.parentNode.children[i];
              if (child !== row && child.classList.contains('selected')) {
                child.classList.remove('selected');
                if (pcState.queue && pcState.queue[i]) {
                  var pt = child.querySelector('.yt-qi-title');
                  if (pt) pt.textContent = pcState.queue[i].title || '';
                }
              }
            }
          }

          var titleEl = row.querySelector('.yt-qi-title');
          if (!titleEl || idx < 0 || !pcState.queue || !pcState.queue[idx]) return;
          if (isSelected) {
            pcApplyScroll(row, pcState.queue[idx].title || '');
          } else {
            titleEl.textContent = pcState.queue[idx].title || '';
          }
        });
        """
