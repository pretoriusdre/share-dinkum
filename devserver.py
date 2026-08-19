"""Entry points for `uv run dev` and `uv run update`.

`dev` starts the Django development server. Any extra args are passed through to manage.py, so
`uv run dev migrate` or `uv run dev test share_dinkum_app` also work. With no args it runs
`runserver`.

`update` backs up your data, pulls the latest code, syncs dependencies and applies any migrations.
"""

import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT / "share_dinkum_proj"
MANAGE = PROJECT / "manage.py"

DATABASE = PROJECT / "db.sqlite3"
MEDIA = PROJECT / "media"
BACKUP_ROOT = Path.home() / "share-dinkum-backups"


def _call(command, cwd=None):
    """Run a command to completion and return its exit code, surviving Ctrl+C.

    Ctrl+C in a console is delivered to every process attached to it, so the child gets it too and
    stops on its own. Waiting through the interrupt lets it print its own shutdown message and set
    its own exit code, instead of this process dying first and printing a traceback over the top.
    A second Ctrl+C means the child is not stopping by itself, so it gets stopped here rather than
    leaving the window stuck with no way out.
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


def main():
    argv = sys.argv[1:] or ["runserver"]
    raise SystemExit(_call([sys.executable, str(MANAGE), *argv]))


def _run(description, command):
    """Run one step, stopping the update if it fails."""
    print(f"\n==> {description}")
    print(f"    {' '.join(command)}")
    if _call(command, cwd=ROOT) != 0:
        print(f"\nUpdate stopped: '{' '.join(command)}' failed.")
        print("Your data has not been changed. Fix the problem above, then run the update again.")
        raise SystemExit(1)


def _git(*args):
    """Read-only git command. Returns None if git cannot answer."""
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _local_changes():
    """Paths you have changed, including untracked ones.

    An untracked file still blocks a pull that wants to create the same path, so both matter.
    """
    status = _git("status", "--porcelain")
    if status is None:
        return None

    paths = set()
    for line in status.splitlines():
        path = line[3:].strip()
        if " -> " in path:  # renames are reported as "old -> new"
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path.strip('"'))
    return paths


def _incoming_changes():
    """Paths the update would change. Returns None if there is no upstream to compare against."""
    upstream = _git("rev-parse", "--abbrev-ref", "@{u}")
    if not upstream:
        return None

    changed = _git("diff", "--name-only", f"HEAD..{upstream.strip()}")
    if changed is None:
        return None
    return {line.strip() for line in changed.splitlines() if line.strip()}


def conflicting_paths(local_changes, incoming_changes):
    """Files that both you and the update have touched, which are the only ones that can conflict.

    Editing your own copy of the import notebook, or merely running it, must not block an update
    that does not go near it. With no upstream to compare against there is no way to tell, so every
    local change is treated as a possible conflict.
    """
    if incoming_changes is None:
        return sorted(local_changes)
    return sorted(local_changes & incoming_changes)


def _copy_database(source, destination):
    """Copy a SQLite database using its online backup API, which is safe against concurrent writes."""
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            with destination_connection:
                source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


def _backup():
    """Copy the database and media folder to the user's home directory."""
    if not DATABASE.exists() and not MEDIA.exists():
        print("\n==> No data to back up yet, skipping.")
        return None

    backup_path = BACKUP_ROOT / datetime.now().strftime("%Y-%m-%dT%H%M%S")
    backup_path.mkdir(parents=True, exist_ok=True)

    print(f"\n==> Backing up your data to {backup_path}")
    if DATABASE.exists():
        _copy_database(DATABASE, backup_path / DATABASE.name)
        print(f"    database  {DATABASE.stat().st_size / 1024 / 1024:.1f} MB")
    if MEDIA.exists():
        shutil.copytree(MEDIA, backup_path / MEDIA.name)
        print(f"    media     {sum(1 for _ in (MEDIA).rglob('*') if _.is_file())} files")

    return backup_path


def update():
    """Back up, pull the latest code, sync dependencies, and apply migrations."""

    # Each step below prints before handing off to a child process that writes to the same terminal.
    # Without line buffering this output is block-buffered when redirected, and the steps appear
    # out of order relative to the output of the commands they describe.
    sys.stdout.reconfigure(line_buffering=True)

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
