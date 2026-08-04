"""The prompts sent to each agent.

Kept apart from the runner so the wording can be tuned without touching the
state machine, and so tests can assert on it directly.
"""

from __future__ import annotations

from .models import Status, Task, Workflow

VERDICT_APPROVE = "VERDICT: APPROVE"
VERDICT_REQUEST_CHANGES = "VERDICT: REQUEST_CHANGES"


TASKS_HEADING = "TASKS:"


def plan_prompt(workflow: Workflow) -> str:
    return f"""You are the PLANNING agent in a three-agent workflow.

Task from the user:
{workflow.prompt}

You are in a git worktree on branch '{workflow.branch}', branched from
'{workflow.base_branch}'. Read whatever you need to understand the codebase.

Write an implementation plan for the agent that will do the work next. Cover
which files to change, in what order, and how to verify the result. Be concrete
and specific to this repository.

Then end your response with a checklist, introduced by a line containing
exactly:

{TASKS_HEADING}

followed by a numbered list. Each item must be one self-contained step that
can be implemented and committed on its own, in the order given, phrased as an
instruction. Aim for between 2 and 10 items; prefer fewer, larger items over
many trivial ones. For example:

{TASKS_HEADING}
1. Add the ScopeCustomField entity and its EF configuration.
2. Add the repository methods for reading and writing custom fields.
3. Expose the endpoints on ScopeController and cover them with tests.

Do NOT modify any files. Output only the plan and the checklist.
"""


def execute_prompt(workflow: Workflow, feedback: str = "") -> str:
    revision = ""
    if feedback:
        revision = f"""
A previous attempt was reviewed and rejected. Address this feedback:

{feedback}
"""

    return f"""You are the EXECUTION agent in a three-agent workflow.

Task from the user:
{workflow.prompt}

The planning agent produced this plan:

{workflow.plan_text}
{revision}
You are in a git worktree on branch '{workflow.branch}'. Implement the plan:
make the changes, run the project's tests if it has any, and commit your work
with a clear message. Do not merge, push, or switch branches.
"""


def task_prompt(workflow: Workflow, task: Task) -> str:
    """Ask for one checklist item, with the rest of the list as context."""
    checklist = "\n".join(
        f"  {'[x]' if t.status is Status.DONE else '[ ]'} {t.index}. {t.text}"
        for t in workflow.tasks
    )
    return f"""You are the EXECUTION agent in a three-agent workflow.

Overall task from the user:
{workflow.prompt}

The agreed plan:

{workflow.plan_text}

Checklist ({workflow.task_progress} done):
{checklist}

You are in a git worktree on branch '{workflow.branch}'.

Work on item {task.index} ONLY:

    {task.text}

Implement it, run the project's tests if it has any, and commit your work with
a clear message. Items already ticked are done; do not redo them. Do not start
later items. Do not merge, push, or switch branches.

If item {task.index} turns out to need no changes, say so explicitly and make
no commit.
"""


def continue_prompt(workflow: Workflow, what: str, state: str) -> str:
    """Resume an execution attempt that ended without committing anything.

    The worktree is the state that matters, so the agent is pointed back at it
    rather than at a saved session. That keeps this working identically across
    CLIs and safe when several workflows run at once.
    """
    return f"""You are the EXECUTION agent in a three-agent workflow, resuming
work you did not finish.

Overall task from the user:
{workflow.prompt}

The agreed plan:

{workflow.plan_text}

You were asked to do this:

    {what}

You stopped without committing anything, so this is not done yet. You may have
run out of turns, or explored without making changes.

The current state of the worktree:

{state}

Read whatever you need to re-orient, then finish the work and commit it. Make
the change; do not merely describe it.
"""


def review_prompt(workflow: Workflow, diff_text: str) -> str:
    return f"""You are the REVIEW agent in a three-agent workflow.

Original task from the user:
{workflow.prompt}

The plan that was agreed:

{workflow.plan_text}

Here is the complete diff of branch '{workflow.branch}' against
'{workflow.base_branch}':

```diff
{diff_text}
```

Review this change. Judge whether it correctly and completely accomplishes the
original task, whether it is consistent with the surrounding code, and whether
it introduces bugs.

Write your reasoning first. Then end your response with a single line that is
exactly one of:

{VERDICT_APPROVE}
{VERDICT_REQUEST_CHANGES}

If you request changes, say precisely what must be fixed.
"""
