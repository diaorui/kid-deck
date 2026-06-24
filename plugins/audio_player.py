import random
import threading
import time
from datetime import datetime, time as dtime
from enum import Enum, auto
from pathlib import Path
from queue import Queue, Empty

from camera_client import convert_to_pcm
from plugins import Plugin


class Command(Enum):
    PAUSE = auto()
    RESUME = auto()
    SET_VOLUME = auto()
    STOP = auto()


def parse_time(time_str: str) -> dtime:
    parts = time_str.strip().split(":")
    return dtime(int(parts[0]), int(parts[1]))


class AudioPlayerPlugin(Plugin):
    name = "audio_player"
    title = "Player"
    icon = "🔊"
    order = 0

    def __init__(self, controller, config: dict):
        super().__init__(controller, config)

        self.stories_root = Path(config["folder"])
        self.rate = config.get("playback_rate", 16000)
        self.volume = config.get("volume", 60)
        self.bytes_per_second = self.rate * 2

        sched = config["schedule"]
        self.stop_time = parse_time(sched["stop_time"])

        self.cache_dir = Path(__file__).resolve().parent.parent / "cache"
        self.cache_dir.mkdir(exist_ok=True)

        self.cmd_queue: Queue = Queue()
        self._abort_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._thread: threading.Thread | None = None

        self.current_series = ""
        self.current_file = ""
        self.current_index = 0
        self.total_in_series = 0
        self.paused = False
        self.playing = False
        self.streaming = False

        self.audio_files_by_series: dict[str, list[Path]] = {}
        self.pcm_cache: dict[Path, bytes] = {}
        self.series_order: list[str] = []
        self.selected_series = config.get("selected_series", "")

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
            if "audio_player" not in cfg["plugins"]:
                cfg["plugins"]["audio_player"] = {}
            cfg["plugins"]["audio_player"]["selected_series"] = self.selected_series
            with open(path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            print(f"Audio player: failed to save config: {e}", flush=True)

    def scan_files(self):
        patterns = ["*.mp3", "*.MP3", "*.wav", "*.WAV"]
        self.audio_files_by_series = {}

        if not self.stories_root.exists():
            raise RuntimeError(f"Stories folder not found: {self.stories_root}")

        detected = []
        for item in sorted(self.stories_root.iterdir()):
            if item.is_dir() and not item.name.startswith(".") and item.name != "cache":
                if (item / "audio").is_dir() or (item / "index.md").exists():
                    detected.append(item.name)
        series = self.config.get("series") or detected
        if not series:
            raise RuntimeError(f"No story series found in {self.stories_root}")

        for series_name in series:
            series_dir = self.stories_root / series_name / "audio"
            if not series_dir.is_dir():
                continue
            files = []
            for p in patterns:
                files.extend(sorted(series_dir.glob(p), key=lambda f: f.name))
            if files:
                self.audio_files_by_series[series_name] = files

        if not self.audio_files_by_series:
            raise RuntimeError(f"No mp3/wav files found under {self.stories_root}")

        self.series_order = list(self.audio_files_by_series.keys())
        parts = ", ".join(f"{k}={len(v)}" for k, v in self.audio_files_by_series.items())
        total = sum(len(v) for v in self.audio_files_by_series.values())
        print(f"Audio player: found files — {parts}, total={total}")

    def build_cache(self):
        print("Audio player: building PCM cache...")
        self.pcm_cache = {}
        for series_name, files in self.audio_files_by_series.items():
            for fp in files:
                cache_path = self._get_cache_path(fp)
                if cache_path.exists() and fp.stat().st_mtime <= cache_path.stat().st_mtime:
                    pcm = cache_path.read_bytes()
                    if pcm:
                        self.pcm_cache[fp] = pcm
                        continue
                try:
                    pcm = convert_to_pcm(str(fp), self.rate)
                except (RuntimeError, FileNotFoundError) as e:
                    print(f"  SKIP {fp.name}: {e}", flush=True)
                    continue
                cache_path.write_bytes(pcm)
                self.pcm_cache[fp] = pcm

        if not self.pcm_cache:
            raise RuntimeError("No audio files could be converted")

        total_dur = sum(len(p) for p in self.pcm_cache.values()) / self.bytes_per_second
        print(f"Audio player: {len(self.pcm_cache)} files cached, {total_dur:.1f}s total")

    def _get_cache_path(self, file_path: Path) -> Path:
        rel = file_path.relative_to(self.stories_root)
        cache_dir = self.cache_dir / rel.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{file_path.stem}.pcm"

    def _process_commands(self):
        while True:
            try:
                cmd, arg = self.cmd_queue.get_nowait()
            except Empty:
                return

            if cmd == Command.PAUSE:
                self.paused = True
                self._resume_event.clear()
                self._emit_state()

            elif cmd == Command.RESUME:
                if self.paused:
                    self.paused = False
                    self._resume_event.set()
                    self._emit_state()

            elif cmd == Command.SET_VOLUME:
                self.volume = max(0, min(100, arg))
                self._emit_state()

            elif cmd == Command.STOP:
                self._abort_event.set()
                self._resume_event.set()
                self.playing = False

    def _emit_state(self):
        if self.controller:
            self.controller.emit("audio_player:state_changed", state=self.get_state())

    def get_state(self) -> dict:
        if self.paused:
            state = "paused"
        elif self.streaming:
            state = "playing"
        elif self.playing:
            state = "preparing"
        else:
            state = "idle"

        return {
            "state": state,
            "playing": self.playing,
            "paused": self.paused,
            "streaming": self.streaming,
            "current_series": self.current_series,
            "current_file": self.current_file,
            "current_index": self.current_index,
            "total_in_series": self.total_in_series,
            "volume": self.volume,
            "series_list": list(self.audio_files_by_series.keys()),
            "series_file_counts": {k: len(v) for k, v in self.audio_files_by_series.items()},
            "selected_series": self.selected_series,
            "past_stop_time": self._past_stop_time(),
            "stop_time": self.stop_time.strftime("%H:%M"),
            "current_series_files": [
                f.stem for f in self.audio_files_by_series.get(self.current_series, [])
            ] if self.current_series else [],
        }

    def _wait_while_paused(self):
        while self.playing and self.paused:
            self._process_commands()
            if not self.paused:
                break
            self._resume_event.wait(timeout=0.5)

    def _process_commands_loop(self, interval: float = 1.0, count: int = 1):
        for _ in range(count):
            if not self.playing:
                return False
            self._process_commands()
            if self.paused:
                self._wait_while_paused()
            time.sleep(interval)
        return self.playing

    def _play_loop(self):
        self.playing = True
        loop_num = 0

        while self.playing:
            self._process_commands()
            if self.paused:
                self._wait_while_paused()

            if not self.playing:
                break

            if self.selected_series:
                series_list = [self.selected_series]
            else:
                series_list = list(self.series_order)
                random.shuffle(series_list)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Loop {loop_num + 1} starting: {series_list}", flush=True)

            for series_name in series_list:
                if not self.playing:
                    break
                if not self._process_commands_loop(0, 0):
                    break

                files = self.audio_files_by_series[series_name]
                start_idx = 0

                for idx in range(start_idx, len(files)):
                    if not self.playing:
                        break
                    if not self._process_commands_loop(0, 0):
                        break

                    fp = files[idx]
                    if fp not in self.pcm_cache:
                        continue
                    pcm = self.pcm_cache[fp]

                    if self._past_stop_time():
                        self.playing = False
                        break

                    self.current_series = series_name
                    self.current_file = str(fp.relative_to(self.stories_root))
                    self.current_index = idx + 1
                    self.total_in_series = len(files)
                    self._emit_state()

                    dur = len(pcm) / self.bytes_per_second
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ({self.current_file}, {dur:.1f}s)", flush=True)

                    self._abort_event.clear()
                    self.streaming = True
                    self._emit_state()
                    camera = self.controller.camera
                    camera.play_pcm(pcm, self.rate, self.volume,
                                    abort_event=self._abort_event,
                                    tick_callback=self._process_commands)
                    self.streaming = False

                    if self._abort_event.is_set():
                        self._process_commands()
                        break

                    time.sleep(0.15)

            loop_num += 1

        self.playing = False
        self.paused = False
        self._emit_state()

    def start(self):
        super().start()
        print("Audio player: ready (idle)")

    def stop(self):
        self._abort_event.set()
        self._resume_event.set()
        self.playing = False
        if self._thread:
            self._thread.join(timeout=5)
        super().stop()
        print("Audio player: stopped")

    def start_playback(self):
        if self._thread and self._thread.is_alive():
            self.stop_playback()
            self._thread.join(timeout=3)
            self._thread = None
        while not self.cmd_queue.empty():
            try:
                self.cmd_queue.get_nowait()
            except Empty:
                break
        self._abort_event.clear()
        self._resume_event.set()
        self.current_series = ""
        self.current_file = ""
        self.current_index = 0
        self.total_in_series = 0
        self.paused = False
        self.streaming = False
        self._thread = threading.Thread(target=self._play_loop, daemon=True)
        self._thread.start()

    def stop_playback(self):
        self.cmd_queue.put((Command.STOP, None))

    def pause(self):
        self.cmd_queue.put((Command.PAUSE, None))

    def resume(self):
        self.cmd_queue.put((Command.RESUME, None))

    def set_volume(self, volume: int):
        self.cmd_queue.put((Command.SET_VOLUME, volume))

    def register_routes(self, app):
        from fastapi import Request

        @self.router.get("/status")
        async def status():
            return self.get_state()

        @self.router.post("/play")
        async def play_route():
            self.start_playback()
            return {"ok": True}

        @self.router.post("/stop")
        async def stop_route():
            self.stop_playback()
            return {"ok": True}

        @self.router.post("/volume")
        async def volume_route(request: Request):
            data = await request.json()
            self.set_volume(data.get("volume", self.volume))
            return {"ok": True}

        @self.router.post("/select_series")
        async def select_series_route(request: Request):
            data = await request.json()
            series = data.get("series", "")
            if not series or series in self.audio_files_by_series:
                self.selected_series = series
                self._save_config()
            return {"ok": True}

        @self.router.post("/stop_time")
        async def stop_time_route(request: Request):
            data = await request.json()
            time_str = data.get("time", "")
            if time_str:
                self.stop_time = parse_time(time_str)
            return {"ok": True}

        app.include_router(self.router)

    def ui_section(self) -> str:
        return """
        <div class="plugin-section" id="plugin-audio_player">
          <div class="yt-status-line" id="ap-status-line">Idle</div>

          <div class="volume-row">
            <span class="vol-label">Volume</span>
            <input type="range" class="vol-slider" id="ap-volume-slider" min="0" max="100"
                   oninput="apSetVolume(this.value)">
            <span class="vol-value" id="ap-volume-display">60</span>
          </div>

          <div class="auto-stop-row">
            <span class="auto-stop-label">Auto-stop</span>
            <span class="auto-stop-value" id="ap-stop-time-display" onclick="apEditStopTime()">--:--</span>
            <input type="time" id="ap-stop-time-input" style="display:none" onchange="apSaveStopTime()">
          </div>

          <details class="collapsible">
            <summary>Series</summary>
            <div class="series-pills" id="ap-series-pills"></div>
          </details>

          <div class="transport">
            <button class="transport-btn transport-play" onclick="apToggle()" id="ap-play-btn">&#x25B6;</button>
          </div>

          <div class="track-section" id="ap-track-section">
            <div id="ap-track-list" class="track-list"></div>
            <div class="np-progress" id="ap-progress" style="margin-top:8px">
              <div class="progress-track-bg">
                <div class="progress-track-fill" id="ap-progress-fill" style="width:0%"></div>
              </div>
              <span class="progress-text" id="ap-progress-text">0 / 0</span>
            </div>
          </div>
        </div>
        """

    def ui_js(self) -> str:
        return (
            "let apState = {};\n"
            "async function apFetch(url, body) {\n"
            "  try {\n"
            "    const opts = { method: 'POST' };\n"
            "    if (body) { opts.body = JSON.stringify(body); opts.headers = { 'Content-Type': 'application/json' }; }\n"
            "    await fetch(url, opts);\n"
            "  } catch(e) { console.error(e); }\n"
            "}\n"
            "function apToggle() {\n"
            "  apFetch('/api/audio_player/' + (apState.state === 'idle' ? 'play' : 'stop'));\n"
            "}\n"
            "async function apSetVolume(val) {\n"
            "  document.getElementById('ap-volume-display').textContent = val;\n"
            "  await apFetch('/api/audio_player/volume', { volume: parseInt(val) });\n"
            "}\n"
            "function apSelectSeries(name) { apFetch('/api/audio_player/select_series', { series: name }); }\n"
"function apEditStopTime() {\n"
"  var input = document.getElementById('ap-stop-time-input');\n"
"  if (input.showPicker) input.showPicker(); else input.click();\n"
"}\n"
"async function apSaveStopTime() {\n"
"  var val = document.getElementById('ap-stop-time-input').value;\n"
"  if (val) {\n"
"    document.getElementById('ap-stop-time-display').textContent = val;\n"
"    await apFetch('/api/audio_player/stop_time', { time: val });\n"
"  }\n"
"}\n"
            "async function apPoll() {\n"
            "  try {\n"
            "    const r = await fetch('/api/audio_player/status');\n"
            "    const s = await r.json();\n"
            "    apState = s;\n"
            "    var statusEl = document.getElementById('ap-status-line');\n"
            "    if (s.state === 'idle') {\n"
            "      statusEl.textContent = 'Idle';\n"
            "    } else {\n"
            "      var label = ({playing:'Playing', paused:'Paused', preparing:'Preparing...'})[s.state] || s.state;\n"
            "      statusEl.textContent = label + ': ' + (s.current_series || '?') + ' \\u00B7 ' + (s.current_index || 0) + '/' + (s.total_in_series || 0);\n"
            "    }\n"
            "    document.getElementById('ap-stop-time-display').textContent = s.stop_time || '--:--';\n"
            "    document.getElementById('ap-stop-time-input').value = s.stop_time || '--:--';\n"
            "    var playBtn = document.getElementById('ap-play-btn');\n"
            "    if (s.state === 'idle') {\n"
            "      playBtn.innerHTML = '\\u25B6';\n"
            "      playBtn.className = 'transport-btn transport-play';\n"
            "    } else {\n"
            "      playBtn.innerHTML = '\\u23F9';\n"
            "      playBtn.className = 'transport-btn transport-play stop-btn';\n"
            "    }\n"
            "    document.getElementById('ap-volume-slider').value = s.volume;\n"
            "    document.getElementById('ap-volume-display').textContent = s.volume;\n"
            "    var pills = document.getElementById('ap-series-pills');\n"
            "    if (s.series_list) {\n"
            "      var isPlaying = s.state !== 'idle';\n"
            "      pills.innerHTML = s.series_list.map(function(n) {\n"
            "        var active = isPlaying ? (n === s.current_series) : (n === s.selected_series);\n"
            "        var attrs = ' class=\"series-pill' + (active ? ' active' : '') + '\"';\n"
            "        if (isPlaying) attrs += ' disabled';\n"
            "        return '<button' + attrs + ' onclick=\"apSelectSeries(\\'' + n + '\\')\">' + n + '</button>';\n"
            "      }).join('');\n"
            "    }\n"
            "    var trackSection = document.getElementById('ap-track-section');\n"
            "    var trackList = document.getElementById('ap-track-list');\n"
            "    if (s.current_series_files && s.current_series_files.length > 0 && (s.state === 'playing' || s.state === 'paused')) {\n"
            "      trackSection.style.display = '';\n"
            "      var currentIdx = Math.max(0, (s.current_index || 1) - 1);\n"
            "      trackList.innerHTML = s.current_series_files.map(function(f, i) {\n"
            "        var isCurrent = (i === currentIdx);\n"
            "        var cls = isCurrent ? 'track-item current' : 'track-item upcoming';\n"
            "        var indicator = isCurrent ? '\\u25B6' : '';\n"
            "        return '<div class=\"' + cls + '\"><span class=\"track-indicator\">' + indicator + '</span><span class=\"track-title\">' + f + '</span></div>';\n"
            "      }).join('');\n"
            "      if (s.total_in_series > 0) {\n"
            "        document.getElementById('ap-progress').style.display = '';\n"
            "        var pct = Math.round((s.current_index / s.total_in_series) * 100);\n"
            "        document.getElementById('ap-progress-fill').style.width = Math.min(pct, 100) + '%';\n"
            "        document.getElementById('ap-progress-text').textContent = s.current_index + ' / ' + s.total_in_series;\n"
            "      }\n"
            "    } else {\n"
            "      trackSection.style.display = 'none';\n"
            "    }\n"
            "  } catch(e) { console.error('Audio player poll failed', e); }\n"
            "}\n"
            "setInterval(apPoll, 1500);\n"
            "apPoll();\n"
        )
