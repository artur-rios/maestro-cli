"""Everything the shell draws.

Presentation only: these helpers render text and never decide anything, so the
command handlers stay about behaviour. Output goes through a single rich
Console so styling, wrapping and terminal detection are handled in one place.

The screen is a frame of three bands: a header that does not move, a content
area written inside a gutter rail, and a footer sitting directly above the
cursor. One width governs all of them, so every element shares an edge.
"""

from __future__ import annotations

import time

from rich.box import ROUNDED, SIMPLE
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from ..models import Role, Status, Step, Workflow

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
        "rail": "bright_black",
        "divider": "bright_black",
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
        out(Text(
            f"... {dropped} earlier line{'s' if dropped != 1 else ''}"
            f" - /logs or /show for the whole thing",
            style="muted",
        ))
        lines = lines[-max_lines:]
    for line in lines:
        console.print(Text.from_ansi(line))

#: Pure ASCII: a console on a legacy code page cannot render box drawing, and
#: this is the first thing printed.
WORDMARK = (
    r"    __  ______    _____________________  ____  ",
    r"   /  |/  /   |  / ____/ ___/_  __/ __ \/ __ \ ",
    r"  / /|_/ / /| | / __/  \__ \ / / / /_/ / / / / ",
    r" / /  / / ___ |/ /___ ___/ // / / _, _/ /_/ /  ",
    r"/_/  /_/_/  |_/_____//____//_/ /_/ |_|\____/   ",
)
WORDMARK_WIDTH = max(len(line) for line in WORDMARK)

#: A red gradient, light at the top.
_SHADES = ("color(210)", "color(203)", "color(196)", "color(160)", "color(124)")


# ---------------------------------------------------------------- primitives


def width() -> int:
    return console.size.width


# -------------------------------------------------------------------- frame

#: Content stops widening here. Past this a line of prose is tiring to read,
#: and the prompt would drift far from what it refers to.
FRAME_MAX = 100

#: Space between the left of the terminal and the frame's own left edge.
MARGIN = 2

#: Margin, the rail itself, and the space before content starts.
GUTTER_WIDTH = MARGIN + 2


def frame_width() -> int:
    """How wide the frame's own edges are, excluding the left margin.

    The divider, the gutter rail, the status bar and the input box all measure
    themselves with this, which is the whole point: they line up because they
    ask the same question rather than each guessing.
    """
    return max(10, min(console.size.width, FRAME_MAX) - MARGIN)


def content_width() -> int:
    """Room left for content once the rail has taken its share."""
    return max(10, frame_width() - 2)


def _gutter() -> str:
    return f"{' ' * MARGIN}{'|' if _box().plain else '▌'} "


class Rail:
    """A renderable drawn inside the frame, with the gutter down its left.

    Wrapping happens at the segment level rather than by prefixing strings.
    That is what lets one class handle everything the shell prints: a Table
    re-wraps its columns to the narrowed width instead of spilling past the
    gutter, a Panel draws its border inside the rail, and a wrapped paragraph
    gets the gutter on every line rather than only its first.
    """

    def __init__(self, renderable: RenderableType, wide: bool = False) -> None:
        self.renderable = renderable
        self.wide = wide

    def _inner_width(self) -> int:
        """`wide` lets a renderable off the cap, keeping only the gutter.

        The backends table carries five columns and is already tight at a
        hundred; squeezed into the frame it folds into an unreadable stack.
        For that one case legibility is worth breaking the right edge.
        """
        if self.wide:
            return max(content_width(), console.size.width - GUTTER_WIDTH)
        return content_width()

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        inner = options.update(width=self._inner_width(), height=None)
        style = console.get_style("rail", default="dim")
        gutter = Segment(_gutter(), style)
        # A blank line keeps the rail going -- the bar should be unbroken
        # within a block -- but drops the space after it, so an empty line
        # ends at the rail instead of trailing whitespace into a copy buffer.
        bare = Segment(_gutter().rstrip(), style)
        # pad=False keeps the lines ragged for the same reason.
        for line in console.render_lines(self.renderable, inner, pad=False):
            blank = not any(segment.text.strip() for segment in line)
            yield bare if blank else gutter
            if not blank:
                yield from line
            yield Segment.line()

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        total = self._inner_width() + GUTTER_WIDTH
        return Measurement(total, total)


def framed(renderable: RenderableType, wide: bool = False) -> RenderableType:
    """`renderable` in the rail when there is a screen, untouched otherwise.

    Piping must keep producing clean text -- a gutter in a redirected file
    would break anything reading it -- so the rail is a terminal affordance
    only, the same rule the box-drawing fallback already follows.
    """
    return Rail(renderable, wide=wide) if console.is_terminal else renderable


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


def out(renderable: RenderableType = "", wide: bool = False) -> None:
    """Print into the content area.

    Everything the commands say goes through here, so the rail is applied in
    one place and no call site has to know the frame exists. A bare call is a
    spacer: it stays a genuinely blank line, which breaks the rail into blocks
    instead of drawing one unbroken bar down the screen.
    """
    if renderable == "":
        console.print()
        return
    console.print(framed(renderable, wide=wide))


def plain(renderable: RenderableType = "") -> None:
    """Print chrome -- banner, divider, footer -- outside the rail."""
    console.print(renderable)


def success(message: str) -> None:
    out(Text(message, style="success"))


def warn(message: str) -> None:
    out(Text(message, style="warning"))


def error(message: str) -> None:
    """A failure the user needs to act on, with the cause spelled out."""
    line = Text("x ", style="error")
    line.append(message, style="red")
    out(line)


def info(message: str) -> None:
    out(Text(message, style="info"))


def muted(message: str) -> None:
    out(Text(message, style="muted"))


def hint(message: str) -> None:
    """A nudge towards the command that would help."""
    out(Text(f"-> {message}", style="muted"))


def field(label: str, value: RenderableType, label_width: int = 8) -> None:
    line = Text()
    line.append(f"{label:<{label_width}}", style="muted")
    line.append_text(value if isinstance(value, Text) else Text(str(value)))
    out(line)


# -------------------------------------------------------------------- banner


class _Box:
    """Edge characters, falling back to ASCII where box drawing will not render."""

    def __init__(self, plain: bool) -> None:
        self.plain = plain
        self.h = "-" if plain else "─"
        self.v = "|" if plain else "│"
        self.tl, self.tr = ("+", "+") if plain else ("╭", "╮")
        self.bl, self.br = ("+", "+") if plain else ("╰", "╯")


def _drawable() -> bool:
    """Whether the output encoding can carry box drawing and the rail.

    A Windows console left on a legacy code page raises UnicodeEncodeError on
    the first frame rather than degrading, which would take the whole shell
    down before it has drawn anything. Ask the stream, and fall back to ASCII
    when the answer is no.
    """
    try:
        "─▌".encode(console.encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _box() -> _Box:
    return _Box(
        console.options.legacy_windows
        or not console.is_terminal
        or not _drawable()
    )


#: The left edge drawn beside the input line itself, margin included so the
#: cursor lands under the gutter rather than beside it.
BOX_LEFT = f"{' ' * MARGIN}│"


def _box_width() -> int:
    """Inner width of the input box, edges excluded."""
    return max(10, frame_width() - 2)


def _edge(h: str, label: str, span: int) -> str:
    """A horizontal run with `label` set into it, one dash in from the left."""
    text = f"{h} {label} " if label else ""
    return text + h * max(0, span - len(text))


def box_top(hint: str = "") -> None:
    """Top edge of the input box, with an optional hint written into it."""
    box = _box()
    edge = _edge(box.h, hint, _box_width())
    console.print(f"{' ' * MARGIN}[muted]{box.tl}{edge}{box.tr}[/]",
                  highlight=False, no_wrap=True)


def box_bottom() -> None:
    box = _box()
    console.print(f"{' ' * MARGIN}[muted]{box.bl}{box.h * _box_width()}{box.br}[/]",
                  highlight=False, no_wrap=True)


def divider(label: str = "") -> None:
    """The line under the banner that closes the header off from the content."""
    edge = _edge(_box().h, label, frame_width())
    console.print(f"{' ' * MARGIN}[divider]{edge}[/]",
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
    title = Text(" " * MARGIN)
    title.append("MAESTRO", style="heading")
    if version:
        title.append(f"  v{version}", style="muted")

    if term_width < WORDMARK_WIDTH + 2:
        short = Text(" " * MARGIN)
        short.append("MAESTRO", style="bold accent")
        return [short, Text()]

    art = Text()
    for shade, line in zip(_SHADES, WORDMARK):
        art.append(line + "\n", style=shade)
    return [art, title]


# --------------------------------------------------------------------- runs


def run_table(workflows: list[Workflow], is_running, now: float | None = None) -> RenderableType:
    """One row per run, newest first."""
    if not workflows:
        return Text("no runs yet - type a task to start one", style="muted")

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

    agents = "  ".join(
        f"{role.value} {wf.agent_for(role)}" for role in wf.active_roles
    )
    chosen = ", ".join(f"{r} {m}" for r, m in wf.models.items() if m)
    if chosen:
        agents += f"  [muted]({chosen})[/]"
    facts.add_row("agents", agents)
    rounds = (
        f"   round {wf.round}/{wf.max_rounds}" if wf.runs(Role.REVIEW) else ""
    )
    facts.add_row(
        "mode",
        f"{wf.mode.value}{rounds}   {wf.max_attempts} attempts/step",
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
    return Group(table, Text("change with /set <key> <value>", style="muted"))


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
    header = Text("merge ")
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


#: How the merge is reached, written under the last cell of the diagram.
_MERGE_CAPTION = {"supervised": "you approve", "autonomous": "automatic"}


def workflow_steps(agents: dict[str, str], mode: str) -> list[tuple[str, str]]:
    """The cells of the diagram: the steps that will run, then the merge.

    A step the user turned off is simply not here. Showing it greyed out would
    ask the reader to work out what is going to happen; this way the picture
    is the run.
    """
    cells = [
        (role, agents.get(role) or "")
        for role in ("plan", "execute", "review")
        if agents.get(role)
    ]
    cells.append(("merge", _MERGE_CAPTION.get(mode, mode)))
    return cells


def workflow_diagram(agents: dict[str, str], mode: str,
                     term_width: int | None = None) -> RenderableType:
    """The workflow as a row of boxes, one per step, with its agent beneath.

    A run's shape is the thing worth checking before it starts -- especially
    now that steps can be left out -- and a picture answers that faster than a
    line of arrows does.
    """
    cells = workflow_steps(agents, mode)
    box = _box()
    arrow = " -> " if box.plain else " ──▶ "
    available = term_width if term_width is not None else content_width()

    #: Inner width of each cell; the two border characters sit outside it.
    widths = [max(len(role), len(caption)) + 2 for role, caption in cells]
    drawn = sum(width + 2 for width in widths) + len(arrow) * (len(cells) - 1)
    if drawn > available:
        # Too narrow for boxes; the same run as one line rather than a
        # diagram folded into nonsense.
        line = Text()
        for index, (role, caption) in enumerate(cells):
            if index:
                line.append(arrow, style="muted")
            line.append(role, style="heading")
            if caption:
                line.append(f" {caption}", style="muted")
        return line

    top, middle, bottom, under = Text(), Text(), Text(), Text()
    for index, (role, caption) in enumerate(cells):
        if index:
            for line, filler in ((top, " " * len(arrow)), (middle, arrow),
                                 (bottom, " " * len(arrow)),
                                 (under, " " * len(arrow))):
                line.append(filler, style="muted")
        span = widths[index]
        top.append(f"{box.tl}{box.h * span}{box.tr}", style="muted")
        middle.append(box.v, style="muted")
        middle.append(f"{role:^{span}}", style="heading")
        middle.append(box.v, style="muted")
        bottom.append(f"{box.bl}{box.h * span}{box.br}", style="muted")
        under.append(f" {caption:^{span}} ", style="muted")

    return Group(top, middle, bottom, under)


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
    # The panel's border, its padding and the label column all eat into what
    # the diagram may use, so it is told what is left rather than the width of
    # the screen.
    body.add_row("steps", workflow_diagram(agents, mode, content_width() - 12))

    chosen = {role: name for role, name in (models or {}).items() if name}
    if chosen:
        body.add_row("models", "[muted]" + ", ".join(
            f"{role} {name}" for role, name in chosen.items()
        ) + "[/]")

    run = f"{mode}  [muted]|[/]  {attempts} attempts/step"
    if agents.get("review"):
        run = (
            f"{mode}  [muted]|[/]  {plural(rounds, 'round')}, "
            f"{attempts} attempts/step"
        )
    body.add_row("run", run)
    if source:
        body.add_row("from", f"[muted]{source}[/]")

    return Panel(body, box=ROUNDED, border_style="accent", padding=(0, 1),
                 title="about to run", title_align="left")
