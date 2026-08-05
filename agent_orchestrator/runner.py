"""The workflow state machine.

One background thread per workflow walks it through plan, execute, review and
merge. This is the only module that mutates workflow state; everything else
reads it.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import backends, gitops, prompts, store as store_mod
from .backends import BackendError
from .gitops import GitError
from .models import (
    ACTIVE_STATUSES,
    Mode,
    Role,
    Status,
    Step,
    Task,
    Verdict,
    Workflow,
)

_VERDICT_RE = re.compile(
    r"^\s*(?:[*_`#>\-\s]*)VERDICT\s*[:\-]\s*(APPROVE|REQUEST[_ ]CHANGES)\b",
    re.IGNORECASE | re.MULTILINE,
)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _shorten(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _last_words(output: str, limit: int = 220) -> str:
    """The tail of an agent's output, for putting a cause in a failure message.

    Agent output is full of progress decoration, so colour codes are stripped
    and blank lines dropped before taking the last of what it actually said.
    """
    clean = [line.strip() for line in _ANSI.sub("", output or "").splitlines()]
    meaningful = [line for line in clean if line]
    if not meaningful:
        return "(it printed nothing)"
    tail = " / ".join(meaningful[-3:])
    return tail if len(tail) <= limit else "..." + tail[-limit:]


_NUMBERED = re.compile(r"^\s*(?:[-*]\s*)?(\d{1,2})[.)]\s+(.+?)\s*$")

#: Refuse absurd checklists rather than firing off dozens of invocations.
MAX_TASKS = 15


def parse_tasks(plan_text: str) -> list[Task]:
    """Pull the checklist out of a plan.

    Only the numbered list under the TASKS heading counts; plans are full of
    other numbered prose. An unparseable plan yields nothing, and the caller
    falls back to implementing it in one go.
    """
    lines = (plan_text or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().rstrip(":").upper() == prompts.TASKS_HEADING.rstrip(":"):
            start = index + 1
    if start is None:
        return []

    items: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if items:
                break  # a blank line ends the list once it has begun
            continue
        match = _NUMBERED.match(line)
        if not match:
            if items:
                break
            continue
        items.append(match.group(2).strip())

    cleaned = [text for text in items if text][:MAX_TASKS]
    return [Task(index=i, text=text) for i, text in enumerate(cleaned, start=1)]


def parse_verdict(text: str) -> Verdict:
    """Extract the reviewer's verdict from its output.

    The last verdict line wins, since agents often restate the format before
    committing to an answer. Anything unrecognised counts as REQUEST_CHANGES -
    an ambiguous reviewer must never cause an automatic merge.
    """
    matches = _VERDICT_RE.findall(text or "")
    if not matches:
        return Verdict.REQUEST_CHANGES
    last = matches[-1].upper().replace(" ", "_")
    return Verdict.APPROVE if last == "APPROVE" else Verdict.REQUEST_CHANGES


class Runner:
    """Owns the worker threads and drives workflows to completion."""

    def __init__(self, store: store_mod.Store) -> None:
        self.store = store
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._pauses: dict[str, threading.Event] = {}

    # ---------------------------------------------------------------- lifecycle

    def adopt_orphans(self) -> int:
        """Fail runs left mid-flight by a previous process.

        Their threads died with that process, so on restart they can only be
        reported, not resumed. The branch and logs are left alone.
        """
        count = 0
        for wf in self.store.all():
            if wf.status in ACTIVE_STATUSES and wf.id not in self._threads:
                wf.status = Status.FAILED
                wf.message = "interrupted - the orchestrator exited while this was running"
                for step in wf.steps:
                    if step.status is Status.RUNNING:
                        step.status = Status.FAILED
                        step.ended_at = time.time()
                count += 1
        if count:
            self.store.save()
        return count

    def is_running(self, workflow_id: str) -> bool:
        thread = self._threads.get(workflow_id)
        return thread is not None and thread.is_alive()

    def start(self, workflow: Workflow) -> None:
        """Kick off a workflow in the background."""
        if self.is_running(workflow.id):
            return
        cancel = threading.Event()
        pause = threading.Event()
        self._cancels[workflow.id] = cancel
        self._pauses[workflow.id] = pause
        thread = threading.Thread(
            target=self._run, args=(workflow, cancel, pause), daemon=True,
            name=f"workflow-{workflow.id}",
        )
        self._threads[workflow.id] = thread
        thread.start()

    def cancel(self, workflow: Workflow) -> None:
        """Ask a run to stop at the next step boundary.

        An agent already in flight is left to finish; killing a CLI mid-edit
        would leave the worktree in an unknown state.
        """
        event = self._cancels.get(workflow.id)
        if event:
            event.set()
        if not self.is_running(workflow.id):
            self._finish(workflow, Status.CANCELLED, "cancelled")

    def pause(self, workflow: Workflow) -> None:
        """Stop at the next step boundary, meaning to carry on later.

        Like cancelling, an agent already in flight is left to finish rather
        than killed mid-edit; unlike cancelling, the run is left resumable and
        keeps everything it has committed.
        """
        if workflow.status is Status.MERGED:
            return
        event = self._pauses.get(workflow.id)
        if event:
            event.set()
        if not self.is_running(workflow.id):
            self._finish(workflow, Status.PAUSED, "paused")

    def resume(self, workflow: Workflow) -> None:
        """Continue a stopped run from where it left off.

        Checklist items already committed stay done; a stalled one is put back
        in the queue. Unlike a retry this does not consume a rework round --
        the work was never rejected, it just did not finish.
        """
        if self.is_running(workflow.id) or workflow.status is Status.MERGED:
            return
        # A run paused earlier still carries its stop flag; clear it or the
        # resumed thread would stop again at the first boundary.
        paused = self._pauses.get(workflow.id)
        if paused:
            paused.clear()
        for task in workflow.tasks:
            if task.status is Status.FAILED:
                task.status = Status.PENDING
                task.attempts = 0
        workflow.message = ""
        workflow.verdict = None
        self.start(workflow)

    def retry(self, workflow: Workflow) -> None:
        """Run another rework round over a workflow the reviewer rejected."""
        if self.is_running(workflow.id):
            return
        workflow.round += 1
        workflow.verdict = None
        workflow.message = ""
        self.start(workflow)

    def approve(self, workflow: Workflow) -> None:
        """Human confirmation of a supervised merge."""
        if workflow.status is not Status.AWAITING_APPROVAL:
            return
        thread = threading.Thread(
            target=self._merge_stage, args=(workflow,), daemon=True,
            name=f"merge-{workflow.id}",
        )
        self._threads[workflow.id] = thread
        thread.start()

    def reject(self, workflow: Workflow, reason: str = "") -> None:
        """Human declines a supervised merge."""
        if workflow.status is not Status.AWAITING_APPROVAL:
            return
        self._finish(
            workflow,
            Status.CHANGES_REQUESTED,
            reason or "merge rejected by the user",
        )

    # ------------------------------------------------------------------- stages

    def _run(self, wf: Workflow, cancel: threading.Event,
             pause: threading.Event | None = None) -> None:
        try:
            self._prepare_workspace(wf)

            if self._cancelled(wf, cancel, pause):
                return

            # Plan only when there isn't one. Replanning on a resume would
            # throw away the checklist along with everything already ticked
            # off it, and re-do work that is committed.
            if not wf.plan_text:
                if not self._plan_stage(wf):
                    return

            feedback = ""
            while True:
                if self._cancelled(wf, cancel, pause):
                    return
                if not self._execute_stage(wf, feedback):
                    return
                if self._cancelled(wf, cancel, pause):
                    return

                verdict = self._review_stage(wf)
                if verdict is None:
                    return
                if verdict is Verdict.APPROVE:
                    break

                if wf.round >= wf.max_rounds:
                    self._finish(
                        wf,
                        Status.CHANGES_REQUESTED,
                        f"reviewer still requesting changes after {wf.round} round(s)",
                    )
                    return
                wf.round += 1
                feedback = wf.review_text
                self._touch(wf)

            if wf.mode is Mode.SUPERVISED:
                self._set_status(
                    wf, Status.AWAITING_APPROVAL, "approved by reviewer - awaiting your confirmation"
                )
                return

            self._merge_stage(wf)

        except (GitError, BackendError) as exc:
            self._finish(wf, Status.FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - a crashed thread must not vanish silently
            self._finish(wf, Status.FAILED, f"unexpected error: {exc!r}")

    def _prepare_workspace(self, wf: Workflow) -> None:
        """Make sure the workflow has its branch checked out in a live worktree.

        Covers all three entry states: a fresh run, a retry whose branch and
        worktree still exist, and a retry whose worktree was cleaned up.
        """
        repo = gitops.repo_root(wf.repo_path)
        wf.repo_path = str(repo)

        if not wf.branch:
            wf.branch = gitops.suggest_branch(wf.prompt)

        if wf.branch_created:
            # A retry. Reuse the branch, restoring the worktree if it was
            # cleaned up; never branch again, or the work would be abandoned.
            if not (wf.worktree and Path(wf.worktree).exists()):
                wf.worktree = str(gitops.ensure_worktree(repo, wf.branch, wf.id))
                self._touch(wf)
            return

        # A first run. The suggested name may collide with an unrelated branch
        # or with another workflow started from the same prompt.
        wf.branch = gitops.unique_branch(repo, wf.branch)
        wf.worktree = str(gitops.create_worktree(repo, wf.branch, wf.base_branch, wf.id))
        wf.branch_created = True
        self._touch(wf)

    # Kept as the name the integration tests use to restore a cleaned worktree.
    _reattach_worktree = _prepare_workspace

    def _plan_stage(self, wf: Workflow) -> bool:
        self._set_status(wf, Status.PLANNING)
        step = wf.add_step(Role.PLAN)
        result = self._invoke(wf, step, prompts.plan_prompt(wf))
        if result is None:
            return False
        wf.plan_text = result.strip()
        if not wf.plan_text:
            self._finish(wf, Status.FAILED, "the planning agent produced no output")
            return False

        wf.tasks = parse_tasks(wf.plan_text)
        self._touch(wf)
        return True

    def _execute_stage(self, wf: Workflow, feedback: str) -> bool:
        """Implement the plan, one checklist item at a time where there is one."""
        self._set_status(wf, Status.EXECUTING)

        if feedback:
            # A rework pass addresses the review, not the original checklist.
            return self._attempt(
                wf,
                what="address the review feedback",
                first_prompt=prompts.execute_prompt(wf, feedback),
                commit_message=f"aiorch: address review, round {wf.round}",
            )

        if not wf.tasks:
            return self._attempt(
                wf,
                what="implement the plan",
                first_prompt=prompts.execute_prompt(wf, ""),
                commit_message=f"aiorch: round {wf.round} changes",
            )

        for task in wf.tasks:
            if task.status is Status.DONE:
                continue  # already landed on an earlier attempt
            if not self._run_task(wf, task):
                return False
        return True

    def _run_task(self, wf: Workflow, task: Task) -> bool:
        ok = self._attempt(
            wf,
            what=task.text,
            first_prompt=prompts.task_prompt(wf, task),
            commit_message=f"aiorch: {task.text[:60]}",
            task=task,
        )
        task.status = Status.DONE if ok else Status.FAILED
        self._touch(wf)
        return ok

    def _attempt(
        self,
        wf: Workflow,
        what: str,
        first_prompt: str,
        commit_message: str,
        task: Task | None = None,
    ) -> bool:
        """Run one piece of work, resuming it while it makes no progress.

        An agent that exits cleanly having committed nothing has not done the
        job -- it usually ran out of turns mid-exploration. Rather than failing
        the whole workflow on that, it is pointed back at the worktree and
        asked to finish, up to `max_attempts` times.
        """
        before = gitops.commit_count(wf.repo_path, wf.base_branch, wf.branch)
        prompt = first_prompt
        last_step: Step | None = None
        why: str = ""

        for attempt in range(1, max(1, wf.max_attempts) + 1):
            if task is not None:
                task.attempts = attempt
            step = wf.add_step(Role.EXECUTE, task_index=task.index if task else None)
            last_step = step

            result = self._run_step(wf, step, prompt, attempt=attempt)
            if result is None:
                return False  # the backend is unusable; already reported

            # Agents are asked to commit; not all of them reliably do.
            gitops.commit_all(wf.worktree, commit_message)
            after = gitops.commit_count(wf.repo_path, wf.base_branch, wf.branch)

            if after > before:
                # The work landed. How the process ended is a footnote: an
                # agent that commits and then overruns its timeout has still
                # done the job, and throwing that away would be perverse.
                if result.timed_out:
                    step.status = Status.DONE
                    self._note(
                        wf,
                        f"{step.agent} ran past its timeout on "
                        f"'{_shorten(what)}', but its work was committed",
                    )
                elif not result.ok:
                    step.status = Status.DONE
                    self._note(
                        wf,
                        f"{step.agent} exited with code {result.exit_code} on "
                        f"'{_shorten(what)}', but its work was committed",
                    )
                return True

            if result.timed_out:
                why = f"it timed out with nothing committed"
            elif not result.ok:
                why = f"it exited with code {result.exit_code} and committed nothing"
            else:
                why = "it committed nothing"

            if attempt < wf.max_attempts:
                prompt = prompts.continue_prompt(wf, what, self._worktree_state(wf))
                self._touch(wf)

        agent = last_step.agent if last_step else "the execute agent"
        said = _last_words(last_step.output) if last_step else "(nothing)"
        hint = (
            " - raise its 'timeout' in backends.json, or split the work into "
            "smaller checklist items"
            if last_step is not None and last_step.error == "timed out"
            else ""
        )
        self._finish(
            wf,
            Status.FAILED,
            f"{agent} could not complete '{_shorten(what)}' in "
            f"{plural(wf.max_attempts, 'attempt')}: {why}{hint}. "
            f"Its last words: {said}",
        )
        return False

    def _note(self, wf: Workflow, message: str) -> None:
        """Record a warning that does not stop the run.

        Kept apart from `message`, which the final status overwrites -- a
        warning about an overrun must survive the run going on to merge.
        """
        if message not in wf.notes:
            wf.notes.append(message)
        self._touch(wf)

    def _worktree_state(self, wf: Workflow) -> str:
        """What the resuming agent needs to re-orient itself."""
        try:
            status = gitops.short_status(wf.worktree) or "(no uncommitted changes)"
            log = gitops.recent_commits(wf.repo_path, wf.base_branch, wf.branch)
        except GitError as exc:
            return f"(could not inspect the worktree: {exc})"
        return f"Uncommitted changes:\n{status}\n\nCommits so far on this branch:\n{log or '(none)'}"

    def _review_stage(self, wf: Workflow) -> Verdict | None:
        self._set_status(wf, Status.REVIEWING)
        step = wf.add_step(Role.REVIEW)
        diff_text = gitops.diff(wf.repo_path, wf.base_branch, wf.branch)
        output = self._invoke(wf, step, prompts.review_prompt(wf, diff_text))
        if output is None:
            return None
        wf.review_text = output.strip()
        wf.verdict = parse_verdict(output)
        self._touch(wf)
        return wf.verdict

    def _merge_stage(self, wf: Workflow) -> None:
        self._set_status(wf, Status.MERGING)
        try:
            gitops.merge(Path(wf.repo_path), wf.base_branch, wf.branch)
        except GitError as exc:
            self._finish(wf, Status.FAILED, str(exc))
            return
        self._finish(wf, Status.MERGED, f"merged '{wf.branch}' into '{wf.base_branch}'")

    # ------------------------------------------------------------------ helpers

    def _run_step(
        self, wf: Workflow, step: Step, prompt: str, attempt: int | None = None
    ) -> "backends.RunResult | None":
        """Run one agent and record the step.

        None means the backend could not be invoked at all, which is fatal and
        already reported. A process that timed out or exited badly comes back
        as a result: whether that ends the workflow is the caller's decision,
        because an execute step may well have committed its work first.
        """
        step.status = Status.RUNNING
        step.started_at = time.time()
        step.log_path = str(
            store_mod.log_path_for(
                wf.id, step.role.value, step.round, step.task_index, attempt
            )
        )
        self._touch(wf)

        try:
            result = backends.run(
                step.agent, step.role, prompt, wf.worktree, Path(step.log_path),
                model=wf.model_for(step.role),
            )
        except BackendError as exc:
            step.status = Status.FAILED
            step.ended_at = time.time()
            step.error = str(exc)
            self._finish(wf, Status.FAILED, f"{step.role.value} step: {exc}")
            return None

        step.ended_at = time.time()
        step.exit_code = result.exit_code
        step.output = result.output
        if result.timed_out:
            step.error = "timed out"
        elif not result.ok:
            step.error = f"exit code {result.exit_code}"
        step.status = Status.DONE if result.ok else Status.FAILED
        self._touch(wf)
        return result

    def _invoke(self, wf: Workflow, step: Step, prompt: str) -> str | None:
        """Run an agent whose step has no other measure of success.

        Used for planning and review, where nothing is committed and a timeout
        or bad exit code simply means the step did not happen.
        """
        result = self._run_step(wf, step, prompt)
        if result is None:
            return None
        if result.timed_out:
            self._finish(
                wf, Status.FAILED,
                f"the {step.agent} {step.role.value} step timed out - raise "
                f"'timeout' for {step.agent} in backends.json if it needs longer",
            )
            return None
        if not result.ok:
            self._finish(
                wf,
                Status.FAILED,
                f"the {step.agent} {step.role.value} step exited with code "
                f"{result.exit_code}: {_last_words(result.output)}",
            )
            return None
        return result.output

    def _cancelled(self, wf: Workflow, cancel: threading.Event,
                   pause: threading.Event | None = None) -> bool:
        """True when the run should stop here, recording why."""
        if cancel.is_set():
            self._finish(wf, Status.CANCELLED, "cancelled")
            return True
        if pause is not None and pause.is_set():
            self._finish(wf, Status.PAUSED, "paused - /resume to continue")
            return True
        return False

    def _set_status(self, wf: Workflow, status: Status, message: str = "") -> None:
        wf.status = status
        if message:
            wf.message = message
        self._touch(wf)

    def _finish(self, wf: Workflow, status: Status, message: str) -> None:
        wf.status = status
        wf.message = message
        self._touch(wf)

    def _touch(self, wf: Workflow) -> None:
        wf.updated_at = time.time()
        self.store.add(wf)
