"""The workflow data model and its JSON round trip."""

import pytest

from maestro import models
from maestro.models import Mode, Role, Status, Step, Task, Verdict, Workflow


def make_workflow(**kwargs):
    defaults = dict(
        prompt="Add a health check endpoint",
        repo_path="/tmp/repo",
        base_branch="main",
        agents={"plan": "claude", "execute": "codex", "review": "claude"},
    )
    defaults.update(kwargs)
    return Workflow(**defaults)


class TestWorkflowSerialisation:
    def test_given_a_full_workflow_when_round_tripped_through_a_dict_then_it_is_unchanged(self):
        wf = make_workflow(mode=Mode.AUTONOMOUS, max_rounds=3, models={"plan": "opus"})
        wf.branch = "maestro/add-a-health-check-endpoint-abc123"
        wf.status = Status.REVIEWING
        wf.verdict = Verdict.APPROVE
        wf.plan_text = "step one"
        wf.notes = ["a note"]
        wf.tasks = [Task(1, "first", Status.DONE, attempts=2)]
        step = wf.add_step(Role.PLAN)
        step.status = Status.DONE
        step.exit_code = 0

        restored = Workflow.from_dict(wf.to_dict())

        assert restored.to_dict() == wf.to_dict()
        assert restored.mode is Mode.AUTONOMOUS
        assert restored.status is Status.REVIEWING
        assert restored.verdict is Verdict.APPROVE
        assert restored.steps[0].role is Role.PLAN
        assert restored.steps[0].agent == "claude"
        assert restored.tasks[0].status is Status.DONE

    def test_given_no_verdict_when_round_tripped_then_the_verdict_stays_none(self):
        restored = Workflow.from_dict(make_workflow().to_dict())
        assert restored.verdict is None

    def test_given_a_record_from_an_older_version_when_loaded_then_the_missing_fields_get_defaults(self):
        old = {
            "id": "abc12345", "prompt": "p", "repo_path": "/r", "base_branch": "main",
            "agents": {"execute": "codex"}, "mode": "supervised", "status": "merged",
        }
        restored = Workflow.from_dict(old)
        assert restored.models == {}
        assert restored.max_rounds == 2
        assert restored.max_attempts == 3
        assert restored.steps == []
        assert restored.tasks == []
        assert restored.notes == []
        assert restored.round == 1
        assert restored.branch_created is False

    def test_given_an_older_record_with_a_worktree_when_loaded_then_its_branch_counts_as_created(self):
        old = make_workflow().to_dict()
        del old["branch_created"]
        old["worktree"] = "/somewhere"
        assert Workflow.from_dict(old).branch_created is True

    def test_given_a_task_without_status_or_attempts_when_loaded_then_it_is_pending_and_untried(self):
        task = Task.from_dict({"index": 2, "text": "second"})
        assert task.status is Status.PENDING
        assert task.attempts == 0

    def test_given_a_step_when_round_tripped_then_its_enums_survive(self):
        step = Step(role=Role.REVIEW, agent="claude", round=2, task_index=1, status=Status.FAILED)
        data = step.to_dict()
        assert data["role"] == "review"
        assert data["status"] == "failed"
        assert Step.from_dict(data) == step


class TestWorkflowBehaviour:
    def test_given_a_role_when_its_agent_is_asked_for_then_the_configured_one_is_returned(self):
        assert make_workflow().agent_for(Role.EXECUTE) == "codex"

    def test_given_a_role_with_no_agent_when_its_agent_is_asked_for_then_it_is_blank(self):
        wf = make_workflow(agents={"plan": None, "execute": "codex"})
        assert wf.agent_for(Role.PLAN) == ""
        assert wf.agent_for(Role.REVIEW) == ""

    def test_given_a_model_for_one_role_when_asked_per_role_then_the_others_are_blank(self):
        wf = make_workflow(models={"execute": "opus"})
        assert wf.model_for(Role.EXECUTE) == "opus"
        assert wf.model_for(Role.PLAN) == ""

    def test_given_a_later_round_when_a_step_is_added_then_it_records_the_round_and_agent(self):
        wf = make_workflow()
        wf.round = 2
        step = wf.add_step(Role.REVIEW, task_index=3)
        assert step.round == 2
        assert step.agent == "claude"
        assert step.task_index == 3
        assert wf.steps == [step]

    def test_given_several_steps_when_the_last_one_for_a_role_is_asked_for_then_the_newest_is_returned(self):
        wf = make_workflow()
        wf.add_step(Role.EXECUTE)
        wf.round = 2
        second = wf.add_step(Role.EXECUTE)
        assert wf.last_step(Role.EXECUTE) is second
        assert wf.last_step(Role.PLAN) is None

    def test_given_a_long_multiline_prompt_when_summarised_then_it_is_one_truncated_line(self):
        wf = make_workflow(prompt="a\n  b   c" + "x" * 80)
        assert len(wf.summary) == 50
        assert wf.summary.startswith("a b c")
        assert wf.summary.endswith("...")

    def test_given_a_prompt_of_exactly_fifty_characters_when_summarised_then_it_is_kept_whole(self):
        assert make_workflow(prompt="y" * 50).summary == "y" * 50

    def test_given_a_merged_workflow_when_checked_then_it_is_terminal(self):
        wf = make_workflow()
        assert not wf.is_terminal
        wf.status = Status.MERGED
        assert wf.is_terminal

    @pytest.mark.parametrize("status", [Status.PAUSED, Status.AWAITING_APPROVAL, Status.EXECUTING])
    def test_given_a_run_that_can_still_continue_when_checked_then_it_is_not_terminal(self, status):
        assert not make_workflow(status=status).is_terminal


class TestTaskProgress:
    def test_given_no_checklist_when_progress_is_asked_for_then_it_is_blank(self):
        assert make_workflow().task_progress == ""

    def test_given_a_partly_done_checklist_when_progress_is_asked_for_then_done_over_total_is_shown(self):
        wf = make_workflow(tasks=[Task(1, "a", Status.DONE), Task(2, "b", Status.FAILED), Task(3, "c")])
        assert wf.task_progress == "1/3"
        assert [t.index for t in wf.pending_tasks] == [2, 3]


class TestStepDuration:
    def test_given_a_step_that_never_started_when_its_duration_is_asked_for_then_it_is_none(self):
        assert Step(role=Role.PLAN, agent="claude").duration is None

    def test_given_a_finished_step_when_its_duration_is_asked_for_then_it_is_end_minus_start(self):
        assert Step(role=Role.PLAN, agent="claude", started_at=100.0, ended_at=107.5).duration == 7.5

    def test_given_a_running_step_when_its_duration_is_asked_for_then_it_counts_up_to_now(self, monkeypatch):
        monkeypatch.setattr(models.time, "time", lambda: 130.0)
        assert Step(role=Role.PLAN, agent="claude", started_at=100.0).duration == 30.0
