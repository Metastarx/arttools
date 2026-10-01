"""``python -m src.ui`` - the same thing as ``python main.py ui``.

It goes through the CLI so there is one set of flags to keep right, and it puts the
project root on ``sys.path`` first so the command works from any directory.
"""

from __future__ import annotations

import sys

from src.paths import ensure_importable

ensure_importable()

from src.cli import main  # noqa: E402  (after the path is fixed)

if __name__ == "__main__":
    raise SystemExit(main(["ui", *sys.argv[1:]]))
