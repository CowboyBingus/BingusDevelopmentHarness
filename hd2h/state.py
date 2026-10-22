"""The active-session manifest: every change is recorded here before it is made."""
import json
import os
from pathlib import Path


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'active.json'

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None

    def save(self, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')
        os.replace(temporary, self.path)

    def update(self, **changes):
        data = self.load()
        if data is None:
            raise RuntimeError('no active session to update')
        data.update(changes)
        self.save(data)
        return data

    def clear(self):
        if self.path.exists():
            self.path.unlink()
