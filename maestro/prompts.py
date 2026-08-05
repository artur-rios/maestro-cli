"""The prompts sent to each agent.

Kept apart from the runner so the wording can be tuned without touching the
state machine, and so tests can assert on it directly.
"""

from __future__ import annotations

import re

from .models import Status, Task, Workflow

VERDICT_APPROVE = "VERDICT: APPROVE"
VERDICT_REQUEST_CHANGES = "VERDICT: REQUEST_CHANGES"


TASKS_HEADING = "TASKS:"

#: `UC-03`, `UC 3`, `use case 7`, `use-case 11`.
_USE_CASE = re.compile(
    r"\b(?:UC[-\s]?(\d{1,3})|use[-\s]?cases?\s+(?:number\s+)?(\d{1,3}))\b",
    re.IGNORECASE,
)


def use_case_reference(text: str) -> str:
    """The use case a task names, e.g. "UC-03", or empty when it names none."""
    match = _USE_CASE.search(text or "")
    if not match:
        return ""
    number = match.group(1) or match.group(2)
    return f"UC-{int(number):02d}"


def conventions(workflow: Workflow, may_edit: bool) -> str:
    """How to work: the project's own rules, not this tool's.

    Agents arrive with skills, slash commands and repository instructions of
    their own, and a bare "you are the PLANNING agent" framing makes them
    ignore all of it. This says plainly that the project's way of working wins,
    while reserving branching and merging for maestro so a workflow
    skill does not try to run the git side as well.
    """
    lines = [
        "How to work on this:",
        "",
        "- Start by checking which of your available skills apply to this task,",
        "  and invoke every one that does. Open and read them; do not work from",
        "  a memory or a guess of what a skill contains, and never describe a",
        "  rule you have not actually read.",
        "- Follow this repository's own instructions -- CLAUDE.md, AGENTS.md,",
        "  README, and anything under docs/ covering the development workflow,",
        "  business rules, testing standards or specifications. They outrank",
        "  generic practice and anything in this message.",
        "- Say which skills and repository documents you used, and what each one",
        "  required of you. If none applied, say that instead.",
    ]

    use_case = use_case_reference(workflow.prompt)
    if use_case:
        lines.append(
            f"- This task concerns {use_case}. Find its specification in the"
            "\n  repository and follow the project's use case workflow for it."
        )

    if may_edit:
        lines.append(
            "- Branching, committing and merging are handled for you. Work on"
            "\n  the current branch; do not create branches, open pull requests"
            "\n  or merge anything."
        )
    else:
        lines.append("- Do not modify any files in this step.")

    if workflow.instructions:
        lines += ["", "Standing instructions from the user:", workflow.instructions]

    return "\n".join(lines)


def plan_prompt(workflow: Workflow) -> str:
    return f"""You are the PLANNING agent in a three-agent workflow.

Task from the user:
{workflow.prompt}

You are in a git worktree on branch '{workflow.branch}', branched from
'{workflow.base_branch}'. Read whatever you need to understand the codebase.

{conventions(workflow, may_edit=False)}

Write an implementation plan for the agent that will do the work next. Cover
which files to change, in what order, and how to verify the result. Be concrete
and specific to this repository, and to the conventions it documents.

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

Output only the plan and the checklist.
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
You are in a git worktree on branch '{workflow.branch}'.

{conventions(workflow, may_edit=True)}

Implement the plan: make the changes, run the project's tests if it has any,
and commit your work with a clear message.
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

{conventions(workflow, may_edit=True)}

Work on item {task.index} ONLY:

    {task.text}

Implement it, run the project's tests if it has any, and commit your work with
a clear message. Items already ticked are done; do not redo them. Do not start
later items.

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

{conventions(workflow, may_edit=True)}

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

{conventions(workflow, may_edit=False)}

Review this change. Judge whether it correctly and completely accomplishes the
original task, whether it follows the conventions this repository documents,
whether it is consistent with the surrounding code, and whether it introduces
bugs.

Write your reasoning first. Then end your response with a single line that is
exactly one of:

{VERDICT_APPROVE}
{VERDICT_REQUEST_CHANGES}

If you request changes, say precisely what must be fixed.
"""
