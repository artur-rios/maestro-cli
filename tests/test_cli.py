"""The click entry point.

Every slash command has a subcommand here, so the same things are reachable
from a script as from the shell.
"""

from pathlib import Path
from unittest import mock

import click
import pytest
from click.testing import CliRunner

from maestro import cli
from maestro import settings as settings_mod
from maestro.models import Status, Workflow
from maestro.shell.commands import TABLE, CommandError
from maestro.shell.promptlib import PromptError
from maestro.store import Store


@pytest.fixture
def invoke():
    runner = CliRunner()

    def run(*args):
        return runner.invoke(cli.cli, list(args), obj={})
    return run


@pytest.fixture
def add(tmp_path):
    def make(status=Status.MERGED, prompt="a task"):
        wf = Workflow(
            prompt=prompt, repo_path=str(tmp_path), base_branch="main",
            agents={"plan": "claude", "execute": "claude", "review": "claude"},
        )
        wf.status = status
        wf.branch = "feature/thing"
        Store().add(wf)
        return wf
    return make


class TestStructure:
    def test_given_help_when_invoked_then_every_command_is_listed(self, invoke):
        result = invoke("--help")
        assert result.exit_code == 0
        for name in ("new", "runs", "show", "logs", "watch", "approve",
                     "reject", "resume", "retry", "cancel", "pause", "rm", "clear",
                     "prompts", "backends", "agents", "model", "config", "set"):
            assert name in result.output

    def test_given_the_shell_commands_when_compared_then_each_has_a_subcommand(self):
        """Nothing reachable in the shell should be missing from the CLI."""
        handlers = {handler.__name__ for handler in TABLE.values()}
        covered = {
            "cmd_runs", "cmd_show", "cmd_logs", "cmd_watch", "cmd_progress",
            "cmd_pending", "cmd_approve", "cmd_reject", "cmd_resume",
            "cmd_retry", "cmd_cancel", "cmd_pause", "cmd_rm", "cmd_clear",
            "cmd_backends",
            "cmd_agents", "cmd_model", "cmd_set", "cmd_config", "cmd_prompts",
            "cmd_help",
        }
        assert handlers - covered == set()

    def test_given_version_when_invoked_then_it_is_reported(self, invoke):
        result = invoke("--version")
        assert result.exit_code == 0
        assert "maestro" in result.output

    def test_given_help_when_invoked_then_the_docstring_describes_the_tool(self, invoke):
        result = invoke("--help")
        assert "plans" in result.output
        assert "reviews" in result.output

    def test_given_no_subcommand_when_invoked_then_the_shell_opens_on_the_repo(self, invoke, tmp_path):
        with mock.patch.object(cli, "Shell") as shell:
            shell.return_value.run.return_value = 0
            result = invoke("--repo", str(tmp_path))
        assert result.exit_code == 0
        assert shell.call_args.kwargs["cwd"] == Path(tmp_path)
        shell.return_value.run.assert_called_once()


class TestListing:
    def test_given_no_runs_when_listed_then_it_says_so(self, invoke):
        result = invoke("runs")
        assert result.exit_code == 0
        assert "no runs yet" in result.output

    def test_given_a_run_when_listed_then_it_is_shown(self, invoke, add):
        add(prompt="add rate limiting")
        result = invoke("runs")
        assert "add rate limiting" in result.output

    def test_given_a_bad_id_when_shown_then_it_exits_nonzero(self, invoke, add):
        add()
        result = invoke("show", "zzzz")
        assert result.exit_code == 1
        assert "no run starts with" in result.output

    def test_given_config_when_invoked_then_the_defaults_are_printed(self, invoke):
        result = invoke("config")
        assert result.exit_code == 0
        assert "mode" in result.output

    def test_given_backends_when_invoked_then_the_builtins_are_listed(self, invoke):
        result = invoke("backends")
        assert result.exit_code == 0
        for name in ("claude", "codex", "opencode"):
            assert name in result.output


class TestSubcommandDispatch:
    @pytest.mark.parametrize("args, handler, passed", [
        (["runs"], "cmd_runs", []),
        (["show", "ab12"], "cmd_show", ["ab12"]),
        (["logs", "ab12", "plan"], "cmd_logs", ["ab12", "plan"]),
        (["logs", "ab12"], "cmd_logs", ["ab12"]),
        (["watch", "ab12"], "cmd_watch", ["ab12"]),
        (["watch", "ab12", "--progress"], "cmd_progress", ["ab12"]),
        (["pending"], "cmd_pending", []),
        (["approve", "ab12"], "cmd_approve", ["ab12"]),
        (["reject", "ab12", "not", "this"], "cmd_reject", ["ab12", "not", "this"]),
        (["resume", "ab12"], "cmd_resume", ["ab12"]),
        (["retry", "ab12"], "cmd_retry", ["ab12"]),
        (["pause", "ab12"], "cmd_pause", ["ab12"]),
        (["cancel", "ab12"], "cmd_cancel", ["ab12"]),
        (["rm", "ab12"], "cmd_rm", ["ab12"]),
        (["clear"], "cmd_clear", ["finished"]),
        (["clear", "all"], "cmd_clear", ["all"]),
        (["prompts", "add", "/some/dir"], "cmd_prompts", ["add", "/some/dir"]),
        (["backends"], "cmd_backends", []),
        (["agents", "opencode", "build", "execute"], "cmd_agents", ["opencode", "build", "execute"]),
        (["model", "opencode", "x/y"], "cmd_model", ["opencode", "x/y"]),
        (["config"], "cmd_config", []),
        (["set", "mode", "auto"], "cmd_set", ["mode", "auto"]),
    ])
    def test_given_a_subcommand_when_invoked_then_its_shell_handler_gets_the_non_blank_arguments(
            self, invoke, args, handler, passed):
        with mock.patch.object(cli.cmd, handler) as fake:
            fake.__name__ = handler
            result = invoke(*args)
        assert result.exit_code == 0, result.output
        assert fake.call_args.args[1].args == passed

    @pytest.mark.parametrize("error", [CommandError("bad run"), PromptError("bad prompt")])
    def test_given_a_handler_that_refuses_when_invoked_then_it_exits_one_with_the_message(self, invoke, error):
        with mock.patch.object(cli.cmd, "cmd_runs", side_effect=error) as fake:
            fake.__name__ = "cmd_runs"
            result = invoke("runs")
        assert result.exit_code == 1
        assert str(error) in result.output

    def test_given_ctrl_c_in_a_handler_when_invoked_then_it_exits_130(self, invoke):
        with mock.patch.object(cli.cmd, "cmd_runs", side_effect=KeyboardInterrupt) as fake:
            fake.__name__ = "cmd_runs"
            result = invoke("runs")
        assert result.exit_code == 130
        assert "cancelled" in result.output

    @pytest.mark.parametrize("args", [["clear", "everything"], ["logs", "abc", "compile"], ["set", "mode"]])
    def test_given_an_argument_click_rejects_when_invoked_then_it_exits_nonzero(self, invoke, args):
        assert invoke(*args).exit_code != 0


class TestSettings:
    def test_given_a_valid_setting_when_set_then_the_default_changes(self, invoke):
        result = invoke("set", "mode", "autonomous")
        assert result.exit_code == 0
        assert settings_mod.load()["mode"] == "autonomous"

    def test_given_an_unknown_setting_when_set_then_it_exits_nonzero(self, invoke):
        result = invoke("set", "nonsense", "1")
        assert result.exit_code == 1
        assert "not a setting" in result.output


class TestNewRun:
    """`new` reuses the shell's parser, so one grammar serves both."""

    @pytest.fixture
    def start(self):
        with mock.patch.object(cli.cmd, "start_task") as start:
            yield start

    def test_given_click_flags_when_a_run_starts_then_they_are_translated_for_the_parser(self, invoke, start):
        result = invoke(
            "new", "add", "rate", "limiting",
            "--mode", "auto", "--execute", "codex", "--rounds", "4",
            "--model", "sonnet-4.6", "--plan-model", "opus", "-y",
        )
        assert result.exit_code == 0
        intent = start.call_args.args[1]
        assert intent.text == "add rate limiting"
        assert intent.overrides == {
            "mode": "autonomous", "execute": "codex", "rounds": 4,
            "model": "sonnet-4.6", "plan-model": "opus",
        }

    def test_given_a_saved_prompt_reference_when_a_run_starts_then_it_survives(self, invoke, start):
        invoke("new", "@scope-spec", "-y")
        assert start.call_args.args[1].ref == "scope-spec"

    def test_given_yes_when_a_run_starts_then_the_confirmation_is_skipped(self, invoke, start):
        invoke("new", "do it", "-y")
        session = start.call_args.args[0]
        assert not session.settings["confirm"]

    def test_given_no_follow_when_a_run_starts_then_it_is_honoured(self, invoke, start):
        invoke("new", "do it", "-y", "--no-follow")
        assert not start.call_args.args[0].settings["follow"]

    def test_given_neither_yes_nor_follow_when_a_run_starts_then_the_saved_settings_apply(self, invoke, start):
        invoke("new", "do it")
        settings = start.call_args.args[0].settings
        assert settings["confirm"] == settings_mod.DEFAULTS["confirm"]
        assert settings["follow"] == settings_mod.DEFAULTS["follow"]

    def test_given_an_unknown_mode_when_a_run_starts_then_click_refuses_it(self, invoke, start):
        assert invoke("new", "do it", "--mode", "sideways").exit_code != 0
        start.assert_not_called()

    def test_given_text_the_parser_rejects_when_a_run_starts_then_it_exits_one(self, invoke, start):
        result = invoke("new", "--", "do it --modes auto")
        assert result.exit_code == 1
        assert "--modes" in result.output
        start.assert_not_called()

    @pytest.mark.parametrize("error", [CommandError("no git repo here"), PromptError("no such prompt")])
    def test_given_a_start_that_fails_when_a_run_starts_then_it_exits_one(self, invoke, start, error):
        start.side_effect = error
        result = invoke("new", "do it", "-y")
        assert result.exit_code == 1
        assert str(error) in result.output

    def test_given_ctrl_c_while_starting_when_a_run_starts_then_it_exits_130(self, invoke, start):
        start.side_effect = KeyboardInterrupt
        assert invoke("new", "do it", "-y").exit_code == 130

    def test_given_no_task_text_when_a_run_starts_then_it_is_refused(self, invoke):
        assert invoke("new").exit_code != 0


class TestManaging:
    def test_given_a_failed_run_when_resumed_then_the_runner_is_asked(self, invoke, add):
        wf = add(Status.FAILED)
        with mock.patch("maestro.runner.Runner.resume") as resume:
            with mock.patch("maestro.runner.Runner.is_running", return_value=False):
                result = invoke("resume", wf.id[:4])
        assert result.exit_code == 0
        resume.assert_called_once()


class TestMain:
    def test_given_a_command_that_succeeds_when_main_runs_then_it_returns_zero(self):
        with mock.patch.object(cli, "cli", return_value=None):
            assert cli.main() == 0

    def test_given_a_click_usage_error_when_main_runs_then_its_exit_code_is_returned(self, capsys):
        with mock.patch.object(cli, "cli", side_effect=click.UsageError("no such option")):
            assert cli.main() == 2
        assert "no such option" in capsys.readouterr().out

    @pytest.mark.parametrize("error", [click.exceptions.Abort(), KeyboardInterrupt()])
    def test_given_an_abort_when_main_runs_then_it_returns_130(self, error):
        with mock.patch.object(cli, "cli", side_effect=error):
            assert cli.main() == 130
