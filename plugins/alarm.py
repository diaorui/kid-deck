import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from camera_client import convert_to_pcm
from plugins import Plugin


class AlarmPlugin(Plugin):
    name = "alarm"
    title = "Alarm"
    icon = "⏰"
    order = 1

    def __init__(self, controller, config: dict):
        super().__init__(controller, config)

        self.alarms: list[dict] = list(config.get("alarms", []))
        self._ensure_ids()
        self._pcm_cache: dict[str, bytes] = {}
        self._triggered_today: set[str] = set()
        self._last_date = datetime.now().date()
        self._thread: threading.Thread | None = None
        self.volume = config.get("volume", 60)
        self._rate = 16000

        self._pcm_lock = threading.Lock()

    def _ensure_ids(self):
        for alarm in self.alarms:
            if "id" not in alarm or not alarm["id"]:
                alarm["id"] = uuid.uuid4().hex[:8]

    def start(self):
        super().start()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()
        print("Alarm: started")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        super().stop()
        print("Alarm: stopped")

    def _scheduler_loop(self):
        while self.running:
            now = datetime.now()

            if now.date() != self._last_date:
                self._triggered_today.clear()
                self._last_date = now.date()

            current_time = now.strftime("%H:%M")
            current_day = now.weekday()

            for alarm in self.alarms:
                if not alarm.get("enabled", False):
                    continue
                if alarm["id"] in self._triggered_today:
                    continue
                if alarm.get("time") != current_time:
                    continue
                days = alarm.get("days", [0, 1, 2, 3, 4, 5, 6])
                if current_day not in days:
                    continue

                self._trigger_alarm(alarm)

            time.sleep(30)

    def _trigger_alarm(self, alarm: dict):
        audio_path = alarm.get("audio", "")
        if not audio_path or not Path(audio_path).exists():
            print(f"Alarm '{alarm['id']}': audio file not found: {audio_path}", flush=True)
            return

        self._triggered_today.add(alarm["id"])
        print(f"Alarm '{alarm['id']}' triggered at {alarm['time']}", flush=True)

        self.controller.emit("alarm:triggered", alarm_id=alarm["id"])

        pcm = self._get_pcm(audio_path)
        if pcm:
            camera = self.controller.camera
            camera.play_pcm(pcm, rate=self._rate, volume=self.volume)

        self.controller.emit("alarm:finished", alarm_id=alarm["id"])
        print(f"Alarm '{alarm['id']}' finished", flush=True)

    def _get_pcm(self, audio_path: str) -> bytes | None:
        with self._pcm_lock:
            if audio_path in self._pcm_cache:
                return self._pcm_cache[audio_path]

        try:
            pcm = convert_to_pcm(audio_path, self._rate)
            with self._pcm_lock:
                self._pcm_cache[audio_path] = pcm
            print(f"Alarm: cached PCM for {audio_path}", flush=True)
            return pcm
        except (RuntimeError, FileNotFoundError) as e:
            print(f"Alarm: failed to convert {audio_path}: {e}", flush=True)
            return None

    def register_routes(self, app):
        from fastapi import Request

        @self.router.get("/list")
        async def list_alarms():
            return {"alarms": self.alarms, "volume": self.volume}

        @self.router.post("/add")
        async def add_alarm(request: Request):
            data = await request.json()
            alarm = {
                "id": uuid.uuid4().hex[:8],
                "time": data.get("time", "08:00"),
                "days": data.get("days", [0, 1, 2, 3, 4, 5, 6]),
                "audio": data.get("audio", ""),
                "label": data.get("label", "Alarm"),
                "enabled": data.get("enabled", True),
            }
            self.alarms.append(alarm)
            self._save_config()
            return {"ok": True, "alarm": alarm}

        @self.router.post("/update")
        async def update_alarm(request: Request):
            data = await request.json()
            alarm_id = data.get("id", "")
            for alarm in self.alarms:
                if alarm["id"] == alarm_id:
                    if "time" in data:
                        alarm["time"] = data["time"]
                    if "days" in data:
                        alarm["days"] = data["days"]
                    if "audio" in data:
                        alarm["audio"] = data["audio"]
                    if "label" in data:
                        alarm["label"] = data["label"]
                    if "enabled" in data:
                        alarm["enabled"] = data["enabled"]
                    self._save_config()
                    return {"ok": True, "alarm": alarm}
            return {"ok": False, "error": "alarm not found"}

        @self.router.post("/delete")
        async def delete_alarm(request: Request):
            data = await request.json()
            alarm_id = data.get("id", "")
            self.alarms = [a for a in self.alarms if a["id"] != alarm_id]
            self._save_config()
            return {"ok": True}

        @self.router.post("/toggle")
        async def toggle_alarm(request: Request):
            data = await request.json()
            alarm_id = data.get("id", "")
            for alarm in self.alarms:
                if alarm["id"] == alarm_id:
                    alarm["enabled"] = not alarm.get("enabled", True)
                    self._save_config()
                    return {"ok": True, "enabled": alarm["enabled"]}
            return {"ok": False, "error": "alarm not found"}

        @self.router.post("/volume")
        async def volume_route(request: Request):
            data = await request.json()
            self.volume = max(0, min(100, data.get("volume", self.volume)))
            return {"ok": True}

        app.include_router(self.router)

    def _save_config(self):
        path = Path(__file__).resolve().parent.parent / "config.yaml"
        try:
            import yaml
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            if "plugins" not in cfg:
                cfg["plugins"] = {}
            cfg["plugins"]["alarm"] = {"alarms": self.alarms}
            with open(path, "w") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            print(f"Alarm: failed to save config: {e}", flush=True)

    def ui_section(self) -> str:
        return """
        <div class="plugin-section" id="plugin-alarm">
          <div class="alarm-header">
            <h2>&#x23F0; Alarms</h2>
            <button class="add-btn" onclick="alarmShowForm()">+ Add</button>
          </div>
          <div id="alarm-list" class="alarm-list"></div>

          <div class="volume-row alarm-volume-row">
            <span class="vol-label">Alarm Vol</span>
            <input type="range" class="vol-slider" id="alarm-volume-slider" min="0" max="100"
                   oninput="alarmSetVolume(this.value)">
            <span class="vol-value" id="alarm-volume-display">60</span>
          </div>

          <div class="modal-overlay hidden" id="alarm-modal">
            <div class="modal">
              <h3 id="alarm-modal-title">Add Alarm</h3>
              <form onsubmit="alarmSave(event)" id="alarm-form">
                <input type="hidden" id="alarm-edit-id" value="">
                <div class="form-row">
                  <label>Time</label>
                  <input type="time" id="alarm-form-time" required>
                </div>
                <div class="form-row">
                  <label>Days</label>
                  <div class="day-picker" id="alarm-form-days">
                    <label class="day-btn"><input type="checkbox" value="0">Sun</label>
                    <label class="day-btn"><input type="checkbox" value="1" checked>Mon</label>
                    <label class="day-btn"><input type="checkbox" value="2" checked>Tue</label>
                    <label class="day-btn"><input type="checkbox" value="3" checked>Wed</label>
                    <label class="day-btn"><input type="checkbox" value="4" checked>Thu</label>
                    <label class="day-btn"><input type="checkbox" value="5" checked>Fri</label>
                    <label class="day-btn"><input type="checkbox" value="6">Sat</label>
                  </div>
                </div>
                <div class="form-row">
                  <label>Label</label>
                  <input type="text" id="alarm-form-label" placeholder="Alarm name" required>
                </div>
                <div class="form-row">
                  <label>Audio file</label>
                  <input type="text" id="alarm-form-audio" placeholder="/path/to/file.wav" required>
                </div>
                <div class="form-actions">
                  <button type="button" class="cancel-btn" onclick="alarmHideForm()">Cancel</button>
                  <button type="submit" class="save-btn">Save</button>
                </div>
              </form>
            </div>
          </div>
        </div>
        """

    def ui_js(self) -> str:
        return """
        let alarmsData = [];

        async function alarmPoll() {
            try {
                const r = await fetch('/api/alarm/list');
                const data = await r.json();
                alarmsData = data.alarms || [];
                alarmRenderList();
                if (data.volume !== undefined) {
                    document.getElementById('alarm-volume-slider').value = data.volume;
                    document.getElementById('alarm-volume-display').textContent = data.volume;
                }
            } catch(e) { console.error('Alarm poll failed', e); }
        }

        async function alarmSetVolume(val) {
            document.getElementById('alarm-volume-display').textContent = val;
            await fetch('/api/alarm/volume', { method:'POST', body:JSON.stringify({volume:parseInt(val)}), headers:{'Content-Type':'application/json'} });
        }

        function alarmRenderList() {
            var container = document.getElementById('alarm-list');
            if (!alarmsData.length) {
                container.innerHTML = '<div class="empty-state">No alarms yet. Tap + to add one.</div>';
                return;
            }
            var dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
            var html = '';
            for (var i = 0; i < alarmsData.length; i++) {
                var a = alarmsData[i];
                var daysHtml = '';
                for (var d = 0; d < 7; d++) {
                    var active = (a.days || []).indexOf(d) !== -1;
                    daysHtml += '<span class="alarm-day' + (active ? ' active' : '') + '">' + dayNames[d][0] + '</span>';
                }
                html += '<div class="alarm-item' + (a.enabled ? '' : ' disabled') + '" onclick="alarmEdit(\\'' + a.id + '\\')">' +
                    '<div class="alarm-left">' +
                    '<div class="alarm-time-row">' +
                    '<span class="alarm-time">' + (a.time || '--:--') + '</span>' +
                    '</div>' +
                    '<div class="alarm-days-row">' + daysHtml + '</div>' +
                    '<div class="alarm-label">' + (a.label || '') + '</div>' +
                    '</div>' +
                    '<div class="alarm-right">' +
                    '<label class="switch" onclick="event.stopPropagation()">' +
                    '<input type="checkbox" ' + (a.enabled ? 'checked' : '') + ' onchange="alarmToggle(\\'' + a.id + '\\')">' +
                    '<span class="track"><span class="thumb"></span></span>' +
                    '</label>' +
                    '<button class="alarm-meta-btn danger" onclick="event.stopPropagation(); alarmDelete(\\'' + a.id + '\\')">&times;</button>' +
                    '</div></div>';
            }
            container.innerHTML = html;
        }

        async function alarmToggle(id) {
            await fetch('/api/alarm/toggle', { method:'POST', body:JSON.stringify({id}), headers:{'Content-Type':'application/json'} });
            alarmPoll();
        }

        async function alarmDelete(id) {
            if (!confirm('Delete this alarm?')) return;
            await fetch('/api/alarm/delete', { method:'POST', body:JSON.stringify({id}), headers:{'Content-Type':'application/json'} });
            alarmPoll();
        }

        function alarmShowForm(alarm) {
            document.getElementById('alarm-modal').classList.remove('hidden');
            if (alarm) {
                document.getElementById('alarm-modal-title').textContent = 'Edit Alarm';
                document.getElementById('alarm-edit-id').value = alarm.id;
                document.getElementById('alarm-form-time').value = alarm.time || '08:00';
                document.getElementById('alarm-form-label').value = alarm.label || '';
                document.getElementById('alarm-form-audio').value = alarm.audio || '';
                const days = alarm.days || [];
                document.querySelectorAll('#alarm-form-days input[type=checkbox]').forEach(cb => {
                    cb.checked = days.includes(parseInt(cb.value));
                });
            } else {
                document.getElementById('alarm-modal-title').textContent = 'Add Alarm';
                document.getElementById('alarm-edit-id').value = '';
                document.getElementById('alarm-form-time').value = '08:00';
                document.getElementById('alarm-form-label').value = '';
                document.getElementById('alarm-form-audio').value = '';
            }
        }

        function alarmHideForm() {
            document.getElementById('alarm-modal').classList.add('hidden');
        }

        function alarmEdit(id) {
            const alarm = alarmsData.find(a => a.id === id);
            if (alarm) alarmShowForm(alarm);
        }

        async function alarmSave(event) {
            event.preventDefault();
            const editId = document.getElementById('alarm-edit-id').value;
            const days = [];
            document.querySelectorAll('#alarm-form-days input[type=checkbox]:checked').forEach(cb => {
                days.push(parseInt(cb.value));
            });
            const data = {
                time: document.getElementById('alarm-form-time').value,
                days: days,
                label: document.getElementById('alarm-form-label').value,
                audio: document.getElementById('alarm-form-audio').value,
                enabled: true,
            };
            const url = editId ? '/api/alarm/update' : '/api/alarm/add';
            if (editId) data.id = editId;
            await fetch(url, { method:'POST', body:JSON.stringify(data), headers:{'Content-Type':'application/json'} });
            alarmHideForm();
            alarmPoll();
        }

        setInterval(alarmPoll, 3000);
        alarmPoll();
        """
