"""Turning a role plus a prompt into an actual CLI invocation.

The command templates come from config; this module only knows how to expand
them, run the process, and tee its output to a log file.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .models import Role


class BackendError(Exception):
    """Raised when a backend cannot be invoked at all."""


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _detached() -> dict[str, Any]:
    """Keep an agent out of the console's Ctrl-C group.

    Windows delivers a console Ctrl-C to every process attached to that
    console, so pressing it to stop following a log would also kill the agent
    running in the background -- it dies with 0xC000013A and the run fails
    through no fault of the agent. A new process group insulates the child.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def sends_prompt_on_stdin(backend: dict[str, Any]) -> bool:
    return backend.get("prompt_via", "arg") == "stdin"


def build_argv(
    backend: dict[str, Any], role: Role, prompt: str, cwd: Path | str = ""
) -> list[str]:
    """Expand a backend's template for `role` into an argv list.

    With `prompt_via: stdin` the prompt is not placed on the command line at
    all and any `{prompt}` placeholder is dropped. This is the default for the
    built-in backends because a command-line argument cannot carry newlines
    through a Windows batch wrapper such as `opencode.cmd` -- cmd.exe silently
    truncates the argument at the first line break, so the agent would receive
    only the opening line of its instructions.

    Otherwise `{prompt}` is substituted as a whole argument rather than
    interpolated into a shell string, so no quoting or escaping is involved.

    `{cwd}` expands to the workflow's worktree. Some CLIs ignore the process
    working directory and need to be told where to work explicitly.
    """
    binary = backend.get("bin")
    if not binary:
        raise BackendError("backend has no 'bin' configured")

    if role is Role.EXECUTE:
        template = backend.get("execute_args") or backend.get("args") or []
    else:
        template = backend.get("args") or []

    if not template:
        raise BackendError(f"backend '{binary}' has no args configured")

    via_stdin = sends_prompt_on_stdin(backend)
    argv = [binary]
    for part in template:
        if "{prompt}" in part:
            if via_stdin:
                continue
            part = part.replace("{prompt}", prompt)
        if "{cwd}" in part:
            part = part.replace("{cwd}", str(cwd))
        argv.append(part)
    return argv


def resolve(name: str, backends: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    backends = backends if backends is not None else config.load_backends()
    backend = backends.get(name)
    if backend is None:
        raise BackendError(f"unknown backend '{name}'")
    return backend


def with_model(argv: list[str], backend: dict[str, Any], model: str) -> list[str]:
    """Point an already-built argv at a specific model.

    Replaces whatever the backend was configured with, so a run can choose a
    model without editing anyone's settings.
    """
    flag = backend.get("model_flag")
    if not model or not flag:
        return argv
    argv = list(argv)
    if flag in argv:
        index = argv.index(flag)
        argv[index + 1 : index + 2] = [model]
    else:
        argv += [flag, model]
    return argv


def run(
    name: str,
    role: Role,
    prompt: str,
    cwd: Path | str,
    log_file: Path,
    backends: dict[str, dict[str, Any]] | None = None,
    model: str = "",
) -> RunResult:
    """Invoke a backend once and capture everything it wrote.

    stdout and stderr are merged: the point of the log is to show the user what
    the agent said, and the CLIs interleave progress on stderr.
    """
    backend = resolve(name, backends)
    argv = with_model(build_argv(backend, role, prompt, cwd), backend, model)

    binary = config.find_binary(backend)
    if binary is None:
        raise BackendError(
            f"'{argv[0]}' could not be found - install it, put it on PATH, "
            f"or fix 'bin' in backends.json"
        )
    argv[0] = binary

    timeout = backend.get("timeout") or config.DEFAULT_TIMEOUT
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # The child gets a pipe or /dev/null, never the terminal. Agents run in
    # background threads while the menu owns the terminal, so an inherited
    # stdin would let them eat the keystrokes meant for the menu.
    via_stdin = sends_prompt_on_stdin(backend)

    header = f"$ {' '.join(argv[:1])} ... (cwd={cwd})\n\n"
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE if via_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line buffered, so the log grows as the agent talks
            **_detached(),
        )
    except OSError as exc:
        raise BackendError(f"failed to start '{argv[0]}': {exc}") from exc

    timed_out = threading.Event()

    def _give_up() -> None:
        timed_out.set()
        try:
            proc.kill()
        except OSError:  # already gone
            pass

    watchdog = threading.Timer(timeout, _give_up)
    watchdog.daemon = True
    watchdog.start()

    if via_stdin:
        # Written on its own thread: a large prompt can fill the pipe buffer,
        # and the child may be blocked writing output we have not read yet.
        threading.Thread(
            target=_feed_stdin, args=(proc, prompt), daemon=True
        ).start()

    chunks: list[str] = []
    try:
        with log_file.open("w", encoding="utf-8", errors="replace") as log:
            log.write(header)
            log.flush()
            if proc.stdout is not None:
                for line in proc.stdout:
                    chunks.append(line)
                    log.write(line)
                    log.flush()  # so `View logs` can follow it live
            if timed_out.is_set():
                log.write(f"\n[timed out after {timeout}s]\n")
                log.flush()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        exit_code = proc.wait()
        watchdog.cancel()

    return RunResult(
        exit_code=exit_code,
        output="".join(chunks),
        timed_out=timed_out.is_set(),
    )


def _feed_stdin(proc: subprocess.Popen, prompt: str) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.write(prompt)
            proc.stdin.close()
    except (OSError, ValueError):
        pass  # the agent exited before reading its prompt


#: `build (primary)` for agents, `opencode-go/glm-5.2` for models.
_CHOICE_LINE = re.compile(
    r"^([A-Za-z0-9][\w./:-]*)\s*(?:\((primary|subagent|all)\))?\s*$"
)


def list_options(backend: dict[str, Any], kind: str) -> list[tuple[str, str]]:
    """What a backend offers for `kind` ('agent' or 'model'), as (name, note).

    Some CLIs bundle several agents with different tool permissions -- opencode
    ships a read-only `plan` and a writing `build` -- and which one runs makes
    the difference between a step that edits files and one that only looks.
    Models are listed the same way where the CLI can enumerate them.
    """
    command = backend.get(f"{kind}s_command")
    binary = config.find_binary(backend)
    if not command or not binary:
        return []
    try:
        proc = subprocess.run(
            [binary, *command], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
            stdin=subprocess.DEVNULL, **_detached(),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        match = _CHOICE_LINE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        found.append((name, match.group(2) or ""))
    return found


def model_choices(backend: dict[str, Any]) -> list[str]:
    """Models to offer for a backend.

    Whatever the CLI can enumerate, else the list configured for it. codex
    has a `models` command but it insists on a terminal, and claude has none
    at all, so both rely on `known_models` from backends.json.
    """
    listed = [name for name, _ in list_options(backend, "model")]
    if listed:
        return listed
    return [str(m) for m in backend.get("known_models", []) if m]


def list_agents(backend: dict[str, Any]) -> list[tuple[str, str]]:
    return list_options(backend, "agent")


def list_models(backend: dict[str, Any]) -> list[tuple[str, str]]:
    return list_options(backend, "model")


def current_flag_value(
    backend: dict[str, Any], role: Role, flag_key: str
) -> str | None:
    """The value the backend passes for `flag_key` on `role`, if any."""
    flag = backend.get(flag_key)
    if not flag:
        return None
    argv = backend.get("execute_args" if role is Role.EXECUTE else "args") or []
    for index, part in enumerate(argv):
        if part == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def set_flag_value(
    backend: dict[str, Any], key: str, flag_key: str, value: str
) -> None:
    """Set (or replace) a flag's value in 'args' or 'execute_args'."""
    flag = backend.get(flag_key)
    if not flag:
        return
    argv = list(backend.get(key) or [])
    if flag in argv:
        index = argv.index(flag)
        argv[index + 1 : index + 2] = [value]
    else:
        argv += [flag, value]
    backend[key] = argv


def current_agent(backend: dict[str, Any], role: Role) -> str | None:
    return current_flag_value(backend, role, "agent_flag")


def set_agent(backend: dict[str, Any], key: str, agent: str) -> None:
    set_flag_value(backend, key, "agent_flag", agent)


def current_model(backend: dict[str, Any], role: Role) -> str | None:
    return current_flag_value(backend, role, "model_flag")


def set_model(backend: dict[str, Any], key: str, model: str) -> None:
    set_flag_value(backend, key, "model_flag", model)


def available(backends: dict[str, dict[str, Any]] | None = None) -> list[tuple[str, bool]]:
    """All configured backend names paired with whether their binary exists."""
    backends = backends if backends is not None else config.load_backends()
    return [(name, config.is_installed(b)) for name, b in sorted(backends.items())]
