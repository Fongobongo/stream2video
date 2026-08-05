"""Exceptions raised by the video/audio concat pipeline."""


class ConcatError(Exception):
    """Raised on concat / encode failures (ffmpeg errors, bad inputs)."""


class FFmpegError(ConcatError):
    """ffmpeg itself failed (non-zero exit, timeout, stall)."""


class FFmpegOutOfMemoryError(FFmpegError):
    """ffmpeg was killed by the OS OOM killer or self-aborted on alloc.

    Distinct from ``FFmpegError`` so the CLI / GUI can surface a
    targeted "lower the memory budget / use Low-memory preset" hint
    instead of dumping a generic ffmpeg stderr snippet.

    Detection (in ``_run_ffmpeg``):

    * POSIX: ``returncode == -9`` (Python convention for "child killed
      by signal SIGKILL") or ``returncode == 137`` (128 + 9, the shell
      convention) — the Linux OOM killer sends SIGKILL.
    * stderr markers (case-insensitive, cross-platform): "out of
      memory", "cannot allocate memory", "malloc failed", "mmap
      failed", "not enough space", "Error splitting input into
      thread: Cannot allocate memory" (libx264's thread init failure).
      On Windows exit code is 1 (generic) so stderr is the only
      signal.
    """


class CancelledError(ConcatError):
    """User cancellation during concat/encode (not a real failure)."""


class EncoderUnavailableError(ConcatError):
    """Hardware encoder unavailable and the fallback policy refused libx264.

    Distinct from ``FFmpegError`` so the CLI can craft a "select a different
    encoder / check the driver" message instead of a generic "ffmpeg failed"
    one -- the encoder wasn't even tried, so its stderr wouldn't be helpful.
    """
