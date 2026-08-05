"""A live progress view for a run in flight.

The log stream shows what an agent is saying; this shows how far the run has
got. Useful for a long checklist, where the interesting question is "how many
items left" rather than the agent's current sentence.
"""

from __future__ import annotations

import time

from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from ..models import Status, Workflow
from . import render
from .render import console

#: How often to repaint. Fast enough to feel live, slow enough to be cheap.
REFRESH = 4


def _columns(total_known: bool) -> Progress:
    columns = [
        SpinnerColumn(style="accent"),
        TextColumn("[progress.description]{task.description}"),
    ]
    if total_known:
        columns += [
            BarColumn(complete_style="green", finished_style="green"),
            TaskProgressColumn(),
        ]
    columns.append(TimeElapsedColumn())
    return Progress(*columns, console=console, transient=False)


def _current_step(wf: Workflow) -> str:
    for step in reversed(wf.steps):
        if step.status is Status.RUNNING:
            scope = f"task {step.task_index}" if step.task_index else f"round {step.round}"
            return f"{step.role.value} | {step.agent} | {scope}"
    return wf.status.value


def watch(wf: Workflow, is_running) -> None:
    """Follow a run's progress until it stops. Ctrl-C returns to the prompt."""
    total = len(wf.tasks) or None
    progress = _columns(total_known=bool(total))
    task_id = progress.add_task(_current_step(wf), total=total or None)

    def frame():
        grid = Table.grid()
        grid.add_row(progress)
        grid.add_row(Text(wf.branch, style="muted"))
        if wf.tasks:
            grid.add_row(render.checklist(wf))
        return render.framed(grid)

    interrupted = False
    try:
        with Live(frame(), console=console, refresh_per_second=REFRESH) as live:
            while True:
                done = sum(1 for t in wf.tasks if t.status is Status.DONE)
                progress.update(
                    task_id,
                    description=_current_step(wf),
                    completed=done if total else None,
                )
                live.update(frame())

                if not is_running(wf.id) and wf.status not in (
                    Status.PENDING, Status.PLANNING, Status.EXECUTING,
                    Status.REVIEWING, Status.MERGING,
                ):
                    break
                time.sleep(1 / REFRESH)
    except KeyboardInterrupt:
        interrupted = True

    render.out()
    if interrupted:
        render.warn("stopped watching - the run continues")
        return

    line = Text()
    line.append(wf.id[:6], style="id")
    line.append(" is ")
    line.append_text(render.status_text(wf.status))
    render.out(line)
    if wf.message:
        style = "error" if wf.status is Status.FAILED else "muted"
        render.out(Text(wf.message, style=style))
