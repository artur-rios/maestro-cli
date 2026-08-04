"""Reading step logs, live or after the fact."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rich.box import ROUNDED
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from ..models import Status, Workflow
from . import render


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


def step_marker(step, starting: bool) -> Rule:
    """A rule around each step, so it is clear who is talking."""
    if starting:
        scope = f"task {step.task_index}" if step.task_index else f"round {step.round}"
        title = f"{step.role.value} | {step.agent} | {scope}"
        return Rule(Text(title, style="info"), style="info", align="left")

    style = "success" if step.status is Status.DONE else "error"
    detail = (
        f"exit {step.exit_code}" if step.exit_code is not None else step.status.value
    )
    title = f"{step.role.value} finished | {detail} | {render.elapsed(step.duration)}"
    return Rule(Text(title, style=style), style=style, align="left")


def stream(wf: Workflow, roles: set[str] | None, is_running) -> None:
    """Print the selected step logs, following a run that is still going.

    Finished steps print in full; a running one is followed live and the next
    is picked up as it starts. Each step is bracketed by a marker so it is
    always clear who is talking and where one step ends.
    """
    render.muted("Ctrl-C stops following; the run keeps going.")

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
                    render.out(step_marker(step, starting=True))
                    opened.add(key)
                if step.log_path:
                    offsets[key] = drain(Path(step.log_path), offsets.get(key, 0))
                if step.status in (Status.DONE, Status.FAILED) and key not in closed:
                    render.out(step_marker(step, starting=False))
                    closed.add(key)

            still_going = is_running(wf.id) or any(
                s.status in (Status.PENDING, Status.RUNNING) for s in steps
            )
            if not still_going:
                break
            time.sleep(0.4)
    except KeyboardInterrupt:
        interrupted = True

    render.out()
    if interrupted:
        render.warn("stopped following - the run continues")
        return

    scope = "all steps" if roles is None else f"the {', '.join(sorted(roles))} step"
    render.out(Rule(Text(f"end of log for {scope}", style="muted"), style="muted"))

    final = Text("  ")
    final.append(wf.id[:6], style="id")
    final.append(" is ")
    final.append_text(render.status_text(wf.status))
    render.out(final)
    if wf.message:
        style = "error" if wf.status is Status.FAILED else "muted"
        render.out(Text(f"  {wf.message}", style=style))
    for note in wf.notes:
        render.warn(f"warning: {note}")


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
        Panel(
            Text(tail(path, lines)),
            title=f"last output of the {culprit.role.value} step",
            title_align="left", box=ROUNDED, border_style="error", padding=(0, 1),
            subtitle=f"full log: {culprit.log_path}", subtitle_align="left",
        ),
    ]
