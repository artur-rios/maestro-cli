"""User defaults for new workflows.

Kept apart from `config`, which holds how to invoke each CLI. This is what the
user prefers: which repo, which agent in which role, how careful to be. Inline
flags override these for a single run without touching the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config

SETTINGS_FILE = "settings.json"

#: What a user types to leave the plan or review step out of a run. Stored as
#: OFF, which is why "" cannot serve: an empty agent means "work it out at
#: launch", and a skipped step has to be told apart from an unset one.
OFF = "off"
OFF_WORDS = frozenset({OFF, "none", "no", "skip", "-"})

#: Empty strings mean "work it out at launch": the repo becomes the working
#: directory, and each role becomes the first installed backend.
DEFAULTS: dict[str, Any] = {
    "repo": "",
    "plan": "",
    "execute": "",
    "review": "",
    # Empty means whatever model the CLI itself would choose.
    "plan_model": "",
    "execute_model": "",
    "review_model": "",
    "mode": "supervised",
    "rounds": 2,
    "attempts": 3,
    "confirm": True,
    "follow": True,
    # Redraw the wordmark above every prompt.
    "pinned": True,
    "branch_type": "",
    # Appended to every agent prompt, for standing house rules.
    "instructions": "",
    "prompt_paths": [],
}

#: Keys `/set` accepts, with how to read the typed value.
EDITABLE: dict[str, str] = {
    "repo": "path",
    "plan": "optional_agent",
    "execute": "required_agent",
    "review": "optional_agent",
    "plan_model": "text",
    "execute_model": "text",
    "review_model": "text",
    "mode": "mode",
    "rounds": "int",
    "attempts": "int",
    "confirm": "bool",
    "follow": "bool",
    "pinned": "bool",
    "branch_type": "text",
    "instructions": "text",
}


class SettingsError(ValueError):
    """A value the user typed cannot be used."""


def path() -> Path:
    return config.home_dir() / SETTINGS_FILE


def load() -> dict[str, Any]:
    """Current settings, with anything missing filled from the defaults."""
    merged = json.loads(json.dumps(DEFAULTS))
    file = path()
    if not file.exists():
        return merged
    try:
        stored = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return merged
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return merged


def save(settings: dict[str, Any]) -> None:
    keep = {k: v for k, v in settings.items() if k in DEFAULTS}
    path().write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")


def coerce(key: str, raw: str) -> Any:
    """Turn a typed value into the stored one, or explain why it will not do."""
    kind = EDITABLE.get(key)
    if kind is None:
        raise SettingsError(
            f"'{key}' is not a setting - try: {', '.join(sorted(EDITABLE))}"
        )

    value = (raw or "").strip()
    if kind == "int":
        if not value.isdigit() or int(value) < 1:
            raise SettingsError(f"{key} must be a whole number of 1 or more")
        return int(value)
    if kind == "bool":
        if value.lower() in {"on", "yes", "true", "1"}:
            return True
        if value.lower() in {"off", "no", "false", "0"}:
            return False
        raise SettingsError(f"{key} must be on or off")
    if kind == "mode":
        return normalise_mode(value)
    if kind == "path":
        return str(Path(value).expanduser()) if value else ""
    if kind == "optional_agent":
        return OFF if is_off(value) else value
    if kind == "required_agent":
        if is_off(value):
            raise SettingsError(
                "the execute step cannot be skipped - it is the one that does "
                "the work"
            )
        return value
    return value


def is_off(value: str) -> bool:
    """Whether a typed agent name means "leave this step out"."""
    return (value or "").strip().lower() in OFF_WORDS


def normalise_mode(value: str) -> str:
    """Accept the short forms people actually type."""
    lowered = (value or "").strip().lower()
    if lowered in {"auto", "autonomous"}:
        return "autonomous"
    if lowered in {"super", "supervised", "manual"}:
        return "supervised"
    raise SettingsError("mode must be 'supervised' or 'autonomous'")


def add_prompt_path(settings: dict[str, Any], raw: str) -> Path:
    """Register another root for the prompt library."""
    directory = Path((raw or "").strip().strip('"').strip("'")).expanduser()
    if not directory.exists():
        raise SettingsError(f"no such directory: {directory}")
    if not directory.is_dir():
        raise SettingsError(f"{directory} is a file, not a directory")

    resolved = str(directory.resolve())
    paths = list(settings.get("prompt_paths", []))
    if resolved not in paths:
        paths.append(resolved)
        settings["prompt_paths"] = paths
    return directory


def remove_prompt_path(settings: dict[str, Any], raw: str) -> bool:
    """Forget a root. True when one was actually removed."""
    wanted = (raw or "").strip().strip('"').strip("'")
    candidates = {wanted}
    try:
        candidates.add(str(Path(wanted).expanduser().resolve()))
    except OSError:
        pass

    paths = list(settings.get("prompt_paths", []))
    kept = [p for p in paths if p not in candidates]
    settings["prompt_paths"] = kept
    return len(kept) != len(paths)
