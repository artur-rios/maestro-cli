"""Turning a typed line into an intent.

Pure string handling with no I/O, which is what makes the shell's trickiest
part -- telling a task from a command, and a flag from prose -- cheap to test.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

#: Flags that tune a single run. Anything else starting with `--` is a typo,
#: and is reported rather than silently absorbed into the task text.
FLAGS: dict[str, str] = {
    "plan": "text",
    "execute": "text",
    "review": "text",
    "model": "text",
    "plan-model": "text",
    "execute-model": "text",
    "review-model": "text",
    "mode": "mode",
    "repo": "text",
    "base": "text",
    "branch": "text",
    "rounds": "int",
    "attempts": "int",
}

FLAG_HELP = "  ".join(f"--{name}" for name in FLAGS)


class ParseError(ValueError):
    """The line cannot be understood; the message is meant for the user."""


@dataclass
class Command:
    """A slash command: `/runs`, `/show 7f2a`, `/set mode auto`."""

    name: str
    args: list[str] = field(default_factory=list)

    @property
    def arg(self) -> str:
        """The arguments as one string, for commands that take free text."""
        return " ".join(self.args)


@dataclass
class TaskIntent:
    """A request to start a workflow."""

    text: str = ""
    #: The `@name` that supplied the task, when one did.
    ref: str | None = None
    overrides: dict[str, object] = field(default_factory=dict)


def parse(line: str) -> Command | TaskIntent | None:
    """Read one line. None means there was nothing on it."""
    stripped = (line or "").strip()
    if not stripped:
        return None
    if stripped.startswith("/"):
        return _command(stripped)
    return _task(stripped)


def _command(line: str) -> Command:
    try:
        parts = shlex.split(line[1:])
    except ValueError:
        parts = line[1:].split()
    if not parts:
        raise ParseError("that is just a slash - try /help")
    return Command(name=parts[0].lower(), args=parts[1:])


def _task(line: str) -> TaskIntent:
    words = line.split()
    overrides: dict[str, object] = {}
    rest: list[str] = []

    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith("--"):
            name, value, consumed = _flag(words, index)
            overrides[name] = value
            index += consumed
            continue
        rest.append(word)
        index += 1

    ref = None
    if rest and rest[0].startswith("@"):
        ref = rest[0][1:]
        if not ref:
            raise ParseError("'@' needs a prompt name, e.g. @scope-spec")
        rest = rest[1:]

    text = " ".join(rest).strip()
    if not ref and not text:
        raise ParseError("nothing to do - type a task, or /help for commands")
    return TaskIntent(text=text, ref=ref, overrides=overrides)


def _flag(words: list[str], index: int) -> tuple[str, object, int]:
    """Read one `--flag value` or `--flag=value`. Returns (name, value, used)."""
    token = words[index][2:]
    consumed = 1

    if "=" in token:
        name, _, raw = token.partition("=")
    else:
        name = token
        if index + 1 >= len(words) or words[index + 1].startswith("--"):
            raise ParseError(f"--{name} needs a value")
        raw = words[index + 1]
        consumed = 2

    name = name.lower()
    if name not in FLAGS:
        raise ParseError(f"unknown option --{name}. Try: {FLAG_HELP}")
    if not raw:
        raise ParseError(f"--{name} needs a value")

    return name, _value(name, raw), consumed


def _value(name: str, raw: str) -> object:
    kind = FLAGS[name]
    if kind == "int":
        if not raw.isdigit() or int(raw) < 1:
            raise ParseError(f"--{name} must be a whole number of 1 or more")
        return int(raw)
    if kind == "mode":
        lowered = raw.lower()
        if lowered in {"auto", "autonomous"}:
            return "autonomous"
        if lowered in {"super", "supervised", "manual"}:
            return "supervised"
        raise ParseError("--mode must be 'supervised' or 'autonomous'")
    return raw
