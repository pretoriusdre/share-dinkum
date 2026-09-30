"""Entry points for `uv run dev` and `uv run update`.

`dev` starts the Django development server. Any extra args are passed through to manage.py, so
`uv run dev migrate` or `uv run dev test share_dinkum_app` also work. With no args it runs
`runserver`.

`update` backs up your data, pulls the latest code, syncs dependencies and applies any migrations.
"""

import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "share_dinkum_proj"
MANAGE = PROJECT / "manage.py"

DATABASE = PROJECT / "db.sqlite3"
MEDIA = PROJECT / "media"

# The backup itself is shared with the application, so the update path and the dashboard
# button cannot drift apart. It imports nothing from Django, but it lives inside the project
# directory, which is not on the path when this script is run from the repository root.
sys.path.insert(0, str(PROJECT))
from share_dinkum_app import backup  # noqa: E402


def _call(command: list[str], cwd: Path | None = None) -> int:
    """Run a command and return its exit code, surviving Ctrl+C.

    The child also receives Ctrl+C, so the first one waits for it to exit cleanly. A second
    stops it.
    """
    process = subprocess.Popen(command, cwd=cwd)

    interrupts = 0
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            interrupts += 1
            if interrupts > 1:
                process.terminate()


def main() -> None:
    argv = sys.argv[1:] or ["runserver"]
    raise SystemExit(_call([sys.executable, str(MANAGE), *argv]))


def _run(description: str, command: list[str]) -> None:
    """Run one step, stopping the update if it fails."""
    print(f"\n==> {description}")
    print(f"    {' '.join(command)}")
    if _call(command, cwd=ROOT) != 0:
        print(f"\nUpdate stopped: '{' '.join(command)}' failed.")
        print("Your data has not been changed. Fix the problem above, then run the update again.")
        raise SystemExit(1)


def _git(*args: str) -> str | None:
    """Read-only git command. Returns None if git cannot answer."""
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _local_changes() -> set[str] | None:
    """Locally changed paths, including untracked ones (which can also block a pull)."""
    status = _git("status", "--porcelain")
    if status is None:
        return None

    paths: set[str] = set()
    for line in status.splitlines():
        path = line[3:].strip()
        if " -> " in path:  # renames are reported as "old -> new"
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path.strip('"'))
    return paths


def _incoming_changes() -> set[str] | None:
    """Paths the update would change. Returns None if there is no upstream to compare against."""
    upstream = _git("rev-parse", "--abbrev-ref", "@{u}")
    if not upstream:
        return None

    changed = _git("diff", "--name-only", f"HEAD..{upstream.strip()}")
    if changed is None:
        return None
    return {line.strip() for line in changed.splitlines() if line.strip()}


def conflicting_paths(local_changes: set[str], incoming_changes: set[str] | None) -> list[str]:
    """Paths changed both locally and by the update.

    If `incoming_changes` is None (no upstream), every local change counts.
    """
    if incoming_changes is None:
        return sorted(local_changes)
    return sorted(local_changes & incoming_changes)


def _backup() -> Path | None:
    """Back up the database and media to the shared backup folder. Returns its path, or None."""
    result = backup.make_backup(DATABASE, MEDIA)
    if result is None:
        print("\n==> No data to back up yet, skipping.")
        return None

    print(f"\n==> Backing up your data to {result['path']}")
    if result["database_bytes"]:
        print(f"    database  {result['database_bytes'] / 1024 / 1024:.1f} MB")
    if result["media_files"]:
        print(f"    media     {result['media_files']} files")
    if result["removed"]:
        print(f"    pruned    {len(result['removed'])} older backup(s)")

    return result["path"]


def update() -> None:
    """Back up, pull the latest code, sync dependencies, and apply migrations."""

    # Each step below prints before handing off to a child process that writes to the same terminal.
    # Without line buffering this output is block-buffered when redirected, and the steps appear
    # out of order relative to the output of the commands they describe.
    cast(io.TextIOWrapper, sys.stdout).reconfigure(line_buffering=True)

    local_changes = _local_changes()
    if local_changes is None:
        print("Could not run git. Is it installed, and is this a git clone?")
        raise SystemExit(1)

    _run("Checking for updates", ["git", "fetch"])
    incoming_changes = _incoming_changes()

    if incoming_changes is not None and not incoming_changes:
        print("\nAlready up to date. Nothing to do.")
        return

    conflicts = conflicting_paths(local_changes, incoming_changes)
    if conflicts:
        print("\nUpdate stopped: the new version changes files you have edited:\n")
        for path in conflicts:
            print(f"    {path}")
        print("\nCommit or discard your changes to those files, then run the update again.")
        print("Nothing has been changed.")
        raise SystemExit(1)

    backup_path = _backup()

    _run("Getting the latest code", ["git", "pull"])
    _run("Installing any new dependencies", ["uv", "sync"])
    _run("Updating the database structure", [sys.executable, str(MANAGE), "migrate"])

    print("\nUpdate complete. Start the app with:  uv run dev")
    if backup_path:
        print(f"If something looks wrong, your previous data is in {backup_path}")


if __name__ == "__main__":
    # Normally reached as the `dev` console script rather than as a file. Without this, running the
    # file directly defines these functions, does nothing and exits 0, which looks like a server
    # that started and stopped instantly.
    main()
