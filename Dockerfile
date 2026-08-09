# stream2video CLI / integration test image (LOCAL USE ONLY — not for CI).
#
# Purpose: provide a reproducible Linux environment for developers on
# Windows who want to verify their code runs the same way the GitHub
# Actions CI sees it, without manually setting up WSL2 + Python + ffmpeg.
# The existing .github/workflows/ci.yml already runs the full test
# suite on every push/PR via uv — this Dockerfile does NOT replace it.
#
# Why keep it despite the duplication: on a Windows host, `docker run`
# is faster to spin up than WSL2 + manual apt installs, and it gives
# a clean-room check that nothing in the code path accidentally relies
# on Windows-specific behaviour (path separators, ctypes, etc).
#
# Usage:
#   docker build -t stream2video-test .
#   docker run --rm stream2video-test           # runs pytest + ruff + mypy
#   docker run --rm stream2video-test stream2video --help
#   docker run --rm -v "$PWD/sample.mp4:/in.mp4" -v "$PWD/out:/out" \
#     stream2video-test stream2video /in.mp4 -o /out --encoder libx264
#
# Size: ~250 MB (Python 3.13 slim + ffmpeg + project deps).
#
# ── Lightweight CLI-only variant ────────────────────────────────────────
# For pipelines that only need the transcoding step (no GUI, no tests,
# no lint): build with the target ``cli`` to get a smaller image.
#   docker build -t stream2video-cli --target cli .
#   docker run --rm -v "$PWD/in.mp4:/in.mp4" -v "$PWD/out:/out" \
#     stream2video-cli /in.mp4 -o /out --dry-run
#
# Entrypoint is the CLI itself; a positional argument maps to
# ``INPUT_VIDEO``. The image omits the dev extras (pytest/ruff/mypy/
# PySide6) so it's roughly half the size.

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


# ── Target: cli ────────────────────────────────────────────────────────
# Lightweight image with only the runtime dependencies — no dev
# toolchain, no test runner, no GUI extras. Suitable as the base for
# a GitHub Actions step or a cronjob encoding worker. The entrypoint
# is the ``stream2video`` binary; positional args land in
# ``INPUT_VIDEO`` the way the host CLI would receive them.
FROM python:3.13-slim AS cli

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY stream2video/ stream2video/

# Runtime-only install (no .[dev] extras).
RUN pip install --no-cache-dir .

# Drop the build user for security (uid 1000 is conventional).
RUN useradd --create-home --uid 1000 stream2video
USER stream2video

ENTRYPOINT ["stream2video"]
CMD ["--help"]
