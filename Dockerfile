# syntax=docker/dockerfile:1.6
#
# Precipice Sandstone POC — MODFLOW 6 + Python image.
#
# This image bundles:
#   - Python 3.11
#   - The MODFLOW 6 executable (mf6) on PATH, fetched via FloPy's official downloader
#   - The geospatial Python stack (geopandas, rasterio etc) installed via wheels
#   - The project source code
#
# Build:
#   docker build -t precipice-poc .
# Run:
#   docker compose run --rm app python -m src.cli run --config config.yaml

FROM python:3.11-slim AS base

# --- System deps ----------------------------------------------------------
# - libexpat1 is needed by some GDAL wheels at runtime
# - curl + ca-certificates are needed by flopy.utils.get_modflow
# - build-essential is only kept in the build stage (we install via wheels)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      libexpat1 \
 && rm -rf /var/lib/apt/lists/*

# --- Python deps ----------------------------------------------------------
# Install in a deliberate order so wheels are used. pyproject.toml is the
# source of truth for versions; we copy it first to maximise layer caching.
WORKDIR /app
COPY pyproject.toml ./

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

# --- MODFLOW 6 binary -----------------------------------------------------
# FloPy ships a downloader that pulls the official USGS release for the
# current platform and drops the binary into the chosen directory.
# This is the canonical way to install MF6 in a Python environment.
RUN python -m flopy.utils.get_modflow /usr/local/bin --subset mf6 \
 && mf6 --version

# --- Source code ----------------------------------------------------------
COPY src/ ./src/
COPY config.yaml ./config.yaml

# Default runtime user (non-root for safety; container can still write to
# bind-mounted volumes if the host directory is writable by this UID, or
# if you override with `--user` at runtime).
RUN useradd --create-home --uid 1000 modeller
USER modeller

# --- Default command ------------------------------------------------------
# Override in docker-compose.yml or at the CLI for notebooks / shells.
CMD ["python", "-m", "src.cli", "run", "--config", "config.yaml"]
