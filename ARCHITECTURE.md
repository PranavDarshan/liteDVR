# Architecture

Each enabled monitor owns one supervised FFmpeg process. The normal command maps the first video stream and optional first audio stream and uses `-c copy`; it has no decode/encode pipeline. Recordings are cut at fixed UTC three-hour boundaries (00:00, 03:00, 06:00, and so on), independent of restart time. Fragmented MP4 output is flushed frequently so active recordings can become playable sooner.

FFmpeg runs in its own process group. On backend shutdown the group is terminated, escalated to a forced kill after five seconds if necessary, and recreated automatically when the backend starts. Live MJPEG clients share the recorder's single camera connection; playback uses the stored MP4 HTTP streamer and never contacts the camera.

SQLite contains metadata only. Video stays beneath the configured recordings directory. The aiohttp process is designed to run under systemd and never depends on a frontend process.
