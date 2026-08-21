from __future__ import annotations
import aiosqlite
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS monitor_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitors (
 id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES monitor_groups(id) ON DELETE RESTRICT,
 name TEXT NOT NULL, rtsp_url TEXT NOT NULL, username TEXT, password TEXT,
 enabled INTEGER NOT NULL DEFAULT 1, recording_enabled INTEGER NOT NULL DEFAULT 1,
 segment_minutes INTEGER NOT NULL DEFAULT 60 CHECK(segment_minutes IN (30,60,180,1440)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recordings (
 id INTEGER PRIMARY KEY, monitor_id INTEGER REFERENCES monitors(id) ON DELETE SET NULL,
 monitor_name TEXT, group_name TEXT,
 start_time TEXT NOT NULL, end_time TEXT, duration REAL, file_path TEXT NOT NULL UNIQUE,
 file_size INTEGER, status TEXT NOT NULL CHECK(status IN ('RECORDING','COMPLETE','INTERRUPTED','ERROR')), created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_recordings_monitor_start ON recordings(monitor_id, start_time);
CREATE INDEX IF NOT EXISTS idx_recordings_end_time ON recordings(end_time);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

async def open_database(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    monitor_sql_row = await (await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='monitors'")).fetchone()
    monitor_sql = monitor_sql_row[0] if monitor_sql_row else ""
    if "segment_minutes INTEGER NOT NULL DEFAULT 60 CHECK(segment_minutes IN (30,60,180,1440))" not in monitor_sql:
        await db.executescript("""
        PRAGMA foreign_keys = OFF;
        ALTER TABLE monitors RENAME TO monitors_legacy;
        CREATE TABLE monitors (
         id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES monitor_groups(id) ON DELETE RESTRICT,
         name TEXT NOT NULL, rtsp_url TEXT NOT NULL, username TEXT, password TEXT,
         enabled INTEGER NOT NULL DEFAULT 1, recording_enabled INTEGER NOT NULL DEFAULT 1,
         segment_minutes INTEGER NOT NULL DEFAULT 60 CHECK(segment_minutes IN (30,60,180,1440)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO monitors(id,group_id,name,rtsp_url,username,password,enabled,recording_enabled,segment_minutes,created_at,updated_at)
        SELECT id,group_id,name,rtsp_url,username,password,enabled,recording_enabled,segment_minutes,created_at,updated_at
        FROM monitors_legacy;
        DROP TABLE monitors_legacy;
        PRAGMA foreign_keys = ON;
        """)
    foreign_key = await (await db.execute("PRAGMA foreign_key_list(recordings)" )).fetchone()
    # Rebuild recordings if an older migration left its FK pointing at the
    # temporary monitors_legacy table (or used the wrong delete action).
    if foreign_key and (foreign_key[2] != "monitors" or foreign_key[6].upper() != "SET NULL"):
        await db.executescript("""
        DROP INDEX IF EXISTS idx_recordings_monitor_start;
        DROP INDEX IF EXISTS idx_recordings_end_time;
        ALTER TABLE recordings RENAME TO recordings_legacy;
        CREATE TABLE recordings (
         id INTEGER PRIMARY KEY, monitor_id INTEGER REFERENCES monitors(id) ON DELETE SET NULL,
         monitor_name TEXT, group_name TEXT, start_time TEXT NOT NULL, end_time TEXT, duration REAL,
         file_path TEXT NOT NULL UNIQUE, file_size INTEGER,
         status TEXT NOT NULL CHECK(status IN ('RECORDING','COMPLETE','INTERRUPTED','ERROR')), created_at TEXT NOT NULL);
        INSERT INTO recordings(id,monitor_id,monitor_name,group_name,start_time,end_time,duration,file_path,file_size,status,created_at)
        SELECT id,monitor_id,monitor_name,group_name,start_time,end_time,duration,file_path,file_size,status,created_at FROM recordings_legacy;
        DROP TABLE recordings_legacy;
        CREATE INDEX IF NOT EXISTS idx_recordings_monitor_start ON recordings(monitor_id, start_time);
        CREATE INDEX IF NOT EXISTS idx_recordings_end_time ON recordings(end_time);
        """)
    columns = {row[1] for row in await (await db.execute("PRAGMA table_info(recordings)")).fetchall()}
    for column in ("monitor_name", "group_name"):
        if column not in columns:
            await db.execute(f"ALTER TABLE recordings ADD COLUMN {column} TEXT")
    await db.commit()
    return db
