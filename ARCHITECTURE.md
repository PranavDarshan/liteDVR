# Architecture

LiteDVR uses one persistent ingest pipeline per enabled camera. The RTSP
connection is opened once and remains owned by that camera's recorder. FFmpeg
packet-copies the native video stream into independent three-hour fragmented
MP4 files and simultaneously emits a low-rate MJPEG preview stream. The
recorded video is never decoded or transcoded.

```text
RTSP camera
    | one persistent connection
    v
Camera recorder (supervised FFmpeg)
    +-- segment muxer --> 3-hour MP4 files --> SQLite metadata/timeline
    +-- MJPEG publisher --> isolated WebSocket subscribers (live view)

Browser playback --HTTP Range--> backend MP4 streamer --> stored MP4
```

The segment muxer owns rotation at fixed UTC three-hour boundaries
(`00:00`, `03:00`, `06:00`, and so on), so rotating from one file to the next
does not reconnect to the camera. A lightweight indexer watches the segment
directory and keeps SQLite start/end times, size, and status current. Active
segments are marked `RECORDING`; completed or interrupted files remain
individually addressable for timeline seeking and download.

Each camera has its own live subscriber set. Opening or closing a live
WebSocket only adds or removes that browser from the camera's publisher; it
does not start another RTSP connection and does not stop recording. Playback
never contacts the camera: it uses the backend's range-capable MP4 streamer.

FFmpeg runs in its own process group. On backend shutdown the entire group is
terminated (escalating to a forced kill after five seconds if needed), and the
supervisor restarts the ingest after an unexpected exit. Corrupt RTSP packets
are discarded where possible; a genuine RTSP disconnect marks the active file
interrupted and retries the same camera with bounded backoff.

SQLite contains metadata only. Video remains beneath the configured
recordings directory (`<group>/<camera>/segments` for new segmented ingest),
which is bind-mounted by Docker. The aiohttp backend is independent of the
frontend and can run under Docker Compose or systemd.
