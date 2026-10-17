"""Update Share Dinkum: stop the server, then run `uv run update.py` from the repository root.

A file rather than a console script, so the process running the update is the environment's
python.exe. `uv sync` replaces the project's own Scripts/*.exe, which Windows refuses while one of
them is running ("Access is denied").
"""

import devserver

if __name__ == "__main__":
    devserver.update()
