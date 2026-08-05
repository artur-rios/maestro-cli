"""The prompt loop.

Reads a line, turns it into an intent, acts on it, and reports on background
runs between prompts.
"""

from __future__ import annotations

from pathlib import Path

import sys

from rich.text import Text

from .. import __version__, backends, settings as settings_mod
from ..models import Status, Workflow
from ..runner import Runner
from ..store import Store
from . import commands, promptlib, render
from .commands import CommandError
from .parser import Command, ParseError, TaskIntent, parse
from .promptlib import PromptError
from .prompts import Asker


class Notices:
    """Reports background progress without interrupting typing.

    Holds the last state shown for each run and, when asked, returns one line
    per change since. Called just before a prompt is drawn, so a message never
    lands in the middle of what the user is writing.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _state(wf: Workflow) -> tuple[str, str]:
        return wf.status.value, wf.task_progress

    def seed(self, wf: Workflow) -> None:
        """Record a run's state without announcing it."""
        self._seen[wf.id] = self._state(wf)

    def forget(self, workflow_id: str) -> None:
        self._seen.pop(workflow_id, None)

    def drain(self, workflows: list[Workflow]) -> list[Text]:
        lines: list[Text] = []
        for wf in workflows:
            state = self._state(wf)
            previous = self._seen.get(wf.id)
            self._seen[wf.id] = state
            if previous is None or previous == state:
                continue
            lines.append(self._describe(wf, previous, state))
        return lines

    def _describe(self, wf, previous: tuple[str, str], state: tuple[str, str]) -> Text:
        line = Text()
        line.append(f"[{wf.id[:6]}]", style="id")
        line.append(" ")
        status, progress = state

        if progress and progress != previous[1] and status == previous[0]:
            return line.append(f"{progress} tasks done")
        if wf.status is Status.AWAITING_APPROVAL:
            line.append("awaiting approval", style="warning")
            return line.append(f" - /approve {wf.id[:6]}", style="muted")
        if wf.status is Status.MERGED:
            line.append("merged", style="success")
            return line.append(f" into {wf.base_branch}", style="muted")
        if wf.status is Status.FAILED:
            line.append("failed", style="error")
            return line.append(f" - /show {wf.id[:6]}", style="muted")
        if wf.status is Status.CHANGES_REQUESTED:
            line.append("changes requested", style="warning")
            return line.append(f" - /show {wf.id[:6]}", style="muted")
        if wf.status is Status.PAUSED:
            line.append("paused", style="warning")
            return line.append(f" - /resume {wf.id[:6]}", style="muted")
        if wf.status is Status.CANCELLED:
            return line.append("cancelled", style="muted")
        return line.append_text(render.status_text(wf.status))


class Session:
    """What the commands act on: state, settings, and the user's terminal."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd())
        self.store = Store()
        self.runner = Runner(self.store)
        self.settings = settings_mod.load()
        self.notices = Notices()
        self.asker = Asker()
        self._prompt_files: list[promptlib.PromptFile] | None = None

    def default_backend(self) -> str:
        """The first installed CLI, used when no role has been configured."""
        for name, ok in backends.available():
            if ok:
                return name
        return ""

    def prompt_files(self, refresh: bool = False) -> list[promptlib.PromptFile]:
        if refresh or self._prompt_files is None:
            self._prompt_files = promptlib.discover(self.settings, self.cwd)
        return self._prompt_files

    def ask_line(self, label: str, default: str = "") -> str:
        """Read one value, showing the default. Enter keeps it."""
        return self.asker.line(label, default)

    def ask_select(self, label: str, options: list[str], default: str = "") -> str:
        """Pick one of `options`, or type it when there is no terminal."""
        return self.asker.select(label, options, default)

    def ask_confirm(self, message: str = "Run it?", require: str = "") -> bool:
        """True when the user wants to go ahead.

        `require` marks the cautious prompts -- deletions and the like -- where
        the default is no rather than yes.
        """
        return self.asker.confirm(message, default=not require)

    def ask_action(self, label: str, options: list[tuple[str, str]], default: str) -> str:
        """Choose between a few named actions, e.g. run / change / cancel."""
        labels = [text for _, text in options]
        by_label = {text: key for key, text in options}
        wanted = next((t for k, t in options if k == default), labels[0])
        chosen = self.asker.select(label, labels, wanted)
        return by_label.get(chosen, default)


class Shell:
    def __init__(self, cwd: Path | None = None) -> None:
        self.session = Session(cwd)

    # ------------------------------------------------------------------ start

    def header(self) -> None:
        """The wordmark, closed off by a divider. The top of the frame."""
        render.plain()
        for part in render.banner(render.width(), __version__):
            render.plain(part)
        render.divider()

    def footer(self) -> None:
        """What is installed and what is pending, just above the input box.

        Drawn every turn rather than once, because it reports live state. It
        must be printed before the turn's recording starts, or it would be
        replayed into the content area on the next redraw and the status line
        would march down the screen.
        """
        session = self.session
        runs = session.store.all()
        render.plain()
        render.line(render.status_bar(
            backends.available(),
            Path(session.settings.get("repo") or str(session.cwd)).name,
            len(runs),
            sum(1 for w in runs if w.status is Status.AWAITING_APPROVAL),
        ))

    def greet(self) -> None:
        self.header()
        orphans = self.session.runner.adopt_orphans()
        if orphans:
            render.warn(
                f"{render.plural(orphans, 'run')} interrupted when the "
                f"maestro last exited - /resume to continue"
            )
            render.out()
        for wf in self.session.store.all():
            self.session.notices.seed(wf)

    # ------------------------------------------------------------------- loop

    def run(self) -> int:
        self.greet()
        first = True
        last_output = ""

        while True:
            # Redraw the header so the wordmark stays at the top, then put the
            # last command's output back underneath it. Clearing alone would
            # scroll the very thing the user asked for out of sight.
            if not first and self.session.settings.get("pinned", True):
                render.clear()
                self.header()
                render.replay(last_output, self.output_room())
            first = False

            for line in self.session.notices.drain(self.session.store.all()):
                render.out(line)

            self.footer()
            # Clearing here drops the footer from the recording along with it,
            # which is the point: the turn starts on a clean slate.
            render.take_output()  # start recording this turn's output
            try:
                raw = self.session.asker.command(hint=self.hint())
            except KeyboardInterrupt:
                render.out()
                last_output = render.take_output()
                continue
            except EOFError:
                render.out()
                render.muted("bye")
                return 0

            keep_going = self.handle(raw)
            last_output = render.take_output()
            if not keep_going:
                return 0

    def output_room(self) -> int:
        """Lines available for replayed output, after the header and the box."""
        height = render.console.size.height
        # Header: a blank, the wordmark, its title, the divider. Footer: a
        # blank and the status bar. Then the input box's three lines, and one
        # spare so the result never sits flush against the prompt.
        chrome = len(render.WORDMARK) + 3 + 2 + 3 + 1
        return max(4, height - chrome)

    def hint(self) -> str:
        """What to write in the top edge of the input box."""
        waiting = sum(
            1 for w in self.session.store.all()
            if w.status is Status.AWAITING_APPROVAL
        )
        if waiting:
            return f"{waiting} awaiting approval - /approve"
        return "type a task, @prompt or /help"

    def handle(self, raw: str) -> bool:
        """Act on one line. False means the session should end."""
        try:
            intent = parse(raw)
        except ParseError as exc:
            render.error(str(exc))
            return True

        if intent is None:
            return True

        if isinstance(intent, Command):
            if intent.name in {"exit", "quit", "q"}:
                # quit() returns 0 to leave, -1 when the user changes their mind.
                return self.quit() != 0
            try:
                commands.dispatch(self.session, intent)
            except (CommandError, PromptError) as exc:
                render.error(str(exc))
            except KeyboardInterrupt:
                render.out()
                render.warn("cancelled")
            return True

        assert isinstance(intent, TaskIntent)
        try:
            commands.start_task(self.session, intent)
        except (CommandError, PromptError) as exc:
            render.error(str(exc))
        except KeyboardInterrupt:
            render.out()
            render.warn("cancelled")
        return True

    def quit(self) -> int:
        running = [
            w for w in self.session.store.all()
            if self.session.runner.is_running(w.id)
        ]
        if running:
            render.warn(
                f"{render.plural(len(running), 'run')} still going - they stop "
                f"when this exits, and can be resumed later"
            )
            if not self.session.ask_confirm("Exit anyway?"):
                return -1
        render.muted("bye")
        return 0


def main() -> int:
    return Shell().run()
