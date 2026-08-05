"""Pausing a run, and picking it up again."""

import os
import tempfile
import threading
import unittest
from unittest import mock

from agent_orchestrator.backends import RunResult
from agent_orchestrator.gitops import GitError
from agent_orchestrator.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    Mode,
    Role,
    Status,
    Task,
    Workflow,
)
from agent_orchestrator.runner import Runner
from agent_orchestrator.store import Store


class PauseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("AIORCH_HOME")
        os.environ["AIORCH_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

        self.store = Store()
        self.runner = Runner(self.store)
        self.git = mock.patch("agent_orchestrator.runner.gitops").start()
        self.git.GitError = GitError
        self.git.diff.return_value = "diff"
        self.git.short_status.return_value = ""
        self.git.recent_commits.return_value = ""
        self.commits = 0
        self.git.commit_count.side_effect = lambda *a, **k: self.commits
        self.addCleanup(mock.patch.stopall)
        self.executed: list[str] = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AIORCH_HOME", None)
        else:
            os.environ["AIORCH_HOME"] = self._old

    def make(self, status=Status.PENDING, tasks=None):
        wf = Workflow(
            prompt="build the thing", repo_path=self.tmp.name, base_branch="main",
            agents={"plan": "claude", "execute": "opencode", "review": "claude"},
            mode=Mode.AUTONOMOUS, max_rounds=1, max_attempts=1,
        )
        wf.branch = "feature/thing"
        wf.branch_created = True
        wf.worktree = self.tmp.name
        wf.plan_text = "the plan"
        wf.status = status
        wf.tasks = tasks or []
        self.store.add(wf)
        return wf

    def agent(self):
        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            if role is Role.EXECUTE:
                self.executed.append(prompt)
                self.commits += 1
                return RunResult(0, "done")
            if role is Role.PLAN:
                return RunResult(0, "plan\n\nTASKS:\n1. one\n2. two\n")
            return RunResult(0, "VERDICT: APPROVE")
        return _run

    def drive(self, wf, cancel=None, pause=None):
        with mock.patch("agent_orchestrator.runner.backends.run",
                        side_effect=self.agent()):
            self.runner._run(wf, cancel or threading.Event(), pause)


class Pausing(PauseTestCase):
    def test_a_pause_stops_the_run_before_the_next_step(self):
        wf = self.make(tasks=[Task(1, "first")])
        paused = threading.Event()
        paused.set()

        self.drive(wf, pause=paused)

        self.assertIs(wf.status, Status.PAUSED)
        self.assertEqual(self.executed, [])

    def test_the_message_says_how_to_carry_on(self):
        wf = self.make()
        paused = threading.Event()
        paused.set()
        self.drive(wf, pause=paused)
        self.assertIn("/resume", wf.message)

    def test_pausing_an_idle_run_marks_it_immediately(self):
        wf = self.make(Status.FAILED)
        self.runner.pause(wf)
        self.assertIs(wf.status, Status.PAUSED)

    def test_a_merged_run_cannot_be_paused(self):
        wf = self.make(Status.MERGED)
        self.runner.pause(wf)
        self.assertIs(wf.status, Status.MERGED)

    def test_paused_is_neither_running_nor_finished(self):
        """It must survive a restart and not be swept up by /clear finished."""
        self.assertNotIn(Status.PAUSED, ACTIVE_STATUSES)
        self.assertNotIn(Status.PAUSED, TERMINAL_STATUSES)

    def test_an_orphan_sweep_leaves_a_paused_run_alone(self):
        wf = self.make(Status.PAUSED)
        self.assertEqual(Runner(self.store).adopt_orphans(), 0)
        self.assertIs(wf.status, Status.PAUSED)

    def test_a_paused_run_survives_a_reload(self):
        wf = self.make(Status.PAUSED)
        self.assertIs(Store().get(wf.id).status, Status.PAUSED)

    def test_pause_is_not_cancel(self):
        """Cancelling ends a run; pausing leaves it to be continued."""
        wf = self.make()
        cancelled = threading.Event()
        cancelled.set()
        self.drive(wf, cancel=cancelled)
        self.assertIs(wf.status, Status.CANCELLED)


class ResumingAfterPause(PauseTestCase):
    def resume(self, wf):
        with mock.patch("agent_orchestrator.runner.backends.run",
                        side_effect=self.agent()):
            self.runner.resume(wf)
            thread = self.runner._threads.get(wf.id)
            if thread:
                thread.join(timeout=10)

    def test_a_paused_run_carries_on(self):
        wf = self.make(tasks=[Task(1, "first"), Task(2, "second")])
        self.runner.start(wf)
        self.runner._threads[wf.id].join(timeout=10)
        self.runner.pause(wf)
        self.assertIs(wf.status, Status.PAUSED)

        self.resume(wf)
        self.assertIs(wf.status, Status.MERGED, msg=wf.message)

    def test_the_pause_flag_is_cleared_on_resume(self):
        """Otherwise the resumed thread stops again at the first boundary."""
        wf = self.make(tasks=[Task(1, "first")])
        self.runner.start(wf)
        self.runner._threads[wf.id].join(timeout=10)
        self.runner.pause(wf)

        self.resume(wf)
        self.assertIsNot(wf.status, Status.PAUSED)

    def test_finished_items_are_kept_when_resuming(self):
        wf = self.make(Status.PAUSED, tasks=[
            Task(1, "first", Status.DONE), Task(2, "second"),
        ])
        self.resume(wf)
        self.assertEqual(len(self.executed), 1)
        self.assertIn("Work on item 2", self.executed[0])

    def test_a_failed_run_can_also_be_resumed(self):
        wf = self.make(Status.FAILED, tasks=[Task(1, "first", Status.FAILED)])
        self.resume(wf)
        self.assertIs(wf.status, Status.MERGED, msg=wf.message)

    def test_a_cancelled_run_can_also_be_resumed(self):
        wf = self.make(Status.CANCELLED, tasks=[Task(1, "first")])
        self.resume(wf)
        self.assertIs(wf.status, Status.MERGED, msg=wf.message)

    def test_resuming_does_not_consume_a_rework_round(self):
        wf = self.make(Status.PAUSED, tasks=[Task(1, "first")])
        before = wf.round
        self.resume(wf)
        self.assertEqual(wf.round, before)


if __name__ == "__main__":
    unittest.main()
