"""Plan and review are optional; execute is not.

A run is at minimum "implement this, then merge it". These tests cover the
whole path of that choice: what the user types, what is stored, which steps
the state machine takes, what the agents are told, and what the confirmation
draws.
"""

import unittest

from maestro import prompts, settings as settings_mod
from maestro.models import Mode, Role, Status, Workflow
from maestro.shell import commands, render

from tests.test_render import draw
from tests.test_runner import RunnerTestCase
from tests.test_shell import TaskStartTestCase


def _workflow(**agents) -> Workflow:
    filled = {"plan": "claude", "execute": "codex", "review": "claude"}
    filled.update(agents)
    wf = Workflow(
        prompt="add a health check", repo_path="/repo", base_branch="main",
        agents=filled,
    )
    wf.branch = "feature/health-check"
    return wf


class TheModel(unittest.TestCase):
    def test_a_role_with_no_agent_does_not_run(self):
        wf = _workflow(plan="", review="")
        self.assertFalse(wf.runs(Role.PLAN))
        self.assertFalse(wf.runs(Role.REVIEW))
        self.assertTrue(wf.runs(Role.EXECUTE))

    def test_the_active_roles_keep_their_order(self):
        self.assertEqual(
            _workflow(plan="").active_roles, [Role.EXECUTE, Role.REVIEW]
        )
        self.assertEqual(
            _workflow(review="").active_roles, [Role.PLAN, Role.EXECUTE]
        )

    def test_a_skipped_role_survives_a_round_trip(self):
        wf = Workflow.from_dict(_workflow(review="").to_dict())
        self.assertEqual(wf.agent_for(Role.REVIEW), "")
        self.assertFalse(wf.runs(Role.REVIEW))


class TheSetting(unittest.TestCase):
    def test_the_words_people_type_all_mean_off(self):
        for word in ("off", "OFF", "none", "no", "skip", "-"):
            self.assertEqual(settings_mod.coerce("plan", word), "off", word)
            self.assertEqual(settings_mod.coerce("review", word), "off", word)

    def test_a_backend_name_is_left_alone(self):
        self.assertEqual(settings_mod.coerce("plan", "claude"), "claude")

    def test_execute_cannot_be_turned_off(self):
        with self.assertRaises(settings_mod.SettingsError) as caught:
            settings_mod.coerce("execute", "off")
        self.assertIn("cannot be skipped", str(caught.exception))

    def test_off_is_not_the_same_as_unset(self):
        """Blank means "pick one for me", which is the opposite of skipping."""
        self.assertFalse(settings_mod.is_off(""))


class ResolvingAgents(TaskStartTestCase):
    def test_a_flag_turns_a_step_off_for_one_run(self):
        self.do("add rate limiting --plan off")
        self.assertEqual(self.started().agents["plan"], "")

    def test_a_setting_turns_a_step_off_for_every_run(self):
        self.session.settings["review"] = "off"
        self.do("add rate limiting")
        self.assertEqual(self.started().agents["review"], "")

    def test_a_flag_can_put_a_step_back(self):
        self.session.settings["plan"] = "off"
        self.do("add rate limiting --plan claude")
        self.assertEqual(self.started().agents["plan"], "claude")

    def test_the_execute_step_cannot_be_skipped(self):
        out = self.do("add rate limiting --execute off")
        self.assertIn("cannot be skipped", out)
        self.session.runner.start.assert_not_called()

    def test_a_skipped_step_carries_no_model(self):
        self.session.settings["plan_model"] = "opus"
        self.do("add rate limiting --plan off")
        self.assertEqual(self.started().models.get("plan", ""), "")

    def test_the_confirmation_shows_only_the_steps_that_will_run(self):
        out = self.do("add rate limiting --plan off")
        self.assertNotIn("plan", out)
        for cell in ("execute", "review", "merge"):
            self.assertIn(cell, out)

    def test_skipping_is_saved_as_off_not_as_blank(self):
        plan = {
            "agents": {"plan": "", "execute": "opencode", "review": "codex"},
            "models": {}, "mode": "supervised", "rounds": 2, "attempts": 3,
        }
        commands._save_as_defaults(self.session, plan)
        self.assertEqual(self.session.settings["plan"], "off")
        self.assertEqual(self.session.settings["execute"], "opencode")


class WithoutAPlanStep(RunnerTestCase):
    def make(self, **kwargs):
        wf = super().make(**kwargs)
        wf.agents["plan"] = ""
        return wf

    def test_no_planning_agent_is_invoked(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertEqual(
            [s.role for s in wf.steps], [Role.EXECUTE, Role.REVIEW]
        )
        self.assertNotIn(Role.PLAN, [role for role, _ in self.calls])

    def test_the_run_still_reaches_the_merge(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertIs(wf.status, Status.MERGED)
        self.git.merge.assert_called_once()

    def test_the_execute_agent_is_told_there_is_no_plan(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        prompt = next(p for role, p in self.calls if role is Role.EXECUTE)
        self.assertIn("no planning step", prompt)
        self.assertIn("add a health check", prompt)

    def test_the_reviewer_still_sees_the_task(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        prompt = next(p for role, p in self.calls if role is Role.REVIEW)
        self.assertIn("no planning step", prompt)
        self.assertIn("add a health check", prompt)


class WithoutAReviewStep(RunnerTestCase):
    def make(self, **kwargs):
        wf = super().make(**kwargs)
        wf.agents["review"] = ""
        return wf

    def test_no_review_agent_is_invoked(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertEqual([s.role for s in wf.steps], [Role.PLAN, Role.EXECUTE])
        self.assertIsNone(wf.verdict)

    def test_an_autonomous_run_merges_straight_after_the_work(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertIs(wf.status, Status.MERGED)
        self.git.merge.assert_called_once()

    def test_a_supervised_run_still_asks_before_merging(self):
        wf = self.make(mode=Mode.SUPERVISED)
        self.drive(wf, self.fake_agent())

        self.assertIs(wf.status, Status.AWAITING_APPROVAL)
        self.assertIn("committed", wf.message)
        self.assertNotIn("reviewer", wf.message)
        self.git.merge.assert_not_called()

    def test_the_work_is_never_sent_back(self):
        """With nobody to reject it, one execution pass is the whole run."""
        wf = self.make(mode=Mode.AUTONOMOUS, max_rounds=3)
        self.drive(wf, self.fake_agent())

        self.assertEqual(wf.round, 1)
        self.assertEqual(
            sum(1 for s in wf.steps if s.role is Role.EXECUTE),
            len(wf.tasks) or 1,
        )


class WithOnlyTheExecuteStep(RunnerTestCase):
    def make(self, **kwargs):
        wf = super().make(**kwargs)
        wf.agents["plan"] = ""
        wf.agents["review"] = ""
        return wf

    def test_the_run_is_one_agent_doing_the_work(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        self.assertEqual([s.role for s in wf.steps], [Role.EXECUTE])
        self.assertIs(wf.status, Status.MERGED)

    def test_the_agent_is_not_told_it_is_one_of_three(self):
        wf = self.make(mode=Mode.AUTONOMOUS)
        self.drive(wf, self.fake_agent())

        prompt = next(p for role, p in self.calls if role is Role.EXECUTE)
        self.assertIn("single-agent", prompt)
        self.assertNotIn("three-agent", prompt)


class ThePrompts(unittest.TestCase):
    def test_the_framing_counts_the_steps_that_will_run(self):
        self.assertEqual(prompts.framing(_workflow()), "a three-agent")
        self.assertEqual(prompts.framing(_workflow(plan="")), "a two-agent")
        self.assertEqual(
            prompts.framing(_workflow(plan="", review="")), "a single-agent"
        )

    def test_a_plan_is_quoted_when_there_is_one(self):
        wf = _workflow()
        wf.plan_text = "STEP ONE: do the thing"
        self.assertIn("STEP ONE", prompts.execute_prompt(wf))

    def test_the_conventions_still_reach_a_planless_run(self):
        wf = _workflow(plan="")
        for text in (prompts.execute_prompt(wf),
                     prompts.review_prompt(wf, "diff --git a/x b/x")):
            self.assertIn("CLAUDE.md", text)
            self.assertIn("skills", text)


class TheDiagram(unittest.TestCase):
    def diagram(self, mode="supervised", **agents):
        filled = {"plan": "claude", "execute": "opencode", "review": "codex"}
        filled.update(agents)
        return draw(render.workflow_diagram(filled, mode))

    def test_every_step_gets_a_cell_with_its_agent(self):
        out = self.diagram()
        for cell in ("plan", "execute", "review", "merge"):
            self.assertIn(cell, out)
        for agent in ("claude", "opencode", "codex"):
            self.assertIn(agent, out)

    def test_a_skipped_step_is_left_out_entirely(self):
        out = self.diagram(plan="")
        self.assertNotIn("plan", out)
        self.assertNotIn("claude", out)
        self.assertIn("execute", out)

    def test_the_merge_says_who_decides(self):
        self.assertIn("you approve", self.diagram(mode="supervised"))
        self.assertIn("automatic", self.diagram(mode="autonomous"))

    def test_the_cells_are_drawn_in_order(self):
        out = self.diagram()
        positions = [out.index(cell)
                     for cell in ("plan", "execute", "review", "merge")]
        self.assertEqual(positions, sorted(positions))

    def test_a_narrow_terminal_gets_one_line_instead_of_boxes(self):
        agents = {"plan": "claude", "execute": "opencode", "review": "codex"}
        line = draw(render.workflow_diagram(agents, "supervised", term_width=30))
        self.assertEqual(len(line.strip().splitlines()), 1)
        for cell in ("plan", "execute", "review", "merge"):
            self.assertIn(cell, line)


if __name__ == "__main__":
    unittest.main()
