"""Finding and loading saved prompt files.

A task description is often written down before it is run -- a spec in the
repo, a note in a prompts folder. This locates those files across a few roots
and resolves `@name` references against them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SUFFIXES = (".md", ".txt", ".markdown")

#: How deep to walk a recursive root. Enough for docs/prompts/<area>/<file>.
MAX_DEPTH = 3

#: Directories never worth searching for a task description.
SKIP = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", "bin", "obj", ".idea", ".vscode", ".mypy_cache",
}


class PromptError(ValueError):
    """Something is wrong with a prompt file or reference."""


@dataclass(frozen=True)
class PromptFile:
    path: Path
    source: str  # which root it came from, for grouping in the listing

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def label(self) -> str:
        try:
            return str(self.path.relative_to(Path.cwd()))
        except ValueError:
            return str(self.path)


def roots(settings: dict, cwd: Path | None = None) -> list[tuple[str, Path, bool]]:
    """Where to look, as (label, directory, recursive), in priority order.

    The working directory is searched flat: a repository root holds hundreds of
    markdown files and walking it would bury the ones actually written as
    tasks.
    """
    here = Path(cwd or Path.cwd())
    found: list[tuple[str, Path, bool]] = [("current directory", here, False)]

    docs = here / "docs" / "prompts"
    if docs.is_dir():
        found.append(("docs/prompts", docs, True))

    for raw in settings.get("prompt_paths", []):
        directory = Path(raw).expanduser()
        if directory.is_dir():
            found.append((str(directory), directory, True))
    return found


def discover(settings: dict, cwd: Path | None = None) -> list[PromptFile]:
    """Every prompt file across the roots, first occurrence winning."""
    seen: set[Path] = set()
    files: list[PromptFile] = []

    for label, directory, recursive in roots(settings, cwd):
        for path in _scan(directory, recursive):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(PromptFile(path=path, source=label))
    return files


def _scan(directory: Path, recursive: bool) -> list[Path]:
    found: list[Path] = []
    try:
        if not recursive:
            entries = [p for p in directory.iterdir() if p.is_file()]
        else:
            entries = _walk(directory)
    except OSError:
        return []

    for path in entries:
        if path.suffix.lower() in SUFFIXES:
            found.append(path)
    return sorted(found, key=lambda p: p.name.lower())


def _walk(directory: Path) -> list[Path]:
    files: list[Path] = []
    base_depth = len(directory.parts)
    for current, dirnames, filenames in os.walk(directory):
        here = Path(current)
        if len(here.parts) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
        files.extend(here / name for name in filenames)
    return files


def resolve(ref: str, files: list[PromptFile], cwd: Path | None = None) -> Path:
    """Turn an `@ref` into a path.

    A reference containing a separator is treated as a path. Otherwise it
    matches a filename stem, exactly first and then by prefix, so `@scope`
    finds `scope-permissions.md` while an ambiguous stem is reported rather
    than guessed at.
    """
    wanted = (ref or "").strip()
    if not wanted:
        raise PromptError("'@' needs a prompt name")

    if "/" in wanted or "\\" in wanted or Path(wanted).suffix:
        candidate = Path(wanted).expanduser()
        if not candidate.is_absolute():
            candidate = Path(cwd or Path.cwd()) / candidate
        if candidate.is_file():
            return candidate
        raise PromptError(f"no such prompt file: {wanted}")

    lowered = wanted.lower()
    exact = [f for f in files if f.name.lower() == lowered]
    if len(exact) == 1:
        return exact[0].path
    if len(exact) > 1:
        raise PromptError(_ambiguous(wanted, exact))

    partial = [f for f in files if f.name.lower().startswith(lowered)]
    if len(partial) == 1:
        return partial[0].path
    if partial:
        raise PromptError(_ambiguous(wanted, partial))

    raise PromptError(f"no saved prompt matches '{wanted}' - try /prompts")


def _ambiguous(ref: str, matches: list[PromptFile]) -> str:
    names = ", ".join(sorted(m.label for m in matches)[:6])
    return f"'{ref}' matches several prompts: {names}"


def load(raw: str | Path) -> tuple[Path, str]:
    """Read a task description from a file.

    Raises PromptError with a message meant for the user. Quotes are stripped
    because a path dragged into a terminal arrives wrapped in them.
    """
    cleaned = str(raw).strip().strip('"').strip("'").strip()
    if not cleaned:
        raise PromptError("no path given")

    path = Path(os.path.expandvars(os.path.expanduser(cleaned)))
    if not path.exists():
        raise PromptError(f"no such file: {path}")
    if path.is_dir():
        raise PromptError(f"{path} is a directory, not a file")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise PromptError(f"{path.name} is not UTF-8 text") from None
    except OSError as exc:
        raise PromptError(f"could not read {path.name}: {exc}") from None

    if not text.strip():
        raise PromptError(f"{path.name} is empty")
    return path, text.strip()
