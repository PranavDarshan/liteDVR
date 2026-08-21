# Configuration

The default configuration file is /etc/litedvr/config.toml. Retention is restricted to 30, 60, or 90 days; 30 is the default. Segment length is restricted to 30 or 60 minutes; 60 is the default.

allowed_origins is an explicit list for detached LAN frontends. Do not use a wildcard when authentication is introduced.

## Docker environment

When running in Docker, environment variables take precedence over TOML. Copy .env.example to .env and configure retention, default segment length, CORS origins, and mock mode there.

GET /api/settings and PUT /api/settings manage retention and default segment choice at runtime. Network, CORS, FFmpeg, and storage-path changes must be made through TOML/environment then followed by a restart.
