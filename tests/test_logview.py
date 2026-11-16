"""Reading step logs, after the fact or while a run is still going."""

import io
import re

import pytest
from rich.console import Console

from maestro.models import Role, Status, Step, Workflow
from maestro.shell import logview, render

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def screen(monkeypatch):
    """Capture what the shell prints, with the sleeps between polls removed."""
    buffer = io.StringIO()
    console = Console(theme=render.THEME, file=buffer, width=100, force_terminal=False,
                      legacy_windows=False, highlight=False)
    monkeypatch.setattr(render, "console", console)
    monkeypatch.setattr(logview.time, "sleep", lambda seconds: None)

    def printed():
        return ANSI.sub("", buffer.getvalue())
    return printed


def make(status=Status.MERGED, **kwargs):
    wf = Workflow(
        prompt="a task", repo_path="/r", base_branch="main",
        agents={"plan": "claude", "execute": "opencode", "review": "codex"}, **kwargs,
    )
    wf.status = status
    return wf


def step_with_log(wf, tmp_path, role=Role.EXECUTE, text="", status=Status.DONE, name=None):
    step = wf.add_step(role)
    step.status = status
    step.exit_code = 0 if status is Status.DONE else 1
    step.started_at, step.ended_at = 0.0, 5.0
    path = tmp_path / (name or f"{role.value}-{len(wf.steps)}.log")
    path.write_text(text, encoding="utf-8")
    step.log_path = str(path)
    return step


class TestDrain:
    def test_given_new_text_when_drained_then_only_what_was_appended_is_printed(self, tmp_path, capsys):
        log = tmp_path / "step.log"
        log.write_text("one\n", encoding="utf-8")
        position = logview.drain(log, 0)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("two\n")

        assert logview.drain(log, position) == log.stat().st_size
        assert capsys.readouterr().out == "one\ntwo\n"

    def test_given_nothing_new_when_drained_then_nothing_is_printed_and_the_position_holds(self, tmp_path, capsys):
        log = tmp_path / "step.log"
        log.write_text("one\n", encoding="utf-8")
        end = logview.drain(log, 0)
        capsys.readouterr()
        assert logview.drain(log, end) == end
        assert capsys.readouterr().out == ""

    def test_given_a_log_not_written_yet_when_drained_then_the_position_is_unchanged(self, tmp_path):
        assert logview.drain(tmp_path / "missing.log", 7) == 7


class TestTail:
    def test_given_a_long_log_when_tailed_then_the_last_non_empty_lines_are_kept(self, tmp_path):
        log = tmp_path / "step.log"
        log.write_text("a\n\nb\n  \nc\nd\n", encoding="utf-8")
        assert logview.tail(log, 2) == "c\nd"

    def test_given_an_empty_log_when_tailed_then_it_says_so(self, tmp_path):
        log = tmp_path / "step.log"
        log.write_text("\n\n", encoding="utf-8")
        assert logview.tail(log, 5) == "(the log is empty)"

    def test_given_an_unreadable_log_when_tailed_then_the_reason_is_returned(self, tmp_path):
        assert logview.tail(tmp_path / "missing.log", 5).startswith("(could not read the log:")


class TestStepMarker:
    def test_given_a_step_starting_on_a_task_when_marked_then_role_agent_and_task_are_named(self):
        step = Step(role=Role.EXECUTE, agent="opencode", task_index=2)
        assert logview.step_marker(step, starting=True).title.plain == "execute | opencode | task 2"

    def test_given_a_step_starting_without_a_task_when_marked_then_the_round_is_named(self):
        step = Step(role=Role.REVIEW, agent="codex", round=3)
        assert logview.step_marker(step, starting=True).title.plain == "review | codex | round 3"

    def test_given_a_finished_step_when_marked_then_its_exit_code_and_time_are_shown(self):
        step = Step(role=Role.PLAN, agent="claude", status=Status.DONE, exit_code=0,
                    started_at=0.0, ended_at=12.0)
        marker = logview.step_marker(step, starting=False)
        assert marker.title.plain == f"plan finished | exit 0 | {render.elapsed(12.0)}"
        assert marker.style == "success"

    def test_given_a_step_that_never_ran_when_marked_finished_then_its_status_stands_in(self):
        step = Step(role=Role.PLAN, agent="claude", status=Status.FAILED)
        marker = logview.step_marker(step, starting=False)
        assert "plan finished | failed |" in marker.title.plain
        assert marker.style == "error"


class TestStream:
    def test_given_a_finished_run_when_streamed_then_every_log_is_printed_between_markers(
            self, tmp_path, screen, capsys):
        wf = make(Status.MERGED)
        wf.message = "merged 'feature/x' into 'main'"
        step_with_log(wf, tmp_path, Role.PLAN, "the plan\n")
        step_with_log(wf, tmp_path, Role.EXECUTE, "did the work\n")

        logview.stream(wf, None, lambda workflow_id: False)

        assert capsys.readouterr().out == "the plan\ndid the work\n"
        shown = screen()
        assert shown.index("plan | claude") < shown.index("plan finished")
        assert shown.index("execute | opencode") < shown.index("execute finished")
        assert "end of log for all steps" in shown
        assert "merged 'feature/x'" in shown

    def test_given_a_role_filter_when_streamed_then_only_that_step_is_printed(self, tmp_path, screen, capsys):
        wf = make(Status.MERGED)
        step_with_log(wf, tmp_path, Role.PLAN, "the plan\n")
        step_with_log(wf, tmp_path, Role.EXECUTE, "did the work\n")

        logview.stream(wf, {"execute"}, lambda workflow_id: False)

        assert capsys.readouterr().out == "did the work\n"
        assert "end of log for the execute step" in screen()

    def test_given_a_running_step_when_streamed_then_it_is_followed_until_it_finishes(
            self, tmp_path, screen, capsys):
        wf = make(Status.EXECUTING)
        step = step_with_log(wf, tmp_path, Role.EXECUTE, "first\n", status=Status.RUNNING)
        polls = []

        def is_running(workflow_id):
            polls.append(workflow_id)
            if len(polls) == 1:
                with open(step.log_path, "a", encoding="utf-8") as handle:
                    handle.write("second\n")
            elif len(polls) == 2:
                step.status = Status.DONE
                wf.status = Status.MERGED
            return len(polls) < 3

        logview.stream(wf, None, is_running)

        assert capsys.readouterr().out == "first\nsecond\n"
        assert screen().count("execute finished") == 1

    def test_given_a_failed_run_with_notes_when_streamed_then_the_failure_and_the_notes_are_shown(
            self, tmp_path, screen):
        wf = make(Status.FAILED)
        wf.message = "the execute step committed nothing"
        wf.notes = ["opencode ran past its timeout"]
        step_with_log(wf, tmp_path, Role.EXECUTE, "x\n", status=Status.FAILED)

        logview.stream(wf, None, lambda workflow_id: False)

        shown = screen()
        assert "committed nothing" in shown
        assert "warning: opencode ran past its timeout" in shown

    def test_given_ctrl_c_while_following_when_streamed_then_it_stops_and_says_the_run_continues(
            self, tmp_path, screen):
        wf = make(Status.EXECUTING)
        step_with_log(wf, tmp_path, Role.EXECUTE, "", status=Status.RUNNING)

        def interrupted(workflow_id):
            raise KeyboardInterrupt

        logview.stream(wf, None, interrupted)

        shown = screen()
        assert "stopped following - the run continues" in shown
        assert "end of log" not in shown

    def test_given_a_step_without_a_log_yet_when_streamed_then_only_its_markers_are_printed(self, screen, capsys):
        wf = make(Status.FAILED)
        step = wf.add_step(Role.PLAN)
        step.status = Status.FAILED

        logview.stream(wf, None, lambda workflow_id: False)

        assert capsys.readouterr().out == ""
        assert "plan finished" in screen()


class TestFailureContext:
    def test_given_a_failed_step_when_asked_then_its_log_tail_is_panelled(self, tmp_path):
        wf = make(Status.FAILED)
        step_with_log(wf, tmp_path, Role.PLAN, "fine\n")
        step_with_log(wf, tmp_path, Role.EXECUTE, "boom\n", status=Status.FAILED)
        step_with_log(wf, tmp_path, Role.REVIEW, "later\n")

        blank, panel = logview.failure_context(wf)
        assert blank == ""
        assert panel.title == "last output of the execute step"
        assert panel.renderable.plain == "boom"

    def test_given_no_failed_step_when_asked_then_the_last_step_is_used(self, tmp_path):
        wf = make(Status.FAILED)
        step_with_log(wf, tmp_path, Role.PLAN, "first\n")
        step_with_log(wf, tmp_path, Role.EXECUTE, "last\n")
        assert logview.failure_context(wf)[1].renderable.plain == "last"

    def test_given_no_steps_when_asked_then_there_is_nothing_to_show(self):
        assert logview.failure_context(make(Status.FAILED)) == []

    def test_given_a_step_whose_log_is_gone_when_asked_then_there_is_nothing_to_show(self, tmp_path):
        wf = make(Status.FAILED)
        step = wf.add_step(Role.EXECUTE)
        step.status = Status.FAILED
        step.log_path = str(tmp_path / "deleted.log")
        assert logview.failure_context(wf) == []

    def test_given_a_step_with_no_log_path_when_asked_then_there_is_nothing_to_show(self):
        wf = make(Status.FAILED)
        wf.add_step(Role.EXECUTE).status = Status.FAILED
        assert logview.failure_context(wf) == []
