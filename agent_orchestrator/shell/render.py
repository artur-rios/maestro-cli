"""Everything the shell draws.

Formatting only: these functions return or print text and never decide
anything, so the command handlers stay about behaviour.
"""

from __future__ import annotations

import os
import shutil
import sys
import time

from ..models import Status, Step, Workflow

RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
GREY = "\033[90m"

STATUS_COLOR = {
    Status.PENDING: GREY,
    Status.PLANNING: CYAN,
    Status.EXECUTING: CYAN,
    Status.REVIEWING: CYAN,
    Status.MERGING: CYAN,
    Status.AWAITING_APPROVAL: YELLOW,
    Status.MERGED: GREEN,
    Status.CHANGES_REQUESTED: YELLOW,
    Status.FAILED: RED,
    Status.CANCELLED: GREY,
    Status.RUNNING: CYAN,
    Status.DONE: GREEN,
}

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

#: A red gradient, light at the top. 256-colour codes are the same escape
#: family as the rest of the palette, and VT processing is already enabled.
_SHADES = (
    "\033[38;5;210m",
    "\033[38;5;203m",
    "\033[38;5;196m",
    "\033[38;5;160m",
    "\033[38;5;124m",
)


def init_console() -> None:
    """Make the console safe to print to before anything is written."""
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:  # noqa: BLE001 - colour is cosmetic
        pass


def width() -> int:
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def colored(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def status_label(status: Status) -> str:
    return colored(status.value, STATUS_COLOR.get(status, ""))


def status_column(status: Status, size: int) -> str:
    """Colour codes have no display width, so pad against the plain text."""
    return status_label(status) + " " * max(1, size - len(status.value))


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


def banner(term_width: int, version: str = "") -> list[str]:
    """The wordmark, or a plain title when the terminal is too narrow."""
    title = "AI AGENT ORCHESTRATOR"
    stamp = f"  {GREY}v{version}{RESET}" if version else ""
    if term_width < WORDMARK_WIDTH + 2:
        return [f"{BOLD}{CYAN}{title}{RESET}{stamp}"]
    lines = [f"{shade}{art}{RESET}" for shade, art in zip(_SHADES, WORDMARK)]
    lines.append("")
    lines.append(f"  {BOLD}{title}{RESET}{stamp}")
    return lines


def run_table(
    workflows: list[Workflow], is_running, now: float | None = None
) -> list[str]:
    """One line per run, newest first."""
    if not workflows:
        return [f"{GREY}  no runs yet - type a task to start one{RESET}"]

    moment = now or time.time()
    lines = [
        f"  {BOLD}{'ID':<8}{'STATUS':<20}{'DONE':<6}{'AGE':<8}TASK{RESET}"
    ]
    for wf in workflows:
        live = colored(" *", CYAN) if is_running(wf.id) else ""
        progress = wf.task_progress or f"{wf.round}/{wf.max_rounds}"
        lines.append(
            f"  {wf.id[:6]:<8}{status_column(wf.status, 20)}{progress:<6}"
            f"{elapsed(moment - wf.created_at):<8}{wf.summary}{live}"
        )
    return lines


def run_detail(wf: Workflow, is_running) -> list[str]:
    """The full account of one run."""
    out = [
        f"  {BOLD}{wf.id}{RESET}  {status_label(wf.status)}"
        + (colored("  running", CYAN) if is_running(wf.id) else ""),
        f"  {GREY}branch  {RESET}{wf.branch or '-'}  {GREY}base {wf.base_branch}{RESET}",
        f"  {GREY}repo    {RESET}{wf.repo_path}",
        f"  {GREY}agents  {RESET}{wf.agents.get('plan')} -> "
        f"{wf.agents.get('execute')} -> {wf.agents.get('review')}"
        + (
            f"  {GREY}({', '.join(f'{r} {m}' for r, m in wf.models.items() if m)}){RESET}"
            if any(wf.models.values()) else ""
        ),
        f"  {GREY}mode    {RESET}{wf.mode.value}   round {wf.round}/{wf.max_rounds}"
        f"   {wf.max_attempts} attempts/step",
    ]
    if wf.prompt_source:
        out.append(f"  {GREY}source  {RESET}{wf.prompt_source}")
    if wf.verdict:
        color = GREEN if wf.verdict.value == "APPROVE" else YELLOW
        out.append(f"  {GREY}verdict {RESET}{colored(wf.verdict.value, color)}")
    if wf.message:
        color = RED if wf.status is Status.FAILED else GREY
        out.append(f"  {GREY}note    {RESET}{colored(wf.message, color)}")
    for note in wf.notes:
        out.append(f"  {GREY}warning {RESET}{colored(note, YELLOW)}")

    out.append("")
    out.append(f"  {BOLD}Task{RESET}")
    out.append(indent(wf.prompt))

    if wf.tasks:
        out.append("")
        out.append(f"  {BOLD}Checklist{RESET}  {wf.task_progress} done")
        for task in wf.tasks:
            if task.status is Status.DONE:
                mark, color = "[x]", GREEN
            elif task.status is Status.FAILED:
                mark, color = "[!]", RED
            else:
                mark, color = "[ ]", GREY
            tries = f"  {GREY}({task.attempts} attempts){RESET}" if task.attempts > 1 else ""
            out.append(f"  {colored(mark, color)} {task.index}. {task.text}{tries}")

    if wf.steps:
        out.append("")
        out.append(f"  {BOLD}Steps{RESET}")
        for index, step in enumerate(wf.steps, start=1):
            out.append(f"  {index:>2}. {step_line(step)}")
    return out


def step_line(step: Step) -> str:
    scope = f"task {step.task_index}" if step.task_index else f"round {step.round}"
    detail = f"  {RED}{step.error}{RESET}" if step.error else ""
    return (
        f"{step.role.value:<8} {step.agent:<10} {scope:<8} "
        f"{status_column(step.status, 10)}{elapsed(step.duration):<7}{detail}"
    )


def summary_line(
    branch: str,
    agents: dict[str, str],
    mode: str,
    rounds: int,
    attempts: int,
    models: dict[str, str] | None = None,
) -> list[str]:
    """What a run is about to do, shown before it starts."""
    lines = [
        f"  {BOLD}{branch}{RESET}",
        f"  {agents['plan']} -> {agents['execute']} -> {agents['review']}"
        f"  {GREY}|{RESET}  {mode}"
        f"  {GREY}|{RESET}  {plural(rounds, 'round')}, {attempts} attempts/step",
    ]
    chosen = {role: name for role, name in (models or {}).items() if name}
    if chosen:
        parts = ", ".join(f"{role} {name}" for role, name in chosen.items())
        lines.append(f"  {GREY}models: {parts}{RESET}")
    return lines
