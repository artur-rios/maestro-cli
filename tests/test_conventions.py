"""Agents must work the project's way, not this tool's.

An agent arrives with skills, commands and repository instructions of its own.
A bare "you are the PLANNING agent" framing makes it ignore all of that, so
every prompt now says plainly that the project's conventions win.
"""


from maestro import prompts
from maestro.models import Task, Workflow


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


class TestSkillsAreInvited:
    def test_given_any_step_when_its_prompt_is_built_then_it_tells_the_agent_to_use_its_skills(self):
        for step, text in every_prompt(make()).items():
            assert "skills" in text, f"{step} prompt never mentions skills"

    def test_given_any_step_when_its_prompt_is_built_then_it_points_at_the_repository_instructions(self):
        for step, text in every_prompt(make()).items():
            assert "CLAUDE.md" in text, step
            assert "AGENTS.md" in text, step
            assert "docs/" in text, step

    def test_given_the_plan_prompt_when_built_then_the_repository_outranks_this_tool(self):
        text = prompts.plan_prompt(make())
        assert "outrank" in text

    def test_given_the_plan_prompt_when_built_then_the_agent_is_told_to_open_skills_not_recall_them(self):
        """Naming a skill is not enough; it has to read the body.

        With a softer instruction an agent will cite a skill from its
        description alone. Telling it to open and read the skill is what
        actually got the rules out of the file.
        """
        text = prompts.plan_prompt(make())
        assert "invoke every one that does" in text
        assert "read them" in text
        assert "do not work from" in text

    def test_given_any_step_when_its_prompt_is_built_then_the_agent_must_report_the_skills_it_used(self):
        for step, text in every_prompt(make()).items():
            assert "Say which skills" in text, step

    def test_given_plan_and_review_prompts_when_built_then_they_stay_read_only(self):
        assert "Do not modify any files" in prompts.plan_prompt(make())
        assert "Do not modify any files" in prompts.review_prompt(make(), "d")

    def test_given_a_working_step_when_its_prompt_is_built_then_maestro_keeps_the_git_side(self):
        """A workflow skill must not start branching or opening PRs itself."""
        for step in ("execute", "task", "continue"):
            text = every_prompt(make())[step]
            assert "do not create branches" in text.lower(), step
            assert "pull request" in text.lower(), step

    def test_given_the_execute_prompt_when_built_then_it_does_not_ban_editing(self):
        text = prompts.execute_prompt(make())
        assert "Do not modify any files" not in text


class TestUseCases:
    def test_given_a_common_spelling_of_a_use_case_when_read_then_it_is_recognised(self):
        for text, expected in [
            ("Implement UC-03", "UC-03"),
            ("implement uc-3", "UC-03"),
            ("Start UC 11", "UC-11"),
            ("let's do use case 7", "UC-07"),
            ("implement use-case 12 now", "UC-12"),
            ("Implement use cases number 5", "UC-05"),
        ]:
            assert prompts.use_case_reference(text) == expected, text

    def test_given_an_ordinary_task_when_read_then_it_names_no_use_case(self):
        for text in ("add rate limiting", "fix the login bug", "", None,
                     "refactor the UseCaseRunner class"):
            assert prompts.use_case_reference(text) == ""

    def test_given_a_use_case_task_when_prompts_are_built_then_they_point_at_the_project_workflow(self):
        for step, text in every_prompt(make("Implement UC-11")).items():
            assert "UC-11" in text, step
            assert "use case workflow" in text, step

    def test_given_an_ordinary_task_when_prompts_are_built_then_they_say_nothing_about_use_cases(self):
        assert "use case workflow" not in prompts.plan_prompt(make())


class TestStandingInstructions:
    def test_given_standing_instructions_when_prompts_are_built_then_every_prompt_carries_them(self):
        wf = make(instructions="Always run dotnet format before committing.")
        for step, text in every_prompt(wf).items():
            assert "dotnet format" in text, step

    def test_given_no_standing_instructions_when_prompts_are_built_then_no_section_is_added(self):
        assert "Standing instructions" not in prompts.plan_prompt(make())

    def test_given_standing_instructions_when_the_workflow_round_trips_then_they_are_kept(self):
        wf = make(instructions="house rules")
        assert Workflow.from_dict(wf.to_dict()).instructions == "house rules"

    def test_given_a_workflow_from_before_instructions_existed_when_loaded_then_they_are_blank(self):
        data = make().to_dict()
        del data["instructions"]
        assert Workflow.from_dict(data).instructions == ""


class TestSettings:
    def test_given_the_settings_when_listed_then_instructions_are_editable_with_a_default(self):
        from maestro import settings as settings_mod
        assert "instructions" in settings_mod.EDITABLE
        assert "instructions" in settings_mod.DEFAULTS

    def test_given_instructions_with_padding_when_coerced_then_they_are_stripped(self):
        from maestro import settings as settings_mod
        assert settings_mod.coerce("instructions", " be careful ") == "be careful"
