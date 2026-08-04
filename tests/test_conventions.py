"""Agents must work the project's way, not this tool's.

An agent arrives with skills, commands and repository instructions of its own.
A bare "you are the PLANNING agent" framing makes it ignore all of that, so
every prompt now says plainly that the project's conventions win.
"""

import unittest

from agent_orchestrator import prompts
from agent_orchestrator.models import Status, Task, Workflow


def make(prompt="add rate limiting", instructions="", **kwargs):
    wf = Workflow(
        prompt=prompt, repo_path="/repo", base_branch="main",
        agents={"plan": "claude", "execute": "opencode", "review": "codex"},
        instructions=instructions, **kwargs,
    )
    wf.branch = "feature/thing"
    wf.plan_text = "the plan"
    return wf


def every_prompt(wf: Workflow) -> dict[str, str]:
    """Each prompt an agent can receive, keyed by the step it belongs to."""
    wf.tasks = wf.tasks or [Task(1, "first item")]
    return {
        "plan": prompts.plan_prompt(wf),
        "execute": prompts.execute_prompt(wf),
        "task": prompts.task_prompt(wf, wf.tasks[0]),
        "continue": prompts.continue_prompt(wf, "first item", "clean"),
        "review": prompts.review_prompt(wf, "diff --git a/x b/x"),
    }


class SkillsAreInvited(unittest.TestCase):
    def test_every_prompt_tells_the_agent_to_use_its_skills(self):
        for step, text in every_prompt(make()).items():
            self.assertIn("skills", text, f"{step} prompt never mentions skills")

    def test_every_prompt_points_at_the_repository_instructions(self):
        for step, text in every_prompt(make()).items():
            self.assertIn("CLAUDE.md", text, step)
            self.assertIn("AGENTS.md", text, step)
            self.assertIn("docs/", text, step)

    def test_the_repository_outranks_this_tool(self):
        text = prompts.plan_prompt(make())
        self.assertIn("outrank", text)

    def test_the_agent_is_told_to_open_skills_not_recall_them(self):
        """Naming a skill is not enough; it has to read the body.

        With a softer instruction an agent will cite a skill from its
        description alone. Telling it to open and read the skill is what
        actually got the rules out of the file.
        """
        text = prompts.plan_prompt(make())
        self.assertIn("invoke every one that does", text)
        self.assertIn("read them", text)
        self.assertIn("do not work from", text)

    def test_the_agent_must_report_what_it_used(self):
        for step, text in every_prompt(make()).items():
            self.assertIn("Say which skills", text, step)

    def test_planning_is_still_read_only(self):
        self.assertIn("Do not modify any files", prompts.plan_prompt(make()))
        self.assertIn("Do not modify any files", prompts.review_prompt(make(), "d"))

    def test_the_orchestrator_keeps_the_git_side(self):
        """A workflow skill must not start branching or opening PRs itself."""
        for step in ("execute", "task", "continue"):
            text = every_prompt(make())[step]
            self.assertIn("do not create branches", text.lower(), step)
            self.assertIn("pull request", text.lower(), step)

    def test_the_old_blanket_ban_on_editing_is_gone_from_execute(self):
        text = prompts.execute_prompt(make())
        self.assertNotIn("Do not modify any files", text)


class UseCases(unittest.TestCase):
    def test_common_spellings_are_recognised(self):
        for text, expected in [
            ("Implement UC-03", "UC-03"),
            ("implement uc-3", "UC-03"),
            ("Start UC 11", "UC-11"),
            ("let's do use case 7", "UC-07"),
            ("implement use-case 12 now", "UC-12"),
            ("Implement use cases number 5", "UC-05"),
        ]:
            self.assertEqual(prompts.use_case_reference(text), expected, text)

    def test_an_ordinary_task_names_no_use_case(self):
        for text in ("add rate limiting", "fix the login bug", "", None,
                     "refactor the UseCaseRunner class"):
            self.assertEqual(prompts.use_case_reference(text), "")

    def test_a_use_case_task_points_at_the_project_workflow(self):
        for step, text in every_prompt(make("Implement UC-11")).items():
            self.assertIn("UC-11", text, step)
            self.assertIn("use case workflow", text, step)

    def test_an_ordinary_task_says_nothing_about_use_cases(self):
        self.assertNotIn("use case workflow", prompts.plan_prompt(make()))


class StandingInstructions(unittest.TestCase):
    def test_they_reach_every_prompt(self):
        wf = make(instructions="Always run dotnet format before committing.")
        for step, text in every_prompt(wf).items():
            self.assertIn("dotnet format", text, step)

    def test_nothing_is_added_when_there_are_none(self):
        self.assertNotIn("Standing instructions", prompts.plan_prompt(make()))

    def test_they_are_recorded_on_the_workflow(self):
        wf = make(instructions="house rules")
        self.assertEqual(Workflow.from_dict(wf.to_dict()).instructions, "house rules")

    def test_a_workflow_from_before_this_existed_still_loads(self):
        data = make().to_dict()
        del data["instructions"]
        self.assertEqual(Workflow.from_dict(data).instructions, "")


class Settings(unittest.TestCase):
    def test_instructions_are_an_editable_setting(self):
        from agent_orchestrator import settings as settings_mod
        self.assertIn("instructions", settings_mod.EDITABLE)
        self.assertIn("instructions", settings_mod.DEFAULTS)

    def test_a_new_run_carries_the_configured_instructions(self):
        from agent_orchestrator import settings as settings_mod
        self.assertEqual(settings_mod.coerce("instructions", " be careful "),
                         "be careful")


if __name__ == "__main__":
    unittest.main()
