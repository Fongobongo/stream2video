"""stream2video: CLI tool to compress stream recordings by removing silence."""

import importlib.metadata
import tomllib
from pathlib import Path


def _read_version() -> str:
    """Resolve the package version from a single source of truth.

    Source checkout → the ``project.version`` in ``pyproject.toml``
    (development runs, tests, and portable builds run the code in-tree,
    where the file is authoritative — a stale ``dist-info`` from an older
    install must not shadow the current checkout). Installed distribution
    without the source tree → ``importlib.metadata``. The historical
    hard-coded ``__version__`` string drifted from ``pyproject.toml``;
    both readers now converge on the same value, and any future bump only
    touches ``pyproject.toml``.
    """
    try:
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return importlib.metadata.version("stream2video")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read_version()
