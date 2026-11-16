"""The command layer and the prompt loop."""

import io
import re
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from maestro import backends, config
from maestro import settings as settings_mod
from maestro import store as store_mod
from maestro.models import Mode, Role, Status, Task, Workflow
from maestro.shell import commands, render, repl
from maestro.shell.commands import CommandError
from maestro.shell.parser import Command

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ShellBase:
    @pytest.fixture(autouse=True)
    def _shell(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.monkeypatch = monkeypatch
        # Every backend counts as installed and none can list its agents or
        # models, whatever CLIs this machine happens to have: the shell's
        # questions must not depend on them, and a test must never start one.
        monkeypatch.setattr(config, "find_binary", lambda backend: f"/fake/bin/{backend.get('bin')}"
                            if backend.get("bin") else None)
        monkeypatch.setattr(backends, "list_options", lambda backend, kind: [])

        self.shell = repl.Shell(cwd=self.root)
        self.session = self.shell.session
        self.session.runner = mock.MagicMock()
        self.session.runner.is_running.return_value = False

        # `confirm` answers the y/N prompts; `lines` is a queue of typed
        # values, and anything not queued is answered by pressing Enter.
        self.confirm = True
        self.lines: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.session.ask_confirm = lambda *a, **k: self.confirm

        def ask_line(label, default=""):
            self.asked.append((label, default))
            return self.lines.pop(0) if self.lines else default

        def ask_select(label, options, default=""):
            self.asked.append((label, default))
            answer = self.lines.pop(0) if self.lines else default
            return answer or default

        def ask_action(label, options, default):
            self.asked.append((label, default))
            return self.lines.pop(0) if self.lines else default

        self.session.ask_line = ask_line
        self.session.ask_select = ask_select
        self.session.ask_action = ask_action
        self.session.asker.force_plain = True
        # Following is exercised by its own tests; elsewhere it is noise.
        self.session.settings["follow"] = False

    def add(self, status=Status.MERGED, prompt="a task", **kwargs):
        wf = Workflow(
            prompt=prompt, repo_path=str(self.root), base_branch="main",
            agents={"plan": "claude", "execute": "claude", "review": "claude"},
            **kwargs,
        )
        wf.status = status
        wf.branch = "feature/thing"
        self.session.store.add(wf)
        self.session.notices.seed(wf)
        return wf

    def do(self, line):
        """Run one typed line, returning what was printed, ANSI stripped."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.shell.handle(line)
        return ANSI.sub("", buffer.getvalue())

    def run_command(self, name, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            commands.dispatch(self.session, Command(name=name, args=list(args)))
        return ANSI.sub("", buffer.getvalue())


class TestDispatch(ShellBase):
    def test_given_a_misspelt_command_when_typed_then_a_near_one_is_suggested(self):
        out = self.do("/runss")
        assert "no command /runss" in out
        assert "did you mean /runs" in out

    def test_given_an_unrecognisable_command_when_typed_then_it_points_at_help(self):
        assert "/help" in self.do("/zzzzz")

    def test_given_an_alias_when_typed_then_it_reaches_the_same_handler(self):
        self.add()
        for alias in ("runs", "ls", "list"):
            assert "STATUS" in self.run_command(alias)

    def test_given_a_line_that_does_not_parse_when_typed_then_the_error_is_one_line_not_a_traceback(self):
        out = self.do("do it --nonsense x")
        assert "--nonsense" in out
        assert "Traceback" not in out

    def test_given_a_blank_line_when_typed_then_nothing_happens(self):
        assert self.do("   ") == ""

    def test_given_help_when_typed_then_the_commands_are_listed(self):
        out = self.run_command("help")
        for expected in ("/runs", "/approve", "/prompts", "/set"):
            assert expected in out


class TestFindRun(ShellBase):
    def test_given_an_id_prefix_when_a_run_is_looked_up_then_it_resolves(self):
        wf = self.add()
        assert commands.find_run(self.session, wf.id[:4]) is wf

    def test_given_a_single_run_when_none_is_named_then_it_is_assumed(self):
        wf = self.add()
        assert commands.find_run(self.session, "") is wf

    def test_given_several_runs_when_none_is_named_then_it_asks_which(self):
        self.add(prompt="one")
        self.add(prompt="two")
        with pytest.raises(CommandError) as ctx:
            commands.find_run(self.session, "")
        assert "which run" in str(ctx.value)

    def test_given_an_unknown_id_when_a_run_is_looked_up_then_it_says_so(self):
        self.add()
        with pytest.raises(CommandError) as ctx:
            commands.find_run(self.session, "zzzz")
        assert "no run starts with" in str(ctx.value)

    def test_given_no_runs_when_a_run_is_looked_up_then_it_says_so(self):
        with pytest.raises(CommandError) as ctx:
            commands.find_run(self.session, "abc")
        assert "no runs yet" in str(ctx.value)


class TestInspecting(ShellBase):
    def test_given_a_run_when_runs_is_typed_then_it_is_listed(self):
        self.add(prompt="add rate limiting")
        out = self.run_command("runs")
        assert "add rate limiting" in out
        assert "merged" in out

    def test_given_no_runs_when_runs_is_typed_then_it_says_so(self):
        assert "no runs yet" in self.run_command("runs")

    def test_given_a_run_with_a_checklist_when_shown_then_the_detail_is_printed(self):
        wf = self.add(prompt="add rate limiting")
        wf.tasks = [Task(1, "first", Status.DONE), Task(2, "second")]
        out = self.run_command("show", wf.id[:4])
        assert "add rate limiting" in out
        assert "feature/thing" in out
        assert "[x] 1. first" in out
        assert "[ ] 2. second" in out

    def test_given_a_run_without_logs_when_logs_is_typed_then_it_says_so(self):
        wf = self.add()
        with pytest.raises(CommandError) as ctx:
            commands.cmd_logs(self.session, Command("logs", [wf.id[:4]]))
        assert "logs yet" in str(ctx.value)


class TestMergeGate(ShellBase):
    def test_given_mixed_runs_when_pending_is_typed_then_only_those_awaiting_are_listed(self):
        self.add(Status.MERGED, prompt="already done")
        self.add(Status.AWAITING_APPROVAL, prompt="needs a look")
        out = self.run_command("pending")
        assert "needs a look" in out
        assert "already done" not in out

    def test_given_nothing_awaiting_when_pending_is_typed_then_it_says_so(self):
        assert "nothing is waiting" in self.run_command("pending")

    def test_given_a_run_awaiting_approval_when_approved_and_confirmed_then_it_merges(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        with mock.patch.object(commands.gitops, "diffstat", return_value=" 1 file"):
            self.run_command("approve", wf.id[:4])
        self.session.runner.approve.assert_called_once_with(wf)

    def test_given_a_run_awaiting_approval_when_the_confirmation_is_declined_then_it_is_left_unmerged(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        self.confirm = False
        with mock.patch.object(commands.gitops, "diffstat", return_value=""):
            out = self.run_command("approve", wf.id[:4])
        self.session.runner.approve.assert_not_called()
        assert "left unmerged" in out

    def test_given_no_run_awaiting_approval_when_approve_is_typed_then_it_is_refused(self):
        self.add(Status.MERGED)
        with pytest.raises(CommandError):
            self.run_command("approve", "")

    def test_given_a_reason_when_a_run_is_rejected_then_the_reason_is_passed_on(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        self.run_command("reject", wf.id[:4], "not", "what", "I", "asked")
        self.session.runner.reject.assert_called_once()
        assert "not what I asked" in self.session.runner.reject.call_args.args[1]


class TestManagingRuns(ShellBase):
    def test_given_a_failed_run_when_resumed_then_the_runner_is_asked(self):
        wf = self.add(Status.FAILED)
        self.run_command("resume", wf.id[:4])
        self.session.runner.resume.assert_called_once_with(wf)

    def test_given_a_merged_run_when_resumed_then_it_is_refused(self):
        wf = self.add(Status.MERGED)
        with pytest.raises(CommandError) as ctx:
            self.run_command("resume", wf.id[:4])
        assert "already merged" in str(ctx.value)

    def test_given_a_running_run_when_resumed_then_it_is_refused(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        with pytest.raises(CommandError):
            self.run_command("resume", wf.id[:4])

    def test_given_requested_changes_when_retried_then_the_runner_is_asked(self):
        wf = self.add(Status.CHANGES_REQUESTED)
        self.run_command("retry", wf.id[:4])
        self.session.runner.retry.assert_called_once_with(wf)

    def test_given_a_finished_run_when_cancelled_then_it_is_refused(self):
        wf = self.add(Status.MERGED)
        with pytest.raises(CommandError) as ctx:
            self.run_command("cancel", wf.id[:4])
        assert "not going" in str(ctx.value)

    def test_given_a_running_run_when_cancelled_then_the_runner_is_asked(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        self.run_command("cancel", wf.id[:4])
        self.session.runner.cancel.assert_called_once_with(wf)

    def test_given_a_run_with_logs_when_removed_then_both_are_forgotten_but_the_branch_is_kept(self):
        wf = self.add()
        log = store_mod.log_path_for(wf.id, "plan", 1)
        log.write_text("output", encoding="utf-8")

        out = self.run_command("rm", wf.id[:4])

        assert self.session.store.get(wf.id) is None
        assert not log.exists()
        assert "left alone" in out  # the branch is kept

    def test_given_a_declined_confirmation_when_removing_then_the_run_is_kept(self):
        wf = self.add()
        self.confirm = False
        self.run_command("rm", wf.id[:4])
        assert self.session.store.get(wf.id) is not None

    def test_given_a_running_run_when_removed_then_it_is_refused(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        with pytest.raises(CommandError) as ctx:
            self.run_command("rm", wf.id[:4])
        assert "/cancel" in str(ctx.value)


class TestPausing(ShellBase):
    def test_given_a_running_run_when_paused_then_the_runner_is_asked_and_it_says_how_to_resume(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        out = self.run_command("pause", wf.id[:4])
        self.session.runner.pause.assert_called_once_with(wf)
        assert "pause at the next step" in out
        assert "/resume" in out

    def test_given_an_idle_run_when_paused_then_it_reports_it_paused(self):
        wf = self.add(Status.FAILED)
        out = self.run_command("pause", wf.id[:4])
        self.session.runner.pause.assert_called_once_with(wf)
        assert "paused" in out

    def test_given_a_merged_run_when_paused_then_it_is_refused(self):
        wf = self.add(Status.MERGED)
        with pytest.raises(CommandError) as ctx:
            self.run_command("pause", wf.id[:4])
        assert "already merged" in str(ctx.value)

    def test_given_a_paused_run_when_paused_again_then_it_says_so(self):
        wf = self.add(Status.PAUSED)
        with pytest.raises(CommandError) as ctx:
            self.run_command("pause", wf.id[:4])
        assert "already paused" in str(ctx.value)

    def test_given_a_paused_run_when_resumed_then_the_runner_is_asked(self):
        wf = self.add(Status.PAUSED)
        self.run_command("resume", wf.id[:4])
        self.session.runner.resume.assert_called_once_with(wf)

    def test_given_a_run_awaiting_approval_when_resumed_then_it_points_at_approve(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        with pytest.raises(CommandError) as ctx:
            self.run_command("resume", wf.id[:4])
        assert "/approve" in str(ctx.value)

    def test_given_hold_when_typed_then_it_pauses(self):
        wf = self.add(Status.FAILED)
        self.run_command("hold", wf.id[:4])
        self.session.runner.pause.assert_called_once()


class TestClearingHistory(ShellBase):
    def test_given_mixed_runs_when_merged_ones_are_cleared_then_the_rest_are_kept(self):
        merged = self.add(Status.MERGED)
        failed = self.add(Status.FAILED)
        self.run_command("clear", "merged")
        assert self.session.store.get(merged.id) is None
        assert self.session.store.get(failed.id) is not None

    def test_given_mixed_runs_when_finished_ones_are_cleared_then_unfinished_ones_are_kept(self):
        merged = self.add(Status.MERGED)
        active = self.add(Status.PLANNING)
        self.run_command("clear", "finished")
        assert self.session.store.get(merged.id) is None
        assert self.session.store.get(active.id) is not None

    def test_given_runs_when_all_are_cleared_then_the_history_is_empty(self):
        self.add(Status.MERGED)
        self.add(Status.FAILED)
        self.run_command("clear", "all")
        assert self.session.store.all() == []

    def test_given_a_running_run_when_all_are_cleared_then_it_survives(self):
        active = self.add(Status.EXECUTING)
        self.add(Status.MERGED)
        self.session.runner.is_running.side_effect = lambda i: i == active.id
        self.run_command("clear", "all")
        assert self.session.store.get(active.id) is not None

    def test_given_a_declined_confirmation_when_clearing_then_nothing_changes(self):
        self.add(Status.MERGED)
        self.confirm = False
        self.run_command("clear", "all")
        assert len(self.session.store.all()) == 1

    def test_given_an_unknown_scope_when_clearing_then_it_is_refused(self):
        with pytest.raises(CommandError):
            self.run_command("clear", "everything")


class TestSettings(ShellBase):
    def test_given_a_valid_value_when_set_then_it_is_saved(self):
        self.run_command("set", "mode", "auto")
        assert self.session.settings["mode"] == "autonomous"
        assert settings_mod.load()["mode"] == "autonomous"

    def test_given_a_bad_value_or_key_when_set_then_it_is_refused(self):
        with pytest.raises(CommandError):
            self.run_command("set", "rounds", "zero")
        with pytest.raises(CommandError) as ctx:
            self.run_command("set", "nonsense", "1")
        assert "not a setting" in str(ctx.value)

    def test_given_no_value_when_set_then_it_is_refused(self):
        with pytest.raises(CommandError):
            self.run_command("set", "mode")

    def test_given_config_when_typed_then_the_defaults_are_shown(self):
        out = self.run_command("config")
        for key in ("repo", "mode", "rounds", "attempts", "confirm"):
            assert key in out

    def test_given_confirm_off_when_set_then_confirmation_is_disabled(self):
        self.run_command("set", "confirm", "off")
        assert self.session.settings["confirm"] is False


class TestPrompts(ShellBase):
    def test_given_prompt_files_when_prompts_is_typed_then_they_are_listed(self):
        (self.root / "docs" / "prompts").mkdir(parents=True)
        (self.root / "docs" / "prompts" / "scope.md").write_text("x", encoding="utf-8")
        out = self.run_command("prompts")
        assert "@scope" in out
        assert "docs/prompts" in out

    def test_given_no_prompt_files_when_prompts_is_typed_then_it_explains_where_it_looked(self):
        out = self.run_command("prompts")
        assert "no prompt files found" in out
        assert "current directory" in out

    def test_given_a_prompt_root_when_added_and_removed_then_the_listing_follows(self):
        extra = self.root / "library"
        extra.mkdir()
        (extra / "thing.md").write_text("x", encoding="utf-8")

        self.run_command("prompts", "add", str(extra))
        assert str(extra.resolve()) in self.session.settings["prompt_paths"]
        assert "@thing" in self.run_command("prompts")

        self.run_command("prompts", "rm", str(extra))
        assert self.session.settings["prompt_paths"] == []

    def test_given_a_missing_directory_when_added_as_a_root_then_it_is_refused(self):
        with pytest.raises(CommandError) as ctx:
            self.run_command("prompts", "add", str(self.root / "nope"))
        assert "no such directory" in str(ctx.value)


class TaskStartBase(ShellBase):
    """A session where typing a task reaches a fake git and fake backends.

    Carries no tests of its own so other suites can build on it without
    dragging this one's cases along.
    """

    @pytest.fixture(autouse=True)
    def _task_start(self, _shell):
        self.git = mock.MagicMock()
        self.monkeypatch.setattr(commands, "gitops", self.git)
        self.git.repo_root.return_value = Path(self.root)
        self.git.current_branch.return_value = "main"
        self.git.validate_target.return_value = Path(self.root)
        self.git.suggest_branch.side_effect = (
            lambda prompt, t=None: f"{t or 'feature'}/{prompt.split()[0].lower()}"
        )
        self.git.suggest_branch_type.return_value = "feature"
        self.git.sanitize_branch.side_effect = lambda n: n
        self.git.unique_branch.side_effect = lambda repo, name: name
        self.git.GitError = commands.GitError

        self.session.settings.update(
            {"plan": "claude", "execute": "opencode", "review": "codex"}
        )

    def started(self):
        self.session.runner.start.assert_called_once()
        return self.session.runner.start.call_args.args[0]


class TestStartingRuns(TaskStartBase):
    """Typing a task is the primary action, so it gets the closest look."""

    def test_given_a_task_when_typed_then_a_workflow_starts(self):
        out = self.do("add rate limiting to the API")
        wf = self.started()
        assert wf.prompt == "add rate limiting to the API"
        assert wf.mode is Mode.SUPERVISED
        assert "started" in out

    def test_given_a_task_when_typed_then_the_summary_is_shown_before_it_runs(self):
        out = self.do("add rate limiting")
        assert "feature/add" in out
        for cell in ("plan", "execute", "review", "merge", "claude", "codex"):
            assert cell in out
        assert "supervised" in out

    def test_given_a_cancelled_confirmation_when_a_task_is_typed_then_nothing_starts(self):
        self.lines = ["cancel"]
        out = self.do("add rate limiting")
        self.session.runner.start.assert_not_called()
        assert "cancelled" in out
        assert self.session.store.all() == []

    def test_given_confirmation_off_when_a_task_is_typed_then_it_starts_without_asking(self):
        self.session.settings["confirm"] = False
        self.session.ask_action = mock.Mock(side_effect=AssertionError("asked!"))
        self.do("add rate limiting")
        self.started()

    def test_given_inline_flags_when_a_task_is_typed_then_they_override_the_defaults(self):
        self.do("add rate limiting --mode auto --execute codex --rounds 4 --attempts 6")
        wf = self.started()
        assert wf.mode is Mode.AUTONOMOUS
        assert wf.agents["execute"] == "codex"
        assert wf.max_rounds == 4
        assert wf.max_attempts == 6

    def test_given_inline_flags_when_a_task_is_typed_then_the_saved_defaults_are_unchanged(self):
        self.do("add rate limiting --mode auto")
        assert self.session.settings["mode"] == "supervised"
        assert settings_mod.load()["mode"] == "supervised"

    def test_given_a_branch_flag_without_a_type_when_a_task_is_typed_then_it_gets_one(self):
        self.do("add rate limiting --branch scope-lookup")
        assert self.started().branch == "feature/scope-lookup"

    def test_given_a_branch_flag_with_a_type_when_a_task_is_typed_then_it_is_kept(self):
        self.do("add rate limiting --branch fix/scope-lookup")
        assert self.started().branch == "fix/scope-lookup"

    def test_given_a_dirty_repository_when_a_task_is_typed_then_nothing_starts(self):
        self.git.validate_target.side_effect = commands.GitError(
            "has uncommitted changes"
        )
        out = self.do("add rate limiting")
        self.session.runner.start.assert_not_called()
        assert "uncommitted changes" in out

    def test_given_an_uninstalled_agent_when_a_task_is_typed_then_nothing_starts(self):
        with mock.patch.object(commands, "_installed", return_value=False):
            out = self.do("add rate limiting")
        self.session.runner.start.assert_not_called()
        assert "not installed" in out

    def test_given_an_unknown_agent_when_a_task_is_typed_then_nothing_starts(self):
        out = self.do("add rate limiting --plan nosuchcli")
        self.session.runner.start.assert_not_called()
        assert "unknown backend" in out

    def test_given_a_reference_when_typed_then_the_file_becomes_the_task(self):
        prompts = self.root / "docs" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "scope-spec.md").write_text(
            "# Scope spec\n\nAdd custom fields.", encoding="utf-8"
        )
        self.session.prompt_files(refresh=True)

        out = self.do("@scope-spec")
        wf = self.started()
        assert "Add custom fields" in wf.prompt
        assert wf.prompt_source.endswith("scope-spec.md")
        assert "from" in out

    def test_given_text_after_a_reference_when_typed_then_it_is_appended(self):
        prompts = self.root / "docs" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "spec.md").write_text("Base requirement.", encoding="utf-8")
        self.session.prompt_files(refresh=True)

        self.do("@spec and also add tests")
        wf = self.started()
        assert "Base requirement." in wf.prompt
        assert "Additionally: and also add tests" in wf.prompt

    def test_given_an_unknown_reference_when_typed_then_nothing_starts(self):
        out = self.do("@nothing-here")
        self.session.runner.start.assert_not_called()
        assert "no saved prompt matches" in out

    def test_given_a_reference_with_flags_when_typed_then_the_flags_apply(self):
        prompts = self.root / "docs" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "spec.md").write_text("Do the thing.", encoding="utf-8")
        self.session.prompt_files(refresh=True)

        self.do("@spec --mode auto")
        assert self.started().mode is Mode.AUTONOMOUS

    def test_given_change_when_confirming_then_every_setting_is_asked(self):
        # c, then Enter for each field (the queue runs out and defaults apply),
        # then Enter at the final confirm.
        self.lines = ["change"]
        self.do("add rate limiting")
        asked = [label for label, _ in self.asked]
        for expected in ("plan agent", "plan model", "execute agent", "execute model",
                         "review agent", "review model",
                         "mode", "rework rounds",
                         "attempts per step", "branch"):
            assert expected in asked

    def test_given_change_when_confirming_then_every_field_offers_its_current_value(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        defaults = dict(self.asked)
        assert defaults["plan agent"] == "claude"
        assert defaults["execute agent"] == "opencode"
        assert defaults["review agent"] == "codex"
        assert defaults["mode"] == "supervised"
        assert defaults["rework rounds"] == "2"
        assert defaults["attempts per step"] == "3"

    def test_given_change_and_only_enter_when_confirming_then_the_defaults_are_kept(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        wf = self.started()
        assert wf.agents == {"plan": "claude", "execute": "opencode",
                                     "review": "codex"}
        assert wf.mode is Mode.SUPERVISED
        assert wf.max_rounds == 2

    def test_given_change_and_typed_values_when_confirming_then_they_are_used(self):
        self.lines = [
            "change",
            "codex", "",        # plan agent, plan model
            "claude", "",       # execute
            "opencode", "",     # review
            "autonomous", "4", "6", "fix/my-branch",
        ]
        self.do("add rate limiting")
        wf = self.started()
        assert wf.agents == {"plan": "codex", "execute": "claude",
                                     "review": "opencode"}
        assert wf.mode is Mode.AUTONOMOUS
        assert wf.max_rounds == 4
        assert wf.max_attempts == 6
        assert wf.branch == "fix/my-branch"

    def test_given_a_bad_mode_when_editing_then_it_is_asked_again(self):
        self.lines = ["change", "claude", "", "opencode", "", "codex", "", "autonomous"]
        out = self.do("add rate limiting")
        assert "supervised" in out  # the complaint
        assert self.started().mode is Mode.AUTONOMOUS

    def test_given_an_unknown_agent_when_editing_then_it_is_asked_again(self):
        self.lines = ["change", "nosuchcli", "claude"]
        out = self.do("add rate limiting")
        assert "unknown backend" in out
        assert self.started().agents["plan"] == "claude"

    def test_given_a_zero_round_count_when_editing_then_it_is_asked_again(self):
        self.lines = ["change", "claude", "", "opencode", "", "codex", "", "supervised", "0", "2"]
        out = self.do("add rate limiting")
        assert "1 or more" in out
        assert self.started().max_rounds == 2

    def test_given_edits_and_yes_to_saving_when_confirming_then_they_become_the_defaults(self):
        self.lines = ["change", "codex", "", "codex", "", "codex", "", "autonomous", "5", "7"]
        self.confirm = True  # yes to "save these as your defaults?"
        self.do("add rate limiting")

        saved = settings_mod.load()
        assert saved["plan"] == "codex"
        assert saved["mode"] == "autonomous"
        assert saved["rounds"] == 5
        assert saved["attempts"] == 7

    def test_given_edits_and_no_to_saving_when_confirming_then_only_this_run_uses_them(self):
        self.lines = ["change", "codex", "", "codex", "", "codex", "", "autonomous", "5", "7"]
        self.confirm = False  # no to saving
        self.do("add rate limiting")

        saved = settings_mod.load()
        assert saved["mode"] == "supervised"
        assert saved.get("rounds") != 5
        # ...but this run still used them
        assert self.started().max_rounds == 5

    def test_given_an_unrecognised_answer_when_confirming_then_it_asks_again_rather_than_running(self):
        self.lines = ["what?", "run"]
        out = self.do("add rate limiting")
        assert "choose run, change or cancel" in out
        self.started()  # the second answer ran it

    def test_given_change_when_confirming_then_a_model_is_offered_for_every_role(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        asked = [label for label, _ in self.asked]
        assert "plan model" in asked
        assert "execute model" in asked
        assert "review model" in asked

    def test_given_chosen_models_when_editing_then_they_reach_the_workflow(self):
        self.lines = [
            "change",
            "claude", "opus-4.6",
            "opencode", "opencode-go/glm-5.2",
            "codex", "",
        ]
        self.do("add rate limiting")
        wf = self.started()
        assert wf.models["plan"] == "opus-4.6"
        assert wf.models["execute"] == "opencode-go/glm-5.2"
        assert "review" not in wf.models  # blank means the CLI's default

    def test_given_blank_models_when_editing_then_the_backend_defaults_are_left_alone(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        assert self.started().models == {}

    def test_given_the_model_flag_when_a_task_is_typed_then_it_applies_to_every_role(self):
        self.do("add rate limiting --model sonnet-4.6")
        wf = self.started()
        assert wf.models == {"plan": "sonnet-4.6", "execute": "sonnet-4.6",
                        "review": "sonnet-4.6"}

    def test_given_a_per_role_model_flag_when_a_task_is_typed_then_it_wins_over_the_general_one(self):
        self.do("add rate limiting --model sonnet --execute-model opus")
        wf = self.started()
        assert wf.models["execute"] == "opus"
        assert wf.models["plan"] == "sonnet"

    def test_given_a_model_when_a_task_is_typed_then_it_is_shown_in_the_summary(self):
        out = self.do("add rate limiting --model sonnet-4.6")
        assert "sonnet-4.6" in out
        assert "plan sonnet-4.6" in " ".join(out.split())

    def test_given_a_model_default_when_a_task_is_typed_then_it_is_used(self):
        self.session.settings["execute_model"] = "opencode-go/glm-5.2"
        self.do("add rate limiting")
        assert self.started().models["execute"] == "opencode-go/glm-5.2"

    def test_given_edited_models_and_yes_to_saving_when_confirming_then_they_are_saved(self):
        self.lines = ["change", "claude", "opus-4.6", "opencode", "", "codex", ""]
        self.confirm = True
        self.do("add rate limiting")
        assert settings_mod.load()["plan_model"] == "opus-4.6"

    def test_given_follow_on_when_a_run_starts_then_its_logs_are_followed(self):
        self.session.settings["follow"] = True
        with mock.patch.object(commands.logview, "stream") as stream:
            out = self.do("add rate limiting")
        stream.assert_called_once()
        assert stream.call_args.args[0] is self.started()
        assert stream.call_args.args[1] is None  # every step
        assert "Ctrl-C returns to the prompt" in out

    def test_given_follow_off_when_a_run_starts_then_its_logs_are_not_followed(self):
        self.session.settings["follow"] = False
        with mock.patch.object(commands.logview, "stream") as stream:
            self.do("add rate limiting")
        stream.assert_not_called()

    def test_given_a_followed_run_when_the_log_is_left_then_it_is_not_replayed_as_a_notice(self):
        self.session.settings["follow"] = True
        with mock.patch.object(commands.logview, "stream"):
            self.do("add rate limiting")
        wf = self.started()
        wf.status = Status.EXECUTING
        # Seeded again on the way out, so only changes from here are announced.
        assert len(self.session.notices.drain([wf])) == 1

    def test_given_a_new_run_when_started_then_it_is_not_announced_late(self):
        self.do("add rate limiting")
        wf = self.started()
        assert self.session.notices.drain([wf]) == []


class TestFrame(ShellBase):
    """The header stays put; the footer rides just above the prompt."""

    def draw(self, fn):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            fn()
        return ANSI.sub("", buffer.getvalue())

    def test_given_the_header_when_drawn_then_it_is_the_banner_closed_off_by_a_divider(self):
        drawn = self.draw(self.shell.header)
        assert "MAESTRO" in drawn
        assert render._box().h * 10 in drawn

    def test_given_the_header_when_drawn_then_it_has_no_status_bar(self):
        self.add()
        assert "1 run" not in self.draw(self.shell.header)

    def test_given_the_footer_when_drawn_then_it_carries_the_status_bar(self):
        self.add()
        assert "1 run" in self.draw(self.shell.footer)

    def test_given_the_footer_when_output_is_replayed_then_it_is_not_in_the_content_area(self):
        """Otherwise the status line marches down the screen on each redraw."""
        self.add()
        with redirect_stdout(io.StringIO()):
            # The loop's order: draw the footer, then clear the recording so
            # the turn starts on a clean slate, then run the command.
            self.shell.footer()
            render.take_output()
            render.out("the result of a command")
            replayed = ANSI.sub("", render.take_output())

        assert "the result of a command" in replayed
        assert "1 run" not in replayed

    def sized(self, height):
        console = Console(theme=render.THEME, file=io.StringIO(), width=100,
                          height=height, force_terminal=False,
                          legacy_windows=False, record=True, highlight=False)
        return mock.patch.object(render, "console", console)

    def test_given_a_tall_terminal_when_output_room_is_measured_then_space_is_left_for_the_chrome(self):
        with self.sized(40):
            room = self.shell.output_room()
        chrome = len(render.WORDMARK) + 3 + 2 + 3 + 1
        assert room == 40 - chrome

    def test_given_a_short_terminal_when_output_room_is_measured_then_some_room_remains(self):
        with self.sized(5):
            assert self.shell.output_room() >= 4


class TestNotices:
    def make(self, status=Status.PENDING, tasks=None):
        wf = Workflow(
            prompt="a task", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
        )
        wf.status = status
        wf.tasks = tasks or []
        return wf

    def test_given_an_unchanged_run_when_notices_are_drained_then_nothing_is_said(self):
        board = repl.Notices()
        wf = self.make()
        board.seed(wf)
        assert board.drain([wf]) == []

    def test_given_a_seeded_run_when_notices_are_drained_then_its_first_sighting_is_not_announced(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        assert board.drain([wf]) == []

    def test_given_a_status_change_when_notices_are_drained_then_it_is_announced(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        wf.status = Status.MERGED
        lines = " ".join(t.plain for t in board.drain([wf]))
        assert "merged" in lines
        assert wf.id[:6] in lines

    def test_given_checklist_progress_when_notices_are_drained_then_it_is_announced(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING, tasks=[Task(1, "a"), Task(2, "b")])
        board.seed(wf)
        wf.tasks[0].status = Status.DONE
        lines = " ".join(t.plain for t in board.drain([wf]))
        assert "1/2 tasks done" in lines

    def test_given_a_run_now_awaiting_approval_when_notices_are_drained_then_it_points_at_approve(self):
        board = repl.Notices()
        wf = self.make(Status.REVIEWING)
        board.seed(wf)
        wf.status = Status.AWAITING_APPROVAL
        lines = " ".join(t.plain for t in board.drain([wf]))
        assert "awaiting approval" in lines
        assert "/approve" in lines

    def test_given_a_change_when_notices_are_drained_twice_then_it_is_announced_once(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        wf.status = Status.MERGED
        assert len(board.drain([wf])) == 1
        assert board.drain([wf]) == []

    def test_given_a_forgotten_run_when_notices_are_drained_then_it_is_seeded_again_not_announced(self):
        board = repl.Notices()
        wf = self.make(Status.MERGED)
        board.seed(wf)
        board.forget(wf.id)
        assert board.drain([wf]) == []  # re-seeded, not announced

    @pytest.mark.parametrize("status, expected", [
        (Status.FAILED, "failed - /show"),
        (Status.CHANGES_REQUESTED, "changes requested - /show"),
        (Status.PAUSED, "paused - /resume"),
        (Status.CANCELLED, "cancelled"),
        (Status.REVIEWING, "reviewing"),
    ])
    def test_given_a_change_to_a_status_when_notices_are_drained_then_it_is_described(self, status, expected):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        wf.status = status
        assert expected in board.drain([wf])[0].plain



class TestMoreCommands(ShellBase):
    """The less travelled branches of the slash commands."""

    def test_given_a_prefix_two_runs_share_when_a_run_is_looked_up_then_it_asks_for_more(self):
        first = self.add()
        second = self.add()
        first.id, second.id = "ab" + first.id[2:], "ab" + second.id[2:]
        with pytest.raises(CommandError) as ctx:
            commands.find_run(self.session, "ab", among=[first, second])
        assert "matches 2 runs" in str(ctx.value)

    def test_given_a_failed_run_when_shown_then_the_failing_step_output_follows(self):
        wf = self.add(Status.FAILED)
        step = wf.add_step(Role.EXECUTE)
        step.status = Status.FAILED
        log = store_mod.log_path_for(wf.id, "execute", 1)
        log.write_text("the agent said boom\n", encoding="utf-8")
        step.log_path = str(log)
        assert "the agent said boom" in self.run_command("show", wf.id[:4])

    def test_given_a_step_name_when_logs_is_typed_then_only_that_step_is_streamed(self):
        wf = self.add()
        wf.add_step(Role.PLAN)
        with mock.patch.object(commands.logview, "stream") as stream:
            self.run_command("logs", wf.id[:4], "PLAN")
        assert stream.call_args.args[0] is wf
        assert stream.call_args.args[1] == {"plan"}

    @pytest.mark.parametrize("name, module, function", [
        ("watch", "logview", "stream"), ("progress", "progress", "watch"),
    ])
    def test_given_one_running_run_when_watched_then_it_is_picked_without_an_id(self, name, module, function):
        self.add(prompt="finished")
        running = self.add(Status.EXECUTING, prompt="going")
        self.session.runner.is_running.side_effect = lambda i: i == running.id
        with mock.patch.object(getattr(commands, module), function) as follow:
            self.run_command(name)
        assert follow.call_args.args[0] is running

    def test_given_a_diff_that_cannot_be_read_when_approving_then_the_reason_is_shown_instead(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        self.confirm = False
        with mock.patch.object(commands.gitops, "diffstat", side_effect=commands.GitError("bad object")):
            out = self.run_command("approve", wf.id[:4])
        assert "could not read the diff: bad object" in out

    def test_given_a_run_with_a_checklist_when_resumed_then_the_finished_items_are_mentioned(self):
        wf = self.add(Status.FAILED)
        wf.tasks = [Task(1, "a", Status.DONE), Task(2, "b")]
        assert "keeping 1/2 finished items" in self.run_command("resume", wf.id[:4])

    def test_given_a_running_run_when_retried_then_it_is_refused(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        with pytest.raises(CommandError) as ctx:
            self.run_command("retry", wf.id[:4])
        assert "already going" in str(ctx.value)

    def test_given_nothing_matching_when_clearing_then_it_says_so(self):
        self.add(Status.FAILED)
        assert "nothing to clear" in self.run_command("clear", "merged")

    def test_given_more_than_ten_runs_when_clearing_then_the_rest_are_counted(self):
        for _ in range(12):
            self.add(Status.MERGED)
        out = self.run_command("clear", "all")
        assert "... and 2 more" in out
        assert self.session.store.all() == []

    def test_given_a_run_with_a_worktree_when_removed_then_the_worktree_is_removed_too(self):
        wf = self.add()
        wf.worktree = str(self.root)
        with mock.patch.object(commands.gitops, "remove_worktree") as remove:
            self.run_command("rm", wf.id[:4])
        remove.assert_called_once_with(wf.repo_path, wf.worktree)

    def test_given_set_without_arguments_when_typed_then_the_config_is_shown(self):
        assert "mode" in self.run_command("set")

    def test_given_an_unknown_root_when_removed_then_it_is_refused(self):
        with pytest.raises(CommandError) as ctx:
            self.run_command("prompts", "rm", str(self.root / "never-added"))
        assert "is not a configured root" in str(ctx.value)

    def test_given_no_path_when_a_root_is_added_then_the_usage_is_shown(self):
        with pytest.raises(CommandError) as ctx:
            self.run_command("prompts", "add")
        assert "/prompts add <path>" in str(ctx.value)


class TestAgentAndModelCommands(ShellBase):
    """`/agents` and `/model` edit the backends file."""

    def options(self, *names):
        self.monkeypatch.setattr(backends, "list_options", lambda backend, kind: [(n, "") for n in names])

    def test_given_no_backend_when_agents_is_typed_then_the_ones_with_agents_are_named(self):
        with pytest.raises(CommandError) as ctx:
            self.run_command("agents")
        assert "which backend? opencode" in str(ctx.value)

    def test_given_no_backend_supporting_it_when_agents_is_typed_then_it_says_so(self):
        config.save_backends({name: {**b, "agent_flag": ""} for name, b in config.load_backends().items()})
        with pytest.raises(CommandError) as ctx:
            self.run_command("agents")
        assert "no backend supports choosing a agent" in str(ctx.value)

    def test_given_an_unknown_backend_when_model_is_typed_then_it_is_refused(self):
        with pytest.raises(CommandError) as ctx:
            self.run_command("model", "nosuchcli")
        assert "unknown backend" in str(ctx.value)

    def test_given_a_backend_without_agents_when_agents_is_typed_then_it_is_refused(self):
        with pytest.raises(CommandError) as ctx:
            self.run_command("agents", "claude")
        assert "claude has no agent option" in str(ctx.value)

    def test_given_only_a_backend_when_agents_is_typed_then_the_choices_and_current_values_are_shown(self):
        self.options("build", "plan")
        out = self.run_command("agents", "opencode")
        assert "build" in out
        assert "now: plan/review plan, execute build" in out
        assert "/agents opencode <value> [execute|read]" in out

    def test_given_a_backend_with_no_model_set_when_model_is_typed_then_default_is_shown(self):
        out = self.run_command("model", "claude")
        assert "now: plan/review default, execute default" in out
        assert "/model claude <value>" in out

    def test_given_a_value_when_agents_is_typed_then_both_roles_are_set_and_saved(self):
        self.options("build", "plan", "general")
        out = self.run_command("agents", "opencode", "general")
        saved = config.load_backends()["opencode"]
        assert saved["args"][-2:] == ["--agent", "general"]
        assert saved["execute_args"][-2:] == ["--agent", "general"]
        assert "opencode agent set to general" in out

    @pytest.mark.parametrize("scope, changed, kept", [
        ("execute", "execute_args", "args"), ("read", "args", "execute_args"),
    ])
    def test_given_a_scope_when_agents_is_typed_then_only_that_side_changes(self, scope, changed, kept):
        self.options("build", "plan", "general")
        before = config.load_backends()["opencode"][kept]
        out = self.run_command("agents", "opencode", "general", scope)
        saved = config.load_backends()["opencode"]
        assert saved[changed][-2:] == ["--agent", "general"]
        assert saved[kept] == before
        assert f"for {scope}" in out

    def test_given_part_of_one_name_when_model_is_typed_then_that_model_is_chosen(self):
        self.options("opencode-go/glm-5.2", "anthropic/claude-opus-5")
        self.run_command("model", "opencode", "glm")
        assert config.load_backends()["opencode"]["execute_args"][-2:] == ["--model", "opencode-go/glm-5.2"]

    def test_given_part_of_several_names_when_model_is_typed_then_the_candidates_are_listed(self):
        self.options("opencode-go/glm-5.2", "opencode-go/glm-4.9")
        with pytest.raises(CommandError) as ctx:
            self.run_command("model", "opencode", "glm")
        assert "'glm' matches:" in str(ctx.value)

    def test_given_a_name_the_backend_lacks_when_model_is_typed_then_it_is_refused(self):
        self.options("opencode-go/glm-5.2")
        with pytest.raises(CommandError) as ctx:
            self.run_command("model", "opencode", "gpt")
        assert "opencode has no model 'gpt'" in str(ctx.value)

    def test_given_a_backend_that_cannot_list_when_model_is_typed_then_any_value_is_taken(self):
        self.run_command("model", "claude", "opus")
        assert config.load_backends()["claude"]["args"][-2:] == ["--model", "opus"]


class TestEditingDetails(TaskStartBase):
    """The edit walk's model questions and its rarer answers."""

    def asked_labels(self):
        return [label for label, _ in self.asked]

    def test_given_review_turned_off_when_editing_then_rounds_are_not_asked(self):
        self.lines = ["change", "claude", "", "opencode", "", commands.SKIP_STEP]
        self.do("add rate limiting")
        assert "rework rounds" not in self.asked_labels()
        assert self.started().agents["review"] == ""

    def test_given_no_cli_installed_when_editing_then_the_values_are_kept_without_a_picker(self):
        self.monkeypatch.setattr(config, "find_binary", lambda backend: None)
        self.monkeypatch.setattr(commands, "_installed", lambda name: True)
        self.lines = ["change"]
        out = self.do("add rate limiting")
        assert "available:" not in out
        assert "plan agent" not in self.asked_labels()
        assert self.started().agents == {"plan": "claude", "execute": "opencode", "review": "codex"}

    def test_given_some_clis_installed_when_an_uninstalled_one_is_chosen_then_it_is_asked_again(self):
        self.monkeypatch.setattr(
            config, "find_binary", lambda backend: None if backend.get("bin") == "codex" else "/fake/bin/x")
        self.monkeypatch.setattr(commands, "_installed", lambda name: True)
        self.lines = ["change", "claude", "", "codex", "opencode", "", "claude"]
        out = self.do("add rate limiting")
        assert "'codex' is not installed" in out
        assert self.started().agents["execute"] == "opencode"

    def test_given_a_backend_without_a_model_flag_when_editing_then_no_model_is_asked(self):
        backends_file = config.load_backends()
        backends_file["claude"]["model_flag"] = ""
        config.save_backends(backends_file)
        self.lines = ["change"]
        self.do("add rate limiting")
        assert "plan model" not in self.asked_labels()

    def listed(self, *names):
        self.monkeypatch.setattr(backends, "list_options", lambda backend, kind: [(n, "") for n in names])

    def test_given_listed_models_when_the_default_entry_is_picked_then_the_model_is_blank(self):
        self.listed("m/one", "m/two")
        self.lines = ["change", "claude", "", "opencode", "(opencode default)"]
        self.do("add rate limiting")
        assert self.started().models.get("execute", "") == ""

    def test_given_listed_models_when_one_is_picked_then_it_is_used(self):
        self.listed("m/one", "m/two")
        self.lines = ["change", "claude", "", "opencode", "m/two"]
        self.do("add rate limiting")
        assert self.started().models["execute"] == "m/two"

    def test_given_listed_models_when_other_is_picked_then_the_id_is_typed(self):
        self.listed("m/one")
        self.lines = ["change", "claude", "", "opencode", commands.OTHER_MODEL, "  my/own-model "]
        out = self.do("add rate limiting")
        assert self.started().models["execute"] == "my/own-model"
        assert "known_models" in out


class TestPromptLoop:
    """The Session and Shell themselves, with the terminal faked."""

    @pytest.fixture(autouse=True)
    def _shell(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(config, "find_binary", lambda backend: None)
        self.shell = repl.Shell(cwd=tmp_path)
        self.session = self.shell.session
        self.session.asker = mock.Mock()
        self.buffer = io.StringIO()
        console = Console(theme=render.THEME, file=self.buffer, width=100, height=40, force_terminal=False,
                          legacy_windows=False, record=True, highlight=False)
        monkeypatch.setattr(render, "console", console)

    def printed(self):
        return ANSI.sub("", self.buffer.getvalue())

    def add(self, status):
        wf = Workflow(prompt="a task", repo_path=str(self.root), base_branch="main",
                      agents={"plan": "", "execute": "claude", "review": ""})
        wf.status = status
        self.session.store.add(wf)
        return wf

    def test_given_installed_clis_when_a_default_is_needed_then_the_first_installed_one_is_used(self):
        self.monkeypatch.setattr(backends, "available", lambda: [("claude", False), ("codex", True), ("x", True)])
        assert self.session.default_backend() == "codex"

    def test_given_no_installed_cli_when_a_default_is_needed_then_it_is_blank(self):
        assert self.session.default_backend() == ""

    def test_given_the_session_questions_when_asked_then_the_asker_answers_them(self):
        self.session.asker.line.return_value = "typed"
        self.session.asker.select.return_value = "b"
        self.session.asker.confirm.return_value = True
        assert self.session.ask_line("label", "d") == "typed"
        assert self.session.ask_select("label", ["a", "b"], "a") == "b"
        assert self.session.ask_confirm("Delete?", require="y") is True
        assert self.session.asker.confirm.call_args.kwargs["default"] is False

    def test_given_named_actions_when_one_is_chosen_then_its_key_comes_back(self):
        self.session.asker.select.return_value = "change the settings"
        options = [("run", "run it"), ("change", "change the settings")]
        assert self.session.ask_action("Start?", options, "run") == "change"
        assert self.session.asker.select.call_args.args == ("Start?", ["run it", "change the settings"], "run it")

    def test_given_an_answer_that_is_no_action_when_chosen_then_the_default_key_comes_back(self):
        self.session.asker.select.return_value = "gibberish"
        assert self.session.ask_action("Start?", [("run", "run it")], "run") == "run"

    def test_given_runs_left_by_a_crash_when_greeting_then_they_are_reported(self):
        self.add(Status.EXECUTING)
        self.shell.greet()
        assert "1 run interrupted" in self.printed()

    def test_given_runs_awaiting_approval_when_the_hint_is_built_then_it_points_at_approve(self):
        assert self.shell.hint() == "type a task, @prompt or /help"
        self.add(Status.AWAITING_APPROVAL)
        assert self.shell.hint() == "1 awaiting approval - /approve"

    def test_given_end_of_input_when_the_loop_runs_then_it_says_bye_and_returns_zero(self):
        self.session.asker.command.side_effect = EOFError
        assert self.shell.run() == 0
        assert "bye" in self.printed()

    def test_given_ctrl_c_then_exit_when_the_loop_runs_then_it_carries_on_and_then_leaves(self):
        self.session.asker.command.side_effect = [KeyboardInterrupt, "/runs", "/exit"]
        assert self.shell.run() == 0
        assert self.session.asker.command.call_count == 3
        assert "no runs yet" in self.printed()

    def test_given_pinning_off_when_the_loop_runs_then_the_screen_is_not_redrawn(self):
        cleared = []
        self.monkeypatch.setattr(render, "clear", lambda: cleared.append(True))
        self.session.settings["pinned"] = False
        self.session.asker.command.side_effect = ["/runs", "/exit"]
        self.shell.run()
        assert cleared == []

    def test_given_a_running_run_when_quitting_and_declining_then_the_shell_stays_open(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running = lambda workflow_id: workflow_id == wf.id
        self.session.asker.confirm.return_value = False
        assert self.shell.handle("/quit") is True
        assert "1 run still going" in self.printed()

    def test_given_a_running_run_when_quitting_and_confirming_then_the_shell_closes(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running = lambda workflow_id: workflow_id == wf.id
        self.session.asker.confirm.return_value = True
        assert self.shell.handle("/q") is False

    @pytest.mark.parametrize("line", ["/runs", "do the thing"])
    def test_given_ctrl_c_inside_a_command_or_task_when_handled_then_it_is_reported_as_cancelled(self, line):
        self.monkeypatch.setattr(commands, "dispatch", mock.Mock(side_effect=KeyboardInterrupt))
        self.monkeypatch.setattr(commands, "start_task", mock.Mock(side_effect=KeyboardInterrupt))
        assert self.shell.handle(line) is True
        assert "cancelled" in self.printed()

    def test_given_a_task_that_cannot_start_when_handled_then_the_error_is_one_line(self):
        self.monkeypatch.setattr(commands, "start_task", mock.Mock(side_effect=CommandError("no git repo here")))
        assert self.shell.handle("do the thing") is True
        assert "no git repo here" in self.printed()
