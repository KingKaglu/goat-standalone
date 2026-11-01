"""One place that knows where GOAT's home is.

From source, home is the repo root (parent of this file's folder). In a
PyInstaller bundle __file__ points into the extracted/dist folder instead,
so the frozen exe falls back to the real install path — overridable with a
GOAT_ROOT environment variable if the whole folder ever moves.
"""
import os
import sys

if os.environ.get("GOAT_ROOT"):
    GOAT_ROOT = os.environ["GOAT_ROOT"]
elif getattr(sys, "frozen", False):
    # The old fallback named a "user" account that no longer exists after
    # the 2026-11-01 reinstall; the bundle sits next to its own exe.
    GOAT_ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    GOAT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
