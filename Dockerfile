FROM debian:bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-dev build-essential ffmpeg tini \
    && python3 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY litedvr ./litedvr
COPY config.example.toml /etc/litedvr/config.toml
RUN pip install --no-cache-dir --upgrade pip setuptools \
    && pip install --no-cache-dir .
RUN useradd --system --uid 10001 --create-home litedvr && mkdir -p /var/lib/litedvr/recordings && chown -R litedvr:litedvr /var/lib/litedvr
USER litedvr
VOLUME ["/var/lib/litedvr"]
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["litedvr", "--config", "/etc/litedvr/config.toml"]
