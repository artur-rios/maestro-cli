"""Plan and review are optional; execute is not.

A run is at minimum "implement this, then merge it". These tests cover the
whole path of that choice: what the user types, what is stored, which steps
the state machine takes, what the agents are told, and what the confirmation
draws.
"""

import pytest

from maestro import prompts, settings as settings_mod
from maestro.models import Mode, Role, Status, Workflow
from maestro.shell import commands, render

from tests.test_render import draw
from tests.test_runner import RunnerBase
from tests.test_shell import TaskStartBase


def _workflow(**agents) -> Workflow:
    filled = {"plan": "claude", "execute": "codex", "review": "claude"}
    filled.update(agents)
    wf = Workflow(
        prompt="add a health check", repo_path="/repo", base_branch="main",
        agents=filled,
    )
    wf.branch = "feature/health-check"
    return wf


class TestTheModel:
    def test_given_a_role_with_no_agent_when_checked_then_it_does_not_run(self):
        wf = _workflow(plan="", review="")
        assert not wf.runs(Role.PLAN)
        assert not wf.runs(Role.REVIEW)
        assert wf.runs(Role.EXECUTE)

    def test_given_a_skipped_role_when_listing_active_roles_then_the_rest_keep_their_order(self):
        assert _workflow(plan="").active_roles == [Role.EXECUTE, Role.REVIEW]
        assert _workflow(review="").active_roles == [Role.PLAN, Role.EXECUTE]

    def test_given_a_skipped_role_when_round_tripped_then_it_stays_skipped(self):
        wf = Workflow.from_dict(_workflow(review="").to_dict())
        assert wf.agent_for(Role.REVIEW) == ""
        assert not wf.runs(Role.REVIEW)


class TestTheSetting:
    def test_given_the_words_people_type_for_off_when_coerced_then_they_all_mean_off(self):
        for word in ("off", "OFF", "none", "no", "skip", "-"):
            assert settings_mod.coerce("plan", word) == "off", word
            assert settings_mod.coerce("review", word) == "off", word

    def test_given_a_backend_name_when_coerced_then_it_is_left_alone(self):
        assert settings_mod.coerce("plan", "claude") == "claude"

    def test_given_off_for_execute_when_coerced_then_it_is_refused(self):
        with pytest.raises(settings_mod.SettingsError) as caught:
            settings_mod.coerce("execute", "off")
        assert "cannot be skipped" in str(caught.value)

    def test_given_a_blank_value_when_checked_then_it_is_not_off(self):
        """Blank means "pick one for me", which is the opposite of skipping."""
        assert not settings_mod.is_off("")


class TestResolvingAgents(TaskStartBase):
    def test_given_an_off_flag_when_a_task_is_typed_then_the_step_is_off_for_that_run(self):
        self.do("add rate limiting --plan off")
        assert self.started().agents["plan"] == ""

    def test_given_an_off_setting_when_a_task_is_typed_then_the_step_is_off(self):
        self.session.settings["review"] = "off"
        self.do("add rate limiting")
        assert self.started().agents["review"] == ""

    def test_given_an_off_setting_and_an_agent_flag_when_a_task_is_typed_then_the_step_is_back(self):
        self.session.settings["plan"] = "off"
        self.do("add rate limiting --plan claude")
        assert self.started().agents["plan"] == "claude"

    def test_given_execute_off_when_a_task_is_typed_then_it_is_refused(self):
        out = self.do("add rate limiting --execute off")
        assert "cannot be skipped" in out
        self.session.runner.start.assert_not_called()

    def test_given_a_skipped_step_with_a_model_default_when_a_task_is_typed_then_it_carries_no_model(self):
        self.session.settings["plan_model"] = "opus"
        self.do("add rate limiting --plan off")
        assert self.started().models.get("plan", "") == ""

    def test_given_a_skipped_step_when_confirming_then_only_the_steps_that_run_are_shown(self):
        out = self.do("add rate limiting --plan off")
        assert "plan" not in out
        for cell in ("execute", "review", "merge"):
            assert cell in out

    def test_given_a_skipped_step_when_saved_as_defaults_then_it_is_saved_as_off(self):
        plan = {
            "agents": {"plan": "", "execute": "opencode", "review": "codex"},
            "models": {}, "mode": "supervised", "rounds": 2, "attempts": 3,
        }
        commands._save_as_defaults(self.session, plan)
        assert self.session.settings["plan"] == "off"
        assert self.session.settings["execute"] == "opencode"


class TestWithoutAPlanStep(RunnerBase):
    def make(self, **kwargs):
        wf = super().make(**kwargs)
        wf.agents["plan"] = ""
        return wf

    def test_given_no_plan_agent_when_run_then_no_planning_step_is_invoked(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert [s.role for s in wf.steps] == [Role.EXECUTE, Role.REVIEW]
        assert Role.PLAN not in [role for role, _ in self.calls]

    def test_given_no_plan_agent_when_run_then_it_still_merges(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert wf.status is Status.MERGED
        self.git.merge.assert_called_once()

    def test_given_no_plan_agent_when_executing_then_the_agent_is_told_there_is_no_plan(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        prompt = next(p for role, p in self.calls if role is Role.EXECUTE)
        assert "no planning step" in prompt
        assert "add a health check" in prompt

    def test_given_no_plan_agent_when_reviewing_then_the_reviewer_still_sees_the_task(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        prompt = next(p for role, p in self.calls if role is Role.REVIEW)
        assert "no planning step" in prompt
        assert "add a health check" in prompt


class TestWithoutAReviewStep(RunnerBase):
    def make(self, **kwargs):
        wf = super().make(**kwargs)
        wf.agents["review"] = ""
        return wf

    def test_given_no_review_agent_when_run_then_no_review_step_is_invoked(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert [s.role for s in wf.steps] == [Role.PLAN, Role.EXECUTE]
        assert wf.verdict is None

    def test_given_no_review_agent_and_autonomous_mode_when_run_then_it_merges_after_the_work(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert wf.status is Status.MERGED
        self.git.merge.assert_called_once()

    def test_given_no_review_agent_and_supervised_mode_when_run_then_it_still_asks_before_merging(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())

        assert wf.status is Status.AWAITING_APPROVAL
        assert "committed" in wf.message
        assert "reviewer" not in wf.message
        self.git.merge.assert_not_called()

    def test_given_no_review_agent_when_run_then_the_work_is_never_sent_back(self):
        """With nobody to reject it, one execution pass is the whole run."""
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=3)
        self.drive(wf, self.fake_agent())

        assert wf.round == 1
        assert sum(1 for s in wf.steps if s.role is Role.EXECUTE) == (len(wf.tasks) or 1)


class TestWithOnlyTheExecuteStep(RunnerBase):
    def make(self, **kwargs):
        wf = super().make(**kwargs)
        wf.agents["plan"] = ""
        wf.agents["review"] = ""
        return wf

    def test_given_only_an_execute_agent_when_run_then_one_agent_does_the_work(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        assert [s.role for s in wf.steps] == [Role.EXECUTE]
        assert wf.status is Status.MERGED

    def test_given_only_an_execute_agent_when_prompted_then_it_is_told_it_works_alone(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        prompt = next(p for role, p in self.calls if role is Role.EXECUTE)
        assert "single-agent" in prompt
        assert "three-agent" not in prompt


class TestThePrompts:
    def test_given_the_active_steps_when_framed_then_the_count_matches(self):
        assert prompts.framing(_workflow()) == "a three-agent"
        assert prompts.framing(_workflow(plan="")) == "a two-agent"
        assert prompts.framing(_workflow(plan="", review="")) == "a single-agent"

    def test_given_a_plan_when_the_execute_prompt_is_built_then_it_is_quoted(self):
        wf = _workflow()
        wf.plan_text = "STEP ONE: do the thing"
        assert "STEP ONE" in prompts.execute_prompt(wf)

    def test_given_no_plan_agent_when_prompts_are_built_then_the_conventions_still_reach_them(self):
        wf = _workflow(plan="")
        for text in (prompts.execute_prompt(wf),
                     prompts.review_prompt(wf, "diff --git a/x b/x")):
            assert "CLAUDE.md" in text
            assert "skills" in text


class TestTheDiagram:
    def diagram(self, mode="supervised", **agents):
        filled = {"plan": "claude", "execute": "opencode", "review": "codex"}
        filled.update(agents)
        return draw(render.workflow_diagram(filled, mode))

    def test_given_every_step_when_the_diagram_is_drawn_then_each_gets_a_cell_with_its_agent(self):
        out = self.diagram()
        for cell in ("plan", "execute", "review", "merge"):
            assert cell in out
        for agent in ("claude", "opencode", "codex"):
            assert agent in out

    def test_given_a_skipped_step_when_the_diagram_is_drawn_then_it_is_left_out(self):
        out = self.diagram(plan="")
        assert "plan" not in out
        assert "claude" not in out
        assert "execute" in out

    def test_given_a_mode_when_the_diagram_is_drawn_then_the_merge_says_who_decides(self):
        assert "you approve" in self.diagram(mode="supervised")
        assert "automatic" in self.diagram(mode="autonomous")

    def test_given_every_step_when_the_diagram_is_drawn_then_the_cells_come_in_order(self):
        out = self.diagram()
        positions = [out.index(cell)
                     for cell in ("plan", "execute", "review", "merge")]
        assert positions == sorted(positions)

    def test_given_a_narrow_terminal_when_the_diagram_is_drawn_then_it_is_one_line(self):
        agents = {"plan": "claude", "execute": "opencode", "review": "codex"}
        line = draw(render.workflow_diagram(agents, "supervised", term_width=30))
        assert len(line.strip().splitlines()) == 1
        for cell in ("plan", "execute", "review", "merge"):
            assert cell in line
