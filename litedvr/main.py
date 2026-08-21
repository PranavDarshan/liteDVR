from __future__ import annotations
import argparse
import asyncio
import json
import logging
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from aiohttp import web
from .config import load_config
from .database import open_database
from .recorder import RecorderManager, rtsp_with_credentials

STARTED = time.monotonic()

class LiveStreamManager:
    """Routes live clients to the recorder's single RTSP/FFmpeg pipeline."""
    def __init__(self, recorder_manager):
        self.recorder_manager = recorder_manager

    async def subscribe(self, monitor_id: int, websocket: web.WebSocketResponse) -> None:
        recorder = self.recorder_manager.recorders.get(monitor_id)
        if not recorder:
            raise web.HTTPServiceUnavailable(text="camera recorder is not running")
        await recorder.live_subscribe(websocket)

    async def unsubscribe(self, monitor_id: int, websocket: web.WebSocketResponse) -> None:
        recorder = self.recorder_manager.recorders.get(monitor_id)
        if recorder:
            await recorder.live_unsubscribe(websocket)

    async def stop_all(self) -> None:
        for recorder in self.recorder_manager.recorders.values():
            for websocket in list(recorder._live_clients):
                await websocket.close()
            recorder._live_clients.clear()

def monitor_json(row, status: str) -> dict:
    return {"id": row["id"], "group_id": row["group_id"], "group_name": row["group_name"],
        "name": row["name"], "rtsp_url": row["rtsp_url"], "enabled": bool(row["enabled"]),
        "recording_enabled": bool(row["recording_enabled"]), "segment_minutes": row["segment_minutes"],
        "status": status, "password_set": bool(row["password"]), "created_at": row["created_at"],
        "updated_at": row["updated_at"]}

async def monitor_rows(request: web.Request, monitor_id: int | None = None):
    query = """SELECT m.*, COALESCE(g.name, 'Ungrouped') AS group_name
        FROM monitors m LEFT JOIN monitor_groups g ON g.id=m.group_id"""
    params = ()
    if monitor_id is not None:
        query += " WHERE m.id=?"
        params = (monitor_id,)
    return await (await request.app["db"].execute(query, params)).fetchall()

def require_object(payload):
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="body must be a JSON object")
    return payload

def clean_name(value, field: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value.strip()) <= 120):
        raise web.HTTPBadRequest(text=f"{field} must be 1 to 120 characters")
    return value.strip()

async def read_json(request: web.Request) -> dict:
    try:
        return require_object(await request.json())
    except web.HTTPException:
        raise
    except Exception as exc:
        raise web.HTTPBadRequest(text="body must be valid JSON") from exc

async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

async def system_status(request: web.Request) -> web.Response:
    cfg, db = request.app["config"], request.app["db"]
    row = await (await db.execute("SELECT COUNT(*) AS monitors, SUM(recording_enabled=1) AS active FROM monitors")).fetchone()
    disk = shutil.disk_usage(cfg.recordings_path)
    return web.json_response({"uptime_seconds": round(time.monotonic() - STARTED), "monitors": row["monitors"],
        "active_recordings": row["active"] or 0, "disk": {"used": disk.used, "free": disk.free, "total": disk.total},
        "time": datetime.now(UTC).isoformat()})

async def get_settings(request: web.Request) -> web.Response:
    rows = await (await request.app["db"].execute(
        "SELECT key, value FROM settings WHERE key IN ('retention_days', 'default_segment_minutes')")).fetchall()
    values = {row["key"]: int(row["value"]) for row in rows}
    cfg = request.app["config"]
    return web.json_response({
        "retention_days": values.get("retention_days", cfg.retention_days),
        "default_segment_minutes": 180,
        "recordings_path": str(cfg.recordings_path),
        "allowed_origins": list(cfg.allowed_origins),
        "restart_required_fields": ["allowed_origins", "bind_address", "port", "ffmpeg_path"],
    })

async def update_settings(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="body must be valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) - {"retention_days", "default_segment_minutes", "recordings_path"}:
        raise web.HTTPBadRequest(text="only retention_days, default_segment_minutes, and recordings_path can be changed here")
    if "retention_days" in payload and payload["retention_days"] not in (30, 60, 90):
        raise web.HTTPBadRequest(text="retention_days must be 30, 60, or 90")
    if "default_segment_minutes" in payload:
        payload["default_segment_minutes"] = 180
    if "recordings_path" in payload:
        if not isinstance(payload["recordings_path"], str) or not payload["recordings_path"].strip():
            raise web.HTTPBadRequest(text="recordings_path must be a non-empty path")
        new_path = Path(payload["recordings_path"]).expanduser().resolve()
        try:
            new_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise web.HTTPBadRequest(text="recordings_path cannot be created") from exc
        object.__setattr__(request.app["config"], "recordings_path", new_path)
    db = request.app["db"]
    for key, value in payload.items():
        if key == "recordings_path":
            value = str(request.app["config"].recordings_path)
        await db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))
    await db.commit()
    return await get_settings(request)

async def list_groups(request: web.Request) -> web.Response:
    rows = await (await request.app["db"].execute(
        """SELECT g.id,g.name,g.created_at,COUNT(m.id) AS monitor_count
           FROM monitor_groups g LEFT JOIN monitors m ON m.group_id=g.id
           GROUP BY g.id ORDER BY g.name COLLATE NOCASE""")).fetchall()
    return web.json_response({"items": [dict(row) for row in rows]})

async def create_group(request: web.Request) -> web.Response:
    payload = await read_json(request)
    name = clean_name(payload.get("name"), "name")
    now = datetime.now(UTC).isoformat()
    try:
        cursor = await request.app["db"].execute("INSERT INTO monitor_groups(name,created_at) VALUES (?,?)", (name, now))
        await request.app["db"].commit()
    except Exception as exc:
        raise web.HTTPConflict(text="group name already exists") from exc
    return web.json_response({"id": cursor.lastrowid, "name": name, "created_at": now, "monitor_count": 0}, status=201)

async def update_group(request: web.Request) -> web.Response:
    payload = await read_json(request)
    name = clean_name(payload.get("name"), "name")
    result = await request.app["db"].execute("UPDATE monitor_groups SET name=? WHERE id=?", (name, request.match_info["id"]))
    await request.app["db"].commit()
    if result.rowcount != 1:
        raise web.HTTPNotFound()
    await request.app["recorder_manager"].reload()
    return web.json_response({"id": int(request.match_info["id"]), "name": name})

async def delete_group(request: web.Request) -> web.Response:
    group_id = request.match_info["id"]
    if request.query.get("cascade") == "1":
        await request.app["db"].execute(
            """UPDATE recordings SET monitor_name=COALESCE(monitor_name,m.name), group_name=COALESCE(group_name,g.name)
               FROM monitors m JOIN monitor_groups g ON g.id=m.group_id
               WHERE recordings.monitor_id=m.id AND g.id=?""", (group_id,))
        await request.app["db"].execute("DELETE FROM monitors WHERE group_id=?", (group_id,))
        result = await request.app["db"].execute("DELETE FROM monitor_groups WHERE id=?", (group_id,))
        await request.app["db"].commit()
        if result.rowcount != 1:
            raise web.HTTPNotFound()
        await request.app["recorder_manager"].reload()
        return web.Response(status=204)
    count = await (await request.app["db"].execute("SELECT COUNT(*) AS n FROM monitors WHERE group_id=?", (group_id,))).fetchone()
    if count["n"]:
        raise web.HTTPConflict(text="cannot delete a group containing monitors")
    result = await request.app["db"].execute("DELETE FROM monitor_groups WHERE id=?", (group_id,))
    await request.app["db"].commit()
    if result.rowcount != 1:
        raise web.HTTPNotFound()
    return web.Response(status=204)

async def list_monitors(request: web.Request) -> web.Response:
    rows = await monitor_rows(request)
    manager = request.app["recorder_manager"]
    return web.json_response({"items": [monitor_json(row, manager.status_for(row["id"])) for row in rows]})

async def create_monitor(request: web.Request) -> web.Response:
    payload = await read_json(request)
    required = ("name", "group_id", "rtsp_url")
    if any(key not in payload for key in required):
        raise web.HTTPBadRequest(text="name, group_id, and rtsp_url are required")
    name = clean_name(payload["name"], "name")
    if not isinstance(payload["group_id"], int):
        raise web.HTTPBadRequest(text="group_id must be an integer")
    if not isinstance(payload["rtsp_url"], str) or not payload["rtsp_url"].startswith(("rtsp://", "rtsps://")):
        raise web.HTTPBadRequest(text="rtsp_url must begin with rtsp:// or rtsps://")
    segment = 180
    enabled = int(bool(payload.get("enabled", True)))
    recording_enabled = int(bool(payload.get("recording_enabled", True)))
    username, password = payload.get("username"), payload.get("password")
    if username is not None and not isinstance(username, str):
        raise web.HTTPBadRequest(text="username must be a string")
    if password is not None and not isinstance(password, str):
        raise web.HTTPBadRequest(text="password must be a string")
    now = datetime.now(UTC).isoformat()
    try:
        cursor = await request.app["db"].execute(
            """INSERT INTO monitors(group_id,name,rtsp_url,username,password,enabled,recording_enabled,segment_minutes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (payload["group_id"], name, payload["rtsp_url"], username, password, enabled, recording_enabled, segment, now, now))
        await request.app["db"].commit()
    except Exception as exc:
        raise web.HTTPBadRequest(text="group_id is invalid") from exc
    await request.app["recorder_manager"].reload()
    row = (await monitor_rows(request, cursor.lastrowid))[0]
    return web.json_response(monitor_json(row, request.app["recorder_manager"].status_for(row["id"])), status=201)

async def delete_monitor(request: web.Request) -> web.Response:
    result = await request.app["db"].execute("DELETE FROM monitors WHERE id=?", (request.match_info["id"],))
    await request.app["db"].commit()
    if result.rowcount != 1:
        raise web.HTTPNotFound()
    await request.app["recorder_manager"].reload()
    return web.Response(status=204)

async def live_monitor_websocket(request: web.Request) -> web.WebSocketResponse:
    websocket = web.WebSocketResponse(heartbeat=30)
    await websocket.prepare(request)
    monitor_id = int(request.match_info["id"])
    await request.app["live_manager"].subscribe(monitor_id, websocket)
    try:
        async for message in websocket:
            if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    finally:
        await request.app["live_manager"].unsubscribe(monitor_id, websocket)
    return websocket

def recording_json(row) -> dict:
    file_size = row["file_size"]
    if file_size is None:
        try:
            file_size = Path(row["file_path"]).stat().st_size
        except OSError:
            file_size = 0
    return {"id": row["id"], "monitor_id": row["monitor_id"], "monitor_name": row["monitor_name"],
        "group_id": row["group_id"], "group_name": row["group_name"], "start_time": row["start_time"],
        "end_time": row["end_time"], "duration": row["duration"], "file_size": file_size,
        "status": row["status"], "created_at": row["created_at"]}

async def recording_row(request: web.Request, recording_id: str):
    row = await (await request.app["db"].execute(
          """SELECT r.*,COALESCE(m.name,r.monitor_name,'Removed camera') AS monitor_name,
              COALESCE(m.group_id,0) AS group_id,COALESCE(g.name,r.group_name,'Removed group') AS group_name
              FROM recordings r LEFT JOIN monitors m ON m.id=r.monitor_id
              LEFT JOIN monitor_groups g ON g.id=m.group_id WHERE r.id=?""", (recording_id,))).fetchone()
    if not row:
        raise web.HTTPNotFound()
    return row

async def list_recordings(request: web.Request) -> web.Response:
    q = request.query
    clauses, params = [], []
    if "monitor_id" in q:
        clauses.append("r.monitor_id=?"); params.append(int(q["monitor_id"]))
    if "group_id" in q:
        clauses.append("m.group_id=?"); params.append(int(q["group_id"]))
    if "group_name" in q:
        clauses.append("COALESCE(g.name,r.group_name,'Removed group')=?"); params.append(q["group_name"])
    if "camera_name" in q:
        clauses.append("COALESCE(m.name,r.monitor_name,'Removed camera')=?"); params.append(q["camera_name"])
    if "date" in q:
        try: day = datetime.strptime(q["date"], "%Y-%m-%d")
        except ValueError: raise web.HTTPBadRequest(text="date must be YYYY-MM-DD")
        try: tz_offset = int(q.get("tz_offset_minutes", "0"))
        except ValueError: raise web.HTTPBadRequest(text="tz_offset_minutes must be an integer")
        day = day - timedelta(minutes=tz_offset)
        clauses.append("r.start_time >= ? AND r.start_time < datetime(?, '+1 day')")
        # Stored timestamps are ISO-8601 strings. Keep both bounds in the same
        # lexical format; mixing `T` with SQLite's space-separated datetime
        # format incorrectly includes records from the following day.
        clauses[-1] = "r.start_time >= ? AND r.start_time < ?"
        params.extend([day.strftime("%Y-%m-%dT00:00:00"), (day + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")])
    if "start" in q:
        clauses.append("r.start_time >= ?"); params.append(q["start"])
    if "end" in q:
        clauses.append("r.start_time <= ?"); params.append(q["end"])
    try:
        page, limit = max(1, int(q.get("page", 1))), min(100, max(1, int(q.get("limit", 50))))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="page and limit must be integers") from exc
    order = "ASC" if q.get("sort") == "oldest" else "DESC"
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    base = """ FROM recordings r LEFT JOIN monitors m ON m.id=r.monitor_id
        LEFT JOIN monitor_groups g ON g.id=m.group_id"""
    total = (await (await request.app["db"].execute("SELECT COUNT(*) AS n" + base + where, params)).fetchone())["n"]
    rows = await (await request.app["db"].execute(
          """SELECT r.*,COALESCE(m.name,r.monitor_name,'Removed camera') AS monitor_name,
              COALESCE(m.group_id,0) AS group_id,COALESCE(g.name,r.group_name,'Removed group') AS group_name""" + base + where +
        " ORDER BY r.start_time " + order + " LIMIT ? OFFSET ?", [*params, limit, (page - 1) * limit])).fetchall()
    return web.json_response({"items": [recording_json(row) for row in rows], "page": page, "limit": limit, "total": total})

def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="timestamp must be ISO-8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

async def timeline(request: web.Request) -> web.Response:
    monitor_id = request.query.get("monitor_id")
    date_value = request.query.get("date")
    if not monitor_id or not date_value:
        raise web.HTTPBadRequest(text="monitor_id and date are required")
    try:
        day = datetime.strptime(date_value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise web.HTTPBadRequest(text="date must be YYYY-MM-DD") from exc
    try:
        day -= timedelta(minutes=int(request.query.get("tz_offset_minutes", "0")))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="tz_offset_minutes must be an integer") from exc
    next_day = day + timedelta(days=1)
    rows = await (await request.app["db"].execute(
        """SELECT r.*,COALESCE(m.name,r.monitor_name,'Removed camera') AS monitor_name,
           COALESCE(m.group_id,0) AS group_id,COALESCE(g.name,r.group_name,'Removed group') AS group_name
           FROM recordings r LEFT JOIN monitors m ON m.id=r.monitor_id
           LEFT JOIN monitor_groups g ON g.id=m.group_id
           WHERE r.monitor_id=? AND r.start_time>=? AND r.start_time<? ORDER BY r.start_time""",
        (monitor_id, day.isoformat(), next_day.isoformat()))).fetchall()
    now = datetime.now(UTC)
    items = []
    active_rows = [row for row in rows if row["status"] == "RECORDING" and not row["end_time"]]
    active_ids = {row["id"] for row in active_rows[:-1]} if active_rows else set()
    for row in rows:
        # A restart can leave old RECORDING rows behind. Only the newest
        # active file is seekable; hide stale active rows from the timeline.
        if row["id"] in active_ids:
            continue
        start = parse_timestamp(row["start_time"])
        end = parse_timestamp(row["end_time"]) if row["end_time"] else now
        # Keep every MP4 as an independent chunk. Do not merge overlapping or
        # active rows: the player needs the real file boundaries for seeking.
        start = max(start, day)
        end = min(end, next_day, now)
        if end <= start:
            continue
        items.append({"id": row["id"], "monitor_id": row["monitor_id"], "monitor_name": row["monitor_name"],
            "group_name": row["group_name"], "start": start.isoformat(), "end": end.isoformat(),
            "duration": max(0, (end - start).total_seconds()), "status": row["status"],
            "file_size": recording_json(row)["file_size"]})
    return web.json_response({"monitor_id": int(monitor_id), "date": date_value, "recordings": items})

async def resolve_timeline(request: web.Request) -> web.Response:
    monitor_id, timestamp_value = request.query.get("monitor_id"), request.query.get("timestamp")
    if not monitor_id or not timestamp_value:
        raise web.HTTPBadRequest(text="monitor_id and timestamp are required")
    timestamp = parse_timestamp(timestamp_value)
    row = await (await request.app["db"].execute(
        "SELECT id,start_time,end_time FROM recordings WHERE monitor_id=? AND start_time<=? AND (end_time>? OR end_time IS NULL) ORDER BY start_time DESC LIMIT 1",
        (monitor_id, timestamp.isoformat(), timestamp.isoformat()))).fetchone()
    if not row:
        raise web.HTTPNotFound(text="no recording at timestamp")
    offset = max(0, (timestamp - parse_timestamp(row["start_time"])).total_seconds())
    return web.json_response({"recording_id": row["id"], "offset": offset, "timestamp": timestamp.isoformat()})

async def list_recording_sources(request: web.Request) -> web.Response:
    rows = await (await request.app["db"].execute(
        """SELECT COALESCE(g.id,0) AS group_id, COALESCE(m.id,r.monitor_id) AS monitor_id,
              COALESCE(g.name,r.group_name,'Removed group') AS group_name,
                  COALESCE(m.name,r.monitor_name,'Removed camera') AS camera_name,
                  MAX(r.start_time) AS latest
           FROM recordings r LEFT JOIN monitors m ON m.id=r.monitor_id
           LEFT JOIN monitor_groups g ON g.id=m.group_id
           WHERE r.monitor_name IS NOT NULL OR m.id IS NOT NULL
           GROUP BY group_name,camera_name ORDER BY group_name COLLATE NOCASE,camera_name COLLATE NOCASE""")).fetchall()
    return web.json_response({"items": [dict(row) for row in rows]})

async def get_recording(request: web.Request) -> web.Response:
    return web.json_response(recording_json(await recording_row(request, request.match_info["id"])))

def known_recording_path(request: web.Request, row) -> Path:
    root = request.app["config"].recordings_path.resolve()
    path = Path(row["file_path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise web.HTTPNotFound(text="recording file is unavailable")
    return path

async def stream_recording(request: web.Request) -> web.StreamResponse:
    """Serve an MP4 directly with HTTP range support for smooth seeking.

    This endpoint is the recording streamer: it reads the stored file only and
    never starts or contacts FFmpeg/the camera.
    """
    row = await recording_row(request, request.match_info["id"])
    path = known_recording_path(request, row)
    return web.FileResponse(path, headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache",
        "Content-Disposition": f'inline; filename="{path.name}"'})

async def recording_playback_websocket(request: web.Request) -> web.WebSocketResponse:
    """Keep a timestamped playback session for one selected recording chunk.

    Video bytes remain on the range-capable MP4 streamer; this socket carries
    playback position/control messages so a selected 3-hour window has an
    isolated backend session and can be closed deterministically.
    """
    row = await recording_row(request, request.match_info["id"])
    known_recording_path(request, row)
    websocket = web.WebSocketResponse(heartbeat=30)
    await websocket.prepare(request)
    await websocket.send_json({"type": "ready", "recording_id": int(row["id"]),
        "stream_url": f"/api/recordings/{row['id']}/stream", "duration": row["duration"] or 0})
    try:
        async for message in websocket:
            if message.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except (TypeError, ValueError):
                    await websocket.send_json({"type": "error", "message": "invalid playback message"})
                    continue
                if payload.get("type") == "seek":
                    try: offset = max(0.0, float(payload.get("offset", 0)))
                    except (TypeError, ValueError): offset = 0.0
                    await websocket.send_json({"type": "position", "recording_id": int(row["id"]), "offset": offset})
            elif message.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                break
    finally:
        if not websocket.closed:
            await websocket.close()
    return websocket

async def download_recording(request: web.Request) -> web.StreamResponse:
    row = await recording_row(request, request.match_info["id"])
    path = known_recording_path(request, row)
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

async def delete_recording(request: web.Request) -> web.Response:
    row = await recording_row(request, request.match_info["id"])
    if row["status"] == "RECORDING":
        raise web.HTTPConflict(text="an active recording cannot be deleted")
    path = known_recording_path(request, row)
    try:
        path.unlink()
    except OSError as exc:
        raise web.HTTPInternalServerError(text="could not delete recording file") from exc
    await request.app["db"].execute("DELETE FROM recordings WHERE id=?", (row["id"],))
    await request.app["db"].commit()
    return web.Response(status=204)

@web.middleware
async def cors(request: web.Request, handler):
    origin = request.headers.get("Origin")
    allowed = origin and origin in request.app["config"].allowed_origins
    if request.method == "OPTIONS" and allowed:
        response = web.Response(status=204)
    else:
        response = await handler(request)
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

async def database_context(app: web.Application):
    cfg = app["config"]
    app["db"] = await open_database(cfg.database_path)
    saved_path = await (await app["db"].execute("SELECT value FROM settings WHERE key='recordings_path'")).fetchone()
    if saved_path:
        object.__setattr__(cfg, "recordings_path", Path(saved_path["value"]))
    cfg.recordings_path.mkdir(parents=True, exist_ok=True)
    app["recorder_manager"] = RecorderManager(app["db"], cfg)
    app["live_manager"] = LiveStreamManager(app["recorder_manager"])
    await app["recorder_manager"].start()
    yield
    await app["live_manager"].stop_all()
    await app["recorder_manager"].stop()
    await app["db"].close()

def create_app(config_path: str | None = None) -> web.Application:
    app = web.Application(middlewares=[cors])
    app["config"] = load_config(config_path) if config_path else load_config()
    app.cleanup_ctx.append(database_context)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/system/status", system_status)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", update_settings)
    app.router.add_get("/api/groups", list_groups)
    app.router.add_post("/api/groups", create_group)
    app.router.add_put("/api/groups/{id}", update_group)
    app.router.add_delete("/api/groups/{id}", delete_group)
    app.router.add_get("/api/monitors", list_monitors)
    app.router.add_post("/api/monitors", create_monitor)
    app.router.add_delete("/api/monitors/{id}", delete_monitor)
    app.router.add_get("/api/monitors/{id}/live", live_monitor_websocket)
    app.router.add_get("/api/recordings", list_recordings)
    app.router.add_get("/api/timeline", timeline)
    app.router.add_get("/api/timeline/resolve", resolve_timeline)
    app.router.add_get("/api/recording-sources", list_recording_sources)
    app.router.add_get("/api/recordings/{id}", get_recording)
    app.router.add_get("/api/recordings/{id}/stream", stream_recording)
    app.router.add_get("/api/recordings/{id}/playback", recording_playback_websocket)
    app.router.add_get("/api/recordings/{id}/download", download_recording)
    app.router.add_delete("/api/recordings/{id}", delete_recording)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", health)
    return app

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/litedvr/config.toml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(create_app(args.config), host=cfg.bind_address, port=cfg.port)

if __name__ == "__main__":
    main()
