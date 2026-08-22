import asyncio
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp.test_utils import TestClient, TestServer

from litedvr.main import create_app


def test_mock_timeline_and_range_playback():
    async def scenario(tmp_path):
        config = tmp_path / "config.toml"
        database = tmp_path / "mock.sqlite3"
        recordings = tmp_path / "recordings"
        config.write_text(
            "[storage]\n"
            f"database_path = \"{database.as_posix()}\"\n"
            f"recordings_path = \"{recordings.as_posix()}\"\n"
            "[recorder]\ndisable_recorders = true\n"
        )
        app = create_app(config)
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                db = app["db"]
                await db.execute("INSERT INTO monitor_groups(id,name,created_at) VALUES (1,'Mock','2026-08-21T00:00:00+00:00')")
                await db.execute(
                    "INSERT INTO monitors(id,group_id,name,rtsp_url,enabled,recording_enabled,segment_minutes,created_at,updated_at) "
                    "VALUES (1,1,'Mock camera','rtsp://mock.invalid/stream1',1,1,180,'2026-08-21T00:00:00+00:00','2026-08-21T00:00:00+00:00')"
                )
                path = recordings / "Mock" / "Mock-camera" / "segments" / "2026-08-21T21-00-00.mp4"
                path.parent.mkdir(parents=True)
                path.write_bytes(b"mock fragmented mp4 payload")
                now = datetime.now(UTC).isoformat()
                await db.execute(
                    "INSERT INTO recordings(monitor_id,monitor_name,group_name,start_time,end_time,duration,file_path,file_size,status,created_at) "
                    "VALUES (1,'Mock camera','Mock','2026-08-21T21:00:00+00:00',?,?,?,?,?,?)",
                    (now, 3600, str(path), path.stat().st_size, "COMPLETE", now),
                )
                await db.commit()

                health = await client.get("/api/health")
                assert health.status == 200
                assert (await health.json())["status"] == "ok"

                timeline = await client.get("/api/timeline?monitor_id=1&date=2026-08-22&tz_offset_minutes=-330")
                assert timeline.status == 200
                assert len((await timeline.json())["recordings"]) == 1

                recordings_response = await client.get("/api/recordings?monitor_id=1&date=2026-08-22&tz_offset_minutes=-330")
                assert recordings_response.status == 200
                recording_id = (await recordings_response.json())["items"][0]["id"]

                stream = await client.get(f"/api/recordings/{recording_id}/stream", headers={"Range": "bytes=0-5"})
                assert stream.status == 206
                assert await stream.read() == b"mock f"

    with TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory)))
