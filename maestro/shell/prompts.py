"""Asking the user things.

questionary gives arrow-key selection, inline defaults and line editing when a
real terminal is attached. When one is not -- piped input, a captured test, a
dumb terminal -- prompt_toolkit cannot run, so every prompt falls back to plain
`input()` with the same semantics. Callers never need to know which happened.
"""

from __future__ import annotations

import sys

import questionary
from prompt_toolkit.styles import Style

#: Matches the rich theme: red accent, dim secondary text.
STYLE = Style(
    [
        ("qmark", "fg:#ff5f5f bold"),
        ("question", "bold"),
        ("answer", "fg:#5fafff"),
        ("pointer", "fg:#ff5f5f bold"),
        ("highlighted", "fg:#ff5f5f bold"),
        ("selected", "fg:#5fff87"),
        ("instruction", "fg:#808080"),
        ("text", ""),
    ]
)


def interactive() -> bool:
    """True when a real terminal is attached at both ends."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class Asker:
    """The prompts a session needs, each with a non-interactive fallback."""

    def __init__(self, force_plain: bool = False) -> None:
        self.force_plain = force_plain

    @property
    def fancy(self) -> bool:
        return not self.force_plain and interactive()

    # ----------------------------------------------------------------- reading

    def line(self, label: str, default: str = "") -> str:
        """One value, with the default pre-filled. Empty input keeps it."""
        if self.fancy:
            try:
                answer = questionary.text(
                    f"{label}:", default=default, style=STYLE, qmark="  "
                ).unsafe_ask()
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception:  # noqa: BLE001 - any terminal problem falls back
                return self._plain_line(label, default)
            return (answer or default).strip() or default
        return self._plain_line(label, default)

    def _plain_line(self, label: str, default: str) -> str:
        suffix = f" [{default}]" if default else ""
        try:
            answer = input(f"  {label}{suffix}: ").strip()
        except EOFError:
            return default
        return answer or default

    def confirm(self, message: str, default: bool = True) -> bool:
        if self.fancy:
            try:
                return bool(
                    questionary.confirm(
                        message, default=default, style=STYLE, qmark="  "
                    ).unsafe_ask()
                )
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception:  # noqa: BLE001
                pass
        suffix = "Y/n" if default else "y/N"
        try:
            answer = input(f"  {message} [{suffix}] ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        return answer.startswith("y")

    def select(self, label: str, options: list[str], default: str = "") -> str:
        """Pick from a list. Falls back to typing the value."""
        if not options:
            return self.line(label, default)
        if self.fancy:
            try:
                answer = questionary.select(
                    f"{label}:",
                    choices=options,
                    default=default if default in options else None,
                    style=STYLE,
                    qmark="  ",
                    instruction="(arrows, enter)",
                ).unsafe_ask()
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception:  # noqa: BLE001
                return self.line(label, default)
            return answer or default
        return self.line(label, default)

    def command(self, prompt: str = ">", hint: str = "") -> str:
        """The main input line, drawn as a box when a terminal allows.

        prompt_toolkit owns the line the cursor sits on, so the box is drawn
        around it: a top edge with the hint, the input line prefixed by an
        edge character, and a bottom edge once the line is submitted.
        """
        if not self.fancy:
            return input(f"{prompt} ")

        from . import render

        render.box_top(hint)
        try:
            answer = questionary.text(
                "", qmark=render.BOX_LEFT + " " + prompt, style=STYLE
            ).unsafe_ask()
        except (KeyboardInterrupt, EOFError):
            render.box_bottom()
            raise
        except Exception:  # noqa: BLE001
            return input(f"{prompt} ")
        render.box_bottom()
        return answer or ""
