"""Data model for workflows and their steps.

Everything here is a plain dataclass that round-trips through JSON, so the
store can persist state without knowing anything about the runner.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Role(str, Enum):
    """The three jobs an agent can be assigned in a workflow.

    Only EXECUTE is compulsory: a workflow with no plan agent goes straight to
    the work, and one with no review agent stops asking for a verdict. An
    unfilled role is stored as an empty agent name.
    """

    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"


#: The order the roles run in.
ROLE_ORDER = (Role.PLAN, Role.EXECUTE, Role.REVIEW)


class Mode(str, Enum):
    """Whether a human confirms the merge."""

    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class Status(str, Enum):
    """Workflow status. Also reused for individual steps."""

    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    MERGING = "merging"
    MERGED = "merged"
    CHANGES_REQUESTED = "changes_requested"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Stopped on purpose and meant to be continued, so not terminal.
    PAUSED = "paused"

    # Step-only values.
    RUNNING = "running"
    DONE = "done"


TERMINAL_STATUSES = frozenset(
    {Status.MERGED, Status.FAILED, Status.CANCELLED, Status.CHANGES_REQUESTED}
)

#: Statuses where a background thread should be working on the run.
ACTIVE_STATUSES = frozenset(
    {Status.PLANNING, Status.EXECUTING, Status.REVIEWING, Status.MERGING}
)


class Verdict(str, Enum):
    """What the review agent concluded."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass
class Task:
    """One item of the plan's checklist, implemented and committed on its own.

    Splitting the work means each invocation faces a small, well-defined job
    instead of "implement the whole thing", and progress that is committed
    survives a later item failing.
    """

    index: int
    text: str
    status: Status = Status.PENDING
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "status": self.status.value,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            index=data["index"],
            text=data["text"],
            status=Status(data.get("status", Status.PENDING.value)),
            attempts=data.get("attempts", 0),
        )


@dataclass
class Step:
    """One agent invocation inside a workflow."""

    role: Role
    agent: str
    round: int = 1
    #: Which checklist item this invocation was working on, if any.
    task_index: int | None = None
    status: Status = Status.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    log_path: str | None = None
    output: str = ""
    error: str = ""

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.ended_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["role"] = self.role.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        data = dict(data)
        data["role"] = Role(data["role"])
        data["status"] = Status(data["status"])
        return cls(**data)


@dataclass
class Workflow:
    """A single prompt driven through plan, execute, review and merge.

    Which of those steps actually run is the user's choice, recorded in
    `agents`: a role with no agent is skipped. Execution always runs, so a
    workflow is at minimum "implement this, then merge it".
    """

    prompt: str
    repo_path: str
    base_branch: str
    agents: dict[str, str]
    #: Model per role, when this run wants something other than the backend's
    #: own default. Empty values mean "whatever the CLI would pick".
    models: dict[str, str] = field(default_factory=dict)
    #: Where the prompt came from, when read from a file rather than typed.
    prompt_source: str = ""
    #: Standing instructions, copied from settings when the run is created so
    #: the run stays reproducible even if the settings change later.
    instructions: str = ""
    mode: Mode = Mode.SUPERVISED
    max_rounds: int = 2
    #: How many times one execution attempt may be resumed before giving up.
    max_attempts: int = 3
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: Status = Status.PENDING
    branch: str = ""
    #: True once this workflow actually created its branch. Distinguishes a
    #: retry that should reattach from a new run whose suggested name collides.
    branch_created: bool = False
    worktree: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    steps: list[Step] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    plan_text: str = ""
    review_text: str = ""
    verdict: Verdict | None = None
    message: str = ""
    #: Things worth telling the user that did not stop the run, such as an
    #: agent overrunning its timeout after its work was already committed.
    notes: list[str] = field(default_factory=list)
    round: int = 1

    def agent_for(self, role: Role) -> str:
        """Who fills this role, or "" when the step was turned off."""
        return self.agents.get(role.value, "") or ""

    def runs(self, role: Role) -> bool:
        """Whether this step is part of the workflow at all."""
        return role is Role.EXECUTE or bool(self.agent_for(role))

    @property
    def active_roles(self) -> list[Role]:
        """The steps this run will actually take, in order."""
        return [role for role in ROLE_ORDER if self.runs(role)]

    def model_for(self, role: Role) -> str:
        return self.models.get(role.value, "")

    def add_step(self, role: Role, task_index: int | None = None) -> Step:
        step = Step(
            role=role,
            agent=self.agent_for(role),
            round=self.round,
            task_index=task_index,
        )
        self.steps.append(step)
        return step

    @property
    def pending_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status is not Status.DONE]

    @property
    def task_progress(self) -> str:
        """`2/5` when the plan produced a checklist, empty when it did not."""
        if not self.tasks:
            return ""
        done = sum(1 for t in self.tasks if t.status is Status.DONE)
        return f"{done}/{len(self.tasks)}"

    def last_step(self, role: Role) -> Step | None:
        for step in reversed(self.steps):
            if step.role is role:
                return step
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def summary(self) -> str:
        """Single-line prompt snippet for list views."""
        text = " ".join(self.prompt.split())
        return text[:47] + "..." if len(text) > 50 else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "prompt_source": self.prompt_source,
            "instructions": self.instructions,
            "repo_path": self.repo_path,
            "base_branch": self.base_branch,
            "agents": dict(self.agents),
            "models": dict(self.models),
            "mode": self.mode.value,
            "max_rounds": self.max_rounds,
            "max_attempts": self.max_attempts,
            "status": self.status.value,
            "branch": self.branch,
            "branch_created": self.branch_created,
            "worktree": self.worktree,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
            "tasks": [t.to_dict() for t in self.tasks],
            "plan_text": self.plan_text,
            "review_text": self.review_text,
            "verdict": self.verdict.value if self.verdict else None,
            "message": self.message,
            "notes": list(self.notes),
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        return cls(
            id=data["id"],
            prompt=data["prompt"],
            prompt_source=data.get("prompt_source", ""),
            instructions=data.get("instructions", ""),
            repo_path=data["repo_path"],
            base_branch=data["base_branch"],
            agents=dict(data["agents"]),
            models=dict(data.get("models", {})),
            mode=Mode(data["mode"]),
            max_rounds=data.get("max_rounds", 2),
            max_attempts=data.get("max_attempts", 3),
            status=Status(data["status"]),
            branch=data.get("branch", ""),
            branch_created=data.get("branch_created", bool(data.get("worktree"))),
            worktree=data.get("worktree", ""),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            steps=[Step.from_dict(s) for s in data.get("steps", [])],
            tasks=[Task.from_dict(t) for t in data.get("tasks", [])],
            plan_text=data.get("plan_text", ""),
            review_text=data.get("review_text", ""),
            verdict=Verdict(data["verdict"]) if data.get("verdict") else None,
            message=data.get("message", ""),
            notes=list(data.get("notes", [])),
            round=data.get("round", 1),
        )
