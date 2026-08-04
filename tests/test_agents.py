"""Choosing which of a backend's bundled agents runs each role."""

import subprocess
import unittest
from unittest import mock

from agent_orchestrator import backends, config
from agent_orchestrator.models import Role

AGENT_LIST_OUTPUT = """\
build (primary)
  [
  {
    "permission": "*",
    "action": "allow"
  }
  ]
compaction (primary)
explore (subagent)
general (subagent)
plan (primary)
summary (primary)
title (primary)
"""


class Listing(unittest.TestCase):
    BACKEND = {"bin": "opencode", "agents_command": ["agent", "list"]}

    def list_with(self, stdout, returncode=0):
        completed = subprocess.CompletedProcess([], returncode, stdout, "")
        with mock.patch("agent_orchestrator.config.find_binary", return_value="/bin/opencode"):
            with mock.patch("subprocess.run", return_value=completed):
                return backends.list_agents(self.BACKEND)

    def test_parses_names_and_kinds(self):
        self.assertEqual(
            self.list_with(AGENT_LIST_OUTPUT),
            [("build", "primary"), ("compaction", "primary"), ("explore", "subagent"),
             ("general", "subagent"), ("plan", "primary"), ("summary", "primary"),
             ("title", "primary")],
        )

    def test_json_noise_is_ignored(self):
        names = [n for n, _ in self.list_with(AGENT_LIST_OUTPUT)]
        self.assertNotIn("permission", names)
        self.assertNotIn("action", names)

    def test_a_backend_without_the_command_reports_nothing(self):
        self.assertEqual(backends.list_agents({"bin": "claude"}), [])

    def test_an_uninstalled_backend_reports_nothing(self):
        with mock.patch("agent_orchestrator.config.find_binary", return_value=None):
            self.assertEqual(backends.list_agents(self.BACKEND), [])

    def test_a_crashing_cli_reports_nothing_rather_than_raising(self):
        with mock.patch("agent_orchestrator.config.find_binary", return_value="/bin/x"):
            with mock.patch("subprocess.run", side_effect=OSError("boom")):
                self.assertEqual(backends.list_agents(self.BACKEND), [])

    def test_listing_agents_is_also_shielded_from_ctrl_c(self):
        completed = subprocess.CompletedProcess([], 0, AGENT_LIST_OUTPUT, "")
        with mock.patch("agent_orchestrator.config.find_binary", return_value="/bin/x"):
            with mock.patch("subprocess.run", return_value=completed) as runner:
                backends.list_agents(self.BACKEND)
        kwargs = runner.call_args.kwargs
        self.assertTrue(
            "creationflags" in kwargs or kwargs.get("start_new_session")
        )


class Selection(unittest.TestCase):
    def backend(self):
        return {
            "bin": "opencode",
            "agent_flag": "--agent",
            "args": ["run", "--dir", "{cwd}", "--agent", "plan"],
            "execute_args": ["run", "--dir", "{cwd}", "--agent", "build"],
        }

    def test_reads_the_current_agent_per_role(self):
        b = self.backend()
        self.assertEqual(backends.current_agent(b, Role.PLAN), "plan")
        self.assertEqual(backends.current_agent(b, Role.REVIEW), "plan")
        self.assertEqual(backends.current_agent(b, Role.EXECUTE), "build")

    def test_replaces_an_existing_choice_without_duplicating_the_flag(self):
        b = self.backend()
        backends.set_agent(b, "execute_args", "general")
        self.assertEqual(b["execute_args"], ["run", "--dir", "{cwd}", "--agent", "general"])
        self.assertEqual(b["execute_args"].count("--agent"), 1)

    def test_adds_the_flag_when_absent(self):
        b = {"bin": "opencode", "agent_flag": "--agent", "args": ["run"]}
        backends.set_agent(b, "args", "plan")
        self.assertEqual(b["args"], ["run", "--agent", "plan"])

    def test_backends_without_agent_support_are_untouched(self):
        b = {"bin": "claude", "args": ["-p"]}
        backends.set_agent(b, "args", "whatever")
        self.assertEqual(b["args"], ["-p"])
        self.assertIsNone(backends.current_agent(b, Role.PLAN))


class Models(unittest.TestCase):
    BACKEND = {"bin": "opencode", "models_command": ["models"], "model_flag": "--model"}

    def test_provider_slash_model_lines_are_parsed(self):
        out = "opencode/big-pickle\nopencode-go/glm-5.2\nanthropic/claude-opus-5\n"
        completed = subprocess.CompletedProcess([], 0, out, "")
        with mock.patch("agent_orchestrator.config.find_binary", return_value="/bin/x"):
            with mock.patch("subprocess.run", return_value=completed):
                models = backends.list_models(self.BACKEND)
        self.assertEqual(
            [m for m, _ in models],
            ["opencode/big-pickle", "opencode-go/glm-5.2", "anthropic/claude-opus-5"],
        )

    def test_model_is_set_and_replaced_per_role(self):
        b = dict(self.BACKEND, args=["run"], execute_args=["run"])
        backends.set_model(b, "execute_args", "opencode-go/glm-5.2")
        self.assertEqual(b["execute_args"], ["run", "--model", "opencode-go/glm-5.2"])
        self.assertEqual(
            backends.current_model(b, Role.EXECUTE), "opencode-go/glm-5.2"
        )

        backends.set_model(b, "execute_args", "opencode-go/grok-4.5")
        self.assertEqual(b["execute_args"].count("--model"), 1)
        self.assertEqual(backends.current_model(b, Role.EXECUTE), "opencode-go/grok-4.5")

    def test_agent_and_model_coexist(self):
        b = {"bin": "opencode", "agent_flag": "--agent", "model_flag": "--model",
             "execute_args": ["run", "--dir", "{cwd}"]}
        backends.set_agent(b, "execute_args", "build")
        backends.set_model(b, "execute_args", "opencode-go/glm-5.2")
        self.assertEqual(backends.current_agent(b, Role.EXECUTE), "build")
        self.assertEqual(backends.current_model(b, Role.EXECUTE), "opencode-go/glm-5.2")

    def test_every_builtin_can_take_a_model(self):
        for name, backend in config.DEFAULT_BACKENDS.items():
            self.assertTrue(backend.get("model_flag"), f"{name} has no model_flag")


class PerRunModel(unittest.TestCase):
    """A run may pick a model without editing anyone's settings."""

    def test_it_replaces_the_configured_one(self):
        backend = config.DEFAULT_BACKENDS["opencode"]
        argv = backends.build_argv(backend, Role.EXECUTE, "x", "/tree")
        argv = backends.with_model(argv, backend, "opencode-go/glm-5.2")
        self.assertEqual(argv.count("--model"), 1)
        self.assertIn("opencode-go/glm-5.2", argv)

    def test_it_is_added_when_none_was_configured(self):
        backend = config.DEFAULT_BACKENDS["claude"]
        argv = backends.build_argv(backend, Role.PLAN, "x", "/tree")
        self.assertNotIn("--model", argv)
        argv = backends.with_model(argv, backend, "opus-4.6")
        self.assertEqual(argv[-2:], ["--model", "opus-4.6"])

    def test_a_blank_model_changes_nothing(self):
        backend = config.DEFAULT_BACKENDS["codex"]
        argv = backends.build_argv(backend, Role.EXECUTE, "x", "/tree")
        self.assertEqual(backends.with_model(argv, backend, ""), argv)

    def test_a_backend_without_the_flag_is_untouched(self):
        backend = {"bin": "x", "args": ["run"]}
        argv = ["x", "run"]
        self.assertEqual(backends.with_model(argv, backend, "whatever"), argv)

    def test_all_three_backends_accept_one(self):
        for name in ("claude", "codex", "opencode"):
            backend = config.DEFAULT_BACKENDS[name]
            argv = backends.with_model(
                backends.build_argv(backend, Role.EXECUTE, "x", "/tree"),
                backend, "chosen-model",
            )
            self.assertIn("chosen-model", argv, name)


class Defaults(unittest.TestCase):
    def test_opencode_reads_with_plan_and_writes_with_build(self):
        """Its read-only agent cannot implement anything - that split matters."""
        opencode = config.DEFAULT_BACKENDS["opencode"]
        self.assertEqual(backends.current_agent(opencode, Role.PLAN), "plan")
        self.assertEqual(backends.current_agent(opencode, Role.REVIEW), "plan")
        self.assertEqual(backends.current_agent(opencode, Role.EXECUTE), "build")

    def test_opencode_advertises_how_to_list_its_agents(self):
        self.assertEqual(
            config.DEFAULT_BACKENDS["opencode"]["agents_command"], ["agent", "list"]
        )


if __name__ == "__main__":
    unittest.main()
