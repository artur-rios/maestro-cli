"""Resuming a stopped run (the history commands live in test_shell)."""

from unittest import mock

import pytest

from maestro.backends import RunResult
from maestro.gitops import GitError
from maestro.models import Mode, Role, Status, Task, Workflow
from maestro.runner import Runner
from maestro.store import Store


class TestResuming:
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
        self.execute_prompts: list[str] = []

    def make(self, status, tasks=None):
        wf = Workflow(
            prompt="build the thing", repo_path=str(self.tmp), base_branch="main",
            agents={"plan": "claude", "execute": "opencode", "review": "claude"},
            mode=Mode.AUTONOMOUS, max_rounds=2, max_attempts=1,
        )
        wf.branch = "feature/thing"
        wf.branch_created = True
        wf.worktree = str(self.tmp)
        wf.plan_text = "the plan"
        wf.status = status
        wf.tasks = tasks or []
        self.store.add(wf)
        return wf

    def agent(self):
        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            if role is Role.EXECUTE:
                self.execute_prompts.append(prompt)
                self.commits += 1
                return RunResult(0, "done")
            if role is Role.PLAN:
                return RunResult(0, "the plan\n\nTASKS:\n1. one\n2. two\n")
            return RunResult(0, "VERDICT: APPROVE")
        return _run

    def run_resumed(self, wf):
        with mock.patch("maestro.runner.backends.run", side_effect=self.agent()):
            self.runner.resume(wf)
            thread = self.runner._threads.get(wf.id)
            if thread:
                thread.join(timeout=10)
                assert not thread.is_alive()

    def test_given_a_failed_checklist_when_resumed_then_finished_items_are_kept_and_only_the_rest_run(self):
        wf = self.make(Status.FAILED, tasks=[
            Task(1, "first", Status.DONE),
            Task(2, "second", Status.FAILED, attempts=1),
            Task(3, "third", Status.PENDING),
        ])
        self.run_resumed(wf)

        assert wf.status is Status.MERGED, wf.message
        assert len(self.execute_prompts) == 2  # not the finished one
        assert "Work on item 2" in self.execute_prompts[0]
        assert "Work on item 3" in self.execute_prompts[1]

    def test_given_a_stalled_item_when_resumed_then_it_is_queued_again_with_its_attempts_reset(self):
        stalled = Task(1, "first", Status.FAILED, attempts=3)
        wf = self.make(Status.FAILED, tasks=[stalled])
        self.run_resumed(wf)
        assert stalled.status is Status.DONE
        assert stalled.attempts == 1

    def test_given_a_failed_run_when_resumed_then_no_rework_round_is_consumed(self):
        wf = self.make(Status.FAILED, tasks=[Task(1, "first")])
        before = wf.round
        self.run_resumed(wf)
        assert wf.round == before

    def test_given_requested_changes_when_retried_then_a_round_is_consumed(self):
        wf = self.make(Status.CHANGES_REQUESTED, tasks=[Task(1, "first", Status.DONE)])
        before = wf.round
        with mock.patch("maestro.runner.backends.run", side_effect=self.agent()):
            self.runner.retry(wf)
            self.runner._threads[wf.id].join(timeout=10)
        assert wf.round == before + 1

    def test_given_a_run_interrupted_by_an_exit_when_resumed_then_it_finishes(self):
        """adopt_orphans marks these FAILED; resume is the cure."""
        wf = self.make(Status.EXECUTING, tasks=[Task(1, "first")])
        Runner(self.store).adopt_orphans()
        assert wf.status is Status.FAILED
        assert "interrupted" in wf.message

        self.run_resumed(wf)
        assert wf.status is Status.MERGED, wf.message

    def test_given_a_merged_run_when_resumed_then_nothing_runs(self):
        wf = self.make(Status.MERGED)
        self.run_resumed(wf)
        assert self.execute_prompts == []

    def test_given_a_stale_failure_message_when_resumed_then_it_is_cleared(self):
        wf = self.make(Status.FAILED, tasks=[Task(1, "first")])
        wf.message = "something went wrong earlier"
        self.run_resumed(wf)
        assert "went wrong earlier" not in wf.message
