"""Pausing a run, and picking it up again."""

import threading
from unittest import mock

import pytest

from maestro.backends import RunResult
from maestro.gitops import GitError
from maestro.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    Mode,
    Role,
    Status,
    Task,
    Workflow,
)
from maestro.runner import Runner
from maestro.store import Store


class PauseBase:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.store = Store()
        self.runner = Runner(self.store)
        self.git = mock.MagicMock()
        monkeypatch.setattr("maestro.runner.gitops", self.git)
        self.git.GitError = GitError
        self.git.diff.return_value = "diff"
        self.git.short_status.return_value = ""
        self.git.recent_commits.return_value = ""
        self.commits = 0
        self.git.commit_count.side_effect = lambda *a, **k: self.commits
        self.executed: list[str] = []
        # Set by a test that wants the first execute step held open.
        self.started = threading.Event()
        self.release: threading.Event | None = None
        # Every agent call goes to the fake, never to a CLI on the machine.
        monkeypatch.setattr("maestro.runner.backends.run", self.agent)
        yield
        if self.release is not None:
            self.release.set()
        for thread in list(self.runner._threads.values()):
            thread.join(timeout=10)

    def make(self, status=Status.PENDING, tasks=None):
        wf = Workflow(
            prompt="build the thing", repo_path=str(self.tmp), base_branch="main",
            agents={"plan": "claude", "execute": "opencode", "review": "claude"},
            mode=Mode.AUTONOMOUS, max_rounds=1, max_attempts=1,
        )
        wf.branch = "feature/thing"
        wf.branch_created = True
        wf.worktree = str(self.tmp)
        wf.plan_text = "the plan"
        wf.status = status
        wf.tasks = tasks or []
        self.store.add(wf)
        return wf

    def agent(self, name, role, prompt, cwd, log_file, backends=None, model=""):
        if role is Role.EXECUTE:
            self.started.set()
            if self.release is not None:
                assert self.release.wait(timeout=10), "the test never released the agent"
            self.executed.append(prompt)
            self.commits += 1
            return RunResult(0, "done")
        if role is Role.PLAN:
            return RunResult(0, "plan\n\nTASKS:\n1. one\n2. two\n")
        return RunResult(0, "VERDICT: APPROVE")

    def drive(self, wf, cancel=None, pause=None):
        self.runner._run(wf, cancel or threading.Event(), pause)

    def join(self, wf):
        thread = self.runner._threads.get(wf.id)
        if thread:
            thread.join(timeout=10)
            assert not thread.is_alive()

    def pause_mid_run(self, wf):
        """Start the run, pause it while its agent works, then let the agent finish."""
        self.release = threading.Event()
        self.runner.start(wf)
        assert self.started.wait(timeout=10)
        self.runner.pause(wf)
        self.release.set()
        self.join(wf)
        self.release = None


class TestPausing(PauseBase):
    def test_given_a_set_pause_flag_when_the_run_starts_then_it_stops_before_the_next_step(self):
        wf = self.make(tasks=[Task(1, "first")])
        paused = threading.Event()
        paused.set()

        self.drive(wf, pause=paused)

        assert wf.status is Status.PAUSED
        assert self.executed == []

    def test_given_a_paused_run_when_its_message_is_read_then_it_says_how_to_carry_on(self):
        wf = self.make()
        paused = threading.Event()
        paused.set()
        self.drive(wf, pause=paused)
        assert "/resume" in wf.message

    def test_given_an_agent_at_work_when_paused_then_it_finishes_its_step_and_the_run_stops_before_review(self):
        wf = self.make(tasks=[Task(1, "first")])
        self.pause_mid_run(wf)
        assert wf.status is Status.PAUSED, wf.message
        assert len(self.executed) == 1  # the step in flight was not killed
        assert not any(s.role is Role.REVIEW for s in wf.steps)

    def test_given_an_idle_run_when_paused_then_it_is_marked_immediately(self):
        wf = self.make(Status.FAILED)
        self.runner.pause(wf)
        assert wf.status is Status.PAUSED
        assert Store().get(wf.id).status is Status.PAUSED

    def test_given_a_merged_run_when_paused_then_it_stays_merged(self):
        wf = self.make(Status.MERGED)
        self.runner.pause(wf)
        assert wf.status is Status.MERGED

    def test_given_the_paused_status_when_classified_then_it_is_neither_running_nor_finished(self):
        """It must survive a restart and not be swept up by /clear finished."""
        assert Status.PAUSED not in ACTIVE_STATUSES
        assert Status.PAUSED not in TERMINAL_STATUSES

    def test_given_a_paused_run_when_orphans_are_swept_then_it_is_left_alone(self):
        wf = self.make(Status.PAUSED)
        assert Runner(self.store).adopt_orphans() == 0
        assert wf.status is Status.PAUSED

    def test_given_a_paused_run_when_the_store_is_reopened_then_it_is_still_paused(self):
        wf = self.make(Status.PAUSED)
        assert Store().get(wf.id).status is Status.PAUSED

    def test_given_a_set_cancel_flag_when_the_run_starts_then_it_is_cancelled_not_paused(self):
        """Cancelling ends a run; pausing leaves it to be continued."""
        wf = self.make()
        cancelled = threading.Event()
        cancelled.set()
        self.drive(wf, cancel=cancelled)
        assert wf.status is Status.CANCELLED


class TestResumingAfterPause(PauseBase):
    def resume(self, wf):
        self.runner.resume(wf)
        self.join(wf)

    def test_given_a_run_paused_mid_flight_when_resumed_then_it_carries_on_to_the_merge(self):
        wf = self.make(tasks=[Task(1, "first"), Task(2, "second")])
        self.pause_mid_run(wf)
        assert wf.status is Status.PAUSED

        self.resume(wf)
        assert wf.status is Status.MERGED, wf.message
        assert len(self.executed) == 2  # each item ran once, none again

    def test_given_a_run_paused_mid_flight_when_resumed_then_the_pause_flag_is_cleared(self):
        """Otherwise the resumed thread stops again at the first boundary."""
        wf = self.make(tasks=[Task(1, "first")])
        self.pause_mid_run(wf)
        assert self.runner._pauses[wf.id].is_set()

        self.resume(wf)
        assert not self.runner._pauses[wf.id].is_set()
        assert wf.status is Status.MERGED, wf.message

    def test_given_finished_items_when_resumed_then_only_the_rest_run(self):
        wf = self.make(Status.PAUSED, tasks=[
            Task(1, "first", Status.DONE), Task(2, "second"),
        ])
        self.resume(wf)
        assert len(self.executed) == 1
        assert "Work on item 2" in self.executed[0]

    @pytest.mark.parametrize("status, task_status", [
        (Status.FAILED, Status.FAILED), (Status.CANCELLED, Status.PENDING),
    ])
    def test_given_a_stopped_run_when_resumed_then_it_finishes(self, status, task_status):
        wf = self.make(status, tasks=[Task(1, "first", task_status)])
        self.resume(wf)
        assert wf.status is Status.MERGED, wf.message

    def test_given_a_paused_run_when_resumed_then_no_rework_round_is_consumed(self):
        wf = self.make(Status.PAUSED, tasks=[Task(1, "first")])
        before = wf.round
        self.resume(wf)
        assert wf.round == before

    def test_given_a_run_still_working_when_resumed_then_nothing_new_starts(self):
        wf = self.make(tasks=[Task(1, "first")])
        self.release = threading.Event()
        self.runner.start(wf)
        assert self.started.wait(timeout=10)
        thread = self.runner._threads[wf.id]

        self.runner.resume(wf)
        assert self.runner._threads[wf.id] is thread
