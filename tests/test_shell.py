"""The command layer and the prompt loop."""

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from rich.console import Console

from maestro import settings as settings_mod, store as store_mod
from maestro.models import Mode, Status, Task, Workflow
from maestro.shell import commands, render, repl
from maestro.shell.commands import CommandError
from maestro.shell.parser import Command

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ShellTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old = os.environ.get("MAESTRO_HOME")
        os.environ["MAESTRO_HOME"] = str(self.root / "home")
        self.addCleanup(self.tmp.cleanup)

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

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MAESTRO_HOME", None)
        else:
            os.environ["MAESTRO_HOME"] = self._old

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


class Dispatch(ShellTestCase):
    def test_an_unknown_command_suggests_a_near_one(self):
        out = self.do("/runss")
        self.assertIn("no command /runss", out)
        self.assertIn("did you mean /runs", out)

    def test_a_wild_command_just_points_at_help(self):
        self.assertIn("/help", self.do("/zzzzz"))

    def test_aliases_reach_the_same_handler(self):
        self.add()
        for alias in ("runs", "ls", "list"):
            self.assertIn("STATUS", self.run_command(alias))

    def test_a_parse_error_is_one_line_not_a_traceback(self):
        out = self.do("do it --nonsense x")
        self.assertIn("--nonsense", out)
        self.assertNotIn("Traceback", out)

    def test_a_blank_line_does_nothing(self):
        self.assertEqual(self.do("   "), "")

    def test_help_lists_the_commands(self):
        out = self.run_command("help")
        for expected in ("/runs", "/approve", "/prompts", "/set"):
            self.assertIn(expected, out)


class FindRun(ShellTestCase):
    def test_an_id_prefix_resolves(self):
        wf = self.add()
        self.assertIs(commands.find_run(self.session, wf.id[:4]), wf)

    def test_the_only_run_is_assumed_when_none_is_named(self):
        wf = self.add()
        self.assertIs(commands.find_run(self.session, ""), wf)

    def test_an_ambiguous_omission_asks_which(self):
        self.add(prompt="one")
        self.add(prompt="two")
        with self.assertRaises(CommandError) as ctx:
            commands.find_run(self.session, "")
        self.assertIn("which run", str(ctx.exception))

    def test_an_unknown_id_says_so(self):
        self.add()
        with self.assertRaises(CommandError) as ctx:
            commands.find_run(self.session, "zzzz")
        self.assertIn("no run starts with", str(ctx.exception))

    def test_no_runs_at_all_says_so(self):
        with self.assertRaises(CommandError) as ctx:
            commands.find_run(self.session, "abc")
        self.assertIn("no runs yet", str(ctx.exception))


class Inspecting(ShellTestCase):
    def test_runs_lists_them(self):
        self.add(prompt="add rate limiting")
        out = self.run_command("runs")
        self.assertIn("add rate limiting", out)
        self.assertIn("merged", out)

    def test_runs_with_none_is_friendly(self):
        self.assertIn("no runs yet", self.run_command("runs"))

    def test_show_prints_the_detail(self):
        wf = self.add(prompt="add rate limiting")
        wf.tasks = [Task(1, "first", Status.DONE), Task(2, "second")]
        out = self.run_command("show", wf.id[:4])
        self.assertIn("add rate limiting", out)
        self.assertIn("feature/thing", out)
        self.assertIn("[x] 1. first", out)
        self.assertIn("[ ] 2. second", out)

    def test_logs_without_any_says_so(self):
        wf = self.add()
        with self.assertRaises(CommandError) as ctx:
            commands.cmd_logs(self.session, Command("logs", [wf.id[:4]]))
        self.assertIn("logs yet", str(ctx.exception))


class MergeGate(ShellTestCase):
    def test_pending_lists_only_those_awaiting(self):
        self.add(Status.MERGED, prompt="already done")
        self.add(Status.AWAITING_APPROVAL, prompt="needs a look")
        out = self.run_command("pending")
        self.assertIn("needs a look", out)
        self.assertNotIn("already done", out)

    def test_pending_with_none_says_so(self):
        self.assertIn("nothing is waiting", self.run_command("pending"))

    def test_approve_merges_after_confirmation(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        with mock.patch.object(commands.gitops, "diffstat", return_value=" 1 file"):
            self.run_command("approve", wf.id[:4])
        self.session.runner.approve.assert_called_once_with(wf)

    def test_declining_leaves_it_unmerged(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        self.confirm = False
        with mock.patch.object(commands.gitops, "diffstat", return_value=""):
            out = self.run_command("approve", wf.id[:4])
        self.session.runner.approve.assert_not_called()
        self.assertIn("left unmerged", out)

    def test_approve_ignores_runs_not_awaiting(self):
        self.add(Status.MERGED)
        with self.assertRaises(CommandError):
            self.run_command("approve", "")

    def test_reject_passes_the_reason_on(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        self.run_command("reject", wf.id[:4], "not", "what", "I", "asked")
        self.session.runner.reject.assert_called_once()
        self.assertIn("not what I asked", self.session.runner.reject.call_args.args[1])


class ManagingRuns(ShellTestCase):
    def test_resume_calls_the_runner(self):
        wf = self.add(Status.FAILED)
        self.run_command("resume", wf.id[:4])
        self.session.runner.resume.assert_called_once_with(wf)

    def test_a_merged_run_cannot_be_resumed(self):
        wf = self.add(Status.MERGED)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("resume", wf.id[:4])
        self.assertIn("already merged", str(ctx.exception))

    def test_a_running_one_cannot_be_resumed(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        with self.assertRaises(CommandError):
            self.run_command("resume", wf.id[:4])

    def test_retry_calls_the_runner(self):
        wf = self.add(Status.CHANGES_REQUESTED)
        self.run_command("retry", wf.id[:4])
        self.session.runner.retry.assert_called_once_with(wf)

    def test_cancel_needs_a_running_workflow(self):
        wf = self.add(Status.MERGED)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("cancel", wf.id[:4])
        self.assertIn("not going", str(ctx.exception))

    def test_cancel_calls_the_runner(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        self.run_command("cancel", wf.id[:4])
        self.session.runner.cancel.assert_called_once_with(wf)

    def test_rm_forgets_the_run_and_its_logs(self):
        wf = self.add()
        log = store_mod.log_path_for(wf.id, "plan", 1)
        log.write_text("output", encoding="utf-8")

        out = self.run_command("rm", wf.id[:4])

        self.assertIsNone(self.session.store.get(wf.id))
        self.assertFalse(log.exists())
        self.assertIn("left alone", out)  # the branch is kept

    def test_rm_declined_keeps_it(self):
        wf = self.add()
        self.confirm = False
        self.run_command("rm", wf.id[:4])
        self.assertIsNotNone(self.session.store.get(wf.id))

    def test_rm_refuses_a_running_workflow(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        with self.assertRaises(CommandError) as ctx:
            self.run_command("rm", wf.id[:4])
        self.assertIn("/cancel", str(ctx.exception))


class Pausing(ShellTestCase):
    def test_pause_reaches_the_runner(self):
        wf = self.add(Status.EXECUTING)
        self.session.runner.is_running.return_value = True
        out = self.run_command("pause", wf.id[:4])
        self.session.runner.pause.assert_called_once_with(wf)
        self.assertIn("pause at the next step", out)
        self.assertIn("/resume", out)

    def test_pausing_an_idle_run_reports_it_done(self):
        wf = self.add(Status.FAILED)
        out = self.run_command("pause", wf.id[:4])
        self.session.runner.pause.assert_called_once_with(wf)
        self.assertIn("paused", out)

    def test_a_merged_run_cannot_be_paused(self):
        wf = self.add(Status.MERGED)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("pause", wf.id[:4])
        self.assertIn("already merged", str(ctx.exception))

    def test_pausing_twice_says_so(self):
        wf = self.add(Status.PAUSED)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("pause", wf.id[:4])
        self.assertIn("already paused", str(ctx.exception))

    def test_a_paused_run_resumes(self):
        wf = self.add(Status.PAUSED)
        self.run_command("resume", wf.id[:4])
        self.session.runner.resume.assert_called_once_with(wf)

    def test_a_run_waiting_for_approval_is_not_resumable(self):
        wf = self.add(Status.AWAITING_APPROVAL)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("resume", wf.id[:4])
        self.assertIn("/approve", str(ctx.exception))

    def test_hold_is_an_alias(self):
        wf = self.add(Status.FAILED)
        self.run_command("hold", wf.id[:4])
        self.session.runner.pause.assert_called_once()


class ClearingHistory(ShellTestCase):
    def test_clear_merged_keeps_the_rest(self):
        merged = self.add(Status.MERGED)
        failed = self.add(Status.FAILED)
        self.run_command("clear", "merged")
        self.assertIsNone(self.session.store.get(merged.id))
        self.assertIsNotNone(self.session.store.get(failed.id))

    def test_clear_finished_keeps_unfinished(self):
        merged = self.add(Status.MERGED)
        active = self.add(Status.PLANNING)
        self.run_command("clear", "finished")
        self.assertIsNone(self.session.store.get(merged.id))
        self.assertIsNotNone(self.session.store.get(active.id))

    def test_clear_all_empties_it(self):
        self.add(Status.MERGED)
        self.add(Status.FAILED)
        self.run_command("clear", "all")
        self.assertEqual(self.session.store.all(), [])

    def test_a_running_workflow_survives_clear_all(self):
        active = self.add(Status.EXECUTING)
        self.add(Status.MERGED)
        self.session.runner.is_running.side_effect = lambda i: i == active.id
        self.run_command("clear", "all")
        self.assertIsNotNone(self.session.store.get(active.id))

    def test_declining_changes_nothing(self):
        self.add(Status.MERGED)
        self.confirm = False
        self.run_command("clear", "all")
        self.assertEqual(len(self.session.store.all()), 1)

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("clear", "everything")


class Settings(ShellTestCase):
    def test_set_writes_a_value(self):
        self.run_command("set", "mode", "auto")
        self.assertEqual(self.session.settings["mode"], "autonomous")
        self.assertEqual(settings_mod.load()["mode"], "autonomous")

    def test_set_validates(self):
        with self.assertRaises(CommandError):
            self.run_command("set", "rounds", "zero")
        with self.assertRaises(CommandError) as ctx:
            self.run_command("set", "nonsense", "1")
        self.assertIn("not a setting", str(ctx.exception))

    def test_set_needs_a_value(self):
        with self.assertRaises(CommandError):
            self.run_command("set", "mode")

    def test_config_shows_the_defaults(self):
        out = self.run_command("config")
        for key in ("repo", "mode", "rounds", "attempts", "confirm"):
            self.assertIn(key, out)

    def test_confirm_can_be_switched_off(self):
        self.run_command("set", "confirm", "off")
        self.assertIs(self.session.settings["confirm"], False)


class Prompts(ShellTestCase):
    def test_prompts_lists_what_it_finds(self):
        (self.root / "docs" / "prompts").mkdir(parents=True)
        (self.root / "docs" / "prompts" / "scope.md").write_text("x", encoding="utf-8")
        out = self.run_command("prompts")
        self.assertIn("@scope", out)
        self.assertIn("docs/prompts", out)

    def test_prompts_with_none_explains_where_it_looked(self):
        out = self.run_command("prompts")
        self.assertIn("no prompt files found", out)
        self.assertIn("current directory", out)

    def test_a_root_can_be_added_and_removed(self):
        extra = self.root / "library"
        extra.mkdir()
        (extra / "thing.md").write_text("x", encoding="utf-8")

        self.run_command("prompts", "add", str(extra))
        self.assertIn(str(extra.resolve()), self.session.settings["prompt_paths"])
        self.assertIn("@thing", self.run_command("prompts"))

        self.run_command("prompts", "rm", str(extra))
        self.assertEqual(self.session.settings["prompt_paths"], [])

    def test_adding_a_missing_directory_is_refused(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_command("prompts", "add", str(self.root / "nope"))
        self.assertIn("no such directory", str(ctx.exception))


class TaskStartTestCase(ShellTestCase):
    """A session where typing a task reaches a fake git and fake backends.

    Carries no tests of its own so other suites can build on it without
    dragging this one's cases along.
    """

    def setUp(self):
        super().setUp()
        self.git = mock.patch.object(commands, "gitops").start()
        self.addCleanup(mock.patch.stopall)
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

        mock.patch.object(
            commands, "_installed", return_value=True
        ).start()
        self.session.settings.update(
            {"plan": "claude", "execute": "opencode", "review": "codex"}
        )

    def started(self):
        self.session.runner.start.assert_called_once()
        return self.session.runner.start.call_args.args[0]


class StartingRuns(TaskStartTestCase):
    """Typing a task is the primary action, so it gets the closest look."""

    def test_typing_a_task_starts_a_workflow(self):
        out = self.do("add rate limiting to the API")
        wf = self.started()
        self.assertEqual(wf.prompt, "add rate limiting to the API")
        self.assertIs(wf.mode, Mode.SUPERVISED)
        self.assertIn("started", out)

    def test_the_summary_is_shown_before_it_runs(self):
        out = self.do("add rate limiting")
        self.assertIn("feature/add", out)
        for cell in ("plan", "execute", "review", "merge", "claude", "codex"):
            self.assertIn(cell, out)
        self.assertIn("supervised", out)

    def test_declining_the_confirmation_starts_nothing(self):
        self.lines = ["cancel"]
        out = self.do("add rate limiting")
        self.session.runner.start.assert_not_called()
        self.assertIn("cancelled", out)
        self.assertEqual(self.session.store.all(), [])

    def test_confirmation_can_be_turned_off(self):
        self.session.settings["confirm"] = False
        self.session.ask_action = mock.Mock(side_effect=AssertionError("asked!"))
        self.do("add rate limiting")
        self.started()

    def test_inline_flags_override_the_defaults(self):
        self.do("add rate limiting --mode auto --execute codex --rounds 4 --attempts 6")
        wf = self.started()
        self.assertIs(wf.mode, Mode.AUTONOMOUS)
        self.assertEqual(wf.agents["execute"], "codex")
        self.assertEqual(wf.max_rounds, 4)
        self.assertEqual(wf.max_attempts, 6)

    def test_flags_do_not_change_the_saved_defaults(self):
        self.do("add rate limiting --mode auto")
        self.assertEqual(self.session.settings["mode"], "supervised")
        self.assertEqual(settings_mod.load()["mode"], "supervised")

    def test_a_branch_flag_without_a_type_gets_one(self):
        self.do("add rate limiting --branch scope-lookup")
        self.assertEqual(self.started().branch, "feature/scope-lookup")

    def test_a_branch_flag_with_a_type_is_kept(self):
        self.do("add rate limiting --branch fix/scope-lookup")
        self.assertEqual(self.started().branch, "fix/scope-lookup")

    def test_a_dirty_repository_stops_it(self):
        self.git.validate_target.side_effect = commands.GitError(
            "has uncommitted changes"
        )
        out = self.do("add rate limiting")
        self.session.runner.start.assert_not_called()
        self.assertIn("uncommitted changes", out)

    def test_an_uninstalled_agent_stops_it(self):
        with mock.patch.object(commands, "_installed", return_value=False):
            out = self.do("add rate limiting")
        self.session.runner.start.assert_not_called()
        self.assertIn("not installed", out)

    def test_an_unknown_agent_stops_it(self):
        out = self.do("add rate limiting --plan nosuchcli")
        self.session.runner.start.assert_not_called()
        self.assertIn("unknown backend", out)

    def test_a_reference_loads_the_file_as_the_task(self):
        prompts = self.root / "docs" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "scope-spec.md").write_text(
            "# Scope spec\n\nAdd custom fields.", encoding="utf-8"
        )
        self.session.prompt_files(refresh=True)

        out = self.do("@scope-spec")
        wf = self.started()
        self.assertIn("Add custom fields", wf.prompt)
        self.assertTrue(wf.prompt_source.endswith("scope-spec.md"))
        self.assertIn("from", out)

    def test_text_after_a_reference_is_appended(self):
        prompts = self.root / "docs" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "spec.md").write_text("Base requirement.", encoding="utf-8")
        self.session.prompt_files(refresh=True)

        self.do("@spec and also add tests")
        wf = self.started()
        self.assertIn("Base requirement.", wf.prompt)
        self.assertIn("Additionally: and also add tests", wf.prompt)

    def test_an_unknown_reference_stops_it(self):
        out = self.do("@nothing-here")
        self.session.runner.start.assert_not_called()
        self.assertIn("no saved prompt matches", out)

    def test_a_reference_can_take_flags(self):
        prompts = self.root / "docs" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "spec.md").write_text("Do the thing.", encoding="utf-8")
        self.session.prompt_files(refresh=True)

        self.do("@spec --mode auto")
        self.assertIs(self.started().mode, Mode.AUTONOMOUS)

    def test_choosing_to_change_walks_through_every_setting(self):
        # c, then Enter for each field (the queue runs out and defaults apply),
        # then Enter at the final confirm.
        self.lines = ["change"]
        self.do("add rate limiting")
        asked = [label for label, _ in self.asked]
        for expected in ("plan agent", "plan model", "execute agent", "execute model",
                         "review agent", "review model",
                         "mode", "rework rounds",
                         "attempts per step", "branch"):
            self.assertIn(expected, asked)

    def test_every_field_offers_the_current_value_as_the_default(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        defaults = dict(self.asked)
        self.assertEqual(defaults["plan agent"], "claude")
        self.assertEqual(defaults["execute agent"], "opencode")
        self.assertEqual(defaults["review agent"], "codex")
        self.assertEqual(defaults["mode"], "supervised")
        self.assertEqual(defaults["rework rounds"], "2")
        self.assertEqual(defaults["attempts per step"], "3")

    def test_pressing_enter_through_the_edit_keeps_the_defaults(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        wf = self.started()
        self.assertEqual(wf.agents, {"plan": "claude", "execute": "opencode",
                                     "review": "codex"})
        self.assertIs(wf.mode, Mode.SUPERVISED)
        self.assertEqual(wf.max_rounds, 2)

    def test_typed_values_are_used(self):
        self.lines = [
            "change",
            "codex", "",        # plan agent, plan model
            "claude", "",       # execute
            "opencode", "",     # review
            "autonomous", "4", "6", "fix/my-branch",
        ]
        self.do("add rate limiting")
        wf = self.started()
        self.assertEqual(wf.agents, {"plan": "codex", "execute": "claude",
                                     "review": "opencode"})
        self.assertIs(wf.mode, Mode.AUTONOMOUS)
        self.assertEqual(wf.max_rounds, 4)
        self.assertEqual(wf.max_attempts, 6)
        self.assertEqual(wf.branch, "fix/my-branch")

    def test_a_bad_value_is_asked_again(self):
        self.lines = ["change", "claude", "", "opencode", "", "codex", "", "autonomous"]
        out = self.do("add rate limiting")
        self.assertIn("supervised", out)  # the complaint
        self.assertIs(self.started().mode, Mode.AUTONOMOUS)

    def test_an_unknown_agent_is_asked_again(self):
        self.lines = ["change", "nosuchcli", "claude"]
        out = self.do("add rate limiting")
        self.assertIn("unknown backend", out)
        self.assertEqual(self.started().agents["plan"], "claude")

    def test_a_zero_round_count_is_asked_again(self):
        self.lines = ["change", "claude", "", "opencode", "", "codex", "", "supervised", "0", "2"]
        out = self.do("add rate limiting")
        self.assertIn("1 or more", out)
        self.assertEqual(self.started().max_rounds, 2)

    def test_edits_can_be_saved_as_the_new_defaults(self):
        self.lines = ["change", "codex", "", "codex", "", "codex", "", "autonomous", "5", "7"]
        self.confirm = True  # yes to "save these as your defaults?"
        self.do("add rate limiting")

        saved = settings_mod.load()
        self.assertEqual(saved["plan"], "codex")
        self.assertEqual(saved["mode"], "autonomous")
        self.assertEqual(saved["rounds"], 5)
        self.assertEqual(saved["attempts"], 7)

    def test_edits_are_not_saved_unless_asked(self):
        self.lines = ["change", "codex", "", "codex", "", "codex", "", "autonomous", "5", "7"]
        self.confirm = False  # no to saving
        self.do("add rate limiting")

        saved = settings_mod.load()
        self.assertEqual(saved["mode"], "supervised")
        self.assertNotEqual(saved.get("rounds"), 5)
        # ...but this run still used them
        self.assertEqual(self.started().max_rounds, 5)

    def test_an_unrecognised_answer_reprompts_rather_than_running(self):
        self.lines = ["what?", "run"]
        out = self.do("add rate limiting")
        self.assertIn("choose run, change or cancel", out)
        self.started()  # the second answer ran it

    def test_a_model_is_offered_for_every_role(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        asked = [label for label, _ in self.asked]
        self.assertIn("plan model", asked)
        self.assertIn("execute model", asked)
        self.assertIn("review model", asked)

    def test_a_chosen_model_reaches_the_workflow(self):
        self.lines = [
            "change",
            "claude", "opus-4.6",
            "opencode", "opencode-go/glm-5.2",
            "codex", "",
        ]
        self.do("add rate limiting")
        wf = self.started()
        self.assertEqual(wf.models["plan"], "opus-4.6")
        self.assertEqual(wf.models["execute"], "opencode-go/glm-5.2")
        self.assertNotIn("review", wf.models)  # blank means the CLI's default

    def test_a_blank_model_leaves_the_backend_alone(self):
        self.lines = ["change"]
        self.do("add rate limiting")
        self.assertEqual(self.started().models, {})

    def test_the_model_flag_applies_to_every_role(self):
        self.do("add rate limiting --model sonnet-4.6")
        wf = self.started()
        self.assertEqual(
            wf.models, {"plan": "sonnet-4.6", "execute": "sonnet-4.6",
                        "review": "sonnet-4.6"}
        )

    def test_a_per_role_model_flag_wins(self):
        self.do("add rate limiting --model sonnet --execute-model opus")
        wf = self.started()
        self.assertEqual(wf.models["execute"], "opus")
        self.assertEqual(wf.models["plan"], "sonnet")

    def test_models_are_shown_in_the_summary(self):
        out = self.do("add rate limiting --model sonnet-4.6")
        self.assertIn("sonnet-4.6", out)
        self.assertIn("plan sonnet-4.6", " ".join(out.split()))

    def test_a_model_default_can_be_set_and_is_used(self):
        self.session.settings["execute_model"] = "opencode-go/glm-5.2"
        self.do("add rate limiting")
        self.assertEqual(self.started().models["execute"], "opencode-go/glm-5.2")

    def test_models_are_saved_with_the_other_defaults(self):
        self.lines = ["change", "claude", "opus-4.6", "opencode", "", "codex", ""]
        self.confirm = True
        self.do("add rate limiting")
        self.assertEqual(settings_mod.load()["plan_model"], "opus-4.6")

    def test_the_logs_are_followed_as_soon_as_it_starts(self):
        self.session.settings["follow"] = True
        with mock.patch.object(commands.logview, "stream") as stream:
            out = self.do("add rate limiting")
        stream.assert_called_once()
        self.assertIs(stream.call_args.args[0], self.started())
        self.assertIsNone(stream.call_args.args[1])  # every step
        self.assertIn("Ctrl-C returns to the prompt", out)

    def test_following_can_be_turned_off(self):
        self.session.settings["follow"] = False
        with mock.patch.object(commands.logview, "stream") as stream:
            self.do("add rate limiting")
        stream.assert_not_called()

    def test_leaving_the_log_does_not_replay_it_as_a_notice(self):
        self.session.settings["follow"] = True
        with mock.patch.object(commands.logview, "stream"):
            self.do("add rate limiting")
        wf = self.started()
        wf.status = Status.EXECUTING
        # Seeded again on the way out, so only changes from here are announced.
        self.assertEqual(len(self.session.notices.drain([wf])), 1)

    def test_the_new_run_is_seeded_so_it_is_not_announced_late(self):
        self.do("add rate limiting")
        wf = self.started()
        self.assertEqual(self.session.notices.drain([wf]), [])


class Frame(ShellTestCase):
    """The header stays put; the footer rides just above the prompt."""

    def draw(self, fn):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            fn()
        return ANSI.sub("", buffer.getvalue())

    def test_the_header_is_the_banner_closed_off_by_a_divider(self):
        drawn = self.draw(self.shell.header)
        self.assertIn("MAESTRO", drawn)
        self.assertIn(render._box().h * 10, drawn)

    def test_the_header_no_longer_carries_the_status_bar(self):
        self.add()
        self.assertNotIn("1 run", self.draw(self.shell.header))

    def test_the_footer_carries_the_status_bar(self):
        self.add()
        self.assertIn("1 run", self.draw(self.shell.footer))

    def test_the_footer_is_not_replayed_into_the_content_area(self):
        """Otherwise the status line marches down the screen on each redraw."""
        self.add()
        with redirect_stdout(io.StringIO()):
            # The loop's order: draw the footer, then clear the recording so
            # the turn starts on a clean slate, then run the command.
            self.shell.footer()
            render.take_output()
            render.out("the result of a command")
            replayed = ANSI.sub("", render.take_output())

        self.assertIn("the result of a command", replayed)
        self.assertNotIn("1 run", replayed)

    def sized(self, height):
        console = Console(theme=render.THEME, file=io.StringIO(), width=100,
                          height=height, force_terminal=False,
                          legacy_windows=False, record=True, highlight=False)
        return mock.patch.object(render, "console", console)

    def test_output_room_leaves_space_for_the_footer(self):
        with self.sized(40):
            room = self.shell.output_room()
        chrome = len(render.WORDMARK) + 3 + 2 + 3 + 1
        self.assertEqual(room, 40 - chrome)

    def test_a_short_terminal_still_gets_some_room(self):
        with self.sized(5):
            self.assertGreaterEqual(self.shell.output_room(), 4)


class Notices(unittest.TestCase):
    def make(self, status=Status.PENDING, tasks=None):
        wf = Workflow(
            prompt="a task", repo_path="/r", base_branch="main",
            agents={"plan": "a", "execute": "a", "review": "a"},
        )
        wf.status = status
        wf.tasks = tasks or []
        return wf

    def test_nothing_is_said_about_an_unchanged_run(self):
        board = repl.Notices()
        wf = self.make()
        board.seed(wf)
        self.assertEqual(board.drain([wf]), [])

    def test_a_first_sighting_is_not_announced_after_seeding(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        self.assertEqual(board.drain([wf]), [])

    def test_a_status_change_is_announced(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        wf.status = Status.MERGED
        lines = " ".join(t.plain for t in board.drain([wf]))
        self.assertIn("merged", lines)
        self.assertIn(wf.id[:6], lines)

    def test_checklist_progress_is_announced(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING, tasks=[Task(1, "a"), Task(2, "b")])
        board.seed(wf)
        wf.tasks[0].status = Status.DONE
        lines = " ".join(t.plain for t in board.drain([wf]))
        self.assertIn("1/2 tasks done", lines)

    def test_awaiting_approval_points_at_the_command(self):
        board = repl.Notices()
        wf = self.make(Status.REVIEWING)
        board.seed(wf)
        wf.status = Status.AWAITING_APPROVAL
        lines = " ".join(t.plain for t in board.drain([wf]))
        self.assertIn("awaiting approval", lines)
        self.assertIn("/approve", lines)

    def test_a_change_is_announced_only_once(self):
        board = repl.Notices()
        wf = self.make(Status.EXECUTING)
        board.seed(wf)
        wf.status = Status.MERGED
        self.assertEqual(len(board.drain([wf])), 1)
        self.assertEqual(board.drain([wf]), [])

    def test_a_forgotten_run_starts_fresh(self):
        board = repl.Notices()
        wf = self.make(Status.MERGED)
        board.seed(wf)
        board.forget(wf.id)
        self.assertEqual(board.drain([wf]), [])  # re-seeded, not announced


if __name__ == "__main__":
    unittest.main()
