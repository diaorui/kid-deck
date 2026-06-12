import shutil
import socket
import subprocess
import time

import paramiko


def check_deps():
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH. Install ffmpeg first.")
        raise SystemExit(1)

    if shutil.which("play") is None:
        print("SoX 'play' command not found on camera side (expected).")
        print("Will stream PCM and assume 'play' exists on camera.")


def convert_to_pcm(file_path: str, rate: int = 16000) -> bytes:
    """Convert audio file to 16-bit mono PCM s16le via ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(file_path),
         "-f", "s16le", "-ar", str(rate), "-ac", "1", "-"],
        capture_output=True, timeout=300)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace")[:300]
        raise RuntimeError(f"ffmpeg failed for {file_path}: {err}")
    return proc.stdout


class CameraClient:
    """Shared SSH connection to Thingino camera for PCM audio streaming."""

    def __init__(self, config: dict):
        self.host = config["host"]
        self.user = config["user"]
        self.password = config["password"]
        self._client: paramiko.SSHClient | None = None
        self._lock = False

    def connect(self):
        self._ensure_conn()

    def _ensure_conn(self) -> paramiko.SSHClient:
        if self._client is not None:
            try:
                stdin, stdout, stderr = self._client.exec_command("echo ok", timeout=2)
                if stdout.read().decode().strip() == "ok":
                    return self._client
            except Exception:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print(f"Connecting to camera {self.host}...", flush=True)
        client.connect(self.host, username=self.user, password=self.password, timeout=10)
        self._client = client
        return client

    def play_pcm(self, pcm_data: bytes, rate: int = 16000, volume: int = 60,
                  abort_event=None, tick_callback=None) -> bool:
        """Stream PCM audio to camera. Returns False if aborted early."""
        client = self._ensure_conn()
        accumulated = 0.0
        playback_start = 0.0
        buf = pcm_data
        bytes_per_second = rate * 2

        while len(buf) >= bytes_per_second:
            if abort_event and abort_event.is_set():
                return False
            if tick_callback:
                tick_callback()
            if abort_event and abort_event.is_set():
                return False

            chunk = buf[:bytes_per_second]
            buf = buf[bytes_per_second:]

            if playback_start > 0:
                elapsed = time.time() - playback_start
                remaining = accumulated - elapsed
                if remaining > 1.0:
                    time.sleep(remaining - 1.0)

            stdin, stdout, stderr = client.exec_command(
                f"play -s -A -f pcm -r {rate} -v {volume} -")
            stdin.write(chunk)
            stdin.flush()
            stdin.close()
            stdout.channel.recv_exit_status()

            if playback_start == 0:
                playback_start = time.time()
            accumulated += 1.0

        if buf:
            if abort_event and abort_event.is_set():
                return False
            if tick_callback:
                tick_callback()
            if abort_event and abort_event.is_set():
                return False

            if playback_start > 0:
                elapsed = time.time() - playback_start
                remaining = accumulated - elapsed
                if remaining > 1.0:
                    time.sleep(remaining - 1.0)

            stdin, stdout, stderr = client.exec_command(
                f"play -s -A -f pcm -r {rate} -v {volume} -")
            stdin.write(buf)
            stdin.flush()
            stdin.close()
            stdout.channel.recv_exit_status()

        return True

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        try:
            stdin, stdout, stderr = self._client.exec_command("echo ok", timeout=2)
            return stdout.read().decode().strip() == "ok"
        except Exception:
            return False

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
