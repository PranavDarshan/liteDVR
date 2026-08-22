from pathlib import Path
import asyncio
import pytest
from litedvr.config import Config
from litedvr.config import load_config
from litedvr.database import open_database
from litedvr.recorder import Monitor, Recorder, safe_part, rtsp_with_credentials

def test_defaults_match_specification():
    config = Config()
    assert config.retention_days == 30
    assert config.default_segment_minutes == 180

@pytest.mark.parametrize("days", [0, 31, 120])
def test_invalid_retention_is_rejected(days):
    with pytest.raises(ValueError):
        Config(retention_days=days)

def test_path_part_cannot_escape_storage():
    assert safe_part("../../Outside / Front Door") == "Outside-Front-Door"

def test_rtsp_credentials_are_encoded():
    value = rtsp_with_credentials("rtsp://camera.local/live", "user name", "p@ss")
    assert value == "rtsp://user%20name:p%40ss@camera.local/live"

def test_environment_overrides_toml(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[storage]\nretention_days = 60\n[recorder]\ndefault_segment_minutes = 30\n")
    monkeypatch.setenv("LITEDVR_RETENTION_DAYS", "90")
    monkeypatch.setenv("LITEDVR_DEFAULT_SEGMENT_MINUTES", "60")
    assert load_config(config_file).retention_days == 90
    assert load_config(config_file).default_segment_minutes == 60

def test_normal_recorder_is_packet_copy_only():
    recorder = Recorder(None, Config(), Monitor(1, "Home", "Door", "rtsp://camera/live",
        "user", "pass", True, True, 60))
    command = recorder._command(Path("/tmp/output.mp4"))
    assert command[command.index("-c") + 1] == "copy"
    assert command[command.index("-map") + 1] == "0:v:0"
    assert "0:a?" in command
    # Both outputs (MP4 and preview pipe) must stop at the segment boundary.
    assert command.count("-t") == 2
    assert command[command.index("-t") + 1] == command[command.index("-t", command.index("-t") + 1) + 1]

def test_recorder_can_fall_back_to_video_only_for_incompatible_audio():
    recorder = Recorder(None, Config(), Monitor(1, "Home", "Door", "rtsp://camera/live",
        "user", "pass", True, True, 60))
    command = recorder._command(Path("/tmp/output.mp4"), include_audio=False)
    assert "0:a?" not in command
    assert command[command.index("-c") + 1] == "copy"

def test_persistent_ingest_uses_segment_muxer_and_single_rtsp_input(tmp_path):
    recorder = Recorder(None, Config(recordings_path=tmp_path), Monitor(1, "Home", "Door", "rtsp://camera/live",
        "user", "pass", True, True, 180))
    command = recorder._persistent_command(tmp_path)
    assert command.count("-i") == 1
    assert command[command.index("-f") + 1] == "segment"
    assert command[command.index("-segment_time") + 1] == "10800"
    assert "-segment_atclocktime" in command
    assert command[-1] == "pipe:1"

def test_persistent_segment_index_tracks_active_then_complete(tmp_path):
    async def scenario():
        db = await open_database(tmp_path / "mock.sqlite3")
        await db.execute("INSERT INTO monitor_groups(id,name,created_at) VALUES (1,'Home','2026-08-22T00:00:00+00:00')")
        await db.execute("INSERT INTO monitors(id,group_id,name,rtsp_url,enabled,recording_enabled,segment_minutes,created_at,updated_at) VALUES (1,1,'Door','rtsp://camera/live',1,1,180,'2026-08-22T00:00:00+00:00','2026-08-22T00:00:00+00:00')")
        await db.commit()
        recorder = Recorder(db, Config(recordings_path=tmp_path), Monitor(1, "Home", "Door", "rtsp://camera/live",
            "user", "pass", True, True, 180))
        segment_dir = tmp_path / "segments"
        segment_dir.mkdir()
        segment = segment_dir / "2026-08-22T09-00-00.mp4"
        segment.write_bytes(b"mock fragmented mp4")

        await recorder._sync_persistent_segments(segment_dir, active=True)
        row = await (await db.execute("SELECT status,file_size FROM recordings WHERE file_path=?", (str(segment),))).fetchone()
        assert row["status"] == "RECORDING"
        assert row["file_size"] == len(b"mock fragmented mp4")

        await recorder._sync_persistent_segments(segment_dir, active=False)
        row = await (await db.execute("SELECT status FROM recordings WHERE file_path=?", (str(segment),))).fetchone()
        assert row["status"] == "COMPLETE"
        await db.close()

    asyncio.run(scenario())
