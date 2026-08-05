"""The click entry point.

Every slash command has a subcommand here, so the same things are reachable
from a script as from the shell.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from agent_orchestrator import cli
from agent_orchestrator.models import Status, Workflow
from agent_orchestrator.store import Store


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old = os.environ.get("AIORCH_HOME")
        os.environ["AIORCH_HOME"] = str(self.root / "home")
        self.addCleanup(self.tmp.cleanup)
        self.runner = CliRunner()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("AIORCH_HOME", None)
        else:
            os.environ["AIORCH_HOME"] = self._old

    def invoke(self, *args):
        return self.runner.invoke(cli.cli, list(args), obj={})

    def add(self, status=Status.MERGED, prompt="a task"):
        wf = Workflow(
            prompt=prompt, repo_path=str(self.root), base_branch="main",
            agents={"plan": "claude", "execute": "claude", "review": "claude"},
        )
        wf.status = status
        wf.branch = "feature/thing"
        Store().add(wf)
        return wf


class Structure(CliTestCase):
    def test_help_lists_the_commands(self):
        result = self.invoke("--help")
        self.assertEqual(result.exit_code, 0)
        for name in ("new", "runs", "show", "logs", "watch", "approve",
                     "reject", "resume", "retry", "cancel", "pause", "rm", "clear",
                     "prompts", "backends", "agents", "model", "config", "set"):
            self.assertIn(name, result.output)

    def test_every_shell_command_has_a_subcommand(self):
        """Nothing reachable in the shell should be missing from the CLI."""
        from agent_orchestrator.shell.commands import TABLE

        handlers = {handler.__name__ for handler in TABLE.values()}
        covered = {
            "cmd_runs", "cmd_show", "cmd_logs", "cmd_watch", "cmd_progress",
            "cmd_pending", "cmd_approve", "cmd_reject", "cmd_resume",
            "cmd_retry", "cmd_cancel", "cmd_pause", "cmd_rm", "cmd_clear",
            "cmd_backends",
            "cmd_agents", "cmd_model", "cmd_set", "cmd_config", "cmd_prompts",
            "cmd_help",
        }
        self.assertEqual(handlers - covered, set())

    def test_version_is_reported(self):
        result = self.invoke("--version")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("orchestrator", result.output)

    def test_the_docstring_describes_the_tool(self):
        result = self.invoke("--help")
        self.assertIn("plans", result.output)
        self.assertIn("reviews", result.output)


class Listing(CliTestCase):
    def test_runs_with_none(self):
        result = self.invoke("runs")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("no runs yet", result.output)

    def test_runs_lists_them(self):
        self.add(prompt="add rate limiting")
        result = self.invoke("runs")
        self.assertIn("add rate limiting", result.output)

    def test_show_reports_a_bad_id_with_a_nonzero_exit(self):
        self.add()
        result = self.invoke("show", "zzzz")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no run starts with", result.output)

    def test_config_prints_the_defaults(self):
        result = self.invoke("config")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("mode", result.output)

    def test_backends_lists_them(self):
        result = self.invoke("backends")
        self.assertEqual(result.exit_code, 0)
        for name in ("claude", "codex", "opencode"):
            self.assertIn(name, result.output)


class Settings(CliTestCase):
    def test_set_changes_a_default(self):
        result = self.invoke("set", "mode", "autonomous")
        self.assertEqual(result.exit_code, 0)
        from agent_orchestrator import settings as settings_mod
        self.assertEqual(settings_mod.load()["mode"], "autonomous")

    def test_a_bad_setting_exits_nonzero(self):
        result = self.invoke("set", "nonsense", "1")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not a setting", result.output)


class NewRun(CliTestCase):
    """`new` reuses the shell's parser, so one grammar serves both."""

    def test_flags_are_translated_for_the_parser(self):
        with mock.patch.object(cli.cmd, "start_task") as start:
            result = self.invoke(
                "new", "add", "rate", "limiting",
                "--mode", "auto", "--execute", "codex", "--rounds", "4",
                "--model", "sonnet-4.6", "-y",
            )
        self.assertEqual(result.exit_code, 0)
        intent = start.call_args.args[1]
        self.assertEqual(intent.text, "add rate limiting")
        self.assertEqual(intent.overrides["mode"], "autonomous")
        self.assertEqual(intent.overrides["execute"], "codex")
        self.assertEqual(intent.overrides["rounds"], 4)
        self.assertEqual(intent.overrides["model"], "sonnet-4.6")

    def test_a_saved_prompt_reference_survives(self):
        with mock.patch.object(cli.cmd, "start_task") as start:
            self.invoke("new", "@scope-spec", "-y")
        self.assertEqual(start.call_args.args[1].ref, "scope-spec")

    def test_yes_skips_the_confirmation(self):
        with mock.patch.object(cli.cmd, "start_task") as start:
            self.invoke("new", "do it", "-y")
        session = start.call_args.args[0]
        self.assertFalse(session.settings["confirm"])

    def test_no_follow_is_honoured(self):
        with mock.patch.object(cli.cmd, "start_task") as start:
            self.invoke("new", "do it", "-y", "--no-follow")
        self.assertFalse(start.call_args.args[0].settings["follow"])

    def test_an_unknown_mode_is_refused_by_click(self):
        result = self.invoke("new", "do it", "--mode", "sideways")
        self.assertNotEqual(result.exit_code, 0)

    def test_a_failure_exits_nonzero(self):
        from agent_orchestrator.shell.commands import CommandError

        with mock.patch.object(cli.cmd, "start_task",
                               side_effect=CommandError("no git repo here")):
            result = self.invoke("new", "do it", "-y")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no git repo here", result.output)

    def test_task_text_is_required(self):
        self.assertNotEqual(self.invoke("new").exit_code, 0)


class Managing(CliTestCase):
    def test_resume_reaches_the_runner(self):
        wf = self.add(Status.FAILED)
        with mock.patch("agent_orchestrator.runner.Runner.resume") as resume:
            with mock.patch("agent_orchestrator.runner.Runner.is_running",
                            return_value=False):
                result = self.invoke("resume", wf.id[:4])
        self.assertEqual(result.exit_code, 0)
        resume.assert_called_once()

    def test_clear_rejects_an_unknown_scope(self):
        self.assertNotEqual(self.invoke("clear", "everything").exit_code, 0)

    def test_logs_rejects_an_unknown_step(self):
        self.assertNotEqual(self.invoke("logs", "abc", "compile").exit_code, 0)


if __name__ == "__main__":
    unittest.main()
