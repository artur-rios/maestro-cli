"""Where things live on disk, and the editable backend definitions.

Backend command templates live in a JSON file rather than in code because the
flags of `claude`, `codex` and `opencode` change often enough that the user
needs to fix them without a code edit.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
from pathlib import Path
from typing import Any

APP_NAME = "maestro"

#: Bumped whenever the built-in command templates change. A stored file from an
#: older version has its built-in templates replaced on load.
CONFIG_VERSION = 4

#: Reserved key holding CONFIG_VERSION inside backends.json.
VERSION_KEY = "_version"

#: How long a single agent invocation may run before it is killed, in seconds.
#: A real checklist item against a large codebase can take well over half an
#: hour, so this is generous by default; lower it per backend if you prefer.
DEFAULT_TIMEOUT = 5400

DEFAULT_BACKENDS: dict[str, dict[str, Any]] = {
    "claude": {
        "bin": "claude",
        "prompt_via": "stdin",
        "args": ["-p"],
        "execute_args": ["-p", "--permission-mode", "acceptEdits"],
        # No list command; a model id is typed in.
        "model_flag": "--model",
        # `claude --model` takes an alias for the current model of that
        # size, so these stay correct as models are replaced. Edit freely.
        "known_models": ["fable", "opus", "sonnet", "haiku"],
        "search_globs": [
            "~/.local/bin/claude.exe",
            "~/.local/bin/claude",
            "~/AppData/Roaming/npm/claude.cmd",
            "~/.npm-global/claude.cmd",
        ],
        "timeout": DEFAULT_TIMEOUT,
    },
    "codex": {
        "bin": "codex",
        "prompt_via": "stdin",
        "args": ["exec", "-C", "{cwd}", "-s", "read-only"],
        "execute_args": ["exec", "-C", "{cwd}", "-s", "workspace-write"],
        "model_flag": "--model",
        # `codex models` needs a terminal, so it cannot be read here. This is
        # a starting point; add the ids you use to backends.json.
        "known_models": ["gpt-5.6-terra"],
        # Codex installs to a stable directory in newer builds, and to one
        # named after the version in older ones. Neither is reliably on PATH.
        "search_globs": [
            "%LOCALAPPDATA%/Programs/OpenAI/Codex/bin/codex.exe",
            "%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe",
            "~/.local/bin/codex",
            "~/.local/share/openai/codex/bin/*/codex",
        ],
        "timeout": DEFAULT_TIMEOUT,
    },
    "opencode": {
        "bin": "opencode",
        "prompt_via": "stdin",
        # opencode ignores the process working directory and roots itself at
        # its own project detection, so the worktree must be named explicitly
        # or the agent edits whatever repository it happens to find.
        # It also ships several agents: `plan` reads, `build` writes, which
        # lines up exactly with the read-only and writing roles here.
        "args": ["run", "--dir", "{cwd}", "--agent", "plan"],
        "execute_args": ["run", "--dir", "{cwd}", "--agent", "build"],
        "agent_flag": "--agent",
        "agents_command": ["agent", "list"],
        "model_flag": "--model",
        "models_command": ["models"],
        # Installed by npm, so the location follows the npm global prefix.
        "search_globs": [
            "~/.npm-global/opencode.cmd",
            "~/AppData/Roaming/npm/opencode.cmd",
            "~/.npm-global/bin/opencode",
            "~/.local/bin/opencode",
            "/usr/local/bin/opencode",
        ],
        "timeout": DEFAULT_TIMEOUT,
    },
}


def home_dir() -> Path:
    """Root of all maestro state. Override with MAESTRO_HOME for tests."""
    override = os.environ.get("MAESTRO_HOME")
    root = Path(override) if override else Path.home() / f".{APP_NAME}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def backends_path() -> Path:
    return home_dir() / "backends.json"


def state_path() -> Path:
    return home_dir() / "state.json"


def logs_dir() -> Path:
    path = home_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def worktrees_dir() -> Path:
    path = home_dir() / "worktrees"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_backends() -> dict[str, dict[str, Any]]:
    """Read backends.json, writing the defaults out on first run.

    Unknown keys in the file are preserved so the user can add their own CLIs;
    missing built-ins are filled back in.
    """
    path = backends_path()
    if not path.exists():
        save_backends(DEFAULT_BACKENDS)
        return json.loads(json.dumps(DEFAULT_BACKENDS))

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_BACKENDS))

    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_BACKENDS))

    stored_version = data.get(VERSION_KEY, 1) if isinstance(data, dict) else 1
    stale = stored_version < CONFIG_VERSION

    merged = json.loads(json.dumps(DEFAULT_BACKENDS))
    migrated = stale
    for name, backend in data.items():
        if name == VERSION_KEY or not isinstance(backend, dict):
            continue
        if name in DEFAULT_BACKENDS and (stale or _has_legacy_templates(backend)):
            migrated = True
            # Templates from an older version carry bugs that were fixed since:
            # the prompt on the command line (truncated at the first newline by
            # a batch wrapper), a missing `{cwd}` (opencode edits the wrong
            # repository), or no agent selection (its read-only agent cannot
            # implement anything). The built-in templates win; everything else
            # the user set is preserved.
            backend = {
                k: v for k, v in backend.items() if k not in ("args", "execute_args")
            }
        if name in merged:
            # Merge per key, not per backend. A file written by an older
            # version is missing keys added since -- notably `search_globs` --
            # and replacing the whole entry would silently drop them. The
            # user's own edits still win over the defaults.
            combined = dict(merged[name])
            combined.update(backend)
            merged[name] = combined
        else:
            merged[name] = backend

    if migrated:
        save_backends(merged)
    return merged


def _has_legacy_templates(backend: dict[str, Any]) -> bool:
    """True when a stored backend still passes the prompt on the command line."""
    for key in ("args", "execute_args"):
        for part in backend.get(key) or []:
            if isinstance(part, str) and "{prompt}" in part:
                return True
    return False


def save_backends(backends: dict[str, dict[str, Any]]) -> None:
    payload = {VERSION_KEY: CONFIG_VERSION}
    payload.update({k: v for k, v in backends.items() if k != VERSION_KEY})
    backends_path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def find_binary(backend: dict[str, Any]) -> str | None:
    """Absolute path to a backend's executable, or None if it is not installed.

    PATH is tried first. A CLI can be installed and still be missing from PATH
    -- notably on Windows, where installers drop the binary in a directory
    named after the version -- so `search_globs` gives each backend a list of
    known install locations to fall back on. The newest match wins, which keeps
    working after the tool updates itself into a new directory.
    """
    name = backend.get("bin", "")
    if not name:
        return None

    found = shutil.which(name)
    if found:
        return found

    matches: list[str] = []
    for pattern in backend.get("search_globs", []):
        expanded = os.path.expandvars(os.path.expanduser(pattern))
        matches.extend(p for p in glob.glob(expanded) if os.path.isfile(p))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def is_installed(backend: dict[str, Any]) -> bool:
    """True when the backend's binary can be located."""
    return find_binary(backend) is not None
