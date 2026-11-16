"""The live progress view of a run in flight."""

import io
import re

import pytest
from rich.console import Console

from maestro.models import Role, Status, Task, Workflow
from maestro.shell import progress, render

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@pytest.fixture
def screen(monkeypatch):
    """Capture the view, with the pause between repaints removed."""
    buffer = io.StringIO()
    console = Console(theme=render.THEME, file=buffer, width=100, force_terminal=False,
                      legacy_windows=False, highlight=False)
    monkeypatch.setattr(render, "console", console)
    monkeypatch.setattr(progress, "console", console)
    monkeypatch.setattr(progress.time, "sleep", lambda seconds: None)
    return lambda: ANSI.sub("", buffer.getvalue())


def make(status=Status.EXECUTING, tasks=None):
    wf = Workflow(
        prompt="a task", repo_path="/r", base_branch="main",
        agents={"plan": "claude", "execute": "opencode", "review": "codex"},
    )
    wf.status = status
    wf.branch = "feature/thing"
    wf.tasks = tasks or []
    return wf


class TestCurrentStep:
    def test_given_a_running_task_step_when_described_then_role_agent_and_task_are_named(self):
        wf = make()
        wf.add_step(Role.PLAN).status = Status.DONE
        step = wf.add_step(Role.EXECUTE, task_index=2)
        step.status = Status.RUNNING
        assert progress._current_step(wf) == "execute | opencode | task 2"

    def test_given_a_running_step_without_a_task_when_described_then_the_round_is_named(self):
        wf = make()
        wf.round = 2
        wf.add_step(Role.REVIEW).status = Status.RUNNING
        assert progress._current_step(wf) == "review | codex | round 2"

    def test_given_nothing_running_when_described_then_the_run_status_stands_in(self):
        wf = make(Status.AWAITING_APPROVAL)
        wf.add_step(Role.EXECUTE).status = Status.DONE
        assert progress._current_step(wf) == "awaiting_approval"


class TestColumns:
    def test_given_a_known_total_when_columns_are_made_then_a_bar_is_included(self):
        assert len(progress._columns(total_known=True).columns) == 5

    def test_given_no_total_when_columns_are_made_then_there_is_no_bar(self):
        assert len(progress._columns(total_known=False).columns) == 3


class TestWatch:
    def test_given_a_finished_run_when_watched_then_its_outcome_is_printed_once(self, screen):
        wf = make(Status.MERGED, tasks=[Task(1, "first", Status.DONE)])
        wf.message = "merged 'feature/thing' into 'main'"

        progress.watch(wf, lambda workflow_id: False)

        shown = screen()
        assert f"{wf.id[:6]} is merged" in shown
        assert "merged 'feature/thing' into 'main'" in shown
        assert "feature/thing" in shown

    def test_given_a_running_checklist_when_watched_then_it_repaints_until_the_run_stops(self, screen):
        wf = make(Status.EXECUTING, tasks=[Task(1, "first"), Task(2, "second")])
        polls = []

        def is_running(workflow_id):
            polls.append(workflow_id)
            wf.tasks[len(polls) - 1].status = Status.DONE
            if len(polls) == 2:
                wf.status = Status.FAILED
                wf.message = "the review step failed"
                return False
            return True

        progress.watch(wf, is_running)

        assert len(polls) == 2
        shown = screen()
        assert f"{wf.id[:6]} is failed" in shown
        assert "the review step failed" in shown

    def test_given_a_thread_gone_but_a_status_still_active_when_watched_then_it_keeps_watching(self, screen):
        wf = make(Status.MERGING)
        polls = []

        def is_running(workflow_id):
            polls.append(workflow_id)
            if len(polls) == 3:
                wf.status = Status.MERGED
            return False

        progress.watch(wf, is_running)
        assert len(polls) == 3

    def test_given_ctrl_c_when_watching_then_it_stops_and_says_the_run_continues(self, screen):
        wf = make(Status.EXECUTING)

        def interrupted(workflow_id):
            raise KeyboardInterrupt

        progress.watch(wf, interrupted)

        shown = screen()
        assert "stopped watching - the run continues" in shown
        assert f"{wf.id[:6]} is" not in shown
