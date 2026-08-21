# Architecture

Each enabled monitor owns one supervised FFmpeg process. The normal command maps the first video stream and optional first audio stream and uses -c copy; it has no decode/encode pipeline. Processes restart after failures with 5, 10, 20, then 30-second delays.

SQLite contains metadata only. Video stays beneath the configured recordings directory. The aiohttp process is designed to run under systemd and never depends on a frontend process.
