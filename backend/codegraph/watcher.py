"""File watcher (v4): auto re-parse when the repo changes on disk.

watchdog observer watching the parsed repo path; any change to a file with a
registered language extension triggers a debounced re-parse of the whole repo
(the TRD's sanctioned fallback — incremental per-file patching can come later).
Debouncing matters: editors fire create/write/rename cascades on one save.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .parser import LANGUAGE_REGISTRY

_WATCHED_EXTS = set(LANGUAGE_REGISTRY)


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, callback, debounce_seconds: float, root: Path):
        self.callback = callback
        self.debounce = max(debounce_seconds, 0.2)
        self.root = root
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last_fired: float = 0.0
        self.fire_count: int = 0
        self.last_error: str | None = None

    def _relevant(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in _WATCHED_EXTS:
            return False
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return False
        # skip artifacts of the tool's own data dir if the repo contains it
        return "__pycache__" not in rel.parts and "node_modules" not in rel.parts

    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        try:
            self.callback()
            self.fire_count += 1
            self.last_fired = time.time()
        except Exception as e:  # never let a bad re-parse kill the watcher
            self.last_error = str(e)

    # any mutation of a watched file counts
    def on_modified(self, event):
        if not event.is_directory and self._relevant(str(event.src_path)):
            self._schedule()

    def on_created(self, event):
        if not event.is_directory and self._relevant(str(event.src_path)):
            self._schedule()

    def on_moved(self, event):
        if not event.is_directory and self._relevant(str(event.dest_path)):
            self._schedule()

    def on_deleted(self, event):
        if not event.is_directory and self._relevant(str(event.src_path)):
            self._schedule()


class FileWatcher:
    """Owns the watchdog observer for the currently parsed repo."""

    def __init__(self, on_change, debounce_seconds: float = 1.5):
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._observer: Observer | None = None
        self._watching: str | None = None
        self._lock = threading.Lock()

    def start(self, repo_path: str) -> dict:
        with self._lock:
            root = str(Path(repo_path).resolve())
            if self._observer is not None and self._watching == root:
                return self.status()
            self.stop_locked()
            handler = _DebouncedHandler(self.on_change, self.debounce_seconds, Path(root))
            observer = Observer()
            observer.daemon = True
            observer.schedule(handler, root, recursive=True)
            observer.start()
            self._observer = observer
            self._watching = root
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self.stop_locked()
            return self.status()

    def stop_locked(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
            self._watching = None

    def status(self) -> dict:
        return {
            "active": self._observer is not None,
            "repo": self._watching,
            "debounce_seconds": self.debounce_seconds,
        }
