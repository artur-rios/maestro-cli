"""Everything the shell draws.

Presentation only: these helpers render text and never decide anything, so the
command handlers stay about behaviour. Output goes through a single rich
Console so styling, wrapping and terminal detection are handled in one place.
"""

from __future__ import annotations

import time

from rich.box import ROUNDED, SIMPLE
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from ..models import Status, Step, Workflow

THEME = Theme(
    {
        "success": "bold green",
        "warning": "yellow",
        "error": "bold red",
        "info": "cyan",
        "muted": "dim",
        "accent": "red",
        "heading": "bold",
        "id": "bright_black",
        # One entry per status, so a status is coloured the same everywhere.
        "s.pending": "dim",
        "s.planning": "cyan",
        "s.executing": "cyan",
        "s.reviewing": "cyan",
        "s.merging": "cyan",
        "s.awaiting_approval": "yellow",
        "s.merged": "green",
        "s.changes_requested": "yellow",
        "s.failed": "red",
        "s.cancelled": "dim",
        "s.paused": "yellow",
        "s.running": "cyan",
        "s.done": "green",
    }
)

#: `record` keeps a copy of everything printed while still printing it, which
#: is what lets the shell redraw a command's output under a fresh header
#: instead of losing it to the screen clear.
console = Console(theme=THEME, highlight=False, soft_wrap=False, record=True)


def take_output() -> str:
    """Everything printed since the last call, with its styling, then reset."""
    try:
        return console.export_text(clear=True, styles=True)
    except Exception:  # noqa: BLE001 - recording is a convenience, never fatal
        return ""


def replay(recorded: str, max_lines: int) -> None:
    """Print recorded output again, keeping only the last `max_lines`.

    A long log would push the header straight back off the screen, so only
    the tail that fits is shown, with a note about what was dropped.
    """
    lines = recorded.rstrip("\n").splitlines()
    if not lines:
        return
    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        console.print(
            f"[muted]... {dropped} earlier line{'s' if dropped != 1 else ''}"
            f" - /logs or /show for the whole thing[/]"
        )
        lines = lines[-max_lines:]
    for line in lines:
        console.print(Text.from_ansi(line))

#: Pure ASCII: a console on a legacy code page cannot render box drawing, and
#: this is the first thing printed.
WORDMARK = (
    r"    ___    ____        ____             __  ",
    r"   /   |  /  _/       / __ \_________  / /_ ",
    r"  / /| |  / /        / / / / ___/ ___/ __ \ ",
    r" / ___ |_/ /        / /_/ / /  / /__/ / / / ",
    r"/_/  |_/___/        \____/_/   \___/_/ /_/  ",
)
WORDMARK_WIDTH = max(len(line) for line in WORDMARK)

#: A red gradient, light at the top.
_SHADES = ("color(210)", "color(203)", "color(196)", "color(160)", "color(124)")


# ---------------------------------------------------------------- primitives


def width() -> int:
    return console.size.width


def status_style(status: Status) -> str:
    return f"s.{status.value}"


def status_text(status: Status) -> Text:
    return Text(status.value, style=status_style(status))


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ------------------------------------------------------------------ messages


def out(renderable: RenderableType = "") -> None:
    console.print(renderable)


def success(message: str) -> None:
    console.print(f"  [success]{message}[/]")


def warn(message: str) -> None:
    console.print(f"  [warning]{message}[/]")


def error(message: str) -> None:
    """A failure the user needs to act on, with the cause spelled out."""
    console.print(f"  [error]x[/] {message}", style="red")


def info(message: str) -> None:
    console.print(f"  [info]{message}[/]")


def muted(message: str) -> None:
    console.print(f"  [muted]{message}[/]")


def hint(message: str) -> None:
    """A nudge towards the command that would help."""
    console.print(f"  [muted]-> {message}[/]")


def field(label: str, value: RenderableType, label_width: int = 8) -> None:
    line = Text("  ")
    line.append(f"{label:<{label_width}}", style="muted")
    line.append_text(value if isinstance(value, Text) else Text(str(value)))
    console.print(line)


# -------------------------------------------------------------------- banner


class _Box:
    """Edge characters, falling back to ASCII where box drawing will not render."""

    def __init__(self, plain: bool) -> None:
        self.h = "-" if plain else "─"
        self.v = "|" if plain else "│"
        self.tl, self.tr = ("+", "+") if plain else ("╭", "╮")
        self.bl, self.br = ("+", "+") if plain else ("╰", "╯")


def _box() -> _Box:
    return _Box(console.options.legacy_windows or not console.is_terminal)


#: The left edge drawn beside the input line itself.
BOX_LEFT = "│"


def _box_width() -> int:
    return max(10, min(console.size.width, 100) - 2)


def box_top(hint: str = "") -> None:
    """Top edge of the input box, with an optional hint written into it."""
    box = _box()
    label = f" {hint} " if hint else ""
    fill = box.h * max(0, _box_width() - len(label))
    console.print(f"[muted]{box.tl}{label}{fill}{box.tr}[/]",
                  highlight=False, no_wrap=True)


def box_bottom() -> None:
    box = _box()
    console.print(f"[muted]{box.bl}{box.h * _box_width()}{box.br}[/]",
                  highlight=False, no_wrap=True)


def line(text: RenderableType) -> None:
    """Print one line that must not wrap, such as the status bar."""
    console.print(text, no_wrap=True, overflow="ellipsis")


def clear() -> None:
    """Wipe the screen, when there is a screen to wipe."""
    if console.is_terminal:
        console.clear()


def status_bar(agents: list[tuple[str, bool]], repo: str, runs: int,
               waiting: int) -> Text:
    """One line under the wordmark: what is installed and what is pending."""
    line = Text("  ")
    for index, (name, ok) in enumerate(agents):
        if index:
            line.append("  ")
        line.append(name, style="success" if ok else "error")
    line.append(f"   {repo}", style="muted")
    if runs:
        line.append(f"   {plural(runs, 'run')}", style="muted")
    if waiting:
        line.append(f"   {waiting} awaiting approval", style="warning")
    return line


def banner(term_width: int | None = None, version: str = "") -> list[RenderableType]:
    """The wordmark, or a plain title when the terminal is too narrow."""
    term_width = term_width if term_width is not None else width()
    title = Text("AI AGENT ORCHESTRATOR", style="heading")
    if version:
        title.append(f"  v{version}", style="muted")

    if term_width < WORDMARK_WIDTH + 2:
        return [Text("AI AGENT ORCHESTRATOR", style="bold accent"), Text()]

    art = Text()
    for shade, line in zip(_SHADES, WORDMARK):
        art.append(line + "\n", style=shade)
    return [art, title]


# --------------------------------------------------------------------- runs


def run_table(workflows: list[Workflow], is_running, now: float | None = None) -> RenderableType:
    """One row per run, newest first."""
    if not workflows:
        return Text("  no runs yet - type a task to start one", style="muted")

    moment = now or time.time()
    table = Table(box=SIMPLE, pad_edge=False, expand=False, header_style="heading")
    table.add_column("ID", style="id", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("DONE", justify="right", no_wrap=True)
    table.add_column("AGE", justify="right", no_wrap=True)
    table.add_column("TASK", overflow="ellipsis", max_width=52)

    for wf in workflows:
        marker = Text(" *", style="info") if is_running(wf.id) else Text("")
        task = Text(wf.summary)
        task.append_text(marker)
        table.add_row(
            wf.id[:6],
            status_text(wf.status),
            wf.task_progress or f"{wf.round}/{wf.max_rounds}",
            elapsed(moment - wf.created_at),
            task,
        )
    return table


def run_detail(wf: Workflow, is_running) -> list[RenderableType]:
    """The full account of one run."""
    header = Text()
    header.append(wf.id[:6], style="heading")
    header.append("  ")
    header.append_text(status_text(wf.status))
    if is_running(wf.id):
        header.append("  running", style="info")

    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="muted", no_wrap=True)
    facts.add_column()
    facts.add_row("branch", f"{wf.branch or '-'}  [muted]base {wf.base_branch}[/]")
    facts.add_row("repo", wf.repo_path)

    agents = (
        f"{wf.agents.get('plan')} -> {wf.agents.get('execute')}"
        f" -> {wf.agents.get('review')}"
    )
    chosen = ", ".join(f"{r} {m}" for r, m in wf.models.items() if m)
    if chosen:
        agents += f"  [muted]({chosen})[/]"
    facts.add_row("agents", agents)
    facts.add_row(
        "mode",
        f"{wf.mode.value}   round {wf.round}/{wf.max_rounds}"
        f"   {wf.max_attempts} attempts/step",
    )
    if wf.prompt_source:
        facts.add_row("source", wf.prompt_source)
    if wf.verdict:
        style = "success" if wf.verdict.value == "APPROVE" else "warning"
        facts.add_row("verdict", f"[{style}]{wf.verdict.value}[/]")
    if wf.message:
        style = "error" if wf.status is Status.FAILED else "muted"
        facts.add_row("note", Text(wf.message, style=style))
    for note in wf.notes:
        facts.add_row("warning", Text(note, style="warning"))

    parts: list[RenderableType] = [
        header,
        facts,
        "",
        Panel(Text(wf.prompt), title="task", title_align="left", box=ROUNDED,
              border_style="muted", padding=(0, 1)),
    ]

    if wf.tasks:
        parts.append(checklist(wf))
    if wf.steps:
        parts.append("")
        parts.append(Text("steps", style="heading"))
        steps = Table.grid(padding=(0, 1))
        for column in range(6):
            steps.add_column(no_wrap=(column < 5))
        for index, step in enumerate(wf.steps, start=1):
            steps.add_row(f" {index:>2}.", *step_cells(step))
        parts.append(steps)
    return parts


def checklist(wf: Workflow) -> RenderableType:
    """The plan's items with their marks, as a grid."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column()

    for task in wf.tasks:
        if task.status is Status.DONE:
            mark, style = "[x]", "success"
        elif task.status is Status.FAILED:
            mark, style = "[!]", "error"
        else:
            mark, style = "[ ]", "muted"
        # Text(), not markup: "[x]" and any brackets in the task itself would
        # otherwise be parsed as style tags and vanish.
        body = Text(task.text)
        if task.attempts > 1:
            body.append(f"  ({task.attempts} attempts)", style="muted")
        grid.add_row(Text(mark, style=style), Text(f"{task.index}."), body)

    title = Text("checklist", style="heading")
    title.append(f"  {wf.task_progress} done", style="muted")
    return Group("", title, grid)


def step_cells(step: Step) -> list[RenderableType]:
    scope = f"task {step.task_index}" if step.task_index else f"round {step.round}"
    return [
        Text(step.role.value, style="info"),
        Text(step.agent),
        Text(scope, style="muted"),
        status_text(step.status),
        Text(elapsed(step.duration), style="muted"),
        Text(step.error, style="error") if step.error else Text(""),
    ]


def backend_table(data: dict) -> RenderableType:
    """Which CLIs are configured, whether they are installed, how they run."""
    from .. import config as config_mod

    table = Table(box=SIMPLE, pad_edge=False, header_style="heading")
    table.add_column("BACKEND", no_wrap=True)
    table.add_column("BINARY", style="muted", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("PLAN / REVIEW", style="muted", overflow="fold")
    table.add_column("EXECUTE", style="muted", overflow="fold")

    for name, backend in sorted(data.items()):
        installed = config_mod.is_installed(backend)
        table.add_row(
            Text(name, style="heading"),
            str(backend.get("bin", "")),
            Text("installed", style="success") if installed
            else Text("not found", style="error"),
            " ".join(backend.get("args", [])),
            " ".join(backend.get("execute_args") or backend.get("args", [])),
        )
    return table


def option_table(name: str, kind: str, choices: list[tuple[str, str]]) -> RenderableType:
    """The agents or models a backend reports."""
    table = Table(box=SIMPLE, pad_edge=False, header_style="heading")
    table.add_column(f"{name.upper()} {kind.upper()}S", no_wrap=True)
    table.add_column("", style="muted", no_wrap=True)
    for option, note in choices:
        table.add_row(option, note)
    return table


def settings_table(settings: dict, editable) -> RenderableType:
    """Current defaults for new runs."""
    table = Table(box=SIMPLE, pad_edge=False, header_style="heading")
    table.add_column("SETTING", no_wrap=True)
    table.add_column("VALUE")
    for key in editable:
        value = settings.get(key)
        shown = (
            Text(str(value)) if value not in ("", None)
            else Text("(auto)", style="muted")
        )
        table.add_row(key, shown)
    return Group(table, Text("  change with /set <key> <value>", style="muted"))


def roots_panel(roots: list[str]) -> RenderableType:
    body = Text()
    body.append("always: current directory, ./docs/prompts\n", style="muted")
    for root in roots:
        body.append(f"{root}\n")
    body.append("add more with /prompts add <path>", style="muted")
    return Panel(body, box=ROUNDED, border_style="muted", padding=(0, 1),
                 title="prompt roots", title_align="left")


def prompt_table(files) -> RenderableType:
    """Saved prompts, grouped by the root they came from."""
    table = Table(box=SIMPLE, pad_edge=False, header_style="heading")
    table.add_column("REFERENCE", style="info", no_wrap=True)
    table.add_column("FILE", style="muted", overflow="fold")
    table.add_column("SOURCE", style="muted", no_wrap=True)
    for item in files:
        table.add_row(f"@{item.name}", item.label, item.source)
    return table


def help_table(entries) -> RenderableType:
    """The command reference, blank entries acting as group breaks."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="info", no_wrap=True)
    table.add_column(style="muted")
    for left, right in entries:
        table.add_row(left, right)
    return Group("", table, "")


def merge_preview(wf: Workflow, diffstat: str) -> RenderableType:
    """What approving this run would land, and what the reviewer said."""
    header = Text("  merge ")
    header.append(wf.branch, style="heading")
    header.append(" into ")
    header.append(wf.base_branch, style="heading")

    parts: list[RenderableType] = [
        header,
        Panel(Text(diffstat or "(no changes)"), box=ROUNDED,
              border_style="muted", padding=(0, 1), title="diff",
              title_align="left"),
    ]
    if wf.review_text:
        parts.append(
            Panel(Text(wf.review_text[-800:]), box=ROUNDED,
                  border_style="muted", padding=(0, 1),
                  title="reviewer said", title_align="left")
        )
    return Group(*parts)


def summary_panel(
    branch: str,
    agents: dict[str, str],
    mode: str,
    rounds: int,
    attempts: int,
    models: dict[str, str] | None = None,
    source: str = "",
) -> RenderableType:
    """What a run is about to do, shown before it starts."""
    body = Table.grid(padding=(0, 2))
    body.add_column(style="muted", no_wrap=True)
    body.add_column()

    body.add_row("branch", f"[heading]{branch}[/]")
    line = f"{agents['plan']} -> {agents['execute']} -> {agents['review']}"
    chosen = {role: name for role, name in (models or {}).items() if name}
    if chosen:
        line += "  [muted](" + ", ".join(
            f"{role} {name}" for role, name in chosen.items()
        ) + ")[/]"
    body.add_row("agents", line)
    body.add_row(
        "run",
        f"{mode}  [muted]|[/]  {plural(rounds, 'round')}, {attempts} attempts/step",
    )
    if source:
        body.add_row("from", f"[muted]{source}[/]")

    return Panel(body, box=ROUNDED, border_style="accent", padding=(0, 1),
                 title="about to run", title_align="left")
