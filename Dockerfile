# stream2video CLI / integration test image.
#
# Purpose: provide a reproducible environment for running the full test
# suite (pytest + ruff + mypy) and the CLI against a real ffmpeg/ffprobe
# without requiring a Windows host. The GUI is intentionally NOT
# included — customtkinter / Tk require a display server that's not
# worth wiring up in a CLI-only test image.
#
# Usage:
#   docker build -t stream2video-test .
#   docker run --rm stream2video-test           # runs pytest + ruff + mypy
#   docker run --rm stream2video-test stream2video --help
#   docker run --rm -v "$PWD/sample.mp4:/in.mp4" -v "$PWD/out:/out" \
#     stream2video-test stream2video /in.mp4 -o /out --encoder libx264
#
# Size: ~250 MB (Python 3.13 slim + ffmpeg + project deps). The image
# is intended for CI/local testing, not distribution — a release build
# would pin to a specific yt-dlp/ffmpeg version and tag accordingly.

FROM python:3.13-slim

# ffmpeg + ffprobe are required by the pipeline. yt-dlp is installed
# via pip (declared in pyproject) so we don't duplicate it here.
# ca-certificates is needed for HTTPS download tests; curl is a
# debugging convenience for ad-hoc checks inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so they're cached across code changes.
# Copy only the metadata files needed by pip; the rest of the source
# is copied in the next layer (changed frequently).
COPY pyproject.toml README.md ./
COPY stream2video/ stream2video/

# Install in editable mode with the [dev] extra so pytest/ruff/mypy are
# available. The [gui] extra (Pillow, customtkinter) is intentionally
# NOT installed — Tk requires a display server and would fail to import
# in a headless container. test_import_gui skips when Pillow is
# absent; the rest of the test suite runs headless.
RUN pip install --no-cache-dir -e ".[dev]"

# Default entrypoint: run the full test suite (pytest + ruff + mypy)
# so `docker run stream2video-test` exits non-zero on any failure.
# Override the command to invoke the CLI directly instead.
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy stream2video && pytest -q"]
