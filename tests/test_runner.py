"""State machine tests.

Both git and the agent CLIs are faked, so this suite runs with no repository
and no AI tool installed.
"""

import os
import tempfile
import threading
import unittest
from unittest import mock

from agent_orchestrator.backends import BackendError, RunResult
from agent_orchestrator.gitops import GitError, MergeConflict
from agent_orchestrator.models import Mode, Role, Status, Verdict, Workflow
from agent_orchestrator.runner import Runner
from agent_orchestrator.store import Store

APPROVE = "Looks correct.\nVERDICT: APPROVE"
REJECT = "Tests are missing.\nVERDICT: REQUEST_CHANGES"


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("AIORCH_HOME")
        os.environ["AIORCH_HOME"] = self.tmp.name

        self.store = Store()
        self.runner = Runner(self.store)

        self.git = mock.patch("agent_orchestrator.runner.gitops").start()
        self.git.commit_all.return_value = True
        self.git.diff.return_value = "diff --git a/x b/x"
        self.git.short_status.return_value = ""
        self.git.recent_commits.return_value = ""
        self.git.GitError = GitError
        self.addCleanup(mock.patch.stopall)

        # Progress is measured in commits, so the fake repo counts them and the
        # fake agent adds one whenever it is supposed to have done the work.
        self.commits = 0
        self.agent_commits = True
        self.git.commit_count.side_effect = lambda *a, **k: self.commits
        self.git.has_new_commits.side_effect = lambda *a, **k: self.commits > 0

        self.calls: list[tuple[Role, str]] = []

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("AIORCH_HOME", None)
        else:
            os.environ["AIORCH_HOME"] = self._old_home
        self.tmp.cleanup()

    def make(self, mode=Mode.SUPERVISED, max_rounds=2, max_attempts=1):
        wf = Workflow(
            prompt="add a health check",
            repo_path=self.tmp.name,
            base_branch="main",
            agents={"plan": "claude", "execute": "codex", "review": "claude"},
            mode=mode,
            max_rounds=max_rounds,
            max_attempts=max_attempts,
        )
        wf.branch = "feature/add-health-check"
        wf.branch_created = True  # already provisioned; skip workspace setup
        wf.worktree = self.tmp.name
        self.store.add(wf)
        return wf

    def fake_agent(self, *, review_outputs=None, plan="THE PLAN", execute="done"):
        """Return a stand-in for backends.run driven by role."""
        reviews = list(review_outputs or [APPROVE])

        def _run(name, role, prompt, cwd, log_file, backends=None):
            self.calls.append((role, prompt))
            if role is Role.PLAN:
                return RunResult(0, plan)
            if role is Role.EXECUTE:
                if self.agent_commits:
                    self.commits += 1
                return RunResult(0, execute)
            return RunResult(0, reviews.pop(0) if reviews else APPROVE)

        return _run

    def drive(self, wf, runner_fn):
        with mock.patch("agent_orchestrator.runner.backends.run", side_effect=runner_fn):
            self.runner._run(wf, threading.Event())


class HappyPaths(RunnerTestCase):
    def test_supervised_run_stops_for_approval(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())

        self.assertIs(wf.status, Status.AWAITING_APPROVAL)
        self.assertIs(wf.verdict, Verdict.APPROVE)
        self.git.merge.assert_not_called()

    def test_autonomous_run_merges_without_asking(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertIs(wf.status, Status.MERGED)
        self.git.merge.assert_called_once()

    def test_steps_are_recorded_in_order(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertEqual(
            [s.role for s in wf.steps], [Role.PLAN, Role.EXECUTE, Role.REVIEW]
        )
        self.assertTrue(all(s.status is Status.DONE for s in wf.steps))
        self.assertTrue(all(s.log_path for s in wf.steps))

    def test_the_plan_is_passed_to_the_execute_agent(self):
        wf = self.make()
        self.drive(wf, self.fake_agent(plan="STEP ONE: do the thing"))
        execute_prompt = next(p for role, p in self.calls if role is Role.EXECUTE)
        self.assertIn("STEP ONE: do the thing", execute_prompt)

    def test_the_diff_is_passed_to_the_review_agent(self):
        wf = self.make()
        self.git.diff.return_value = "diff --git a/health.py b/health.py"
        self.drive(wf, self.fake_agent())
        review_prompt = next(p for role, p in self.calls if role is Role.REVIEW)
        self.assertIn("health.py", review_prompt)


class ReworkLoop(RunnerTestCase):
    def test_rejection_feeds_review_back_into_a_second_execute(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=2)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT, APPROVE]))

        execute_prompts = [p for role, p in self.calls if role is Role.EXECUTE]
        self.assertEqual(len(execute_prompts), 2)
        self.assertIn("Tests are missing", execute_prompts[1])
        self.assertIs(wf.status, Status.MERGED)
        self.assertEqual(wf.round, 2)

    def test_planning_happens_only_once_across_rounds(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=3)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT, REJECT, APPROVE]))
        self.assertEqual(len([r for r, _ in self.calls if r is Role.PLAN]), 1)

    def test_exhausting_the_rounds_stops_short_of_merging(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=2)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT, REJECT]))

        self.assertIs(wf.status, Status.CHANGES_REQUESTED)
        self.git.merge.assert_not_called()
        self.assertIn("after 2 round", wf.message)

    def test_a_single_round_means_no_rework(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=1)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT]))

        self.assertIs(wf.status, Status.CHANGES_REQUESTED)
        self.assertEqual(len([r for r, _ in self.calls if r is Role.EXECUTE]), 1)

    def test_unparseable_review_is_treated_as_a_rejection(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=1)
        self.drive(wf, self.fake_agent(review_outputs=["I think it's fine"]))

        self.assertIs(wf.status, Status.CHANGES_REQUESTED)
        self.git.merge.assert_not_called()


class Failures(RunnerTestCase):
    def test_nonzero_exit_fails_the_run(self):
        wf = self.make()

        def failing(name, role, prompt, cwd, log_file, backends=None):
            return RunResult(2, "boom")

        self.drive(wf, failing)
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("exited with code 2", wf.message)
        self.assertIs(wf.steps[0].status, Status.FAILED)

    def test_timeout_fails_the_run(self):
        wf = self.make()

        def slow(name, role, prompt, cwd, log_file, backends=None):
            return RunResult(-1, "", timed_out=True)

        self.drive(wf, slow)
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("timed out", wf.message)

    def test_missing_cli_fails_the_run_with_a_readable_message(self):
        wf = self.make()

        def missing(name, role, prompt, cwd, log_file, backends=None):
            raise BackendError("'codex' is not on PATH")

        self.drive(wf, missing)
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("not on PATH", wf.message)

    def test_empty_plan_fails_the_run(self):
        wf = self.make()
        self.drive(wf, self.fake_agent(plan="   "))
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("no output", wf.message)

    def test_no_commits_explains_itself_and_quotes_the_agent(self):
        wf = self.make(max_attempts=1)
        self.agent_commits = False
        self.drive(wf, self.fake_agent(execute="I looked around but changed nothing"))
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("committed nothing", wf.message)
        self.assertIn("codex", wf.message)  # which agent
        self.assertIn("I looked around", wf.message)  # what it said

    def test_merge_conflict_fails_the_run(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.git.merge.side_effect = MergeConflict("conflicts and was aborted")
        self.drive(wf, self.fake_agent())
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("aborted", wf.message)

    def test_an_unexpected_error_is_reported_not_swallowed(self):
        wf = self.make()
        self.git.diff.side_effect = ValueError("kaboom")
        self.drive(wf, self.fake_agent())
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("kaboom", wf.message)


class Control(RunnerTestCase):
    def test_cancellation_stops_before_the_next_step(self):
        wf = self.make()
        cancel = threading.Event()
        cancel.set()
        with mock.patch(
            "agent_orchestrator.runner.backends.run", side_effect=self.fake_agent()
        ):
            self.runner._run(wf, cancel)
        self.assertIs(wf.status, Status.CANCELLED)
        self.assertEqual(self.calls, [])

    def test_approve_merges_a_supervised_run(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())
        self.assertIs(wf.status, Status.AWAITING_APPROVAL)

        self.runner.approve(wf)
        self.runner._threads[wf.id].join(timeout=5)
        self.assertIs(wf.status, Status.MERGED)
        self.git.merge.assert_called_once()

    def test_approve_is_ignored_unless_awaiting_approval(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())
        self.git.merge.reset_mock()
        self.runner.approve(wf)
        self.git.merge.assert_not_called()

    def test_reject_leaves_the_branch_unmerged(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())
        self.runner.reject(wf, "not what I asked for")
        self.assertIs(wf.status, Status.CHANGES_REQUESTED)
        self.assertIn("not what I asked for", wf.message)
        self.git.merge.assert_not_called()

    def test_retry_reuses_the_plan_and_bumps_the_round(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=1)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT]))
        self.assertIs(wf.status, Status.CHANGES_REQUESTED)

        wf.max_rounds = 3
        with mock.patch(
            "agent_orchestrator.runner.backends.run", side_effect=self.fake_agent()
        ):
            self.runner.retry(wf)
            self.runner._threads[wf.id].join(timeout=5)

        self.assertIs(wf.status, Status.MERGED)
        self.assertEqual(wf.round, 2)
        self.assertEqual(len([r for r, _ in self.calls if r is Role.PLAN]), 1)

    def test_adopt_orphans_fails_runs_left_by_a_dead_process(self):
        wf = self.make()
        wf.status = Status.EXECUTING
        step = wf.add_step(Role.EXECUTE)
        step.status = Status.RUNNING
        self.store.add(wf)

        adopted = Runner(Store()).adopt_orphans()

        self.assertEqual(adopted, 1)
        reloaded = Store().get(wf.id)
        self.assertIs(reloaded.status, Status.FAILED)
        self.assertIn("interrupted", reloaded.message)
        self.assertIs(reloaded.steps[0].status, Status.FAILED)

    def test_adopt_orphans_leaves_finished_runs_alone(self):
        wf = self.make()
        wf.status = Status.MERGED
        self.store.add(wf)
        self.assertEqual(Runner(Store()).adopt_orphans(), 0)


class BranchSetup(RunnerTestCase):
    def fresh(self, branch=""):
        wf = self.make(mode=Mode.AUTONOMOUS)
        wf.branch = branch
        wf.branch_created = False
        wf.worktree = ""
        self.git.repo_root.return_value = self.tmp.name
        self.git.suggest_branch.return_value = "feature/add-health-check"
        self.git.unique_branch.side_effect = lambda repo, name: name
        self.git.create_worktree.return_value = self.tmp.name
        return wf

    def test_a_fresh_workflow_gets_a_branch_and_worktree(self):
        wf = self.fresh()
        self.drive(wf, self.fake_agent())

        self.assertEqual(wf.branch, "feature/add-health-check")
        self.assertTrue(wf.branch_created)
        self.git.create_worktree.assert_called_once()

    def test_a_branch_chosen_at_creation_is_respected(self):
        wf = self.fresh(branch="fix/broken-login")
        self.drive(wf, self.fake_agent())

        self.assertEqual(wf.branch, "fix/broken-login")
        self.git.suggest_branch.assert_not_called()

    def test_a_colliding_name_is_made_unique_for_a_new_run(self):
        wf = self.fresh(branch="feature/same-idea")
        self.git.unique_branch.side_effect = lambda repo, name: name + "-2"

        self.drive(wf, self.fake_agent())

        self.assertEqual(wf.branch, "feature/same-idea-2")

    def test_a_retry_reuses_its_branch_instead_of_making_a_new_one(self):
        wf = self.make(mode=Mode.AUTONOMOUS)  # branch_created=True
        original = wf.branch
        self.drive(wf, self.fake_agent())

        self.assertEqual(wf.branch, original)
        self.git.unique_branch.assert_not_called()
        self.git.create_worktree.assert_not_called()

    def test_a_git_error_during_setup_fails_the_run(self):
        wf = self.make()
        wf.branch = ""
        self.git.repo_root.side_effect = GitError("not a repository")
        self.drive(wf, self.fake_agent())
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("not a repository", wf.message)


if __name__ == "__main__":
    unittest.main()
