# KidDeck

Plugin-based controller for kids' media. Audio stories, mixed video/podcast streaming, alarms — all from your phone browser.

## Features

- **Audio Player** — Stream PCM audio to the camera via SSH. Plays story series (shuffled or selected). Volume control, auto-stop schedule.
- **Alarm** — Schedule alarms that play audio at set times, with day-of-week selection. Android-style toggle switch UI.
- **Stream** — Mixed YouTube video + RSS podcast playback on Chromecast. Smart playlist interleaves videos and audio by screen-time ratio, volume normalization with ffmpeg gain, cover art metadata, auto-advance, auto-stop at bedtime.

## Requirements

- Python 3.10+
- `ffmpeg` on PATH (audio-to-PCM conversion)
- Thingino IP camera with `play` (SoX) via SSH — for Audio Player and Alarm
- Chromecast/Google TV on the same network — for Stream tab
- [Deno](https://deno.land) — for reliable YouTube video extraction (`curl -fsSL https://deno.land/install.sh | sh`)

## Setup

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Edit config.yaml with your camera IP, SSH password, and story paths
```

### config.yaml

| Section | Key | Description |
|---------|-----|-------------|
| `camera` | `host` | Thingino camera IP address |
| `camera` | `password` | Camera SSH password |
| `camera` | `user` | Camera SSH user (default: `root`) |
| `audio_player` | `folder` | Path to story series directories |
| `audio_player.schedule` | `stop_time` | Auto-stop time (24h format, e.g. `23:00`) |
| `stream` | `screen_minutes_per_hour` | Target screen time per hour (1–60, default `12`) |
| `stream` | `max_video_minutes` | Drop videos longer than this (default `30`) |
| `stream` | `max_audio_minutes` | Drop podcasts longer than this (default `30`) |
| `stream` | `playlist_horizon_hours` | Playlist duration budget (default `3.5`) |
| `stream` | `channel_enabled` | YouTube channel handle → enabled (true/false) |
| `stream` | `feeds` | Feed name → `{url, enabled}` |
| `stream.schedule` | `stop_time` | Auto-stop bedtime (24h, e.g. `21:00`) |
| `stream` | `outro_video_url` | YouTube URL played at stop time before exiting |

## Usage

```bash
python controller.py config.yaml
```

Open `http://<device-ip>:8080` on a phone browser on the same WiFi.

### Story Directory Structure

The audio player expects story series organized as:

```
stories/
  series_a/
    audio/
      001_story.wav
      002_story.wav
      ...
    index.md
  series_b/
    audio/
      ...
```

Each series is a subdirectory with an `audio/` folder containing `.wav` or `.mp3` files.

## Plugin Architecture

Each plugin is a self-contained `.py` file in `plugins/` that exports a `Plugin` subclass. Plugins are auto-discovered at startup and provide:

- `ui_section()` — Returns HTML for the plugin's UI panel
- `ui_js()` — Returns JavaScript for the plugin's interactive behavior
- `register_routes()` — Registers FastAPI routes under `/api/<plugin_name>/`

Built-in plugins:
- `audio_player.py` (order 0) — Audio streaming and series playback
- `alarm.py` (order 1) — Scheduled alarms with volume control
- `stream.py` (order 4) — Mixed YouTube + podcast Chromecast streaming

## Restart

Tap the "Restart" button in the UI or send a POST to `/api/restart`. The controller restarts in-place via `os.execv()` without needing a wrapper script.

## License

MIT
