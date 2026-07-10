"""One place that knows where GOAT's home is.

From source, home is the repo root (parent of this file's folder). In a
PyInstaller bundle __file__ points into the extracted/dist folder instead,
so the frozen exe falls back to the real install path — overridable with a
GOAT_ROOT environment variable if the whole folder ever moves.
"""
import os
import sys

if getattr(sys, "frozen", False):
    GOAT_ROOT = os.environ.get("GOAT_ROOT", r"C:\Users\user\goat")
else:
    GOAT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
