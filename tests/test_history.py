"""Resuming a stopped run (the history commands live in test_shell)."""

import os
import tempfile
import threading
import unittest
from unittest import mock

from agent_orchestrator.backends import RunResult
from agent_orchestrator.gitops import GitError
from agent_orchestrator.models import Mode, Role, Status, Task, Workflow
from agent_orchestrator.runner import Runner
from agent_orchestrator.store import Store


class Resuming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("AIORCH_HOME")
        os.environ["AIORCH_HOME"] = self.tmp.name
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
        self.addCleanup(self.tmp.cleanup)
        self.execute_prompts: list[str] = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AIORCH_HOME", None)
        else:
            os.environ["AIORCH_HOME"] = self._old

    def make(self, status, tasks=None):
        wf = Workflow(
            prompt="build the thing", repo_path=self.tmp.name, base_branch="main",
            agents={"plan": "claude", "execute": "opencode", "review": "claude"},
            mode=Mode.AUTONOMOUS, max_rounds=2, max_attempts=1,
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
        def _run(name, role, prompt, cwd, log_file, backends=None):
            if role is Role.EXECUTE:
                self.execute_prompts.append(prompt)
                self.commits += 1
                return RunResult(0, "done")
            if role is Role.PLAN:
                return RunResult(0, "the plan\n\nTASKS:\n1. one\n2. two\n")
            return RunResult(0, "VERDICT: APPROVE")
        return _run

    def run_resumed(self, wf):
        with mock.patch("agent_orchestrator.runner.backends.run", side_effect=self.agent()):
            self.runner.resume(wf)
            thread = self.runner._threads.get(wf.id)
            if thread:
                thread.join(timeout=10)

    def test_finished_items_are_kept_and_only_the_rest_run(self):
        wf = self.make(Status.FAILED, tasks=[
            Task(1, "first", Status.DONE),
            Task(2, "second", Status.FAILED, attempts=1),
            Task(3, "third", Status.PENDING),
        ])
        self.run_resumed(wf)

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertEqual(len(self.execute_prompts), 2)  # not the finished one
        self.assertIn("Work on item 2", self.execute_prompts[0])
        self.assertIn("Work on item 3", self.execute_prompts[1])

    def test_a_stalled_item_is_queued_again_with_its_attempts_reset(self):
        stalled = Task(1, "first", Status.FAILED, attempts=3)
        wf = self.make(Status.FAILED, tasks=[stalled])
        self.run_resumed(wf)
        self.assertIs(stalled.status, Status.DONE)
        self.assertEqual(stalled.attempts, 1)

    def test_resuming_does_not_consume_a_rework_round(self):
        wf = self.make(Status.FAILED, tasks=[Task(1, "first")])
        before = wf.round
        self.run_resumed(wf)
        self.assertEqual(wf.round, before)

    def test_a_retry_does_consume_a_round(self):
        wf = self.make(Status.CHANGES_REQUESTED, tasks=[Task(1, "first", Status.DONE)])
        before = wf.round
        with mock.patch("agent_orchestrator.runner.backends.run", side_effect=self.agent()):
            self.runner.retry(wf)
            self.runner._threads[wf.id].join(timeout=10)
        self.assertEqual(wf.round, before + 1)

    def test_an_interrupted_run_can_be_resumed(self):
        """adopt_orphans marks these FAILED; resume is the cure."""
        wf = self.make(Status.EXECUTING, tasks=[Task(1, "first")])
        Runner(self.store).adopt_orphans()
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("interrupted", wf.message)

        self.run_resumed(wf)
        self.assertIs(wf.status, Status.MERGED, msg=wf.message)

    def test_a_merged_run_is_not_resumable(self):
        wf = self.make(Status.MERGED)
        self.run_resumed(wf)
        self.assertEqual(self.execute_prompts, [])

    def test_the_stale_failure_message_is_cleared(self):
        wf = self.make(Status.FAILED, tasks=[Task(1, "first")])
        wf.message = "something went wrong earlier"
        self.run_resumed(wf)
        self.assertNotIn("went wrong earlier", wf.message)


if __name__ == "__main__":
    unittest.main()
