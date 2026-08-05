"""Persistence for workflows.

State is a single JSON file rewritten atomically. A process-wide lock keeps the
menu thread and the runner threads from interleaving writes; the file is small
enough that rewriting it on every update costs nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

from . import config
from .models import Workflow

_lock = threading.RLock()


class Store:
    """Loads and saves workflows keyed by id."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.state_path()
        self._workflows: dict[str, Workflow] = {}
        self.reload()

    def reload(self) -> None:
        with _lock:
            self._workflows = {}
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            for item in raw.get("workflows", []):
                try:
                    wf = Workflow.from_dict(item)
                except (KeyError, ValueError):
                    continue  # Skip records written by an older schema.
                self._workflows[wf.id] = wf

    def save(self) -> None:
        with _lock:
            payload = {
                "version": 1,
                "workflows": [w.to_dict() for w in self._workflows.values()],
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def add(self, workflow: Workflow) -> None:
        with _lock:
            self._workflows[workflow.id] = workflow
            self.save()

    def get(self, workflow_id: str) -> Workflow | None:
        with _lock:
            return self._workflows.get(workflow_id)

    def all(self) -> list[Workflow]:
        """Newest first."""
        with _lock:
            return sorted(
                self._workflows.values(), key=lambda w: w.created_at, reverse=True
            )

    def remove(self, workflow_id: str) -> None:
        with _lock:
            self._workflows.pop(workflow_id, None)
            self.save()


def log_dir_for(workflow_id: str) -> Path:
    """Where one workflow's step logs live (may not exist yet)."""
    return config.logs_dir() / workflow_id


def discard_logs(workflow_id: str) -> bool:
    """Delete a workflow's logs. True if anything was removed."""
    directory = log_dir_for(workflow_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


def log_path_for(
    workflow_id: str,
    role: str,
    round_no: int,
    task_index: int | None = None,
    attempt: int | None = None,
) -> Path:
    """Path of the log file for one agent invocation, parents created.

    The task and attempt are part of the name because every checklist item is
    its own invocation in the same round -- without them they would all write
    to `execute-round1.log` and overwrite each other.
    """
    directory = config.logs_dir() / workflow_id
    directory.mkdir(parents=True, exist_ok=True)

    name = f"{role}-round{round_no}"
    if task_index is not None:
        name += f"-task{task_index}"
    if attempt is not None and attempt > 1:
        name += f"-try{attempt}"
    return directory / f"{name}.log"
