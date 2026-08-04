"""Reading step logs, live or after the fact."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ..models import Status, Workflow
from . import render
from .render import CYAN, GREEN, GREY, RED, RESET, YELLOW, colored


def drain(path: Path, position: int) -> int:
    """Print whatever was appended since `position`; return the new one."""
    if not path.exists():
        return position
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(position)
        chunk = handle.read()
        new_position = handle.tell()
    if chunk:
        sys.stdout.write(chunk)
        sys.stdout.flush()
    return new_position


def tail(path: Path, lines: int) -> str:
    """Last `lines` non-empty lines, for inline failure context."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read the log: {exc})"
    kept = [line for line in content.splitlines() if line.strip()]
    return "\n".join(kept[-lines:]) or "(the log is empty)"


def step_marker(step, starting: bool) -> str:
    bar = "-" * 12
    if starting:
        scope = f"task {step.task_index}" if step.task_index else f"round {step.round}"
        return (
            f"\n{CYAN}{bar} {step.role.value} | {step.agent} | {scope} | "
            f"started {bar}{RESET}"
        )
    color = GREEN if step.status is Status.DONE else RED
    detail = f"exit {step.exit_code}" if step.exit_code is not None else step.status.value
    return (
        f"\n{color}{bar} {step.role.value} finished | {detail} | "
        f"{render.elapsed(step.duration)} {bar}{RESET}"
    )


def stream(wf: Workflow, roles: set[str] | None, is_running) -> None:
    """Print the selected step logs, following a run that is still going.

    Finished steps print in full; a running one is followed live and the next
    is picked up as it starts. Each step is bracketed by a marker so it is
    always clear who is talking and where one step ends.
    """
    print(f"{GREY}  Ctrl-C stops following; the run keeps going.{RESET}")

    offsets: dict[tuple[str, int, int | None], int] = {}
    opened: set[tuple[str, int, int | None]] = set()
    closed: set[tuple[str, int, int | None]] = set()
    interrupted = False

    try:
        while True:
            steps = [s for s in wf.steps if roles is None or s.role.value in roles]
            for index, step in enumerate(steps):
                key = (step.role.value, step.round, index)
                if key not in opened:
                    print(step_marker(step, starting=True))
                    opened.add(key)
                if step.log_path:
                    offsets[key] = drain(Path(step.log_path), offsets.get(key, 0))
                if step.status in (Status.DONE, Status.FAILED) and key not in closed:
                    print(step_marker(step, starting=False))
                    closed.add(key)

            still_going = is_running(wf.id) or any(
                s.status in (Status.PENDING, Status.RUNNING) for s in steps
            )
            if not still_going:
                break
            time.sleep(0.4)
    except KeyboardInterrupt:
        interrupted = True

    print()
    if interrupted:
        print(colored("  -- stopped following (the run continues) --", YELLOW))
        return

    scope = "all steps" if roles is None else f"the {', '.join(sorted(roles))} step"
    print(colored(f"  -- end of log for {scope} --", GREY))
    print(f"  {wf.id[:6]} is {render.status_label(wf.status)}")
    if wf.message:
        color = RED if wf.status is Status.FAILED else GREY
        print(f"  {colored(wf.message, color)}")
    for note in wf.notes:
        print(f"  {colored('warning: ' + note, YELLOW)}")


def failure_context(wf: Workflow, lines: int = 25) -> list[str]:
    """The tail of whichever step failed, so a failure explains itself."""
    culprit = next(
        (s for s in reversed(wf.steps) if s.status is Status.FAILED),
        wf.steps[-1] if wf.steps else None,
    )
    if not culprit or not culprit.log_path:
        return []
    path = Path(culprit.log_path)
    if not path.exists():
        return []
    return [
        "",
        f"  {GREY}last output of the {culprit.role.value} step:{RESET}",
        render.indent(tail(path, lines)),
        f"  {GREY}full log: {culprit.log_path}{RESET}",
    ]
