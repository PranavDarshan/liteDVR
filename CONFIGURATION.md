# Configuration

The default configuration file is `/etc/litedvr/config.toml`. Retention is restricted to 30, 60, or 90 days; 30 is the default. Recording cuts are fixed at 180 minutes (three hours); this is not user-configurable.

`allowed_origins` accepts comma-separated exact origins and shell-style patterns. The Docker deployment allows localhost, 127.0.0.1, and `http://192.168.*` for LAN frontends. Narrow this pattern before exposing the service outside a trusted private network.

## Docker environment

When running in Docker, environment variables take precedence over TOML. Copy `.env.example` to `.env` and configure retention, CORS origins, storage, platform, and mock mode there. The segment-size variable is retained for compatibility but is normalized to 180 minutes.

GET /api/settings and PUT /api/settings manage retention and expose the fixed 180-minute segment setting. Network, CORS, FFmpeg, and storage-path changes must be made through TOML/environment then followed by a restart.
