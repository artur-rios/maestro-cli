"""Checklist parsing, per-task execution, and the continuation loop."""

import os
import tempfile
import threading
import unittest
from unittest import mock

from maestro import prompts
from maestro.backends import RunResult
from maestro.gitops import GitError
from maestro.models import Mode, Role, Status, Task, Workflow
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


class Parsing(unittest.TestCase):
    def test_only_the_checklist_under_the_heading_counts(self):
        tasks = parse_tasks(PLAN_WITH_TASKS)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0].index, 1)
        self.assertIn("ScopeCustomField entity", tasks[0].text)
        self.assertNotIn(
            "not a task", " ".join(t.text for t in tasks)
        )

    def test_a_plan_without_a_checklist_yields_nothing(self):
        self.assertEqual(parse_tasks("Just do the thing.\n1. step one"), [])

    def test_empty_or_missing_plan_is_safe(self):
        self.assertEqual(parse_tasks(""), [])
        self.assertEqual(parse_tasks(None), [])

    def test_bullets_and_parentheses_are_accepted(self):
        plan = "TASKS:\n- 1) First item\n- 2) Second item\n"
        self.assertEqual([t.text for t in parse_tasks(plan)],
                         ["First item", "Second item"])

    def test_a_blank_line_ends_the_list(self):
        plan = "TASKS:\n1. One\n2. Two\n\nSome trailing prose.\n3. Not a task\n"
        self.assertEqual(len(parse_tasks(plan)), 2)

    def test_the_last_heading_wins(self):
        plan = "TASKS:\n1. Old\n\nRevised.\n\nTASKS:\n1. New one\n2. New two\n"
        self.assertEqual([t.text for t in parse_tasks(plan)], ["New one", "New two"])

    def test_a_runaway_checklist_is_capped(self):
        plan = "TASKS:\n" + "\n".join(f"{i}. item {i}" for i in range(1, 40))
        self.assertLessEqual(len(parse_tasks(plan)), 15)

    def test_tasks_are_numbered_from_one_regardless_of_the_source(self):
        plan = "TASKS:\n7. seven\n8. eight\n"
        self.assertEqual([t.index for t in parse_tasks(plan)], [1, 2])


class TaskRunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("MAESTRO_HOME")
        os.environ["MAESTRO_HOME"] = self.tmp.name
        self.store = Store()
        self.runner = Runner(self.store)

        self.git = mock.patch("maestro.runner.gitops").start()
        self.git.GitError = GitError
        self.git.diff.return_value = "diff"
        self.git.short_status.return_value = " M src/x.cs"
        self.git.recent_commits.return_value = "abc123 did a thing"
        self.addCleanup(mock.patch.stopall)

        self.commits = 0
        self.git.commit_count.side_effect = lambda *a, **k: self.commits
        self.prompts_seen: list[tuple[Role, str]] = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MAESTRO_HOME", None)
        else:
            os.environ["MAESTRO_HOME"] = self._old
        self.tmp.cleanup()

    def make(self, max_attempts=3, mode=Mode.AUTONOMOUS):
        wf = Workflow(
            prompt="add custom fields to scopes",
            repo_path=self.tmp.name, base_branch="main",
            agents={"plan": "claude", "execute": "opencode", "review": "claude"},
            mode=mode, max_rounds=1, max_attempts=max_attempts,
        )
        wf.branch = "feature/custom-fields"
        wf.branch_created = True
        wf.worktree = self.tmp.name
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


class Decomposition(TaskRunnerTestCase):
    def test_the_plan_checklist_becomes_workflow_tasks(self):
        wf = self.make()
        self.drive(wf, self.agent())
        self.assertEqual(len(wf.tasks), 3)
        self.assertTrue(all(t.status is Status.DONE for t in wf.tasks))
        self.assertEqual(wf.task_progress, "3/3")

    def test_one_execute_invocation_per_task(self):
        wf = self.make()
        self.drive(wf, self.agent())
        self.assertEqual(len(self.execute_prompts()), 3)

    def test_each_execute_step_records_its_task(self):
        wf = self.make()
        self.drive(wf, self.agent())
        indexes = [s.task_index for s in wf.steps if s.role is Role.EXECUTE]
        self.assertEqual(indexes, [1, 2, 3])

    def test_each_task_is_asked_for_individually(self):
        wf = self.make()
        self.drive(wf, self.agent())
        first, second, third = self.execute_prompts()
        self.assertIn("Work on item 1 ONLY", first)
        self.assertIn("Work on item 2 ONLY", second)
        self.assertIn("Work on item 3 ONLY", third)

    def test_completed_items_are_ticked_for_later_tasks(self):
        wf = self.make()
        self.drive(wf, self.agent())
        third = self.execute_prompts()[2]
        self.assertIn("[x] 1.", third)
        self.assertIn("[ ] 3.", third)

    def test_each_task_commits_separately(self):
        wf = self.make()
        self.drive(wf, self.agent())
        messages = [c.args[1] for c in self.git.commit_all.call_args_list]
        self.assertEqual(len(messages), 3)
        self.assertIn("ScopeCustomField", messages[0])

    def test_a_plan_without_a_checklist_falls_back_to_one_invocation(self):
        wf = self.make()
        self.drive(wf, self.agent(plan="Just do it, no checklist here."))
        self.assertEqual(wf.tasks, [])
        self.assertEqual(len(self.execute_prompts()), 1)
        self.assertIs(wf.status, Status.MERGED)

    def test_a_failing_task_stops_the_run_and_is_marked(self):
        wf = self.make(max_attempts=1)
        # Only the first task ever commits.
        self.drive(wf, self.agent(commits_on={1}))
        self.assertIs(wf.status, Status.FAILED)
        self.assertIs(wf.tasks[0].status, Status.DONE)
        self.assertIs(wf.tasks[1].status, Status.FAILED)
        self.assertIs(wf.tasks[2].status, Status.PENDING)  # never reached

    def test_the_failure_names_the_task_that_stalled(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent(commits_on={1}))
        self.assertIn("repository methods", wf.message)
        self.assertIn("opencode", wf.message)

    def test_finished_tasks_are_not_redone_on_a_retry(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent(commits_on={1}))
        self.assertIs(wf.status, Status.FAILED)

        self.prompts_seen.clear()
        with mock.patch(
            "maestro.runner.backends.run", side_effect=self.agent()
        ):
            self.runner.retry(wf)
            self.runner._threads[wf.id].join(timeout=10)

        asked = " ".join(self.execute_prompts())
        self.assertNotIn("Work on item 1 ONLY", asked)
        self.assertIn("Work on item 2 ONLY", asked)


class Continuation(TaskRunnerTestCase):
    def test_an_agent_that_commits_nothing_is_asked_to_continue(self):
        wf = self.make(max_attempts=3)
        # The second attempt at task 1 succeeds; then tasks 2 and 3.
        self.drive(wf, self.agent(commits_on={2, 3, 4}))

        self.assertIs(wf.status, Status.MERGED)
        self.assertEqual(wf.tasks[0].attempts, 2)
        self.assertIn("resuming", self.execute_prompts()[1])

    def test_the_continuation_prompt_carries_the_worktree_state(self):
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent(commits_on={2, 3, 4}))
        resumed = self.execute_prompts()[1]
        self.assertIn("You stopped without committing anything", resumed)
        self.assertIn(" M src/x.cs", resumed)
        self.assertIn("abc123 did a thing", resumed)

    def test_it_gives_up_after_the_configured_attempts(self):
        wf = self.make(max_attempts=3)
        self.drive(wf, self.agent(commits_on=set()))
        self.assertIs(wf.status, Status.FAILED)
        self.assertEqual(len(self.execute_prompts()), 3)
        self.assertEqual(wf.tasks[0].attempts, 3)
        self.assertIn("3 attempt", wf.message)

    def test_one_attempt_means_no_continuation(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent(commits_on=set()))
        self.assertEqual(len(self.execute_prompts()), 1)

    def test_a_successful_first_attempt_is_not_repeated(self):
        wf = self.make(max_attempts=3)
        self.drive(wf, self.agent())
        self.assertTrue(all(t.attempts == 1 for t in wf.tasks))

    def test_continuation_also_covers_the_no_checklist_path(self):
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent(plan="No checklist.", commits_on={2}))
        self.assertIs(wf.status, Status.MERGED)
        self.assertEqual(len(self.execute_prompts()), 2)
        self.assertIn("resuming", self.execute_prompts()[1])


class TimeoutHandling(TaskRunnerTestCase):
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

    def test_a_timeout_after_committing_still_counts_as_done(self):
        """The reported bug: 37 minutes of work discarded by the timeout."""
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(timed_out=True, commits=True))

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertIs(wf.tasks[0].status, Status.DONE)

    def test_the_overrun_is_still_reported_as_a_warning(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(timed_out=True, commits=True))
        notes = " ".join(wf.notes)
        self.assertIn("past its timeout", notes)
        self.assertIn("work was committed", notes)
        # The final outcome is still the headline.
        self.assertIn("merged", wf.message)

    def test_a_bad_exit_code_after_committing_also_counts(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(exit_code=3, commits=True))
        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertIn("code 3", " ".join(wf.notes))

    def test_notes_survive_a_reload(self):
        wf = self.make(max_attempts=1)
        self.drive(wf, self.agent_that(timed_out=True, commits=True))
        restored = Workflow.from_dict(wf.to_dict())
        self.assertEqual(restored.notes, wf.notes)

    def test_a_timeout_with_nothing_committed_uses_another_attempt(self):
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

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertEqual(len(self.execute_prompts()), 2)
        self.assertIn("resuming", self.execute_prompts()[1])

    def test_running_out_of_attempts_says_it_timed_out(self):
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent_that(timed_out=True, commits=False))
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("timed out", wf.message)
        self.assertIn("2 attempts", wf.message)
        self.assertIn("timeout", wf.message)  # points at the fix

    def test_a_planning_timeout_still_fails_the_run(self):
        """Planning has no commits to measure, so a timeout is terminal."""
        wf = self.make(max_attempts=3)

        def _run(name, role, prompt, cwd, log_file, backends=None, model=""):
            return RunResult(-1, "thinking...", timed_out=True)

        self.drive(wf, _run)
        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("timed out", wf.message)
        self.assertIn("backends.json", wf.message)


class StepLogPaths(TaskRunnerTestCase):
    def test_each_task_and_attempt_gets_its_own_log_file(self):
        """They all shared execute-round1.log and overwrote each other."""
        wf = self.make(max_attempts=2)
        self.drive(wf, self.agent(commits_on={2, 3, 4}))

        paths = [s.log_path for s in wf.steps if s.role is Role.EXECUTE]
        self.assertEqual(len(paths), len(set(paths)), f"log files collided: {paths}")
        self.assertTrue(any("task1" in p for p in paths))
        self.assertTrue(any("try2" in p for p in paths))


class Serialisation(unittest.TestCase):
    def test_tasks_survive_a_json_round_trip(self):
        wf = Workflow(
            prompt="p", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
            max_attempts=5,
        )
        wf.tasks = [Task(1, "first", Status.DONE, 2), Task(2, "second")]
        step = wf.add_step(Role.EXECUTE, task_index=2)
        step.status = Status.RUNNING

        restored = Workflow.from_dict(wf.to_dict())

        self.assertEqual(restored.max_attempts, 5)
        self.assertEqual(len(restored.tasks), 2)
        self.assertIs(restored.tasks[0].status, Status.DONE)
        self.assertEqual(restored.tasks[0].attempts, 2)
        self.assertEqual(restored.steps[0].task_index, 2)

    def test_a_workflow_from_before_tasks_existed_still_loads(self):
        wf = Workflow(
            prompt="p", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
        )
        data = wf.to_dict()
        del data["tasks"]
        del data["max_attempts"]
        restored = Workflow.from_dict(data)
        self.assertEqual(restored.tasks, [])
        self.assertEqual(restored.max_attempts, 3)

    def test_progress_is_blank_without_a_checklist(self):
        wf = Workflow(
            prompt="p", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
        )
        self.assertEqual(wf.task_progress, "")


class PromptShape(unittest.TestCase):
    def test_the_plan_prompt_demands_a_checklist(self):
        wf = Workflow(
            prompt="p", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
        )
        text = prompts.plan_prompt(wf)
        self.assertIn(prompts.TASKS_HEADING, text)
        self.assertIn("committed on its own", text)


if __name__ == "__main__":
    unittest.main()
