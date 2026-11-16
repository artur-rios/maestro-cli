"""Checklist parsing, per-task execution, and the continuation loop."""

import threading
from unittest import mock

import pytest

from maestro import prompts
from maestro.backends import RunResult
from maestro.gitops import GitError
from maestro.models import Mode, Role, Status, Workflow
from maestro.runner import Runner, parse_tasks
from maestro.store import Store

PLAN_WITH_TASKS = """\
I will add the entity, then the repository, then the endpoints.

Some numbered prose the plan happens to contain:
1. This is not a task, it is discussion.

TASKS:
1. Add the ScopeCustomField entity and its EF configuration.
2. Add repository methods for reading and writing custom fields.
3. Expose the endpoints on ScopeController and cover them with tests.
"""


class TestParsing:
    def test_given_numbered_prose_and_a_checklist_when_parsed_then_only_the_checklist_counts(self):
        tasks = parse_tasks(PLAN_WITH_TASKS)
        assert len(tasks) == 3
        assert tasks[0].index == 1
        assert "ScopeCustomField entity" in tasks[0].text
        assert "not a task" not in " ".join(t.text for t in tasks)

    def test_given_a_plan_without_a_heading_when_parsed_then_there_are_no_tasks(self):
        assert parse_tasks("Just do the thing.\n1. step one") == []

    def test_given_an_empty_or_missing_plan_when_parsed_then_there_are_no_tasks(self):
        assert parse_tasks("") == []
        assert parse_tasks(None) == []

    def test_given_bullets_and_parentheses_when_parsed_then_they_are_accepted(self):
        plan = "TASKS:\n- 1) First item\n- 2) Second item\n"
        assert [t.text for t in parse_tasks(plan)] == ["First item", "Second item"]

    def test_given_a_blank_line_after_items_when_parsed_then_the_list_ends(self):
        plan = "TASKS:\n1. One\n2. Two\n\nSome trailing prose.\n3. Not a task\n"
        assert len(parse_tasks(plan)) == 2

    def test_given_two_headings_when_parsed_then_the_last_one_wins(self):
        plan = "TASKS:\n1. Old\n\nRevised.\n\nTASKS:\n1. New one\n2. New two\n"
        assert [t.text for t in parse_tasks(plan)] == ["New one", "New two"]

    def test_given_a_runaway_checklist_when_parsed_then_it_is_capped(self):
        plan = "TASKS:\n" + "\n".join(f"{i}. item {i}" for i in range(1, 40))
        assert len(parse_tasks(plan)) <= 15

    def test_given_items_numbered_from_seven_when_parsed_then_they_are_renumbered_from_one(self):
        plan = "TASKS:\n7. seven\n8. eight\n"
        assert [t.index for t in parse_tasks(plan)] == [1, 2]


class TaskRunnerBase:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.store = Store()
        self.runner = Runner(self.store)

        self.git = mock.MagicMock()
        monkeypatch.setattr("maestro.runner.gitops", self.git)
        self.git.GitError = GitError
        self.git.diff.return_value = "diff"
        self.git.short_status.return_value = " M src/x.cs"
        self.git.recent_commits.return_value = "abc123 did a thing"

        self.commits = 0
        self.git.commit_count.side_effect = lambda *a, **k: self.commits
        self.prompts_seen: list[tuple[Role, str]] = []
        yield
        for thread in list(self.runner._threads.values()):
            thread.join(timeout=10)

    def make(self, max_attempts=3, mode=Mode.AUTONOMOUS):
        wf = Workflow(
            prompt="add custom fields to scopes",
            repo_path=str(self.tmp), base_branch="main",
            agents={"plan": "claude", "execute": "opencode", "review": "claude"},
            mode=mode, max_rounds=1, max_attempts=max_attempts,
        )
        wf.branch = "feature/custom-fields"
        wf.branch_created = True
        wf.worktree = str(self.tmp)
        self.store.add(wf)
        return wf

    def agent(self, plan=PLAN_WITH_TASKS, commits_on=None, review="VERDICT: APPROVE"):
        """`commits_on` limits which execute calls actually commit."""
        execute_calls = {"n": 0}

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            self.prompts_seen.append((role, prompt))
            if role is Role.PLAN:
                return RunResult(0, plan)
            if role is Role.EXECUTE:
                execute_calls["n"] += 1
                if commits_on is None or execute_calls["n"] in commits_on:
                    self.commits += 1
                return RunResult(0, f"execute call {execute_calls['n']}")
            return RunResult(0, review)

        return _run

    def drive(self, wf, agent):
        with mock.patch("maestro.runner.backends.run", side_effect=agent):
            self.runner._run(wf, threading.Event())

    def execute_prompts(self):
        return [p for role, p in self.prompts_seen if role is Role.EXECUTE]


class TestDecomposition(TaskRunnerBase):
    def test_given_a_plan_checklist_when_run_then_it_becomes_the_workflow_tasks(self):
        wf = self.make()
        self.drive(wf, self.agent())
        assert len(wf.tasks) == 3
        assert all(t.status is Status.DONE for t in wf.tasks)
        assert wf.task_progress == "3/3"

    def test_given_three_tasks_when_run_then_execute_is_invoked_once_per_task(self):
        wf = self.make()
        self.drive(wf, self.agent())
        assert len(self.execute_prompts()) == 3

    def test_given_tasks_when_run_then_each_execute_step_records_its_task(self):
        wf = self.make()
        self.drive(wf, self.agent())
        indexes = [s.task_index for s in wf.steps if s.role is Role.EXECUTE]
        assert indexes == [1, 2, 3]

    def test_given_tasks_when_run_then_each_is_asked_for_on_its_own(self):
        wf = self.make()
        self.drive(wf, self.agent())
        first, second, third = self.execute_prompts()
        assert "Work on item 1 ONLY" in first
        assert "Work on item 2 ONLY" in second
        assert "Work on item 3 ONLY" in third

    def test_given_finished_items_when_a_later_task_runs_then_they_are_ticked(self):
        wf = self.make()
        self.drive(wf, self.agent())
        third = self.execute_prompts()[2]
        assert "[x] 1." in third
        assert "[ ] 3." in third

    def test_given_tasks_when_run_then_each_commits_separately(self):
        wf = self.make()
        self.drive(wf, self.agent())
        messages = [c.args[1] for c in self.git.commit_all.call_args_list]
        assert len(messages) == 3
        assert "ScopeCustomField" in messages[0]

    def test_given_a_plan_without_a_checklist_when_run_then_execute_is_invoked_once(self):
        wf = self.make()
        self.drive(wf, self.agent(plan="Just do it, no checklist here."))
        assert wf.tasks == []
        assert len(self.execute_prompts()) == 1
        assert wf.status is Status.MERGED

    def test_given_a_task_that_fails_when_run_then_the_run_stops_and_the_task_is_marked(self):
        wf = self.make(max_attempts=1)
        # Only the first task ever commits.
        self.drive(wf, self.agent(commits_on={1}))
        assert wf.status is Status.FAILED
        assert wf.tasks[0].status is Status.DONE
        assert wf.tasks[1].status is Status.FAILED
        assert wf.tasks[2].status is Status.PENDING  # never reached

    def test_given_a_task_that_stalls_when_the_run_fails_then_the_message_names_it(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent(commits_on={1}))
        assert "repository methods" in wf.message
        assert "opencode" in wf.message

    def test_given_finished_tasks_when_retried_then_they_are_not_redone(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent(commits_on={1}))
        assert wf.status is Status.FAILED

        self.prompts_seen.clear()
        with mock.patch(
            "maestro.runner.backends.run", side_effect=self.agent()
        ):
            self.runner.retry(wf)
            self.runner._threads[wf.id].join(timeout=10)

        asked = " ".join(self.execute_prompts())
        assert "Work on item 1 ONLY" not in asked
        assert "Work on item 2 ONLY" in asked


class TestContinuation(TaskRunnerBase):
    def test_given_an_agent_that_commits_nothing_when_attempts_remain_then_it_is_asked_to_continue(self):
        wf = self.make(max_attempts=3)
        # The second attempt at task 1 succeeds; then tasks 2 and 3.
        self.drive(wf, self.agent(commits_on={2, 3, 4}))

        assert wf.status is Status.MERGED
        assert wf.tasks[0].attempts == 2
        assert "resuming" in self.execute_prompts()[1]

    def test_given_a_continuation_when_prompted_then_it_carries_the_worktree_state(self):
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent(commits_on={2, 3, 4}))
        resumed = self.execute_prompts()[1]
        assert "You stopped without committing anything" in resumed
        assert " M src/x.cs" in resumed
        assert "abc123 did a thing" in resumed

    def test_given_no_commits_on_every_attempt_when_run_then_it_gives_up_after_the_configured_number(self):
        wf = self.make(max_attempts=3)
        self.drive(wf, self.agent(commits_on=set()))
        assert wf.status is Status.FAILED
        assert len(self.execute_prompts()) == 3
        assert wf.tasks[0].attempts == 3
        assert "3 attempt" in wf.message

    def test_given_one_attempt_when_nothing_is_committed_then_there_is_no_continuation(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent(commits_on=set()))
        assert len(self.execute_prompts()) == 1

    def test_given_a_successful_first_attempt_when_run_then_it_is_not_repeated(self):
        wf = self.make(max_attempts=3)
        self.drive(wf, self.agent())
        assert all(t.attempts == 1 for t in wf.tasks)

    def test_given_no_checklist_when_nothing_is_committed_then_the_agent_is_also_asked_to_continue(self):
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent(plan="No checklist.", commits_on={2}))
        assert wf.status is Status.MERGED
        assert len(self.execute_prompts()) == 2
        assert "resuming" in self.execute_prompts()[1]


class TestTimeoutHandling(TaskRunnerBase):
    """A timeout must not throw away work the agent already committed."""

    def agent_that(self, timed_out=False, exit_code=0, commits=True):
        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            self.prompts_seen.append((role, prompt))
            if role is Role.PLAN:
                return RunResult(0, "plan\n\nTASKS:\n1. only item\n")
            if role is Role.EXECUTE:
                if commits:
                    self.commits += 1
                return RunResult(exit_code, "did some work", timed_out=timed_out)
            return RunResult(0, "VERDICT: APPROVE")
        return _run

    def test_given_a_timeout_after_committing_when_run_then_the_task_counts_as_done(self):
        """The reported bug: 37 minutes of work discarded by the timeout."""
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(timed_out=True, commits=True))

        assert wf.status is Status.MERGED, wf.message
        assert wf.tasks[0].status is Status.DONE

    def test_given_a_timeout_after_committing_when_run_then_the_overrun_is_noted(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(timed_out=True, commits=True))
        notes = " ".join(wf.notes)
        assert "past its timeout" in notes
        assert "work was committed" in notes
        # The final outcome is still the headline.
        assert "merged" in wf.message

    def test_given_a_bad_exit_code_after_committing_when_run_then_the_task_counts_as_done(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(exit_code=3, commits=True))
        assert wf.status is Status.MERGED, wf.message
        assert "code 3" in " ".join(wf.notes)

    def test_given_notes_when_round_tripped_then_they_survive(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(timed_out=True, commits=True))
        restored = Workflow.from_dict(wf.to_dict())
        assert restored.notes == wf.notes

    def test_given_a_timeout_with_nothing_committed_when_run_then_another_attempt_is_used(self):
        wf = self.make(max_attempts=2)
        calls = {"n": 0}

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            self.prompts_seen.append((role, prompt))
            if role is Role.PLAN:
                return RunResult(0, "plan\n\nTASKS:\n1. only item\n")
            if role is Role.EXECUTE:
                calls["n"] += 1
                if calls["n"] == 1:
                    return RunResult(-1, "ran long", timed_out=True)
                self.commits += 1
                return RunResult(0, "done")
            return RunResult(0, "VERDICT: APPROVE")

        self.drive(wf, _run)

        assert wf.status is Status.MERGED, wf.message
        assert len(self.execute_prompts()) == 2
        assert "resuming" in self.execute_prompts()[1]

    def test_given_every_attempt_timed_out_when_the_run_fails_then_it_says_so_and_points_at_the_fix(self):
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent_that(timed_out=True, commits=False))
        assert wf.status is Status.FAILED
        assert "timed out" in wf.message
        assert "2 attempts" in wf.message
        assert "timeout" in wf.message  # points at the fix

    def test_given_a_planning_timeout_when_run_then_the_run_fails(self):
        """Planning has no commits to measure, so a timeout is terminal."""
        wf = self.make(max_attempts=3)

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            return RunResult(-1, "thinking...", timed_out=True)

        self.drive(wf, _run)
        assert wf.status is Status.FAILED
        assert "timed out" in wf.message
        assert "backends.json" in wf.message


class TestStepLogPaths(TaskRunnerBase):
    def test_given_tasks_and_retries_when_run_then_each_gets_its_own_log_file(self):
        """They all shared execute-round1.log and overwrote each other."""
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent(commits_on={2, 3, 4}))

        paths = [s.log_path for s in wf.steps if s.role is Role.EXECUTE]
        assert len(paths) == len(set(paths)), f"log files collided: {paths}"
        assert any("task1" in p for p in paths)
        assert any("try2" in p for p in paths)


class TestPromptShape:
    def test_given_the_plan_prompt_when_built_then_it_demands_a_checklist(self):
        wf = Workflow(
            prompt="p", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
        )
        text = prompts.plan_prompt(wf)
        assert prompts.TASKS_HEADING in text
        assert "committed on its own" in text
