"""Git operations, all through subprocess so no git library is needed.

Every workflow gets its own branch checked out into its own worktree, which is
what lets several runs proceed at once without fighting over the index.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from . import config


class GitError(Exception):
    """A git command failed in a way the caller must surface to the user."""


class MergeConflict(GitError):
    """The merge could not be completed automatically."""


def _git(repo: Path | str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "git failed").strip())
    return (proc.stdout or "").strip()


def repo_root(path: Path | str) -> Path:
    """Resolve `path` to the root of its git repository."""
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel"))
    except GitError as exc:
        raise GitError(f"'{path}' is not inside a git repository ({exc})") from exc


def is_clean(repo: Path | str) -> bool:
    return _git(repo, "status", "--porcelain") == ""


def current_branch(repo: Path | str) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def branch_exists(repo: Path | str, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


#: Words that carry no meaning in a branch name. Dropping them is what turns
#: "create-a-new-use-case-in-which-a" into "create-use-case-scope-owner".
_FILLER = frozenset(
    """a an the this that these those it its their there here of for to in on at by
    from with without and or but as is are was were be been being can could should
    would will shall may might must do does did have has had i we you they me my our
    new one more some any all each every other another so then than when where which
    who whom whose what how why if else also just only very much many able want need
    please make sure into over under about across through""".split()
)

BRANCH_TYPES = ("feature", "fix", "chore", "refactor", "docs", "test")

_TYPE_HINTS = (
    ("fix", ("fix", "bug", "broken", "crash", "error", "regression", "fails",
             "failing", "incorrect", "wrong", "defect", "patch")),
    ("refactor", ("refactor", "restructure", "reorganise", "reorganize", "rename",
                  "clean up", "cleanup", "simplify", "extract")),
    ("docs", ("document", "documentation", "readme", "docs", "changelog", "comment")),
    ("test", ("test", "tests", "coverage", "unit test", "integration test")),
    ("chore", ("bump", "upgrade", "dependency", "dependencies", "config", "ci")),
)


def suggest_branch_type(prompt: str) -> str:
    """Guess a conventional prefix from what the prompt asks for.

    Hints match whole words only. As a substring, "ci" hides inside "specific"
    and would label a feature as a chore.
    """
    lowered = prompt.lower()
    for branch_type, hints in _TYPE_HINTS:
        for hint in hints:
            # Allow ordinary inflections so "crashes" still reads as "crash".
            if re.search(rf"\b{re.escape(hint)}(?:e?s|ed|ing)?\b", lowered):
                return branch_type
    return "feature"


#: Verbs that only restate the branch type. "create a use case" is a feature
#: whether or not the branch says "create", so the subject reads better alone.
_LEAD_VERBS = frozenset(
    """create add implement build make write update remove delete drop fix
    refactor introduce support enable allow setup set change modify improve
    rework rewrite provide""".split()
)


def slugify(text: str, max_words: int = 4, limit: int = 40) -> str:
    """Short, readable branch slug from a free-form prompt.

    Filler words go, a leading verb goes, and repeats are collapsed, so what
    is left is the subject of the request rather than a truncated sentence.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    meaningful = [w for w in words if w not in _FILLER] or words

    while len(meaningful) > 1 and meaningful[0] in _LEAD_VERBS:
        meaningful = meaningful[1:]

    seen: set[str] = set()
    unique = [w for w in meaningful if not (w in seen or seen.add(w))]

    slug = "-".join(unique[:max_words])[:limit].strip("-")
    return slug or "task"


def suggest_branch(prompt: str, branch_type: str | None = None) -> str:
    """A full conventional branch name, e.g. `feature/add-health-endpoint`."""
    branch_type = branch_type or suggest_branch_type(prompt)
    return f"{branch_type}/{slugify(prompt)}"


def compose_branch(branch_type: str, typed: str, prompt: str) -> str:
    """Combine the chosen type with whatever the user typed.

    Nothing typed keeps the suggestion. A bare name becomes the description
    under the chosen type. Only a name that already carries its own
    `type/name` prefix overrides the choice -- typing a plain name must never
    silently discard the type, which is what it used to do.
    """
    typed = (typed or "").strip()
    if not typed:
        return suggest_branch(prompt, branch_type)

    cleaned = sanitize_branch(typed)
    if "/" in cleaned:
        return cleaned
    return f"{branch_type}/{cleaned}"


def sanitize_branch(name: str) -> str:
    """Make a user-typed branch name safe for git."""
    cleaned = re.sub(r"[^A-Za-z0-9/._-]+", "-", name.strip()).strip("-/.")
    cleaned = re.sub(r"/{2,}", "/", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned or "task"


def unique_branch(repo: Path | str, name: str) -> str:
    """`name`, or the first free `name-2`, `name-3`, ... if it is taken.

    A suffix only appears on a real collision, so ordinary runs get a clean
    name rather than one ending in a random id.
    """
    if not branch_exists(repo, name):
        return name
    for suffix in range(2, 100):
        candidate = f"{name}-{suffix}"
        if not branch_exists(repo, candidate):
            return candidate
    return f"{name}-{uuid.uuid4().hex[:6]}"


def validate_target(path: str, base_branch: str) -> Path:
    """Check a repo is usable before a workflow is created.

    Raises GitError with a message meant for the user.
    """
    root = repo_root(path)
    if not branch_exists(root, base_branch):
        raise GitError(f"base branch '{base_branch}' does not exist in {root}")
    if not is_clean(root):
        raise GitError(
            f"{root} has uncommitted changes - commit or stash them first"
        )
    return root


def create_worktree(repo: Path, branch: str, base: str, workflow_id: str) -> Path:
    """Create `branch` from `base` and check it out in a private worktree."""
    dest = config.worktrees_dir() / workflow_id
    if dest.exists():
        raise GitError(f"worktree path already exists: {dest}")
    _git(repo, "worktree", "add", "-b", branch, str(dest), base)
    return dest


def ensure_worktree(repo: Path, branch: str, workflow_id: str) -> Path:
    """Return a live worktree for an existing branch, recreating it if gone.

    A workflow whose worktree was cleaned up can still be retried, and it must
    not run with no working directory at all.
    """
    dest = config.worktrees_dir() / workflow_id
    if (dest / ".git").exists():
        return dest
    if dest.exists():
        raise GitError(f"{dest} exists but is not a worktree - remove it and retry")
    _git(repo, "worktree", "prune", check=False)
    _git(repo, "worktree", "add", str(dest), branch)
    return dest


def remove_worktree(repo: Path | str, worktree: Path | str) -> None:
    _git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def commit_all(worktree: Path | str, message: str) -> bool:
    """Commit anything the agent left uncommitted. True if a commit was made."""
    if is_clean(worktree):
        return False
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)
    return True


def commit_count(repo: Path | str, base: str, branch: str) -> int:
    """How many commits `branch` has that `base` does not."""
    count = _git(repo, "rev-list", "--count", f"{base}..{branch}", check=False)
    return int(count) if count.isdigit() else 0


def has_new_commits(repo: Path | str, base: str, branch: str) -> bool:
    return commit_count(repo, base, branch) > 0


def short_status(worktree: Path | str) -> str:
    return _git(worktree, "status", "--short", check=False)


def recent_commits(repo: Path | str, base: str, branch: str, limit: int = 20) -> str:
    return _git(
        repo, "log", "--oneline", f"-{limit}", f"{base}..{branch}", check=False
    )


def diff(repo: Path | str, base: str, branch: str, max_chars: int = 60000) -> str:
    """Full diff of the branch against its merge base, truncated if enormous."""
    text = _git(repo, "diff", f"{base}...{branch}")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[diff truncated at {max_chars} characters]"
    return text


def diffstat(repo: Path | str, base: str, branch: str) -> str:
    return _git(repo, "diff", "--stat", f"{base}...{branch}")


def merge(repo: Path, base: str, branch: str) -> str:
    """Merge `branch` into `base` with a merge commit.

    The base branch has to be checked out for this, so the caller's branch is
    restored afterwards. A conflict is aborted rather than left in the working
    tree: other workflows share this checkout, and a half-merged index would
    block all of them.
    """
    if not is_clean(repo):
        raise GitError(f"{repo} has uncommitted changes - cannot merge")

    original = current_branch(repo)
    restore = original != base
    _git(repo, "checkout", base)
    try:
        output = _git(repo, "merge", "--no-ff", branch, "-m", f"Merge {branch}")
    except GitError as exc:
        _git(repo, "merge", "--abort", check=False)
        if restore:
            _git(repo, "checkout", original, check=False)
        raise MergeConflict(
            f"merge of '{branch}' into '{base}' conflicts and was aborted; "
            f"resolve it manually with: git checkout {base} && git merge {branch}\n{exc}"
        ) from exc

    if restore:
        _git(repo, "checkout", original, check=False)
    return output
