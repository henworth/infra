-- Bootstrap the per-service logical databases for docker-compose.dev.yaml.
-- `app` is the POSTGRES_USER set in the compose file, so it's already a
-- superuser and owns these by default.
CREATE DATABASE pantry_db;
CREATE DATABASE shopping_db;
