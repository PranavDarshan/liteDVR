# LiteDVR

LiteDVR is a small self-hosted DVR for low-resource Debian hosts. It records RTSP H.264/H.265 streams using FFmpeg packet copy (-c copy): normal recordings are never decoded or transcoded.

The project includes TOML/environment configuration, SQLite metadata, CRUD camera/group management, supervised per-camera recording, retention cleanup, HTTP Range MP4 playback, isolated live/playback sockets, and a detached frontend.

## Docker deployment

Copy `.env.example` to `.env`, set `LITEDVR_DATA_DIR` to a directory with enough disk space, then pull and start the stack:

    docker pull pranavdarshan1/litedvr:latest
    docker pull pranavdarshan1/litedvr-frontend:latest
    docker compose up -d

On Debian systems without the Compose V2 plugin, install `docker-compose` and
run the same commands with a hyphen (`docker-compose pull`,
`docker-compose up -d`).

The published images are used by default:

    pranavdarshan1/litedvr:latest
    pranavdarshan1/litedvr-frontend:latest

### Published Docker images

The complete application is distributed as these two images:

| Image | Purpose |
| --- | --- |
| [`pranavdarshan1/litedvr:latest`](https://hub.docker.com/r/pranavdarshan1/litedvr) | Backend API, recorder, FFmpeg integration, and MP4 playback |
| [`pranavdarshan1/litedvr-frontend:latest`](https://hub.docker.com/r/pranavdarshan1/litedvr-frontend) | Nginx-hosted web interface |

Pull both images explicitly when preparing a host:

    docker pull pranavdarshan1/litedvr:latest
    docker pull pranavdarshan1/litedvr-frontend:latest

The Compose file starts both images together and exposes the frontend on
port `8081` and the backend API on port `8080`.

For a local source build instead of Docker Hub images, run `docker-compose up -d --build`.

Open `http://<debian-host>:8081`. The backend is on port 8080 and the frontend on port 8081. The database and MP4 files are persisted under `LITEDVR_DATA_DIR`; do not remove that directory during upgrades.

The default deployment target is `linux/386` for the requested 32-bit Debian laptop. If the host is amd64 or arm64, set `LITEDVR_PLATFORM` in `.env` to that platform before building. Docker itself must be available on the Debian host; a 32-bit userspace cannot run a 64-bit-only Docker engine.

For a no-Docker local test on Windows, use config.local-test.toml and run the backend on port 8080 plus a static server for frontend on port 8081. The configuration UI lets you add groups and RTSP cameras; start with a disabled camera or a known RTSP endpoint.

The detached frontend includes a recordings view with filters, a metadata-based timeline, standard HTML5 MP4 playback, seeking through Range requests, and download links. It intentionally does not provide a live RTSP view.

Maintainers can publish updated images from a build machine with:

    docker login
    docker buildx build --platform linux/386,linux/amd64 -t <namespace>/litedvr:latest --push .
    docker buildx build --platform linux/386,linux/amd64 -t <namespace>/litedvr-frontend:latest --push ./frontend

The i386 image must be built on native i386 or with Buildx emulation, and should be tested with one camera before adding more.

Before adding a real camera, run scripts/probe-target.sh on the i386 Debian machine. It checks architecture, Python packages, FFmpeg, and RTSP support. Then use one real camera and inspect CPU/RSS before enabling more cameras.

See INSTALL.md, CONFIGURATION.md, and ARCHITECTURE.md.
