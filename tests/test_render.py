"""The rich rendering layer and the questionary prompt fallbacks."""

import io
import re
from unittest import mock

import pytest
from rich.console import Console, Group
from rich.text import Text

from maestro import config
from maestro.models import Mode, Role, Status, Task, Workflow
from maestro.shell import prompts, render

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def draw(renderable, width=100) -> str:
    """Render to plain text, the way a terminal would show it."""
    buffer = io.StringIO()
    console = Console(theme=render.THEME, file=buffer, width=width,
                      force_terminal=False, legacy_windows=False, highlight=False)
    console.print(renderable)
    return buffer.getvalue()


def make(prompt="add rate limiting", status=Status.MERGED, **kwargs):
    wf = Workflow(
        prompt=prompt, repo_path="/repo", base_branch="main",
        agents={"plan": "claude", "execute": "opencode", "review": "codex"},
        **kwargs,
    )
    wf.status = status
    wf.branch = "feature/rate-limiting"
    return wf


class TestMessages:
    def capture(self, fn, message):
        buffer = io.StringIO()
        console = Console(theme=render.THEME, file=buffer, width=100,
                          force_terminal=False, legacy_windows=False)
        with mock.patch.object(render, "console", console):
            fn(message)
        return buffer.getvalue()

    def test_given_each_message_level_when_printed_then_the_message_appears(self):
        for fn in (render.success, render.warn, render.error,
                   render.info, render.muted, render.hint):
            assert "something happened" in self.capture(fn, "something happened")

    def test_given_an_error_when_printed_then_it_is_marked(self):
        assert "x" in self.capture(render.error, "it broke")

    def test_given_a_hint_when_printed_then_it_points_with_an_arrow(self):
        assert "->" in self.capture(render.hint, "/approve 7f2a1c")


class TestRunTable:
    def test_given_runs_when_tabulated_then_every_column_is_present(self):
        out = draw(render.run_table([make()], lambda i: False))
        for header in ("ID", "STATUS", "DONE", "AGE", "TASK"):
            assert header in out

    def test_given_a_run_when_tabulated_then_its_row_shows_id_status_and_task(self):
        wf = make(prompt="add rate limiting to the API")
        out = draw(render.run_table([wf], lambda i: False))
        assert wf.id[:6] in out
        assert "merged" in out
        assert "add rate limiting" in out

    def test_given_a_running_workflow_when_tabulated_then_it_is_marked(self):
        out = draw(render.run_table([make()], lambda i: True))
        assert "*" in out

    def test_given_a_checklist_when_tabulated_then_its_progress_replaces_the_round_counter(self):
        wf = make()
        wf.tasks = [Task(1, "a", Status.DONE), Task(2, "b")]
        assert "1/2" in draw(render.run_table([wf], lambda i: False))

    def test_given_no_runs_when_tabulated_then_it_says_so(self):
        assert "no runs yet" in draw(render.run_table([], lambda i: False))


class TestRunDetail:
    def render_all(self, wf, running=False):
        return "".join(draw(part) for part in render.run_detail(wf, lambda i: running))

    def test_given_a_run_when_its_detail_is_drawn_then_the_facts_are_shown(self):
        wf = make(models={"execute": "glm-5.2"}, mode=Mode.AUTONOMOUS)
        wf.prompt_source = "docs/prompts/spec.md"
        out = self.render_all(wf)
        assert "feature/rate-limiting" in out
        assert "plan claude" in out
        assert "execute opencode" in out
        assert "review codex" in out
        assert "glm-5.2" in out
        assert "autonomous" in out
        assert "docs/prompts/spec.md" in out

    def test_given_a_run_when_its_detail_is_drawn_then_the_task_text_appears(self):
        assert "add rate limiting" in self.render_all(make())

    def test_given_checklist_marks_when_drawn_then_they_survive_markup_parsing(self):
        """"[x]" is valid rich markup; it must not be swallowed."""
        wf = make()
        wf.tasks = [Task(1, "first", Status.DONE), Task(2, "second"),
                    Task(3, "third", Status.FAILED)]
        out = self.render_all(wf)
        assert "[x]" in out
        assert "[ ]" in out
        assert "[!]" in out

    def test_given_square_brackets_in_a_task_when_drawn_then_they_are_not_eaten(self):
        wf = make(prompt="handle [bold] tags and [/] closers")
        out = self.render_all(wf)
        assert "[bold]" in out

    def test_given_steps_when_the_detail_is_drawn_then_they_are_listed(self):
        wf = make()
        step = wf.add_step(Role.EXECUTE, task_index=2)
        step.status = Status.DONE
        step.started_at, step.ended_at = 0.0, 12.0
        out = self.render_all(wf)
        assert "execute" in out
        assert "task 2" in out
        assert "12s" in out

    def test_given_a_failure_message_when_the_detail_is_drawn_then_it_is_shown(self):
        wf = make(status=Status.FAILED)
        wf.message = "the opencode execute step committed nothing"
        assert "committed nothing" in self.render_all(wf)

    def test_given_notes_when_the_detail_is_drawn_then_they_are_shown(self):
        wf = make()
        wf.notes = ["ran past its timeout, but its work was committed"]
        assert "past its timeout" in self.render_all(wf)


class TestSummary:
    def panel(self, **kwargs):
        args = dict(
            branch="feature/thing",
            agents={"plan": "claude", "execute": "opencode", "review": "codex"},
            mode="supervised", rounds=2, attempts=3,
        )
        args.update(kwargs)
        return draw(render.summary_panel(**args))

    def test_given_a_planned_run_when_summarised_then_it_shows_what_will_happen(self):
        out = self.panel()
        assert "feature/thing" in out
        for cell in ("plan", "execute", "review", "merge"):
            assert cell in out
        for agent in ("claude", "opencode", "codex"):
            assert agent in out
        assert "supervised" in out
        assert "2 rounds" in out

    def test_given_chosen_models_when_summarised_then_only_those_appear(self):
        out = self.panel(models={"execute": "glm-5.2", "plan": ""})
        assert "execute glm-5.2" in out
        assert "plan " not in out.split("execute glm-5.2")[1]

    def test_given_a_source_file_when_summarised_then_it_appears(self):
        assert "spec.md" in self.panel(source="docs/prompts/spec.md")


class TestTables:
    def test_given_the_backends_when_tabulated_then_each_is_listed(self):
        out = draw(render.backend_table(config.DEFAULT_BACKENDS), width=140)
        for name in ("claude", "codex", "opencode"):
            assert name in out
        assert "BACKEND" in out

    def test_given_settings_when_tabulated_then_blank_ones_show_auto(self):
        out = draw(render.settings_table({"mode": "supervised", "repo": ""},
                                         ["mode", "repo"]))
        assert "supervised" in out
        assert "(auto)" in out

    def test_given_prompt_files_when_tabulated_then_each_is_listed_as_a_reference(self):
        item = mock.Mock(name="scope", label="docs/prompts/scope.md",
                         source="docs/prompts")
        item.name = "scope"
        out = draw(render.prompt_table([item]))
        assert "@scope" in out
        assert "docs/prompts" in out

    def test_given_help_entries_when_tabulated_then_each_is_listed(self):
        out = draw(render.help_table([("/runs", "every run"), ("", "")]))
        assert "/runs" in out
        assert "every run" in out


class TestBanner:
    def test_given_the_wordmark_when_checked_then_it_is_pure_ascii(self):
        for line in render.WORDMARK:
            assert all(ord(c) < 128 for c in line)

    def test_given_a_wide_terminal_when_the_banner_is_drawn_then_it_shows_the_wordmark(self):
        out = "".join(draw(p) for p in render.banner(100, "2.0.0"))
        assert "MAESTRO" in out
        assert "v2.0.0" in out
        assert render.WORDMARK[0].strip() in out

    def test_given_a_narrow_terminal_when_the_banner_is_drawn_then_it_shows_a_plain_title(self):
        out = "".join(draw(p, width=30) for p in render.banner(30, "2.0.0"))
        assert "MAESTRO" in out
        assert render.WORDMARK[0].strip() not in out


class TestPromptFallbacks:
    """Without a terminal, questionary cannot run; input() must take over."""

    @pytest.fixture(autouse=True)
    def _asker(self):
        self.asker = prompts.Asker(force_plain=True)

    def test_given_typed_text_when_a_line_is_asked_then_it_is_returned_stripped(self):
        with mock.patch("builtins.input", return_value="  codex  "):
            assert self.asker.line("agent", "claude") == "codex"

    def test_given_an_empty_answer_when_a_line_is_asked_then_the_default_is_kept(self):
        with mock.patch("builtins.input", return_value=""):
            assert self.asker.line("agent", "claude") == "claude"

    def test_given_end_of_input_when_a_line_is_asked_then_the_default_is_kept(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            assert self.asker.line("agent", "claude") == "claude"

    def test_given_an_empty_answer_when_confirming_then_the_default_decides(self):
        with mock.patch("builtins.input", return_value=""):
            assert self.asker.confirm("Run it?", default=True)
            assert not self.asker.confirm("Delete it?", default=False)

    def test_given_y_when_confirming_then_it_is_a_yes(self):
        with mock.patch("builtins.input", return_value="y"):
            assert self.asker.confirm("Delete it?", default=False)

    def test_given_no_terminal_when_selecting_then_the_choice_is_typed(self):
        with mock.patch("builtins.input", return_value="codex"):
            assert self.asker.select("agent", ["claude", "codex"], "claude") == "codex"

    def test_given_no_options_when_selecting_then_it_still_asks(self):
        with mock.patch("builtins.input", return_value="typed"):
            assert self.asker.select("model", [], "") == "typed"

    def test_given_no_terminal_when_the_command_prompt_is_shown_then_it_reads_a_line(self):
        with mock.patch("builtins.input", return_value="/runs"):
            assert self.asker.command() == "/runs"

    def test_given_a_pipe_on_stdin_when_checked_then_it_is_not_interactive(self):
        with mock.patch.object(prompts.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            assert not prompts.interactive()

    def test_given_no_stdin_at_all_when_checked_then_it_is_not_interactive(self, monkeypatch):
        monkeypatch.setattr(prompts.sys, "stdin", None)
        assert not prompts.interactive()

    def test_given_a_default_when_a_plain_line_is_asked_then_the_default_is_shown_in_brackets(self):
        with mock.patch("builtins.input", return_value="") as typed:
            self.asker.line("agent", "claude")
        assert typed.call_args.args[0] == "  agent [claude]: "

    @pytest.mark.parametrize("default, suffix", [(True, "[Y/n]"), (False, "[y/N]")])
    def test_given_a_default_when_a_plain_confirm_is_asked_then_the_suffix_shows_it(self, default, suffix):
        with mock.patch("builtins.input", return_value="") as typed:
            self.asker.confirm("Go?", default=default)
        assert typed.call_args.args[0].endswith(suffix + " ")

    def test_given_end_of_input_when_confirming_then_the_default_is_kept(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            assert self.asker.confirm("Go?", default=False) is False

    def test_given_no_terminal_when_the_command_prompt_is_shown_then_the_prompt_is_used_as_is(self):
        with mock.patch("builtins.input", return_value="") as typed:
            self.asker.command(">")
        assert typed.call_args.args[0] == "> "


class FakeQuestion:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    def unsafe_ask(self):
        if self.error:
            raise self.error
        return self.answer


class TestInteractivePrompts:
    """With a terminal attached, questionary asks; a broken one falls back to input()."""

    @pytest.fixture(autouse=True)
    def _terminal(self, monkeypatch):
        monkeypatch.setattr(prompts, "interactive", lambda: True)
        self.questionary = mock.Mock()
        monkeypatch.setattr(prompts, "questionary", self.questionary)
        self.asker = prompts.Asker()
        self.monkeypatch = monkeypatch

    def answers(self, kind, answer=None, error=None):
        getattr(self.questionary, kind).return_value = FakeQuestion(answer, error)

    def test_given_force_plain_when_a_terminal_is_attached_then_it_is_not_fancy(self):
        assert self.asker.fancy
        assert not prompts.Asker(force_plain=True).fancy

    @pytest.mark.parametrize("answer, expected", [("  codex ", "codex"), ("", "claude"), (None, "claude")])
    def test_given_an_answer_when_a_line_is_asked_then_it_is_stripped_or_the_default_kept(self, answer, expected):
        self.answers("text", answer)
        assert self.asker.line("agent", "claude") == expected
        assert self.questionary.text.call_args.kwargs["default"] == "claude"

    def test_given_a_broken_terminal_when_a_line_is_asked_then_input_takes_over(self):
        self.answers("text", error=RuntimeError("no console"))
        with mock.patch("builtins.input", return_value="typed"):
            assert self.asker.line("agent", "claude") == "typed"

    @pytest.mark.parametrize("kind, call", [
        ("text", lambda a: a.line("agent")),
        ("confirm", lambda a: a.confirm("Go?")),
        ("select", lambda a: a.select("agent", ["a", "b"])),
    ])
    @pytest.mark.parametrize("error", [KeyboardInterrupt, EOFError])
    def test_given_ctrl_c_or_end_of_input_when_asked_then_it_reaches_the_caller(self, kind, call, error):
        self.answers(kind, error=error())
        with pytest.raises(error):
            call(self.asker)

    @pytest.mark.parametrize("answer", [True, False])
    def test_given_an_answer_when_confirming_then_it_is_returned(self, answer):
        self.answers("confirm", answer)
        assert self.asker.confirm("Go?") is answer

    def test_given_a_broken_terminal_when_confirming_then_input_takes_over(self):
        self.answers("confirm", error=RuntimeError("no console"))
        with mock.patch("builtins.input", return_value="n"):
            assert self.asker.confirm("Go?", default=True) is False

    def test_given_a_default_among_the_options_when_selecting_then_it_is_preselected(self):
        self.answers("select", "b")
        assert self.asker.select("agent", ["a", "b"], "b") == "b"
        assert self.questionary.select.call_args.kwargs["default"] == "b"

    def test_given_a_default_not_among_the_options_when_selecting_then_nothing_is_preselected(self):
        self.answers("select", None)
        assert self.asker.select("agent", ["a", "b"], "zzz") == "zzz"
        assert self.questionary.select.call_args.kwargs["default"] is None

    def test_given_a_broken_terminal_when_selecting_then_the_value_is_asked_as_a_line(self):
        self.answers("select", error=RuntimeError("no console"))
        self.answers("text", "typed")
        assert self.asker.select("agent", ["a", "b"], "a") == "typed"

    def test_given_a_terminal_when_the_command_prompt_is_shown_then_it_is_drawn_inside_a_box(self):
        drawn = []
        self.monkeypatch.setattr(render, "box_top", lambda hint: drawn.append(("top", hint)))
        self.monkeypatch.setattr(render, "box_bottom", lambda: drawn.append(("bottom",)))
        self.answers("text", "/runs")
        assert self.asker.command(">", hint="type a task") == "/runs"
        assert drawn == [("top", "type a task"), ("bottom",)]

    def test_given_nothing_typed_when_the_command_prompt_is_shown_then_it_returns_blank(self):
        self.monkeypatch.setattr(render, "box_top", lambda hint: None)
        self.monkeypatch.setattr(render, "box_bottom", lambda: None)
        self.answers("text", None)
        assert self.asker.command() == ""

    @pytest.mark.parametrize("error", [KeyboardInterrupt, EOFError])
    def test_given_ctrl_c_at_the_command_prompt_when_shown_then_the_box_is_closed_and_it_reaches_the_caller(
            self, error):
        closed = []
        self.monkeypatch.setattr(render, "box_top", lambda hint: None)
        self.monkeypatch.setattr(render, "box_bottom", lambda: closed.append(True))
        self.answers("text", error=error())
        with pytest.raises(error):
            self.asker.command()
        assert closed == [True]

    def test_given_a_broken_terminal_at_the_command_prompt_when_shown_then_input_takes_over(self):
        self.monkeypatch.setattr(render, "box_top", lambda hint: None)
        self.answers("text", error=RuntimeError("no console"))
        with mock.patch("builtins.input", return_value="/help"):
            assert self.asker.command() == "/help"


class TestInputBox:
    """The prompt is drawn as a box around the line prompt_toolkit owns."""

    def capture(self, fn, *args):
        buffer = io.StringIO()
        console = Console(theme=render.THEME, file=buffer, width=60,
                          force_terminal=True, legacy_windows=False)
        with mock.patch.object(render, "console", console):
            fn(*args)
        return buffer.getvalue()

    def test_given_a_hint_when_the_box_top_is_drawn_then_it_carries_the_hint(self):
        out = self.capture(render.box_top, "type a task or /help")
        assert "type a task or /help" in out

    def test_given_the_input_box_when_drawn_then_both_edges_appear(self):
        top = self.capture(render.box_top, "")
        bottom = self.capture(render.box_bottom)
        assert top.strip()
        assert bottom.strip()

    def test_given_the_input_box_when_drawn_then_both_edges_are_the_same_width(self):
        top = ANSI_RE.sub("", self.capture(render.box_top, "")).strip("\n")
        bottom = ANSI_RE.sub("", self.capture(render.box_bottom)).strip("\n")
        assert len(top) == len(bottom)


class TestFrame:
    """One width governs the divider, the rail and the input box."""

    @pytest.fixture(autouse=True)
    def _monkeypatch(self, monkeypatch):
        self.monkeypatch = monkeypatch

    def use(self, width, terminal=True):
        buffer = io.StringIO()
        console = Console(theme=render.THEME, file=buffer, width=width,
                          force_terminal=terminal, legacy_windows=False,
                          highlight=False)
        self.monkeypatch.setattr(render, "console", console)
        return buffer

    def test_given_a_very_wide_terminal_when_the_frame_is_measured_then_it_stops_at_the_cap(self):
        self.use(200)
        assert render.frame_width() == render.FRAME_MAX - render.MARGIN

    def test_given_a_narrow_terminal_when_the_frame_is_measured_then_it_shrinks(self):
        self.use(50)
        assert render.frame_width() == 50 - render.MARGIN

    def test_given_a_divider_when_drawn_then_it_spans_the_frame(self):
        buffer = self.use(60)
        render.divider()
        drawn = ANSI_RE.sub("", buffer.getvalue()).strip("\n")
        assert len(drawn) == render.MARGIN + render.frame_width()

    def test_given_a_label_when_the_divider_is_drawn_then_it_carries_it(self):
        buffer = self.use(60)
        render.divider("logs")
        drawn = ANSI_RE.sub("", buffer.getvalue()).strip("\n")
        assert "─ logs ─" in drawn
        assert len(drawn) == render.MARGIN + render.frame_width()

    def test_given_a_legacy_code_page_when_drawn_then_the_ascii_fallback_takes_over(self):
        """A legacy code page must degrade, not raise on the first frame."""
        buffer = self.use(60)
        with mock.patch.object(type(render.console), "encoding", "cp1252"):
            render.divider()
            render.box_top("type a task")
            render.out(Text("content"))
        drawn = ANSI_RE.sub("", buffer.getvalue())
        assert "─" not in drawn
        assert "▌" not in drawn
        assert "| content" in drawn

    def test_given_the_divider_and_the_input_box_when_drawn_then_they_are_the_same_width(self):
        buffer = self.use(60)
        render.divider()
        render.box_bottom()
        lines = ANSI_RE.sub("", buffer.getvalue()).strip("\n").splitlines()
        assert len(lines[0]) == len(lines[1])


class TestRail:
    """Content is drawn inside a gutter, but only when there is a screen."""

    def draw(self, renderable, width=60, terminal=True, **kwargs):
        buffer = io.StringIO()
        console = Console(theme=render.THEME, file=buffer, width=width,
                          force_terminal=terminal, legacy_windows=False,
                          highlight=False)
        with mock.patch.object(render, "console", console):
            render.out(renderable, **kwargs)
        return ANSI_RE.sub("", buffer.getvalue())

    def body(self, printed):
        return [line for line in printed.splitlines() if line.strip()]

    def test_given_a_wrapped_paragraph_when_drawn_then_every_line_gets_the_gutter(self):
        paragraph = Text(" ".join(f"word{n}" for n in range(60)))
        lines = self.body(self.draw(paragraph))
        assert len(lines) > 1
        for line in lines:
            assert line.startswith("  ▌ "), line

    def test_given_a_table_when_drawn_then_every_row_is_railed(self):
        wf = make(prompt="add rate limiting")
        table = render.run_table([wf], lambda i: False)
        for line in self.body(self.draw(table)):
            assert line.startswith("  ▌"), line

    def test_given_a_blank_line_when_drawn_then_it_keeps_the_rail_without_trailing_whitespace(self):
        """The bar stays unbroken inside a block without dirtying a copy."""
        printed = self.draw(Group(Text("above"), Text(""), Text("below")))
        assert [line for line in printed.splitlines() if line.strip()] == [
            "  ▌ above", "  ▌", "  ▌ below"]

    def test_given_a_wide_table_when_drawn_then_it_wraps_inside_the_rail(self):
        wf = make(prompt="add rate limiting to the public API gateway")
        printed = self.draw(render.run_table([wf], lambda i: False), width=60)
        for line in self.body(printed):
            assert len(line) <= 60

    def test_given_a_pipe_when_drawn_then_the_text_has_no_gutter(self):
        printed = self.draw(Text("plain output"), terminal=False)
        assert "plain output" in printed
        assert "▌" not in printed

    def test_given_an_empty_string_when_drawn_then_it_stays_a_blank_line(self):
        assert "▌" not in self.draw("")

    def test_given_the_message_helpers_when_printed_then_they_are_railed(self):
        for fn in (render.success, render.warn, render.error,
                   render.info, render.muted, render.hint):
            buffer = io.StringIO()
            console = Console(theme=render.THEME, file=buffer, width=60,
                              force_terminal=True, legacy_windows=False,
                              highlight=False)
            with mock.patch.object(render, "console", console):
                fn("something happened")
            printed = ANSI_RE.sub("", buffer.getvalue())
            assert "something happened" in printed
            assert printed.startswith("  ▌ "), (fn.__name__, printed)

    def test_given_wide_output_when_drawn_then_it_escapes_the_cap_but_keeps_the_gutter(self):
        row = Text("x" * 160)
        lines = self.body(self.draw(row, width=180, wide=True))
        assert len(lines) == 1          # not wrapped into the frame
        assert lines[0].startswith("  ▌ ")

    def test_given_long_output_when_drawn_then_it_is_capped_at_the_frame(self):
        row = Text("x" * 160)
        lines = self.body(self.draw(row, width=180))
        assert len(lines) > 1        # wrapped to the frame width
        assert len(lines[0]) <= render.FRAME_MAX


class TestStatusBar:
    def test_given_agents_and_runs_when_the_status_bar_is_drawn_then_it_shows_them(self):
        bar = render.status_bar([("claude", True), ("codex", False)], "my-api", 3, 1)
        text = bar.plain
        assert "claude" in text
        assert "codex" in text
        assert "my-api" in text
        assert "3 runs" in text
        assert "1 awaiting approval" in text

    def test_given_zero_counts_when_the_status_bar_is_drawn_then_they_are_omitted(self):
        text = render.status_bar([("claude", True)], "api", 0, 0).plain
        assert "run" not in text
        assert "awaiting" not in text


class TestOutputReplay:
    """A command's output must survive the header redraw, not scroll away."""

    @pytest.fixture(autouse=True)
    def _console(self, monkeypatch):
        self.buffer = io.StringIO()
        self.console = Console(theme=render.THEME, file=self.buffer, width=60,
                               force_terminal=False, legacy_windows=False,
                               record=True, highlight=False)
        monkeypatch.setattr(render, "console", self.console)

    def printed(self) -> str:
        return ANSI_RE.sub("", self.buffer.getvalue())

    def test_given_recorded_output_when_replayed_then_it_is_printed_again(self):
        render.out("the result of a command")
        recorded = render.take_output()
        self.buffer.truncate(0)
        self.buffer.seek(0)

        render.replay(recorded, max_lines=20)
        assert "the result of a command" in self.printed()

    def test_given_taken_output_when_more_is_printed_then_the_recording_starts_afresh(self):
        render.out("first")
        render.take_output()
        render.out("second")
        second = ANSI_RE.sub("", render.take_output())
        assert "second" in second
        assert "first" not in second

    def test_given_a_long_result_when_replayed_then_it_is_trimmed_to_what_fits(self):
        for index in range(50):
            render.out(f"line {index}")
        recorded = render.take_output()
        self.buffer.truncate(0)
        self.buffer.seek(0)

        render.replay(recorded, max_lines=10)
        shown = self.printed()
        assert "line 49" in shown       # the tail is kept
        assert "line 5 " not in shown    # the head is dropped
        assert "earlier lines" in shown  # and said so

    def test_given_nothing_recorded_when_replayed_then_nothing_is_printed(self):
        render.replay("", max_lines=10)
        assert self.printed() == ""
