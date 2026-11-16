"""State machine tests.

Both git and the agent CLIs are faked, so this suite runs with no repository
and no AI tool installed.
"""

import threading
from unittest import mock

import pytest

from maestro.backends import BackendError, RunResult
from maestro.gitops import GitError, MergeConflict
from maestro.models import Mode, Role, Status, Verdict, Workflow
from maestro.runner import Runner, _last_words, _shorten, parse_tasks, plural
from maestro.store import Store

APPROVE = "Looks correct.\nVERDICT: APPROVE"
REJECT = "Tests are missing.\nVERDICT: REQUEST_CHANGES"


class RunnerBase:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.store = Store()
        self.runner = Runner(self.store)

        self.git = mock.MagicMock()
        monkeypatch.setattr("maestro.runner.gitops", self.git)
        self.git.commit_all.return_value = True
        self.git.diff.return_value = "diff --git a/x b/x"
        self.git.short_status.return_value = ""
        self.git.recent_commits.return_value = ""
        self.git.GitError = GitError

        # Progress is measured in commits, so the fake repo counts them and the
        # fake agent adds one whenever it is supposed to have done the work.
        self.commits = 0
        self.agent_commits = True
        self.git.commit_count.side_effect = lambda *a, **k: self.commits
        self.git.has_new_commits.side_effect = lambda *a, **k: self.commits > 0

        self.calls: list[tuple[Role, str]] = []
        yield
        for thread in list(self.runner._threads.values()):
            thread.join(timeout=10)

    def make(self, mode=Mode.SUPERVISED, max_rounds=2, max_attempts=1):
        wf = Workflow(
            prompt="add a health check",
            repo_path=str(self.tmp),
            base_branch="main",
            agents={"plan": "claude", "execute": "codex", "review": "claude"},
            mode=mode,
            max_rounds=max_rounds,
            max_attempts=max_attempts,
        )
        wf.branch = "feature/add-health-check"
        wf.branch_created = True  # already provisioned; skip workspace setup
        wf.worktree = str(self.tmp)
        self.store.add(wf)
        return wf

    def fake_agent(self, *, review_outputs=None, plan="THE PLAN", execute="done"):
        """Return a stand-in for backends.run driven by role."""
        reviews = list(review_outputs or [APPROVE])

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
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
        with mock.patch("maestro.runner.backends.run", side_effect=runner_fn):
            self.runner._run(wf, threading.Event())


class TestHappyPaths(RunnerBase):
    def test_given_a_supervised_run_when_the_review_approves_then_it_waits_for_approval(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())

        assert wf.status is Status.AWAITING_APPROVAL
        assert wf.verdict is Verdict.APPROVE
        self.git.merge.assert_not_called()

    def test_given_an_autonomous_run_when_the_review_approves_then_it_merges_without_asking(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert wf.status is Status.MERGED
        self.git.merge.assert_called_once()

    def test_given_a_full_run_when_it_finishes_then_its_steps_are_recorded_in_order(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert [s.role for s in wf.steps] == [Role.PLAN, Role.EXECUTE, Role.REVIEW]
        assert all(s.status is Status.DONE for s in wf.steps)
        assert all(s.log_path for s in wf.steps)

    def test_given_a_plan_when_executing_then_the_plan_is_in_the_execute_prompt(self):
        wf = self.make()
        self.drive(wf, self.fake_agent(plan="STEP ONE: do the thing"))
        execute_prompt = next(p for role, p in self.calls if role is Role.EXECUTE)
        assert "STEP ONE: do the thing" in execute_prompt

    def test_given_a_model_per_role_when_run_then_each_step_gets_its_own(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        wf.models = {"plan": "opus", "execute": "glm-5.2", "review": "sonnet"}
        seen = {}

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            seen[role.value] = model
            if role is Role.EXECUTE:
                self.commits += 1
                return RunResult(0, "done")
            return RunResult(0, "x" if role is Role.PLAN else APPROVE)

        self.drive(wf, _run)
        assert seen == {"plan": "opus", "execute": "glm-5.2",
                                "review": "sonnet"}

    def test_given_a_role_without_a_model_when_run_then_it_gets_an_empty_one(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        wf.models = {"execute": "glm-5.2"}
        seen = {}

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            seen[role.value] = model
            if role is Role.EXECUTE:
                self.commits += 1
                return RunResult(0, "done")
            return RunResult(0, "x" if role is Role.PLAN else APPROVE)

        self.drive(wf, _run)
        assert seen["plan"] == ""
        assert seen["execute"] == "glm-5.2"

    def test_given_a_diff_when_reviewing_then_it_is_in_the_review_prompt(self):
        wf = self.make()
        self.git.diff.return_value = "diff --git a/health.py b/health.py"
        self.drive(wf, self.fake_agent())
        review_prompt = next(p for role, p in self.calls if role is Role.REVIEW)
        assert "health.py" in review_prompt


class TestReworkLoop(RunnerBase):
    def test_given_a_rejection_when_rounds_remain_then_the_review_feeds_a_second_execute(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=2)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT, APPROVE]))

        execute_prompts = [p for role, p in self.calls if role is Role.EXECUTE]
        assert len(execute_prompts) == 2
        assert "Tests are missing" in execute_prompts[1]
        assert wf.status is Status.MERGED
        assert wf.round == 2

    def test_given_several_rounds_when_run_then_planning_happens_only_once(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=3)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT, REJECT, APPROVE]))
        assert len([r for r, _ in self.calls if r is Role.PLAN]) == 1

    def test_given_every_round_rejected_when_they_run_out_then_nothing_is_merged(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=2)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT, REJECT]))

        assert wf.status is Status.CHANGES_REQUESTED
        self.git.merge.assert_not_called()
        assert "after 2 round" in wf.message

    def test_given_a_single_round_when_rejected_then_there_is_no_rework(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=1)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT]))

        assert wf.status is Status.CHANGES_REQUESTED
        assert len([r for r, _ in self.calls if r is Role.EXECUTE]) == 1

    def test_given_an_unparseable_review_when_read_then_it_counts_as_a_rejection(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=1)
        self.drive(wf, self.fake_agent(review_outputs=["I think it's fine"]))

        assert wf.status is Status.CHANGES_REQUESTED
        self.git.merge.assert_not_called()


class TestFailures(RunnerBase):
    def test_given_a_nonzero_exit_when_planning_then_the_run_fails(self):
        wf = self.make()

        def failing(name, role, prompt, cwd, log_file, backends=None, model=""):
            return RunResult(2, "boom")

        self.drive(wf, failing)
        assert wf.status is Status.FAILED
        assert "exited with code 2" in wf.message
        assert wf.steps[0].status is Status.FAILED

    def test_given_a_timeout_when_planning_then_the_run_fails(self):
        wf = self.make()

        def slow(name, role, prompt, cwd, log_file, backends=None, model=""):
            return RunResult(-1, "", timed_out=True)

        self.drive(wf, slow)
        assert wf.status is Status.FAILED
        assert "timed out" in wf.message

    def test_given_a_missing_cli_when_run_then_it_fails_with_a_readable_message(self):
        wf = self.make()

        def missing(name, role, prompt, cwd, log_file, backends=None, model=""):
            raise BackendError("'codex' is not on PATH")

        self.drive(wf, missing)
        assert wf.status is Status.FAILED
        assert "not on PATH" in wf.message

    def test_given_an_empty_plan_when_planning_then_the_run_fails(self):
        wf = self.make()
        self.drive(wf, self.fake_agent(plan="   "))
        assert wf.status is Status.FAILED
        assert "no output" in wf.message

    def test_given_no_commits_when_executing_then_the_failure_explains_itself_and_quotes_the_agent(self):
        wf = self.make(max_attempts=1)
        self.agent_commits = False
        self.drive(wf, self.fake_agent(execute="I looked around but changed nothing"))
        assert wf.status is Status.FAILED
        assert "committed nothing" in wf.message
        assert "codex" in wf.message  # which agent
        assert "I looked around" in wf.message  # what it said

    def test_given_a_merge_conflict_when_merging_then_the_run_fails(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.git.merge.side_effect = MergeConflict("conflicts and was aborted")
        self.drive(wf, self.fake_agent())
        assert wf.status is Status.FAILED
        assert "aborted" in wf.message

    def test_given_an_unexpected_error_when_run_then_it_is_reported_not_swallowed(self):
        wf = self.make()
        self.git.diff.side_effect = ValueError("kaboom")
        self.drive(wf, self.fake_agent())
        assert wf.status is Status.FAILED
        assert "kaboom" in wf.message


class TestControl(RunnerBase):
    def test_given_a_set_cancel_flag_when_run_then_it_stops_before_the_next_step(self):
        wf = self.make()
        cancel = threading.Event()
        cancel.set()
        with mock.patch(
            "maestro.runner.backends.run", side_effect=self.fake_agent()
        ):
            self.runner._run(wf, cancel)
        assert wf.status is Status.CANCELLED
        assert self.calls == []

    def test_given_a_run_awaiting_approval_when_approved_then_it_merges(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())
        assert wf.status is Status.AWAITING_APPROVAL

        self.runner.approve(wf)
        self.runner._threads[wf.id].join(timeout=5)
        assert wf.status is Status.MERGED
        self.git.merge.assert_called_once()

    def test_given_a_run_not_awaiting_approval_when_approved_then_nothing_happens(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())
        self.git.merge.reset_mock()
        self.runner.approve(wf)
        self.git.merge.assert_not_called()

    def test_given_a_run_awaiting_approval_when_rejected_then_the_branch_is_left_unmerged(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())
        self.runner.reject(wf, "not what I asked for")
        assert wf.status is Status.CHANGES_REQUESTED
        assert "not what I asked for" in wf.message
        self.git.merge.assert_not_called()

    def test_given_requested_changes_when_retried_then_the_plan_is_reused_and_the_round_bumped(self):
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=1)
        self.drive(wf, self.fake_agent(review_outputs=[REJECT]))
        assert wf.status is Status.CHANGES_REQUESTED

        wf.max_rounds = 3
        with mock.patch(
            "maestro.runner.backends.run", side_effect=self.fake_agent()
        ):
            self.runner.retry(wf)
            self.runner._threads[wf.id].join(timeout=5)

        assert wf.status is Status.MERGED
        assert wf.round == 2
        assert len([r for r, _ in self.calls if r is Role.PLAN]) == 1

    def test_given_runs_left_by_a_dead_process_when_orphans_are_adopted_then_they_fail(self):
        wf = self.make()
        wf.status = Status.EXECUTING
        step = wf.add_step(Role.EXECUTE)
        step.status = Status.RUNNING
        self.store.add(wf)

        adopted = Runner(Store()).adopt_orphans()

        assert adopted == 1
        reloaded = Store().get(wf.id)
        assert reloaded.status is Status.FAILED
        assert "interrupted" in reloaded.message
        assert reloaded.steps[0].status is Status.FAILED

    def test_given_finished_runs_when_orphans_are_adopted_then_they_are_left_alone(self):
        wf = self.make()
        wf.status = Status.MERGED
        self.store.add(wf)
        assert Runner(Store()).adopt_orphans() == 0


class TestBranchSetup(RunnerBase):
    def fresh(self, branch=""):
        wf = self.make(mode=Mode.AUTONOMOUS)
        wf.branch = branch
        wf.branch_created = False
        wf.worktree = ""
        self.git.repo_root.return_value = str(self.tmp)
        self.git.suggest_branch.return_value = "feature/add-health-check"
        self.git.unique_branch.side_effect = lambda repo, name: name
        self.git.create_worktree.return_value = str(self.tmp)
        return wf

    def test_given_a_fresh_workflow_when_run_then_it_gets_a_branch_and_worktree(self):
        wf = self.fresh()
        self.drive(wf, self.fake_agent())

        assert wf.branch == "feature/add-health-check"
        assert wf.branch_created
        self.git.create_worktree.assert_called_once()

    def test_given_a_branch_chosen_at_creation_when_run_then_it_is_respected(self):
        wf = self.fresh(branch="fix/broken-login")
        self.drive(wf, self.fake_agent())

        assert wf.branch == "fix/broken-login"
        self.git.suggest_branch.assert_not_called()

    def test_given_a_colliding_branch_name_when_a_new_run_starts_then_it_is_made_unique(self):
        wf = self.fresh(branch="feature/same-idea")
        self.git.unique_branch.side_effect = lambda repo, name: name + "-2"

        self.drive(wf, self.fake_agent())

        assert wf.branch == "feature/same-idea-2"

    def test_given_a_retry_when_run_then_its_branch_is_reused_not_made_again(self):
        wf = self.make(mode=Mode.AUTONOMOUS)  # branch_created=True
        original = wf.branch
        self.drive(wf, self.fake_agent())

        assert wf.branch == original
        self.git.unique_branch.assert_not_called()
        self.git.create_worktree.assert_not_called()

    def test_given_a_git_error_during_setup_when_run_then_the_run_fails(self):
        wf = self.make()
        wf.branch = ""
        self.git.repo_root.side_effect = GitError("not a repository")
        self.drive(wf, self.fake_agent())
        assert wf.status is Status.FAILED
        assert "not a repository" in wf.message


class TestLifecycleEdges(RunnerBase):
    """Asking the runner for something it should refuse or ignore."""

    def blocking_agent(self, release):
        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            assert release.wait(timeout=10)
            self.commits += 1
            return RunResult(0, "THE PLAN" if role is Role.PLAN else APPROVE)
        return _run

    def test_given_a_run_already_going_when_started_or_retried_then_no_second_thread_appears(self, monkeypatch):
        wf = self.make()
        release = threading.Event()
        monkeypatch.setattr("maestro.runner.backends.run", self.blocking_agent(release))
        self.runner.start(wf)
        thread = self.runner._threads[wf.id]
        before = wf.round

        self.runner.start(wf)
        self.runner.retry(wf)

        assert self.runner._threads[wf.id] is thread
        assert wf.round == before
        release.set()
        thread.join(timeout=10)

    def test_given_a_run_at_work_when_cancelled_then_it_stops_at_the_next_boundary(self, monkeypatch):
        wf = self.make(mode=Mode.AUTONOMOUS)
        release = threading.Event()
        monkeypatch.setattr("maestro.runner.backends.run", self.blocking_agent(release))
        self.runner.start(wf)
        self.runner.cancel(wf)
        assert wf.status is not Status.CANCELLED  # still in flight
        release.set()
        self.runner._threads[wf.id].join(timeout=10)
        assert wf.status is Status.CANCELLED
        self.git.merge.assert_not_called()

    def test_given_an_idle_run_when_cancelled_then_it_is_marked_at_once(self):
        wf = self.make()
        wf.status = Status.FAILED
        self.runner.cancel(wf)
        assert wf.status is Status.CANCELLED
        assert Store().get(wf.id).status is Status.CANCELLED

    def test_given_a_run_not_awaiting_approval_when_rejected_then_nothing_changes(self):
        wf = self.make()
        wf.status = Status.EXECUTING
        self.runner.reject(wf, "no")
        assert wf.status is Status.EXECUTING

    def test_given_a_merge_that_fails_after_approval_when_approved_then_the_run_fails(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())
        self.git.merge.side_effect = GitError("main has moved")
        self.runner.approve(wf)
        self.runner._threads[wf.id].join(timeout=10)
        assert wf.status is Status.FAILED
        assert "main has moved" in wf.message

    def test_given_a_retry_whose_worktree_was_cleaned_up_when_run_then_it_is_restored(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        wf.worktree = str(self.tmp / "gone")
        self.git.repo_root.return_value = str(self.tmp)
        self.git.ensure_worktree.return_value = self.tmp / "restored"
        self.drive(wf, self.fake_agent())
        self.git.ensure_worktree.assert_called_once()
        assert wf.worktree == str(self.tmp / "restored")


class TestStageFailures(RunnerBase):
    def test_given_a_review_that_cannot_run_when_reviewing_then_the_run_fails_without_merging(self):
        wf = self.make(mode=Mode.AUTONOMOUS)

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            if role is Role.REVIEW:
                return RunResult(1, "crashed")
            self.commits += 1
            return RunResult(0, "THE PLAN")

        self.drive(wf, _run)
        assert wf.status is Status.FAILED
        assert "review step exited with code 1" in wf.message
        self.git.merge.assert_not_called()

    def test_given_an_execute_cli_that_vanishes_when_executing_then_the_run_fails_at_once(self):
        wf = self.make(max_attempts=3)
        calls = []

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            calls.append(role)
            if role is Role.EXECUTE:
                raise BackendError("'codex' could not be found")
            return RunResult(0, "THE PLAN")

        self.drive(wf, _run)
        assert wf.status is Status.FAILED
        assert calls.count(Role.EXECUTE) == 1  # no retries against a missing CLI
        assert "execute step: 'codex' could not be found" in wf.message

    def test_given_a_worktree_git_cannot_read_when_continuing_then_the_prompt_says_so(self):
        wf = self.make(max_attempts=2)
        self.git.short_status.side_effect = GitError("index locked")
        prompts = []

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            if role is Role.EXECUTE:
                prompts.append(prompt)
                if len(prompts) == 2:
                    self.commits += 1
            return RunResult(0, "THE PLAN" if role is Role.PLAN else APPROVE)

        self.drive(wf, _run)
        assert "could not inspect the worktree: index locked" in prompts[1]

    def test_given_the_same_warning_twice_when_noted_then_it_is_kept_once(self):
        wf = self.make()
        self.runner._note(wf, "ran long")
        self.runner._note(wf, "ran long")
        assert wf.notes == ["ran long"]


class TestHelpers:
    @pytest.mark.parametrize("count, text", [(1, "1 attempt"), (2, "2 attempts"), (0, "0 attempts")])
    def test_given_a_count_when_pluralised_then_the_noun_agrees(self, count, text):
        assert plural(count, "attempt") == text

    def test_given_long_text_when_shortened_then_it_is_cut_with_an_ellipsis(self):
        assert _shorten("word " * 30, limit=20) == "word word word wo..."
        assert _shorten("  short\n text ") == "short text"

    def test_given_coloured_output_when_last_words_are_taken_then_the_last_three_lines_are_joined(self):
        output = "\x1b[32mone\x1b[0m\n\n two \nthree\nfour\n"
        assert _last_words(output) == "two / three / four"

    @pytest.mark.parametrize("output", ["", None, "\n  \n\x1b[0m"])
    def test_given_no_output_when_last_words_are_taken_then_it_says_so(self, output):
        assert _last_words(output) == "(it printed nothing)"

    def test_given_very_long_output_when_last_words_are_taken_then_the_tail_is_kept(self):
        words = _last_words("x" * 500, limit=50)
        assert words == "..." + "x" * 50

    def test_given_prose_around_the_checklist_when_parsed_then_the_list_starts_at_the_first_item(self):
        plan = "TASKS:\n\nIntro sentence.\n1. first\nnot an item\n2. ignored\n"
        assert [t.text for t in parse_tasks(plan)] == ["first"]
