import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pychromecast
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
        try:
            dt = parsedate_to_datetime(pub_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        dur_raw = item.findtext("duration", "") or item.findtext("itunes:duration", "", ITUNES_NS)
        dur = _parse_duration(dur_raw) if dur_raw else 0
        link = item.findtext("link", "")
        result.append({
            "title": title,
            "url": mp3_url,
            "published": dt,
            "published_str": dt.strftime("%m/%d"),
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
        self._feed_thread: threading.Thread | None = None
        self._feed_stop_event = threading.Event()

    def _discover(self) -> pychromecast.Chromecast | None:
        try:
            chromecasts, browser = pychromecast.get_chromecasts()
        except Exception as e:
            print(f"Podcast: discovery error: {e}", flush=True)
            return None
        self._browser = browser
        if chromecasts:
            cc = chromecasts[0]
            try:
                cc.wait(timeout=10)
            except Exception as e:
                print(f"Podcast: device wait error: {e}", flush=True)
            return cc
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
            try:
                mc = self._cast.media_controller
                mc.play_media(mp3_url, "audio/mpeg")
                print(f"Podcast: playing [{self.current_index + 1}/{len(self.queue)}] {title}", flush=True)
                return None
            except Exception as e:
                print(f"Podcast: skipping unavailable {title}: {e}", flush=True)
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
        return items[:20]

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
                try:
                    episodes = _fetch_feed(feed_name, info["url"])
                    with self._lock:
                        self._feed_cache[feed_name] = episodes
                except Exception as e:
                    print(f"Podcast feed: failed to fetch {feed_name}: {e}", flush=True)
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
            print(f"Podcast: failed to save config: {e}", flush=True)

    def _monitor_loop(self):
        prev_state = "UNKNOWN"

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
                            else:
                                print("Podcast: queue exhausted", flush=True)
                                self._do_stop()

                if should_play_next:
                    self._play_next()

                prev_state = state
            except Exception as e:
                print(f"Podcast monitor error: {e}", flush=True)

            self._stop_event.wait(2)

    def _do_stop(self):
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
        print("Podcast: ready")

    def stop(self):
        self._stop_event.set()
        self._feed_stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._feed_thread:
            self._feed_thread.join(timeout=5)
        self._do_disconnect()
        super().stop()
        print("Podcast: stopped")

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
                for item in queue_out:
                    item["published"] = str(item["published"])
                return {
                    "status": self.status,
                    "device_name": self.device_name,
                    "queue": queue_out,
                    "current_index": curr_idx,
                    "current": current,
                    "queue_count": len(queue_out),
                    "feeds": {name: dict(info) for name, info in self.feeds.items()},
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
                        with self._lock:
                            self._feed_cache[feed_name] = episodes
                    except Exception as e:
                        print(f"Podcast: refresh failed for {feed_name}: {e}", flush=True)

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
            if (oldRow) { oldRow.className = 'yt-queue-item'; oldRow.querySelector('.yt-qi-indicator').innerHTML = ''; }
            var newRow = idx >= 0 && idx < container.children.length
              ? container.children[idx] : null;
            if (newRow) {
              newRow.className = 'yt-queue-item' + (s.status === 'playing' ? ' playing' : '') + ' current';
              newRow.querySelector('.yt-qi-indicator').innerHTML = s.status === 'playing' ? '&#x25B6;' : '&#x2022;';
              var durEl = newRow.querySelector('.yt-qi-dur');
              if (durEl && !durEl.textContent && s.queue[idx] && s.queue[idx].duration) {
                var min = Math.floor(s.queue[idx].duration / 60);
                var sec = s.queue[idx].duration % 60;
                durEl.textContent = min + ':' + (sec < 10 ? '0' : '') + sec;
              }
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
              '<span class="yt-qi-handle">' + (v.feed_name || '').substring(0, 14) + '</span>' +
              '<span class="yt-qi-title">' + (v.title || '').substring(0, 60) + '</span>' +
              '<span class="yt-qi-dur">' + dur + '</span>' +
              '</div>';
          }
          container.innerHTML = html;
          if (idx >= 0 && idx < s.queue.length) {
            var el = container.children[idx];
            if (el) el.scrollIntoView({ block: 'nearest' });
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

            const playing = s.status === 'playing';
            const playBtn = document.getElementById('pc-play-btn');
            playBtn.style.display = playing ? 'none' : '';
            if (!playing && playBtn.disabled) {
            } else {
              playBtn.innerHTML = '\u25B6';
            }
            document.getElementById('pc-stop-btn').style.display = playing ? '' : 'none';
            document.getElementById('pc-skip-btn').style.display = playing ? '' : 'none';
          } catch(e) { console.error('Podcast poll failed', e); }
        }

        setInterval(pcPoll, 3000);
        pcPoll();
        """
