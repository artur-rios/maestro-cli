"""The prompt loop.

Reads a line, turns it into an intent, acts on it, and reports on background
runs between prompts.
"""

from __future__ import annotations

from pathlib import Path

from .. import __version__, backends, settings as settings_mod
from ..models import Status, Workflow
from ..runner import Runner
from ..store import Store
from . import commands, promptlib, render
from .commands import CommandError
from .parser import Command, ParseError, TaskIntent, parse
from .promptlib import PromptError
from .render import BOLD, CYAN, GREEN, GREY, RED, RESET, YELLOW, colored

PROMPT = f"{BOLD}>{RESET} "


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

    def drain(self, workflows: list[Workflow]) -> list[str]:
        lines: list[str] = []
        for wf in workflows:
            state = self._state(wf)
            previous = self._seen.get(wf.id)
            self._seen[wf.id] = state
            if previous is None or previous == state:
                continue
            lines.append(self._describe(wf, previous, state))
        return [line for line in lines if line]

    def _describe(self, wf, previous: tuple[str, str], state: tuple[str, str]) -> str:
        tag = f"{GREY}[{wf.id[:6]}]{RESET}"
        status, progress = state

        if progress and progress != previous[1] and status == previous[0]:
            return f"{tag} {progress} tasks done"
        if wf.status is Status.AWAITING_APPROVAL:
            return f"{tag} {colored('awaiting approval', YELLOW)} - /approve {wf.id[:6]}"
        if wf.status is Status.MERGED:
            return f"{tag} {colored('merged', GREEN)} into {wf.base_branch}"
        if wf.status is Status.FAILED:
            return f"{tag} {colored('failed', RED)} - /show {wf.id[:6]}"
        if wf.status is Status.CHANGES_REQUESTED:
            return f"{tag} {colored('changes requested', YELLOW)} - /show {wf.id[:6]}"
        if wf.status is Status.CANCELLED:
            return f"{tag} cancelled"
        return f"{tag} {render.status_label(wf.status)}"


class Session:
    """What the commands act on: state, settings, and the user's terminal."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd())
        self.store = Store()
        self.runner = Runner(self.store)
        self.settings = settings_mod.load()
        self.notices = Notices()
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
        suffix = f" {GREY}[{default}]{RESET}" if default else ""
        try:
            answer = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            return default
        return answer or default

    def ask_confirm(self, prompt: str = "  Enter to run, n to cancel: ",
                    require: str = "") -> bool:
        """True when the user wants to go ahead.

        With `require` set, only that letter proceeds; otherwise a bare Enter
        is enough and anything else backs out.
        """
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return False
        if require:
            return answer.startswith(require)
        return answer in {"", "y", "yes"}


class Shell:
    def __init__(self, cwd: Path | None = None) -> None:
        self.session = Session(cwd)

    # ------------------------------------------------------------------ start

    def greet(self) -> None:
        session = self.session
        print()
        for line in render.banner(render.width(), __version__):
            print(line)

        installed = backends.available()
        names = "  ".join(
            colored(name, GREEN) if ok else colored(name, RED)
            for name, ok in installed
        )
        repo = session.settings.get("repo") or str(session.cwd)
        print()
        print(f"  {names}   {GREY}repo {Path(repo).name}{RESET}")

        runs = session.store.all()
        waiting = sum(1 for w in runs if w.status is Status.AWAITING_APPROVAL)
        bits = [render.plural(len(runs), "run")] if runs else []
        if waiting:
            bits.append(colored(f"{waiting} awaiting approval", YELLOW))
        bits.append(f"{GREY}/help for commands{RESET}")
        print(f"  {'  '.join(bits)}")

        orphans = session.runner.adopt_orphans()
        if orphans:
            print()
            print(colored(
                f"  {render.plural(orphans, 'run')} interrupted when the "
                f"orchestrator last exited - /resume to continue", YELLOW))
        for wf in session.store.all():
            session.notices.seed(wf)
        print()

    # ------------------------------------------------------------------- loop

    def run(self) -> int:
        render.init_console()
        self.greet()

        while True:
            for line in self.session.notices.drain(self.session.store.all()):
                print(line)
            try:
                raw = input(PROMPT)
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print(f"\n{GREY}  bye{RESET}")
                return 0

            if not self.handle(raw):
                return 0

    def handle(self, raw: str) -> bool:
        """Act on one line. False means the session should end."""
        try:
            intent = parse(raw)
        except ParseError as exc:
            print(colored(f"  {exc}", RED))
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
                print(colored(f"  {exc}", RED))
            except KeyboardInterrupt:
                print(colored("\n  cancelled", YELLOW))
            return True

        assert isinstance(intent, TaskIntent)
        try:
            commands.start_task(self.session, intent)
        except (CommandError, PromptError) as exc:
            print(colored(f"  {exc}", RED))
        except KeyboardInterrupt:
            print(colored("\n  cancelled", YELLOW))
        return True

    def quit(self) -> int:
        running = [
            w for w in self.session.store.all()
            if self.session.runner.is_running(w.id)
        ]
        if running:
            print(colored(
                f"  {render.plural(len(running), 'run')} still going - they stop "
                f"when this exits, and can be resumed later", YELLOW))
            if not self.session.ask_confirm("  Enter to exit, n to stay: "):
                return -1
        print(f"{GREY}  bye{RESET}")
        return 0


def main() -> int:
    return Shell().run()
