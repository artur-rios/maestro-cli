"""Choosing which of a backend's bundled agents and models runs each role."""

import subprocess
from unittest import mock

import pytest

from maestro import backends, config
from maestro.models import Role

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

OPENCODE = {"bin": "opencode", "agents_command": ["agent", "list"], "models_command": ["models"]}


@pytest.fixture
def cli_output(monkeypatch):
    """Make the backend's binary present and its list command print `stdout`."""
    monkeypatch.setattr("maestro.config.find_binary", lambda backend: "/bin/opencode")

    def set_output(stdout="", side_effect=None):
        completed = subprocess.CompletedProcess([], 0, stdout, "")
        runner = mock.Mock(return_value=completed, side_effect=side_effect)
        monkeypatch.setattr("subprocess.run", runner)
        return runner
    return set_output


class TestListing:
    def test_given_an_agent_list_when_parsed_then_names_and_kinds_are_read(self, cli_output):
        cli_output(AGENT_LIST_OUTPUT)
        assert backends.list_agents(OPENCODE) == [
            ("build", "primary"), ("compaction", "primary"), ("explore", "subagent"),
            ("general", "subagent"), ("plan", "primary"), ("summary", "primary"),
            ("title", "primary"),
        ]

    def test_given_json_noise_in_the_list_when_parsed_then_it_is_ignored(self, cli_output):
        cli_output(AGENT_LIST_OUTPUT)
        names = [n for n, _ in backends.list_agents(OPENCODE)]
        assert "permission" not in names
        assert "action" not in names

    def test_given_a_name_listed_twice_when_parsed_then_it_appears_once(self, cli_output):
        cli_output("build (primary)\nbuild (subagent)\nplan\n")
        assert backends.list_agents(OPENCODE) == [("build", "primary"), ("plan", "")]

    def test_given_a_backend_without_the_command_when_listed_then_nothing_is_reported(self):
        assert backends.list_agents({"bin": "claude"}) == []

    def test_given_an_uninstalled_backend_when_listed_then_nothing_is_reported(self, monkeypatch):
        monkeypatch.setattr("maestro.config.find_binary", lambda backend: None)
        assert backends.list_agents(OPENCODE) == []

    @pytest.mark.parametrize("error", [OSError("boom"), subprocess.TimeoutExpired("x", 60)])
    def test_given_a_cli_that_crashes_when_listed_then_nothing_is_reported_rather_than_raising(
            self, cli_output, error):
        cli_output(side_effect=error)
        assert backends.list_agents(OPENCODE) == []

    def test_given_a_listing_when_the_cli_runs_then_it_is_shielded_from_ctrl_c(self, cli_output):
        runner = cli_output(AGENT_LIST_OUTPUT)
        backends.list_agents(OPENCODE)
        kwargs = runner.call_args.kwargs
        assert "creationflags" in kwargs or kwargs.get("start_new_session")
        assert kwargs["stdin"] == subprocess.DEVNULL


class TestAgentSelection:
    def backend(self):
        return {
            "bin": "opencode",
            "agent_flag": "--agent",
            "args": ["run", "--dir", "{cwd}", "--agent", "plan"],
            "execute_args": ["run", "--dir", "{cwd}", "--agent", "build"],
        }

    def test_given_per_role_args_when_the_agent_is_read_then_each_role_gets_its_own(self):
        b = self.backend()
        assert backends.current_agent(b, Role.PLAN) == "plan"
        assert backends.current_agent(b, Role.REVIEW) == "plan"
        assert backends.current_agent(b, Role.EXECUTE) == "build"

    def test_given_an_existing_choice_when_set_then_it_is_replaced_without_duplicating_the_flag(self):
        b = self.backend()
        backends.set_agent(b, "execute_args", "general")
        assert b["execute_args"] == ["run", "--dir", "{cwd}", "--agent", "general"]
        assert b["execute_args"].count("--agent") == 1

    def test_given_no_choice_yet_when_set_then_the_flag_is_added(self):
        b = {"bin": "opencode", "agent_flag": "--agent", "args": ["run"]}
        backends.set_agent(b, "args", "plan")
        assert b["args"] == ["run", "--agent", "plan"]

    def test_given_a_flag_with_no_value_after_it_when_read_then_there_is_no_agent(self):
        b = {"bin": "opencode", "agent_flag": "--agent", "args": ["run", "--agent"]}
        assert backends.current_agent(b, Role.PLAN) is None

    def test_given_a_backend_without_agent_support_when_set_then_it_is_untouched(self):
        b = {"bin": "claude", "args": ["-p"]}
        backends.set_agent(b, "args", "whatever")
        assert b["args"] == ["-p"]
        assert backends.current_agent(b, Role.PLAN) is None


class TestModels:
    BACKEND = {"bin": "opencode", "models_command": ["models"], "model_flag": "--model"}

    def test_given_provider_slash_model_lines_when_listed_then_they_are_parsed(self, cli_output):
        cli_output("opencode/big-pickle\nopencode-go/glm-5.2\nanthropic/claude-opus-5\n")
        assert [m for m, _ in backends.list_models(self.BACKEND)] == [
            "opencode/big-pickle", "opencode-go/glm-5.2", "anthropic/claude-opus-5"]

    def test_given_a_cli_that_lists_models_when_choices_are_asked_for_then_its_list_wins(self, cli_output):
        cli_output("opencode/big-pickle\n")
        backend = dict(self.BACKEND, known_models=["configured"])
        assert backends.model_choices(backend) == ["opencode/big-pickle"]

    def test_given_a_cli_without_a_list_when_choices_are_asked_for_then_known_models_are_offered(self):
        backend = {"bin": "claude", "model_flag": "--model", "known_models": ["opus", "", "sonnet"]}
        assert backends.model_choices(backend) == ["opus", "sonnet"]

    def test_given_neither_a_list_nor_known_models_when_choices_are_asked_for_then_there_are_none(self):
        assert backends.model_choices({"bin": "x"}) == []

    def test_given_a_model_when_set_per_role_then_it_is_added_and_later_replaced(self):
        b = dict(self.BACKEND, args=["run"], execute_args=["run"])
        backends.set_model(b, "execute_args", "opencode-go/glm-5.2")
        assert b["execute_args"] == ["run", "--model", "opencode-go/glm-5.2"]
        assert backends.current_model(b, Role.EXECUTE) == "opencode-go/glm-5.2"
        assert backends.current_model(b, Role.PLAN) is None

        backends.set_model(b, "execute_args", "opencode-go/grok-4.5")
        assert b["execute_args"].count("--model") == 1
        assert backends.current_model(b, Role.EXECUTE) == "opencode-go/grok-4.5"

    def test_given_an_agent_and_a_model_when_both_are_set_then_they_coexist(self):
        b = {"bin": "opencode", "agent_flag": "--agent", "model_flag": "--model",
             "execute_args": ["run", "--dir", "{cwd}"]}
        backends.set_agent(b, "execute_args", "build")
        backends.set_model(b, "execute_args", "opencode-go/glm-5.2")
        assert backends.current_agent(b, Role.EXECUTE) == "build"
        assert backends.current_model(b, Role.EXECUTE) == "opencode-go/glm-5.2"

    def test_given_the_builtins_when_checked_then_each_can_take_a_model(self):
        for name, backend in config.DEFAULT_BACKENDS.items():
            assert backend.get("model_flag"), f"{name} has no model_flag"


class TestPerRunModel:
    """A run may pick a model without editing anyone's settings."""

    def test_given_a_configured_model_when_a_run_picks_another_then_it_replaces_the_configured_one(self):
        backend = {"bin": "x", "model_flag": "--model", "args": ["run", "--model", "old", "--fast"]}
        argv = backends.build_argv(backend, Role.PLAN, "x", "/tree")
        assert backends.with_model(argv, backend, "new") == ["x", "run", "--model", "new", "--fast"]

    def test_given_no_configured_model_when_a_run_picks_one_then_it_is_added(self):
        backend = config.DEFAULT_BACKENDS["claude"]
        argv = backends.build_argv(backend, Role.PLAN, "x", "/tree")
        assert "--model" not in argv
        argv = backends.with_model(argv, backend, "opus-4.6")
        assert argv[-2:] == ["--model", "opus-4.6"]

    def test_given_a_model_when_applied_then_the_original_argv_is_not_changed(self):
        backend = config.DEFAULT_BACKENDS["claude"]
        argv = backends.build_argv(backend, Role.PLAN, "x", "/tree")
        before = list(argv)
        backends.with_model(argv, backend, "opus")
        assert argv == before

    def test_given_a_blank_model_when_applied_then_nothing_changes(self):
        backend = config.DEFAULT_BACKENDS["codex"]
        argv = backends.build_argv(backend, Role.EXECUTE, "x", "/tree")
        assert backends.with_model(argv, backend, "") == argv

    def test_given_a_backend_without_the_flag_when_a_model_is_applied_then_it_is_untouched(self):
        backend = {"bin": "x", "args": ["run"]}
        argv = ["x", "run"]
        assert backends.with_model(argv, backend, "whatever") == argv

    @pytest.mark.parametrize("name", ["claude", "codex", "opencode"])
    def test_given_a_builtin_when_a_model_is_applied_then_it_reaches_the_argv(self, name):
        backend = config.DEFAULT_BACKENDS[name]
        argv = backends.with_model(
            backends.build_argv(backend, Role.EXECUTE, "x", "/tree"),
            backend, "chosen-model",
        )
        assert argv[argv.index("--model") + 1] == "chosen-model"


class TestDefaults:
    @pytest.mark.parametrize("role, agent", [(Role.PLAN, "plan"), (Role.REVIEW, "plan"), (Role.EXECUTE, "build")])
    def test_given_opencode_when_its_agent_is_read_then_it_reads_with_plan_and_writes_with_build(self, role, agent):
        """Its read-only agent cannot implement anything - that split matters."""
        assert backends.current_agent(config.DEFAULT_BACKENDS["opencode"], role) == agent

    def test_given_opencode_when_checked_then_it_advertises_how_to_list_its_agents(self):
        assert config.DEFAULT_BACKENDS["opencode"]["agents_command"] == ["agent", "list"]
