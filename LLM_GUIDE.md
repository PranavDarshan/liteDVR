# LiteDVR LLM project guide

Use this file as context when asking an AI assistant to explain, install,
debug, or modify this repository.

## Suggested prompt

> You are helping me operate the LiteDVR repository. Read `LLM_GUIDE.md`,
> `README.md`, `ARCHITECTURE.md`, `CONFIGURATION.md`, `API.md`, and the source
> files before making assumptions. First summarize the current code,
> architecture, deployment mode, data paths, and likely risks. Then give
> copy-pasteable commands for my operating system. If debugging, collect
> evidence from service status, container status, logs, camera connectivity,
> FFmpeg/ffprobe output, and API responses before proposing a fix. Preserve
> recordings and configuration unless I explicitly request deletion. Clearly
> distinguish Docker deployment from native systemd deployment.

## Project summary

LiteDVR is a lightweight self-hosted DVR. The backend is Python/aiohttp with
SQLite metadata and supervised per-camera FFmpeg recorder processes. Normal
recording uses FFmpeg packet copy (`-c copy`) so the camera stream is not
decoded or re-encoded. The frontend is a static browser application served by
Nginx. It manages cameras and groups, shows monitor status, displays recording
timelines, and plays MP4 recordings through backend HTTP endpoints.

The target deployment is a low-resource 32-bit Debian laptop (`linux/386`),
but the code can also run on other Linux architectures when the matching image
or native dependencies are available.

## Architecture and important paths

- `litedvr/`: backend package, API, recorder supervision, storage, and config.
- `frontend/`: static HTML/CSS/JavaScript web client and Nginx image.
- `compose.yaml`: Docker deployment for backend and frontend.
- `Dockerfile`: backend image with Python, FFmpeg, and tini.
- `frontend/Dockerfile`: Nginx frontend image.
- `systemd/litedvr.service`: optional native, non-Docker service.
- `config.example.toml`: native configuration template.
- `.env.example`: Docker environment defaults.
- `tests/`: automated tests.
- `assets/`: README screenshots.

In Docker, `/var/lib/litedvr` is the persistent runtime volume. The host
directory configured by `LITEDVR_DATA_DIR` contains the SQLite database and
recordings. These files must not be deleted during image upgrades and are not
published to Docker Hub.

## Deployment decision

Use exactly one backend deployment mode:

1. **Docker Compose (recommended):** pulls
   `pranavdarshan1/litedvr:latest` and
   `pranavdarshan1/litedvr-frontend:latest`. Containers use
   `restart: unless-stopped` and survive Docker daemon restarts.
2. **Native systemd:** runs `/opt/litedvr/.venv/bin/litedvr` directly with
   `/etc/litedvr/config.toml`. Do not run this service together with the Docker
   backend because both use port `8080`.

## Standard Docker setup

On Debian with legacy Compose:

    cp .env.example .env
    docker-compose pull
    docker-compose up -d
    docker-compose ps
    curl http://127.0.0.1:8080/api/health

The UI is available at `http://<host-ip>:8081`; the backend API is on port
`8080`. For a code rebuild, use `docker-compose up -d --build`. For a pulled
release, use `docker-compose pull` followed by
`docker-compose up -d --force-recreate --no-build`.

## Standard native systemd setup

Install Python, FFmpeg, the package virtual environment, configuration, and
the included unit as documented in `README.md`. Then use:

    sudo systemctl enable --now litedvr
    systemctl status litedvr --no-pager
    journalctl -u litedvr -f

If Docker is being used instead, disable the native unit:

    sudo systemctl disable --now litedvr

## Debugging checklist

Collect these facts before changing code:

    docker-compose ps
    docker logs --tail=200 litedvr
    curl -s http://127.0.0.1:8080/api/health
    curl -s http://127.0.0.1:8080/api/monitors
    docker top litedvr -eo pid,pcpu,pmem,args
    df -h /

For a camera that is offline, check the host and container separately:

    ping -c 4 <camera-ip>
    nc -vz <camera-ip> 554
    docker exec litedvr ffprobe -v error -rtsp_transport tcp \
      -select_streams v:0 \
      -show_entries stream=codec_name,width,height,avg_frame_rate,r_frame_rate \
      -of default=noprint_wrappers=1 \
      'rtsp://USER:PASSWORD@<camera-ip>:554/stream1'

Check that the configured stream is the camera's high-resolution stream.
Many cameras provide a low-resolution secondary stream such as `stream2`.
`1920x1080` confirms 1080p; FPS cannot exceed what the camera supplies.

For recording files, use the container path rather than the host path:

    docker exec litedvr find /var/lib/litedvr/recordings -type f -name '*.mp4'
    docker exec litedvr ffprobe -v error -select_streams v:0 \
      -show_entries stream=codec_name,width,height,avg_frame_rate,r_frame_rate \
      -of default=noprint_wrappers=1 '/var/lib/litedvr/recordings/<file>.mp4'

When diagnosing gaps, compare API timeline metadata, recording file times,
FFmpeg exit messages, and camera reachability. Do not delete recordings as a
first troubleshooting step. `status=217/USER` indicates a missing native
systemd service account; `curl` connection failures usually indicate a stopped
container, wrong port, or a backend startup failure.

## Safe change rules for an AI assistant

- Do not delete the database, recordings, Docker volumes, or camera entries
  without explicit confirmation.
- Do not change RTSP URLs, credentials, segment size, or recording policy
  based only on a screenshot.
- Keep recording segments fixed at 3 hours unless the project specification is
  intentionally changed.
- Preserve packet-copy recording and the configured preview behavior unless a
  performance change is explicitly requested.
- After code changes, run `py -3 -m pytest tests -q` on Windows or `pytest`
  on Debian, then state exactly what was tested.
- Report whether a command affects the host, a Docker container, or the
  browser, and distinguish a source rebuild from pulling a published image.
