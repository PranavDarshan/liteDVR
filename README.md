# LiteDVR

LiteDVR is a small self-hosted DVR for low-resource Debian hosts. It records RTSP H.264/H.265 streams using FFmpeg packet copy (-c copy): normal recordings are never decoded or transcoded.

This repository implements the first two milestones: TOML configuration, SQLite bootstrap, health/system endpoints, a settings API, a container deployment, a systemd unit, and a supervised per-camera packet-copy recorder. CRUD management, recordings browsing, retention cleanup, Range streaming, and the detached UI remain later milestones.

## Docker deployment

Copy `.env.example` to `.env`, set `LITEDVR_DATA_DIR` to a directory with enough disk space, then build and start the stack:

    docker compose build
    docker compose up -d

Open `http://<debian-host>:8081`. The backend is on port 8080 and the frontend on port 8081. The database and MP4 files are persisted under `LITEDVR_DATA_DIR`; do not remove that directory during upgrades.

The default deployment target is `linux/386` for the requested 32-bit Debian laptop. If the host is amd64 or arm64, set `LITEDVR_PLATFORM` in `.env` to that platform before building. Docker itself must be available on the Debian host; a 32-bit userspace cannot run a 64-bit-only Docker engine.

For a no-Docker local test on Windows, use config.local-test.toml and run the backend on port 8080 plus a static server for frontend on port 8081. The configuration UI lets you add groups and RTSP cameras; start with a disabled camera or a known RTSP endpoint.

The detached frontend includes a recordings view with filters, a metadata-based timeline, standard HTML5 MP4 playback, seeking through Range requests, and download links. It intentionally does not provide a live RTSP view.

To publish images, authenticate on a build machine and provide your Docker Hub namespace:

    docker login
    docker buildx build --platform linux/386,linux/amd64 -t <namespace>/litedvr:latest --push .
    docker buildx build --platform linux/386,linux/amd64 -t <namespace>/litedvr-frontend:latest --push ./frontend

The i386 image must be built on native i386 or with Buildx emulation, and should be tested with one camera before adding more.

Before adding a real camera, run scripts/probe-target.sh on the i386 Debian machine. It checks architecture, Python packages, FFmpeg, and RTSP support. Then use one real camera and inspect CPU/RSS before enabling more cameras.

See INSTALL.md, CONFIGURATION.md, and ARCHITECTURE.md.
