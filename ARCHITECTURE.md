# Architecture

Each enabled monitor owns one supervised FFmpeg process. The recorder maps the first video stream and uses `-c copy`; it starts video-only because incompatible camera audio codecs can otherwise force an FFmpeg restart and create a recording gap. It has no video decode/encode pipeline. Recordings are cut at fixed UTC three-hour boundaries (00:00, 03:00, 06:00, and so on), independent of restart time. Fragmented MP4 output is flushed frequently so active recordings can become playable sooner.

FFmpeg runs in its own process group. On backend shutdown the group is terminated, escalated to a forced kill after five seconds if necessary, and recreated automatically when the backend starts. Corrupt RTSP packets are discarded where possible so a bad frame does not terminate the recorder. Live MJPEG clients share the recorder's single camera connection. The live preview remains at 5 FPS using the configured camera frame size; recorded MP4 output remains native resolution. Playback uses the stored MP4 HTTP streamer and never contacts the camera.

SQLite contains metadata only. Video stays beneath the configured recordings directory. The aiohttp process is designed to run under systemd and never depends on a frontend process.
