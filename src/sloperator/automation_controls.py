"""Durable enable/disable switches for admin-managed automations."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class AutomationControls:
    """Store disabled automation keys in a small atomically-written JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def disabled(self, kind: str, key: str) -> bool:
        return key in self._read().get(kind, [])

    def set_enabled(self, kind: str, key: str, enabled: bool) -> None:
        state = self._read()
        disabled = set(state.get(kind, []))
        disabled.discard(key) if enabled else disabled.add(key)
        state[kind] = sorted(disabled)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".automation-", text=True)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self) -> dict[str, list[str]]:
        try:
            value = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return {str(k): [str(item) for item in v] for k, v in value.items() if isinstance(v, list)}
