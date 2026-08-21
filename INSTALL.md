# Installation

## Docker (recommended)

On the Debian laptop, install Docker Engine and Compose, then:

    cp .env.example .env
    mkdir -p ./litedvr-data
    sed -i 's#LITEDVR_DATA_DIR=.*#LITEDVR_DATA_DIR=./litedvr-data#' .env
    docker compose build
    docker compose up -d

If `docker compose` reports “compose is not a docker command”, install the
Debian legacy client and use the hyphenated command instead:

    sudo apt update
    sudo apt install docker-compose
    docker-compose build
    docker-compose up -d

The project Compose file supports both command forms. Check availability with
`docker compose version` or `docker-compose --version`.

Check the services with `docker compose ps` (or `docker-compose ps`) and `curl http://127.0.0.1:8080/api/health`. Use the matching `logs -f litedvr` command for recorder/FFmpeg diagnostics. Back up `LITEDVR_DATA_DIR` before upgrades.

For a 32-bit Debian host, confirm `uname -m` reports `i386`/`i686` and that the installed Docker engine supports 32-bit containers. If it does not, build the `linux/386` images on another machine with Buildx and use a supported 32-bit container runtime on the laptop.

Run the target probe first:

    ./scripts/probe-target.sh

Install Debian packages python3-venv and ffmpeg. Create a virtual environment, install this project with pip install ., copy config.example.toml to /etc/litedvr/config.toml, then create /var/lib/litedvr owned by an litedvr service user. Install systemd/litedvr.service and run systemctl enable --now litedvr.

A mock-mode result is not a camera benchmark. Test one real RTSP camera on the i386 laptop before enabling multiple cameras.
