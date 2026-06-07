"""Entry point for `uv run dev` — starts the Django development server.

Any extra args are passed through to manage.py, so `uv run dev migrate`
or `uv run dev test share_dinkum_app` also work. With no args it runs
`runserver`.
"""

import subprocess
import sys
from pathlib import Path


def main():
    manage = Path(__file__).resolve().parent / "share_dinkum_proj" / "manage.py"
    argv = sys.argv[1:] or ["runserver"]
    raise SystemExit(subprocess.call([sys.executable, str(manage), *argv]))
