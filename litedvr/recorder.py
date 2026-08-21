from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import re
import signal
from urllib.parse import quote, urlsplit, urlunsplit

from .config import Config

LOG = logging.getLogger(__name__)
SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")
RTSP_CREDENTIALS = re.compile(r"(rtsp(?:s)?://)[^@\s]+@", re.IGNORECASE)

def safe_part(value: str) -> str:
    return SAFE_PART.sub("-", value.strip()).strip(".-")[:80] or "unnamed"

def redact_rtsp_credentials(value: str) -> str:
    return RTSP_CREDENTIALS.sub(r"\1***@", value)

def rtsp_with_credentials(url: str, username: str | None, password: str | None) -> str:
    """Build an FFmpeg-only URL; callers must never log this value."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("rtsp", "rtsps") or not parsed.hostname:
        raise ValueError("rtsp_url must be an rtsp:// or rtsps:// URL")
    if parsed.username is not None or not username:
        return url
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    auth = quote(username, safe="") + (":" + quote(password, safe="") if password is not None else "")
    return urlunsplit((parsed.scheme, auth + "@" + host, parsed.path, parsed.query, ""))

@dataclass
class Monitor:
    id: int
    group_name: str
    name: str
    rtsp_url: str
    username: str | None
    password: str | None
    enabled: bool
    recording_enabled: bool
    segment_minutes: int

class Recorder:
    """Supervises one FFmpeg packet-copy process per monitor."""
    def __init__(self, db, config: Config, monitor: Monitor):
        self.db, self.config, self.monitor = db, config, monitor
        self.status = "DISABLED" if not monitor.enabled else "OFFLINE"
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._process: asyncio.subprocess.Process | None = None
        self._live_clients: set = set()
        self._live_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"recorder-{self.monitor.id}")

    async def stop(self) -> None:
        self._stopping = True
        await self._stop_process()
        if self._task:
            await self._task

    async def _stop_process(self) -> None:
        process = self._process
        if not process or process.returncode is not None:
            return
        try:
            if os.name == "posix" and process.pid:
                # FFmpeg owns the process group; stop all of its descendants.
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                if os.name == "posix" and process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, OSError):
                pass
            await process.wait()
        except (ProcessLookupError, OSError):
            pass

    async def live_subscribe(self, websocket) -> None:
        self._live_clients.add(websocket)

    async def live_unsubscribe(self, websocket) -> None:
        self._live_clients.discard(websocket)

    async def _pump_live(self, stdout) -> None:
        buffer = b""
        while True:
            chunk = await stdout.read(65536)
            if not chunk:
                return
            buffer += chunk
            while b"\xff\xd8" in buffer and b"\xff\xd9" in buffer:
                start = buffer.index(b"\xff\xd8")
                end = buffer.index(b"\xff\xd9", start) + 2
                frame, buffer = buffer[start:end], buffer[end:]
                for websocket in list(self._live_clients):
                    try:
                        await websocket.send_bytes(frame)
                    except (ConnectionResetError, RuntimeError):
                        self._live_clients.discard(websocket)

    def _path(self, started: datetime) -> Path:
        return self.config.recordings_path / safe_part(self.monitor.group_name) / safe_part(self.monitor.name) / started.strftime("%Y/%m/%d/%H-%M-%S.mp4")

    async def _record_start(self, started: datetime, path: Path) -> int:
        cursor = await self.db.execute(
            "INSERT INTO recordings (monitor_id,monitor_name,group_name,start_time,file_path,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (self.monitor.id, self.monitor.name, self.monitor.group_name, started.isoformat(), str(path), "RECORDING", started.isoformat()))
        await self.db.commit()
        return cursor.lastrowid

    async def _record_finish(self, record_id: int, started: datetime, path: Path, complete: bool) -> None:
        ended = datetime.now(UTC)
        size = path.stat().st_size if path.exists() else 0
        status = "COMPLETE" if complete else ("INTERRUPTED" if size else "ERROR")
        await self.db.execute("UPDATE recordings SET end_time=?,duration=?,file_size=?,status=? WHERE id=?",
            (ended.isoformat(), (ended - started).total_seconds(), size, status, record_id))
        await self.db.commit()

    def _command(self, output: Path, include_audio: bool = True, duration_seconds: float | None = None) -> list[str]:
        if self.config.mock_mode:
            return [self.config.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=1", "-t", "1", "-c:v", "mpeg4", "-y", str(output)]
        source = rtsp_with_credentials(self.monitor.rtsp_url, self.monitor.username, self.monitor.password)
        command = [self.config.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-err_detect", "ignore_err", "-fflags", "+discardcorrupt", "-rtsp_transport", "tcp",
                "-i", source, "-map", "0:v:0"]
        if include_audio:
            command.extend(["-map", "0:a?"])
        duration = duration_seconds or self.monitor.segment_minutes * 60
        return command + ["-c", "copy", "-t",
                str(max(1, duration)), "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof+separate_moof",
                "-frag_duration", "1000000", "-flush_packets", "1", "-y", str(output),
                "-map", "0:v:0", "-an", "-vf", "fps=5", "-c:v", "mjpeg", "-q:v", "6",
                "-f", "mjpeg", "pipe:1"]

    async def _run(self) -> None:
        delay = 5
        while not self._stopping:
            if not (self.monitor.enabled and self.monitor.recording_enabled):
                self.status = "DISABLED"
                await asyncio.sleep(5)
                continue
            started, path, record_id = datetime.now(UTC), None, None
            # End every file on a fixed UTC wall-clock boundary: 00, 03, 06,
            # 09, 12, 15, 18, or 21 hours. This prevents segment drift after
            # restarts and makes the 24-hour/3-hour timeline deterministic.
            day_start = started.replace(hour=0, minute=0, second=0, microsecond=0)
            slot = int((started - day_start).total_seconds() // 10800) + 1
            segment_end = day_start + timedelta(seconds=slot * 10800)
            segment_duration = max(1, (segment_end - started).total_seconds())
            try:
                self.status = "CONNECTING"
                path = self._path(started)
                path.parent.mkdir(parents=True, exist_ok=True)
                record_id = await self._record_start(started, path)
                LOG.info("FFmpeg started for monitor %s", self.monitor.id)
                spawn_options = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
                if os.name == "posix":
                    spawn_options["start_new_session"] = True
                # Start video-only by default. Some cameras expose an audio
                # codec that cannot be packet-copied into MP4; attempting it
                # first causes a restart and a visible recording gap.
                self._process = await asyncio.create_subprocess_exec(
                    *self._command(path, include_audio=False, duration_seconds=segment_duration), **spawn_options)
                self.status = "RECORDING"
                process_started = datetime.now(UTC)
                self._live_task = asyncio.create_task(self._pump_live(self._process.stdout))
                stderr_task = asyncio.create_task(self._process.stderr.read())
                await self._process.wait()
                if self._live_task:
                    await self._live_task
                stderr = await stderr_task
                if self._process.returncode and b"not currently supported in container" in stderr:
                    LOG.warning("Monitor %s audio is incompatible with MP4 packet copy; retrying video-only", self.monitor.id)
                    self._process = await asyncio.create_subprocess_exec(
                        *self._command(path, include_audio=False, duration_seconds=segment_duration), **spawn_options)
                    self._live_task = asyncio.create_task(self._pump_live(self._process.stdout))
                    stderr_task = asyncio.create_task(self._process.stderr.read())
                    await self._process.wait()
                    await self._live_task
                    stderr = await stderr_task
                okay = self._process.returncode == 0
                elapsed = (datetime.now(UTC) - process_started).total_seconds()
                diagnostic = redact_rtsp_credentials(stderr.decode(errors="replace")[-2000:]).strip()
                LOG.info("FFmpeg exited for monitor %s: code=%s elapsed=%.1fs", self.monitor.id, self._process.returncode, elapsed)
                if diagnostic:
                    LOG.warning("FFmpeg diagnostic for monitor %s: %s", self.monitor.id, diagnostic)
                await self._record_finish(record_id, started, path, okay)
                if okay:
                    delay = 5
                    continue
                LOG.warning("FFmpeg stopped for monitor %s: %s", self.monitor.id,
                            redact_rtsp_credentials(stderr.decode(errors="replace")[-500:]))
            except Exception:
                LOG.exception("Recorder failure for monitor %s", self.monitor.id)
                if path and record_id is not None:
                    await self._record_finish(record_id, started, path, False)
            finally:
                if self._live_task and not self._live_task.done():
                    self._live_task.cancel()
                self._live_task = None
                self._process = None
            self.status = "OFFLINE"
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

class RecorderManager:
    """Loads enabled monitors once at service startup and owns their recorders."""
    def __init__(self, db, config: Config):
        self.db, self.config = db, config
        self.recorders: dict[int, Recorder] = {}

    async def start(self) -> None:
        rows = await (await self.db.execute(
            """SELECT m.*, COALESCE(g.name, 'Ungrouped') AS group_name
               FROM monitors AS m LEFT JOIN monitor_groups AS g ON g.id=m.group_id
               WHERE m.enabled=1 AND m.recording_enabled=1""")).fetchall()
        for row in rows:
            monitor = Monitor(id=row["id"], group_name=row["group_name"], name=row["name"],
                rtsp_url=row["rtsp_url"], username=row["username"], password=row["password"],
                enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
                segment_minutes=row["segment_minutes"])
            recorder = Recorder(self.db, self.config, monitor)
            recorder.start()
            self.recorders[monitor.id] = recorder
        LOG.info("Started %s recorder(s)", len(self.recorders))

    async def stop(self) -> None:
        await asyncio.gather(*(recorder.stop() for recorder in self.recorders.values()), return_exceptions=True)
        self.recorders.clear()

    async def reload(self) -> None:
        await self.stop()
        await self.start()

    def status_for(self, monitor_id: int) -> str:
        recorder = self.recorders.get(monitor_id)
        if not recorder:
            return "DISABLED"
        if recorder._process and recorder._process.returncode is None:
            return "RECORDING"
        return recorder.status
