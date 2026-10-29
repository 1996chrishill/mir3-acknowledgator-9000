import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger("history")
MAX_ENTRIES = 5000


class History:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> list:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning("history.json unreadable (%s); starting new", e)
            return []

    def recent(self, limit: int | None = None) -> list:
        """Entries newest first, optionally capped at `limit`."""
        data = self._read()
        data.reverse()
        return data[:limit] if limit else data

    def add(self, entries: list[dict]) -> None:
        if not entries:
            return
        data = self._read()
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        data.extend({"timestamp": ts, **e} for e in entries)
        data = data[-MAX_ENTRIES:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
