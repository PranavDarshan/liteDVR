# API

## Settings

GET /api/settings returns effective retention and default segment settings plus restart-required fields.

PUT /api/settings accepts JSON containing either or both of these values:

    {"retention_days": 60, "default_segment_minutes": 30}

Retention is strictly 30, 60, or 90 days. Default segment length is strictly 30 or 60 minutes. Network bindings, CORS origins, FFmpeg path, and recording path come from TOML/environment and require a container restart.

## Recordings

GET /api/recordings returns a paginated recording list. Optional filters are monitor_id, group_id, date (YYYY-MM-DD), start, end, sort (newest or oldest), page, and limit.

GET /api/recordings/{id}/stream serves only the database-known MP4 and supports HTTP Range requests for browser seeking. GET /api/recordings/{id}/download serves the same file as an attachment. DELETE /api/recordings/{id} removes a completed/interrupted file and metadata, but refuses active recordings.
