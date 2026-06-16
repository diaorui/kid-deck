import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pychromecast
import requests

deno = os.path.expanduser('~/.deno/bin')
if deno not in os.environ.get('PATH', ''):
    os.environ['PATH'] = deno + ':' + os.environ.get('PATH', '')

if not os.path.isfile(os.path.expanduser('~/.deno/bin/deno')):
    print("WARNING: deno JS runtime not found at ~/.deno/bin/deno", flush=True)
    print("  YouTube video extraction may fail for some videos.", flush=True)
    print("  Install: curl -fsSL https://deno.land/install.sh | sh", flush=True)

import yt_dlp

from plugins import Plugin


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
    r = requests.get(f"https://www.youtube.com/{handle}/about",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    for p in [r'"externalId":"(UC[^"]+)"', r'"channelId":"(UC[^"]+)"',
              r'<meta itemprop="channelId" content="(UC[^"]+)"']:
        m = re.search(p, r.text)
        if m:
            return m.group(1)
    return None


def _fetch_channel_videos(channel_id: str) -> list[dict]:
    playlist_id = "UULF" + channel_id[2:]
    r = requests.get(f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.content)
    videos = []
    for entry in root.findall("atom:entry", ns):
        title = entry.find("atom:title", ns).text
        video_id = entry.find("atom:id", ns).text.split(":")[-1]
        published = entry.find("atom:published", ns).text
        link = entry.find("atom:link", ns).attrib["href"]
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        videos.append({
            "title": title,
            "video_id": video_id,
            "published": dt,
            "published_str": dt.strftime("%m/%d"),
            "link": link,
            "handle": "",
        })
    return videos


class YTCastPlugin(Plugin):
    name = "yt_cast"
    title = "YouTube TV"
    icon = "📺"
    order = 2

    def __init__(self, controller, config: dict):
        super().__init__(controller, config)

        self.channels: list[str] = list(config.get("channels", []))
        self.uncast_duration: int = config.get("uncast_duration", 45)

        self.status = "disconnected"
        self.device_name = ""
        self.queue: list[dict] = []
        self.current_index = -1
        saved = config.get("channel_enabled", {})
        self.channel_enabled: dict[str, bool] = {ch: saved.get(ch, True) for ch in self.channels}
        self.cast_start_time: float = 0

        self._cast: pychromecast.Chromecast | None = None
        self._browser: pychromecast.discovery.CastBrowser | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._url_cache: dict[str, str] = {}
        self._duration_cache: dict[str, int] = {}
        self._ydl = yt_dlp.YoutubeDL({
            'format': '18',
            'quiet': True,
            'remote_components': ['ejs:github'],
            'extractor_args': {'youtube': ['player_client=web']},
        })

    def _resolve_url(self, video_id: str) -> str | None:
        if video_id in self._url_cache:
            return self._url_cache[video_id]
        try:
            info = self._ydl.extract_info(f'https://youtube.com/watch?v={video_id}', download=False)
            url = info.get('url')
            duration = info.get('duration')
            if url:
                self._url_cache[video_id] = url
            if duration:
                self._duration_cache[video_id] = int(duration)
            return url
        except Exception as e:
            print(f"YT Cast: failed to resolve {video_id}: {e}", flush=True)
            return None

    def _discover(self) -> pychromecast.Chromecast | None:
        try:
            chromecasts, browser = pychromecast.get_chromecasts()
        except Exception as e:
            print(f"YT Cast: discovery error: {e}", flush=True)
            return None
        self._browser = browser
        if chromecasts:
            cc = chromecasts[0]
            try:
                cc.wait(timeout=10)
            except Exception as e:
                print(f"YT Cast: device wait error: {e}", flush=True)
            return cc
        return None

    def _play_next(self) -> str | None:
        max_attempts = 10
        for _ in range(max_attempts):
            with self._lock:
                if self.current_index < 0 or self.current_index >= len(self.queue):
                    self.status = "connected_idle"
                    return "queue exhausted"
                video = self.queue[self.current_index]
                video_id = video["video_id"]
                title = video["title"]
            try:
                url = self._resolve_url(video_id)
                if url is None:
                    raise RuntimeError(f"could not resolve URL for {video_id}")
                dur = self._duration_cache.get(video_id)
                if dur:
                    with self._lock:
                        if self.current_index < len(self.queue):
                            self.queue[self.current_index]['duration'] = dur
                mc = self._cast.media_controller
                mc.play_media(url, 'video/mp4')
                print(f"YT Cast: playing [{self.current_index + 1}/{len(self.queue)}] {title}", flush=True)
                return None
            except Exception as e:
                print(f"YT Cast: skipping unavailable {title}: {e}", flush=True)
                with self._lock:
                    self.current_index += 1
        with self._lock:
            self.status = "connected_idle"
        return "all videos in queue are unavailable"

    def _apply_channel_filter(self):
        if not self.queue:
            return
        filtered = [v for v in self.queue if self.channel_enabled.get(v["handle"], True)]
        if not filtered:
            self.queue = []
            self._do_stop()
            return
        if self.current_index >= 0 and self.current_index < len(self.queue):
            current = self.queue[self.current_index]
            self.queue = filtered
            try:
                self.current_index = next(i for i, v in enumerate(self.queue) if v["video_id"] == current["video_id"])
            except StopIteration:
                self._do_stop()
        else:
            self.queue = filtered
            self.current_index = -1

    def _save_config(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        try:
            import yaml
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            if "plugins" not in cfg:
                cfg["plugins"] = {}
            cfg["plugins"]["yt_cast"] = {
                "channels": self.channels,
                "uncast_duration": self.uncast_duration,
                "channel_enabled": dict(self.channel_enabled),
            }
            with open(path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            print(f"YT Cast: failed to save config: {e}", flush=True)

    def _monitor_loop(self):
        prev_state = "UNKNOWN"
        prev_content = None

        while not self._stop_event.is_set():
            try:
                cast = self._cast
                if cast is None:
                    self._stop_event.wait(2)
                    continue

                with self._lock:
                    if self.status == "playing":
                        now = time.time()
                        if self.uncast_duration > 0 and self.cast_start_time > 0:
                            elapsed = now - self.cast_start_time
                            if elapsed >= self.uncast_duration * 60:
                                print(f"YT Cast: auto-uncast after {self.uncast_duration}m", flush=True)
                                self._do_stop()
                                self._stop_event.wait(2)
                                continue

                mc = cast.media_controller
                mc.update_status()
                state = mc.status.player_state
                content_id = mc.status.content_id
                idle_reason = mc.status.idle_reason

                should_play_next = False
                with self._lock:
                    if self.status == "playing":
                        if state == "IDLE" and prev_state in ("PLAYING", "BUFFERING") and idle_reason == "FINISHED":
                            next_idx = self.current_index + 1
                            if next_idx < len(self.queue):
                                self.current_index = next_idx
                                should_play_next = True
                            else:
                                print("YT Cast: queue exhausted", flush=True)
                                self._do_stop()

                if should_play_next:
                    self._play_next()

                prev_state = state
                prev_content = content_id
            except Exception as e:
                print(f"YT Cast monitor error: {e}", flush=True)

            self._stop_event.wait(2)

    def _do_stop(self):
        self.cast_start_time = 0
        self.current_index = -1
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
        if self._browser:
            try:
                self._browser.stop_discovery()
            except Exception:
                pass
            self._browser = None

    def _do_disconnect(self):
        self._do_stop()

    def start(self):
        super().start()
        print("YT Cast: ready")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._do_disconnect()
        super().stop()
        print("YT Cast: stopped")

    def register_routes(self, app):
        from fastapi import Request

        @self.router.get("/status")
        async def status():
            with self._lock:
                current = None
                remaining = 0
                if self.current_index >= 0 and self.current_index < len(self.queue):
                    current = dict(self.queue[self.current_index])
                    vid = current["video_id"]
                    if current.get("duration") is None and vid in self._duration_cache:
                        current["duration"] = self._duration_cache[vid]
                if self.status == "playing" and self.cast_start_time > 0 and self.uncast_duration > 0:
                    elapsed = time.time() - self.cast_start_time
                    remaining = max(0, int(self.uncast_duration - elapsed / 60))
                queue_out = []
                for v in self.queue:
                    item = dict(v)
                    vid = v["video_id"]
                    if item.get("duration") is None and vid in self._duration_cache:
                        item["duration"] = self._duration_cache[vid]
                    queue_out.append(item)
                return {
                    "status": self.status,
                    "device_name": self.device_name,
                    "queue": queue_out,
                    "current_index": self.current_index,
                    "current": current,
                    "queue_count": len(self.queue),
                    "channel_enabled": dict(self.channel_enabled),
                    "uncast_duration": self.uncast_duration,
                    "remaining_minutes": remaining,
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
                enabled = [ch for ch in self.channels if self.channel_enabled.get(ch, True)]
                if not enabled:
                    return []
                result = []
                for handle in enabled:
                    for attempt in range(2):
                        try:
                            cid = _resolve_channel_id(handle)
                            if cid is None:
                                break
                            videos = _fetch_channel_videos(cid)
                            if videos:
                                for v in videos:
                                    v["handle"] = handle
                                result.extend(videos)
                                break
                        except Exception as e:
                            if attempt == 0:
                                time.sleep(0.5)
                                continue
                            print(f"YT Cast: failed to fetch {handle}: {e}", flush=True)
                result.sort(key=lambda v: v["published"], reverse=True)
                return result[:20]

            queue = await loop.run_in_executor(None, _do_refresh)
            with self._lock:
                self.queue = queue
                self.current_index = -1 if self.status != "playing" else self.current_index
            print(f"YT Cast: refreshed — {len(queue)} videos", flush=True)
            return {"ok": True, "queue_count": len(queue)}

        @self.router.post("/play")
        async def play():
            import asyncio
            with self._lock:
                if self._cast is None:
                    return {"ok": False, "error": "not connected"}
                if not self.queue:
                    return {"ok": False, "error": "queue empty, refresh first"}
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
                return {"ok": False, "error": err}
            with self._lock:
                self.cast_start_time = time.time()
                self.status = "playing"
            return {"ok": True, "video": self.queue[0] if self.queue else None}

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
                    return {"ok": False, "error": "no more videos"}
                self.current_index = next_idx
            loop = asyncio.get_event_loop()
            err = await loop.run_in_executor(None, self._play_next)
            if err:
                with self._lock:
                    self.current_index = -1
                    self.status = "connected_idle"
                return {"ok": False, "error": err}
            return {"ok": True}

        @self.router.post("/uncast_duration")
        async def uncast_duration_route(request: Request):
            data = await request.json()
            minutes = int(data.get("minutes", self.uncast_duration))
            with self._lock:
                self.uncast_duration = max(0, minutes)
            self._save_config()
            return {"ok": True, "minutes": self.uncast_duration}

        @self.router.post("/toggle_channel")
        async def toggle_channel(request: Request):
            data = await request.json()
            handle = data.get("handle", "")
            enabled = data.get("enabled", True)
            with self._lock:
                if handle in self.channel_enabled:
                    self.channel_enabled[handle] = enabled
                    self._apply_channel_filter()
            self._save_config()
            return {"ok": True}

        app.include_router(self.router)

    def ui_section(self) -> str:
        return """
        <div class="plugin-section" id="plugin-yt_cast">
          <div class="now-playing-card">
            <div class="np-label">Chromecast</div>
            <div class="np-status" id="yt-status">Disconnected</div>
            <div id="yt-errmsg" style="display:none; margin-top:8px; padding:8px 12px; background:#2a1515; border:1px solid #ff4444; border-radius:8px; font-size:13px; color:#ff8888"></div>
          </div>

          <div class="volume-row" id="yt-uncast-row">
            <span class="vol-label">Auto-stop: <span id="yt-uncast-current">45 min</span></span>
            <input type="number" class="vol-slider" id="yt-uncast-input" min="0" max="999" value="45"
                   style="flex:0 0 70px; background:var(--bg); color:var(--text); border:1px solid #333; border-radius:8px; padding:8px 12px; font-size:14px; outline:none">
            <span style="font-size:13px; color:var(--text-dim)">minutes</span>
            <button class="toggle-btn" onclick="ytSetDuration()" id="yt-uncast-set-btn" style="flex-shrink:0">Set</button>
          </div>

          <div class="series-picker" id="yt-channels-section">
            <div class="series-label">Channels</div>
            <div class="series-pills" id="yt-channel-pills"></div>
          </div>

          <div class="series-picker" id="yt-queue-section" style="display:none">
            <div class="series-label" style="display:flex; justify-content:space-between; align-items:center">
              <span>Queue</span>
              <span id="yt-queue-count" style="font-size:12px; color:var(--text-dim)">0 videos</span>
            </div>
            <div id="yt-queue-list" style="max-height:320px; overflow-y:auto; margin-top:8px"></div>
          </div>

          <div class="transport" id="yt-controls" style="gap:12px">
            <button class="transport-btn transport-play" onclick="ytPlay()" id="yt-play-btn">&#x25B6;</button>
            <button class="transport-btn transport-play stop-btn" onclick="ytStop()" id="yt-stop-btn" style="display:none">&#x23F9;</button>
            <button class="transport-btn" onclick="ytSkip()" id="yt-skip-btn" style="display:none">&#x23ED;</button>
          </div>
        </div>
        """

    def ui_js(self) -> str:
        return """
        let ytState = {};

        function ytShowError(msg) {
          const el = document.getElementById('yt-errmsg');
          el.textContent = msg;
          el.style.display = '';
          setTimeout(function(){ el.style.display = 'none'; }, 10000);
        }

        function ytHideError() {
          document.getElementById('yt-errmsg').style.display = 'none';
        }

        async function ytFetch(url, body) {
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

        async function ytPlay() {
          ytHideError();
          const btn = document.getElementById('yt-play-btn');
          const origHTML = btn.innerHTML;
          btn.disabled = true;
          btn.innerHTML = 'Starting\u2026';

          document.getElementById('yt-status').textContent = 'Connecting\u2026';
          const r = await ytFetch('/api/yt_cast/connect');
          if (!r || !r.ok) {
            btn.disabled = false; btn.innerHTML = origHTML;
            document.getElementById('yt-status').textContent = 'Disconnected';
            ytShowError(r ? r.error : 'Connection failed');
            return;
          }

          document.getElementById('yt-status').textContent = 'Loading queue\u2026';
          document.getElementById('yt-queue-list').innerHTML =
            '<div style="color:var(--text-dim);font-size:13px;padding:16px 0;text-align:center">Loading videos\u2026</div>';
          const r2 = await ytFetch('/api/yt_cast/refresh');
          if (!r2 || !r2.ok || !r2.queue_count) {
            btn.disabled = false; btn.innerHTML = origHTML;
            ytPoll();
            ytShowError(r2 ? (r2.queue_count ? r2.error : 'No videos found') : 'Refresh failed');
            return;
          }

          document.getElementById('yt-status').textContent = 'Starting YouTube\u2026';
          const r3 = await ytFetch('/api/yt_cast/play');
          btn.disabled = false; btn.innerHTML = origHTML;
          ytPoll();
          if (!r3 || !r3.ok) { ytShowError(r3 ? r3.error : 'Play failed'); }
        }

        async function ytStop() {
          ytHideError();
          await ytFetch('/api/yt_cast/stop');
          ytPoll();
        }

        async function ytSkip() {
          ytHideError();
          const r = await ytFetch('/api/yt_cast/skip');
          ytPoll();
          if (!r || !r.ok) { ytShowError(r ? r.error : 'Skip failed'); }
        }

        async function ytSetDuration() {
          const val = parseInt(document.getElementById('yt-uncast-input').value) || 45;
          const r = await ytFetch('/api/yt_cast/uncast_duration', { minutes: val });
          if (r && r.ok) {
            const btn = document.getElementById('yt-uncast-set-btn');
            const orig = btn.textContent;
            btn.textContent = '\u2713';
            setTimeout(function() { btn.textContent = orig; }, 1500);
          }
          ytPoll();
        }

        async function ytToggleChannel(handle, enabled) {
          ytHideError();
          const r = await ytFetch('/api/yt_cast/toggle_channel', { handle: handle, enabled: enabled });
          ytPoll();
          if (!r || !r.ok) { ytShowError(r ? r.error : 'Toggle failed'); }
        }

        function ytRenderQueue(s) {
          const container = document.getElementById('yt-queue-list');
          if (!s.queue || !s.queue.length) {
            container.innerHTML =
              '<div style="color:var(--text-dim);font-size:13px;padding:16px 0;text-align:center">Loading videos\u2026</div>';
            return;
          }
          let html = '';
          for (let i = 0; i < s.queue.length; i++) {
            const v = s.queue[i];
            const isCurrent = i === s.current_index;
            const playing = isCurrent && s.status === 'playing';
            const cls = 'yt-queue-item' + (playing ? ' playing' : '') + (isCurrent ? ' current' : '');
            const indicator = playing ? '&#x25B6;' : (isCurrent ? '&#x2022;' : '');
            let dur = '';
            if (v.duration) {
              const m = Math.floor(v.duration / 60);
              const sec = v.duration % 60;
              dur = m + ':' + (sec < 10 ? '0' : '') + sec;
            }
            html += '<div class="' + cls + '">' +
              '<span class="yt-qi-indicator">' + indicator + '</span>' +
              '<span class="yt-qi-date">' + (v.published_str || '') + '</span>' +
              '<span class="yt-qi-handle">' + (v.handle || '').replace('@', '') + '</span>' +
              '<span class="yt-qi-title">' + (v.title || '').substring(0, 60) + '</span>' +
              '<span class="yt-qi-dur">' + dur + '</span>' +
              '</div>';
          }
          container.innerHTML = html;
          if (s.current_index >= 0 && s.current_index < s.queue.length) {
            var el = container.children[s.current_index];
            if (el) el.scrollIntoView({ block: 'nearest' });
          }
        }

        function ytRenderChannels(s) {
          const container = document.getElementById('yt-channel-pills');
          if (!s.channel_enabled) return;
          const handles = Object.keys(s.channel_enabled);
          container.innerHTML = handles.map(function(h) {
            const checked = s.channel_enabled[h];
            const label = h.replace('@', '');
            return '<label class="yt-ch-label' + (checked ? ' active' : '') + '" ' +
              'onclick="ytToggleChannel(\\'' + h + '\\', ' + (!checked) + ')">' +
              label + '</label>';
          }).join('');
        }

        async function ytPoll() {
          try {
            const r = await fetch('/api/yt_cast/status');
            const s = await r.json();
            ytState = s;

            var statusEl = document.getElementById('yt-status');
            if (s.status === 'disconnected') statusEl.innerHTML = 'Disconnected';
            else if (s.status === 'connected_idle')
              statusEl.innerHTML = '&#x25CF; Connected to ' + (s.device_name || 'TV');
            else if (s.status === 'playing') {
              var txt = '&#x25B6; Playing';
              if (s.current) txt += ' (' + (s.current_index + 1) + '/' + s.queue_count + ')';
              if (s.remaining_minutes) txt += ' &#x2022; auto-stop ' + s.remaining_minutes + 'm';
              statusEl.innerHTML = txt;
            }

            const connected = s.status !== 'disconnected';
            document.getElementById('yt-uncast-current').textContent = (s.uncast_duration || 45) + ' min';
            document.getElementById('yt-uncast-input').value = s.uncast_duration || 45;
            document.getElementById('yt-queue-section').style.display = connected ? '' : 'none';

            ytRenderChannels(s);
            if (connected) {
              document.getElementById('yt-queue-count').textContent = (s.queue_count || 0) + ' videos';
              ytRenderQueue(s);
            }

            const playing = s.status === 'playing';
            const playBtn = document.getElementById('yt-play-btn');
            const hasQueue = (s.queue_count || 0) > 0;
            playBtn.style.display = playing ? 'none' : '';
            if (!playing && playBtn.disabled) {
              /* ytPlay() async flow is in progress, keep button text */
            } else {
              playBtn.innerHTML = '\u25B6';
            }
            document.getElementById('yt-stop-btn').style.display = playing ? '' : 'none';
            document.getElementById('yt-skip-btn').style.display = playing ? '' : 'none';
          } catch(e) { console.error('YT Cast poll failed', e); }
        }

        setInterval(ytPoll, 3000);
        ytPoll();
        """
