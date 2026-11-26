"""Run record for the ``generate`` manifest: code version, environment, machine.

Everything here describes *how* a run was made, never the data itself. Values that cannot
be found (no git checkout, a package not installed) are recorded as ``None``.
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

__all__ = ["git_state", "package_versions", "highs_version", "run_info"]

#: Distributions whose versions can change a number in the output.
PACKAGES = ("numpy", "pandas", "pyarrow", "openpyxl", "lxml", "highspy")

#: The folder that holds the ``bessbid`` package (``python/`` in the repository).
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(PACKAGE_ROOT), *args],
                           capture_output=True, text=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip()


def git_state() -> dict[str, Any]:
    """Commit of the checkout that holds the package and whether the package folder has
    uncommitted changes (tracked or untracked, ``.gitignore`` respected). All ``None`` when
    the package is not inside a git checkout (e.g. installed with pip or run from a zip)."""
    commit = _git("rev-parse", "HEAD")
    if not commit:
        return {"commit": None, "dirty": None, "branch": None}
    status = _git("status", "--porcelain", "--", ".")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    return {"commit": commit, "dirty": None if status is None else bool(status), "branch": branch}


def package_versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def highs_version() -> str | None:
    """Version of the HiGHS library inside ``highspy`` (e.g. ``1.16.0``)."""
    try:
        from .engine import _new_highs

        return str(_new_highs().version())
    except Exception:  # pragma: no cover - highspy missing or very old
        return None


def run_info(started: dt.datetime, runtime_s: float, argv: list[str] | None = None) -> dict[str, Any]:
    """The run record: start time (UTC), runtime, command line, git state, Python, package
    and solver versions, and the machine."""
    from . import __version__

    return {
        "started_utc": started.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "runtime_s": round(float(runtime_s), 1),
        "argv": list(sys.argv if argv is None else argv),
        "git": git_state(),
        "bessbid": __version__,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "packages": package_versions(),
        "highs": highs_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "peak_memory_mb": _peak_rss_mb(),
    }


def _peak_rss_mb() -> float | None:
    """Peak resident memory of this process (MB), where the OS reports it."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
