import os
import re
import threading
import time
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


def _fetch_channel_videos(handle: str, ydl: yt_dlp.YoutubeDL) -> list[dict]:
    url = f"https://www.youtube.com/{handle}/videos"
    info = ydl.extract_info(url, download=False)
    entries = info.get("entries", [])
    videos = []
    for entry in entries:
        if not entry or entry.get("media_type") != "video":
            continue
        vid = entry.get("id")
        dur = entry.get("duration", 0)
        ts = entry.get("timestamp")
        now_ts = time.time()
        pub_ts = int(ts) if ts and ts <= now_ts else int(now_ts)
        videos.append({
            "title": entry.get("title", ""),
            "video_id": vid,
            "published": pub_ts,
            "link": f"https://youtube.com/watch?v={vid}",
            "handle": handle,
            "duration": dur,
            "resolved_url": entry.get("url"),
        })
    return videos


class YTCastPlugin(Plugin):
    name = "yt_cast"
    title = "TV"
    icon = "📺"
    order = 2

    def __init__(self, controller, config: dict):
        super().__init__(controller, config)

        self.uncast_duration: int = config.get("uncast_duration", 40)

        self.status = "disconnected"
        self.device_name = ""
        self.queue: list[dict] = []
        self.current_index = -1
        self.channel_enabled: dict[str, bool] = dict(config.get("channel_enabled", {}))
        self.channels: list[str] = list(self.channel_enabled.keys()) or list(config.get("channels", []))
        self.cast_start_time: float = 0
        self.feed_interval: int = config.get("feed_interval", 60)
        self._play_start: float = 0
        self._media_duration_local: int = 0

        self._cast: pychromecast.Chromecast | None = None
        self._browser: pychromecast.discovery.CastBrowser | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._feed_cache: dict[str, list[dict]] = {}
        self._feed_thread: threading.Thread | None = None
        self._feed_stop_event = threading.Event()
        self._ydl = yt_dlp.YoutubeDL({
            'format': '18',
            'quiet': True,
            'playlistend': 5,
            'socket_timeout': 30,
            'remote_components': ['ejs:github'],
            'extractor_args': {'youtube': ['player_client=web']},
        })

    def _build_preview(self) -> list[dict]:
        items = []
        for handle in self.channels:
            if not self.channel_enabled.get(handle, True):
                continue
            items.extend(self._feed_cache.get(handle, []))
        items.sort(key=lambda v: v["published"], reverse=True)
        return items[:30]

    def _feed_fetch_loop(self):
        first_pass = True
        while not self._feed_stop_event.is_set():
            with self._lock:
                handles = list(self.channels)
            for handle in handles:
                if self._feed_stop_event.is_set():
                    return
                if not self.channel_enabled.get(handle, True):
                    continue
                try:
                    videos = _fetch_channel_videos(handle, self._ydl)
                    with self._lock:
                        self._feed_cache[handle] = videos
                except Exception as e:
                    print(f"YT Cast feed: failed to fetch {handle}: {e}", flush=True)
                delay = 0.5 if first_pass else self.feed_interval
                self._feed_stop_event.wait(delay)
            first_pass = False

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
                dur = video.get("duration", 0) or 0
            try:
                # Re-resolve at play time to avoid stale URLs from feed fetching
                info = self._ydl.extract_info(f'https://youtube.com/watch?v={video_id}', download=False)
                url = info.get('url') or video.get("resolved_url")
                dur = int(info.get('duration') or dur)
                if not url:
                    raise RuntimeError(f"could not resolve URL for {video_id}")
                mc = self._cast.media_controller
                mc.play_media(url, 'video/mp4')
                self._play_start = time.time()
                self._media_duration_local = dur
                print(f"YT Cast: playing [{self.current_index + 1}/{len(self.queue)}] {title}", flush=True)
                return None
            except Exception as e:
                print(f"YT Cast: skipping unavailable {title}: {e}", flush=True)
                with self._lock:
                    self.current_index += 1
        with self._lock:
            self.status = "connected_idle"
        return "all videos in queue are unavailable"

    def _save_config(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        try:
            import yaml
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            if "plugins" not in cfg:
                cfg["plugins"] = {}
            cfg["plugins"]["yt_cast"] = {
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
        self._play_start = 0
        self._media_duration_local = 0
        self.current_index = -1
        self.queue = []
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
        self._feed_stop_event.clear()
        self._feed_thread = threading.Thread(target=self._feed_fetch_loop, daemon=True)
        self._feed_thread.start()
        print("YT Cast: ready")

    def stop(self):
        self._stop_event.set()
        self._feed_stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._feed_thread:
            self._feed_thread.join(timeout=5)
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
                if self.status == "playing":
                    queue = self.queue
                    curr_idx = self.current_index
                else:
                    queue = self._build_preview()
                    curr_idx = -1
                if curr_idx >= 0 and curr_idx < len(queue):
                    current = dict(queue[curr_idx])
                if self.status == "playing" and self.cast_start_time > 0 and self.uncast_duration > 0:
                    elapsed = time.time() - self.cast_start_time
                    remaining = max(0, int(self.uncast_duration - elapsed / 60))
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
                    "channel_enabled": dict(self.channel_enabled),
                    "uncast_duration": self.uncast_duration,
                    "remaining_minutes": remaining,
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
                for handle in self.channels:
                    try:
                        videos = _fetch_channel_videos(handle, self._ydl)
                        with self._lock:
                            self._feed_cache[handle] = videos
                    except Exception as e:
                        print(f"YT Cast: refresh failed for {handle}: {e}", flush=True)

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
                self.cast_start_time = time.time()
                self.status = "playing"
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
                return {"ok": False, "error": "Channel not found \u2014 check spelling"}
            with self._lock:
                self.channel_enabled[handle] = True
                self.channels = list(self.channel_enabled.keys())
                self._feed_cache[handle] = []
            self._save_config()
            return {"ok": True, "handle": handle}

        app.include_router(self.router)

    def ui_section(self) -> str:
        return """
        <div class="plugin-section" id="plugin-yt_cast">
          <div class="yt-status-line" id="yt-status">Disconnected</div>
          <div id="yt-errmsg" style="display:none; padding:8px 12px; background:#2a1515; border:1px solid #ff4444; border-radius:8px; font-size:13px; color:#ff8888; margin-bottom:12px"></div>

          <details class="collapsible">
            <summary>Auto-stop</summary>
            <div class="series-pills" id="yt-uncast-pills"></div>
          </details>

          <details class="collapsible" id="yt-channels-section">
            <summary>Channels</summary>
            <div class="series-pills" id="yt-channel-pills"></div>
            <div class="yt-add-channel-row">
              <input type="text" id="yt-add-input" placeholder="@channelname" maxlength="100" onkeydown="if(event.key==='Enter')ytAddChannel()">
              <button onclick="ytAddChannel()">+ Add</button>
            </div>
          </details>

          <div class="transport" id="yt-controls" style="gap:12px">
            <button class="transport-btn transport-play" onclick="ytPlay()" id="yt-play-btn">&#x25B6;</button>
            <button class="transport-btn transport-play stop-btn" onclick="ytStop()" id="yt-stop-btn" style="display:none">&#x23F9;</button>
            <button class="transport-btn" onclick="ytSkip()" id="yt-skip-btn" style="display:none">&#x23ED;</button>
          </div>

          <div class="series-picker" id="yt-queue-section" style="display:none">
            <div class="series-label" style="display:flex; justify-content:space-between; align-items:center">
              <span>Queue</span>
              <span id="yt-queue-count" style="font-size:12px; color:var(--text-dim)">0 videos</span>
            </div>
            <div id="yt-queue-list" style="max-height:320px; overflow-y:auto; margin-top:8px"></div>
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

        async function ytSetDuration(m) {
          ytHideError();
          const r = await ytFetch('/api/yt_cast/uncast_duration', { minutes: m });
          ytPoll();
          if (!r || !r.ok) { ytShowError(r ? r.error : 'Set failed'); }
        }

        async function ytToggleChannel(handle, enabled) {
          ytHideError();
          const r = await ytFetch('/api/yt_cast/toggle_channel', { handle: handle, enabled: enabled });
          ytPoll();
          if (!r || !r.ok) { ytShowError(r ? r.error : 'Toggle failed'); }
        }

        async function ytAddChannel() {
          ytHideError();
          const input = document.getElementById('yt-add-input');
          const btn = document.querySelector('.yt-add-channel-row button');
          const handle = input.value.trim();
          if (!handle) return;
          btn.disabled = true;
          btn.textContent = 'Adding\u2026';
          const r = await ytFetch('/api/yt_cast/add_channel', { handle: handle });
          btn.disabled = false;
          btn.textContent = '+ Add';
          if (r && r.ok) {
            input.value = '';
            ytPoll();
          } else {
            ytShowError(r ? r.error : 'Failed to add channel');
          }
        }

        function ytFormatDate(ts) {
          var d = new Date(ts * 1000);
          return (d.getMonth()+1).toString().padStart(2,'0') + '/' + d.getDate().toString().padStart(2,'0');
        }
        function ytEsc(s) { return (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
        function ytApplyScroll(row, text) {
          var el = row.querySelector('.yt-qi-title');
          if (!el || el.scrollWidth <= el.clientWidth) return;
          var e2 = ytEsc(text);
          el.innerHTML = '<span class="scroll-wrap" style="--dur:' + Math.max(6, text.length / 6) + 's"><span>' + e2 + '&nbsp;&nbsp;&nbsp;</span><span aria-hidden="true">' + e2 + '&nbsp;&nbsp;&nbsp;</span></span>';
        }
        var ytQueueFingerprint = '';
        var ytPrevIndex = -1;
        function ytRenderQueue(s) {
          const container = document.getElementById('yt-queue-list');
          const idx = s.current_index;
          const isEmpty = !s.queue || !s.queue.length;
          if (isEmpty) {
            ytQueueFingerprint = '';
            ytPrevIndex = -1;
            container.innerHTML =
              '<div style="color:var(--text-dim);font-size:13px;padding:16px 0;text-align:center">Loading videos\u2026</div>';
            return;
          }
          var fp = JSON.stringify(s.queue.map(function(v) { return v.video_id + '|' + (v.duration || ''); }));
          var sameData = fp === ytQueueFingerprint;
          var sameIdx = idx === ytPrevIndex;
          if (sameData && sameIdx) return;

          if (sameData) {
            var oldIdx = ytPrevIndex;
            ytPrevIndex = idx;
            var oldRow = oldIdx >= 0 && oldIdx < container.children.length
              ? container.children[oldIdx] : null;
            if (oldRow) {
              oldRow.className = 'yt-queue-item';
              oldRow.querySelector('.yt-qi-indicator').innerHTML = '&#x25B6;';
              var oldTitleEl = oldRow.querySelector('.yt-qi-title');
              if (oldTitleEl && s.queue[oldIdx]) oldTitleEl.innerHTML = ytEsc(s.queue[oldIdx].title || '');
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
              ytApplyScroll(newRow, s.queue[idx].title || '');
            }
            return;
          }

          ytQueueFingerprint = fp;
          ytPrevIndex = idx;
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
            var title = v.title || '', escaped2 = ytEsc(title);
            var titleHtml = escaped2;
            html += '<div class="' + cls + '">' +
              '<span class="yt-qi-indicator">' + indicator + '</span>' +
              '<span class="yt-qi-date">' + (v.published ? ytFormatDate(v.published) : '') + '</span>' +
              '<span class="yt-qi-handle">' + ytEsc(v.handle || '').replace('@', '') + '</span>' +
              '<span class="yt-qi-title">' + titleHtml + '</span>' +
              '<span class="yt-qi-dur">' + dur + '</span>' +
              '</div>';
          }
          container.innerHTML = html;
          if (idx >= 0 && idx < s.queue.length) {
            var el = container.children[idx];
            if (el) {
              el.scrollIntoView({ block: 'nearest' });
              ytApplyScroll(el, s.queue[idx].title || '');
            }
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

            document.getElementById('yt-queue-section').style.display = '';

            const uncastPills = document.getElementById('yt-uncast-pills');
            if (uncastPills) {
              var opts = [0,10,20,30,40,50,60,70,80,90,100,110,120];
              uncastPills.innerHTML = opts.map(function(m) {
                var a = m === (s.uncast_duration || 0) ? ' active' : '';
                var l = m === 0 ? 'Off' : m + 'm';
                return '<button class=\"series-pill' + a + '\" onclick=\"ytSetDuration(' + m + ')\">' + l + '</button>';
              }).join('');
            }

            ytRenderChannels(s);
            document.getElementById('yt-queue-count').textContent = (s.queue_count || 0) + ' videos';
            ytRenderQueue(s);
            document.getElementById('yt-queue-list').classList.toggle('playing-mode', s.status === 'playing');

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

            var ql = document.getElementById('yt-queue-list');
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
          } catch(e) { console.error('YT Cast poll failed', e); }
        }

        setInterval(ytPoll, 3000);
        ytPoll();
        document.getElementById('yt-queue-list').addEventListener('click', function(e) {
          var ind = e.target.closest('.yt-qi-indicator');
          var row = e.target.closest('.yt-queue-item');
          if (!row) return;
          var idx = Array.prototype.indexOf.call(row.parentNode.children, row);

          if (ind && ytState.status === 'playing' && idx >= 0) {
            ytFetch('/api/yt_cast/seek', { index: idx });
            return;
          }

          if (row.classList.contains('current')) return;

          var isSelected = row.classList.toggle('selected');
          if (isSelected) {
            for (var i = 0; i < row.parentNode.children.length; i++) {
              var child = row.parentNode.children[i];
              if (child !== row && child.classList.contains('selected')) {
                child.classList.remove('selected');
                if (ytState.queue && ytState.queue[i]) {
                  var pt = child.querySelector('.yt-qi-title');
                  if (pt) pt.textContent = ytState.queue[i].title || '';
                }
              }
            }
          }

          var titleEl = row.querySelector('.yt-qi-title');
          if (!titleEl || idx < 0 || !ytState.queue || !ytState.queue[idx]) return;
          if (isSelected) {
            ytApplyScroll(row, ytState.queue[idx].title || '');
          } else {
            titleEl.textContent = ytState.queue[idx].title || '';
          }
        });
        """
