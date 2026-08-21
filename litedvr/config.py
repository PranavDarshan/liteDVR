from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib

@dataclass(frozen=True)
class Config:
    bind_address: str = "0.0.0.0"
    port: int = 8080
    database_path: Path = Path("/var/lib/litedvr/litedvr.sqlite3")
    recordings_path: Path = Path("/var/lib/litedvr/recordings")
    ffmpeg_path: str = "ffmpeg"
    retention_days: int = 30
    default_segment_minutes: int = 60
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    mock_mode: bool = False

    def __post_init__(self) -> None:
        if self.retention_days not in (30, 60, 90):
            raise ValueError("retention_days must be 30, 60, or 90")
        if self.default_segment_minutes not in (30, 60, 180, 1440):
            raise ValueError("default_segment_minutes must be 30, 60, 180, or 1440")

def load_config(path: str | Path = "/etc/litedvr/config.toml") -> Config:
    config_path = Path(path)
    raw: dict = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    server, storage = raw.get("server", {}), raw.get("storage", {})
    cors, recorder = raw.get("cors", {}), raw.get("recorder", {})
    env = os.environ
    origins = env.get("LITEDVR_ALLOWED_ORIGINS")
    return Config(
        bind_address=env.get("LITEDVR_BIND_ADDRESS", server.get("bind_address", "0.0.0.0")),
        port=int(env.get("LITEDVR_PORT", server.get("port", 8080))),
        database_path=Path(env.get("LITEDVR_DATABASE_PATH", storage.get("database_path", "/var/lib/litedvr/litedvr.sqlite3"))),
        recordings_path=Path(env.get("LITEDVR_RECORDINGS_PATH", storage.get("recordings_path", "/var/lib/litedvr/recordings"))),
        ffmpeg_path=env.get("LITEDVR_FFMPEG_PATH", recorder.get("ffmpeg_path", "ffmpeg")),
        retention_days=int(env.get("LITEDVR_RETENTION_DAYS", storage.get("retention_days", 30))),
        default_segment_minutes=int(env.get("LITEDVR_DEFAULT_SEGMENT_MINUTES", recorder.get("default_segment_minutes", 60))),
        allowed_origins=tuple(origins.split(",") if origins else cors.get("allowed_origins", [])),
        mock_mode=env.get("LITEDVR_MOCK_MODE", str(recorder.get("mock_mode", False))).lower() == "true")
