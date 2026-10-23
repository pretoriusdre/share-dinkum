"""Replies kept on disk, so a document is sent once per model and a rerun gives the same answer.

The key covers the file's content, the provider, the model, the server and the prompt, so changing any
of them asks again.
"""

import hashlib
import json
from pathlib import Path


def key(*parts: str) -> str:
    return hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()


class Cache:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def get(self, cache_key: str) -> str | None:
        path = self.folder / f'{cache_key}.json'
        if not path.is_file():
            return None
        return str(json.loads(path.read_text(encoding='utf-8'))['reply'])

    def put(self, cache_key: str, reply: str, source_name: str, label: str) -> None:
        """Keep `reply`. `source_name` is the document's file name, redacted, as the cache is plain text."""
        self.folder.mkdir(parents=True, exist_ok=True)
        record = {'source': source_name, 'reader': label, 'reply': reply}
        (self.folder / f'{cache_key}.json').write_text(json.dumps(record, indent=1), encoding='utf-8')
