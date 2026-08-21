from pathlib import Path
import pytest
from litedvr.config import Config
from litedvr.config import load_config
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

def test_recorder_can_fall_back_to_video_only_for_incompatible_audio():
    recorder = Recorder(None, Config(), Monitor(1, "Home", "Door", "rtsp://camera/live",
        "user", "pass", True, True, 60))
    command = recorder._command(Path("/tmp/output.mp4"), include_audio=False)
    assert "0:a?" not in command
    assert command[command.index("-c") + 1] == "copy"
