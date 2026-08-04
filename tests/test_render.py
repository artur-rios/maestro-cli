"""The rich rendering layer and the questionary prompt fallbacks."""

import io
import time
import unittest
from unittest import mock

from rich.console import Console

from agent_orchestrator.models import Mode, Status, Step, Role, Task, Workflow
import re

from agent_orchestrator.shell import prompts, render

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


class Messages(unittest.TestCase):
    def capture(self, fn, message):
        buffer = io.StringIO()
        console = Console(theme=render.THEME, file=buffer, width=100,
                          force_terminal=False, legacy_windows=False)
        with mock.patch.object(render, "console", console):
            fn(message)
        return buffer.getvalue()

    def test_each_level_prints_its_message(self):
        for fn in (render.success, render.warn, render.error,
                   render.info, render.muted, render.hint):
            self.assertIn("something happened", self.capture(fn, "something happened"))

    def test_an_error_is_marked(self):
        self.assertIn("x", self.capture(render.error, "it broke"))

    def test_a_hint_points_somewhere(self):
        self.assertIn("->", self.capture(render.hint, "/approve 7f2a1c"))


class RunTable(unittest.TestCase):
    def test_the_columns_are_all_present(self):
        out = draw(render.run_table([make()], lambda i: False))
        for header in ("ID", "STATUS", "DONE", "AGE", "TASK"):
            self.assertIn(header, out)

    def test_a_row_shows_the_run(self):
        wf = make(prompt="add rate limiting to the API")
        out = draw(render.run_table([wf], lambda i: False))
        self.assertIn(wf.id[:6], out)
        self.assertIn("merged", out)
        self.assertIn("add rate limiting", out)

    def test_a_running_workflow_is_marked(self):
        out = draw(render.run_table([make()], lambda i: True))
        self.assertIn("*", out)

    def test_checklist_progress_replaces_the_round_counter(self):
        wf = make()
        wf.tasks = [Task(1, "a", Status.DONE), Task(2, "b")]
        self.assertIn("1/2", draw(render.run_table([wf], lambda i: False)))

    def test_an_empty_list_says_so(self):
        self.assertIn("no runs yet", draw(render.run_table([], lambda i: False)))


class RunDetail(unittest.TestCase):
    def render_all(self, wf, running=False):
        return "".join(draw(part) for part in render.run_detail(wf, lambda i: running))

    def test_the_facts_are_shown(self):
        wf = make(models={"execute": "glm-5.2"}, mode=Mode.AUTONOMOUS)
        wf.prompt_source = "docs/prompts/spec.md"
        out = self.render_all(wf)
        self.assertIn("feature/rate-limiting", out)
        self.assertIn("claude -> opencode -> codex", out)
        self.assertIn("glm-5.2", out)
        self.assertIn("autonomous", out)
        self.assertIn("docs/prompts/spec.md", out)

    def test_the_task_text_appears(self):
        self.assertIn("add rate limiting", self.render_all(make()))

    def test_checklist_marks_survive_markup_parsing(self):
        """"[x]" is valid rich markup; it must not be swallowed."""
        wf = make()
        wf.tasks = [Task(1, "first", Status.DONE), Task(2, "second"),
                    Task(3, "third", Status.FAILED)]
        out = self.render_all(wf)
        self.assertIn("[x]", out)
        self.assertIn("[ ]", out)
        self.assertIn("[!]", out)

    def test_square_brackets_in_a_task_are_not_eaten(self):
        wf = make(prompt="handle [bold] tags and [/] closers")
        out = self.render_all(wf)
        self.assertIn("[bold]", out)

    def test_steps_are_listed(self):
        wf = make()
        step = wf.add_step(Role.EXECUTE, task_index=2)
        step.status = Status.DONE
        step.started_at, step.ended_at = 0.0, 12.0
        out = self.render_all(wf)
        self.assertIn("execute", out)
        self.assertIn("task 2", out)
        self.assertIn("12s", out)

    def test_a_failure_note_is_shown(self):
        wf = make(status=Status.FAILED)
        wf.message = "the opencode execute step committed nothing"
        self.assertIn("committed nothing", self.render_all(wf))

    def test_warnings_are_shown(self):
        wf = make()
        wf.notes = ["ran past its timeout, but its work was committed"]
        self.assertIn("past its timeout", self.render_all(wf))


class Summary(unittest.TestCase):
    def panel(self, **kwargs):
        args = dict(
            branch="feature/thing",
            agents={"plan": "claude", "execute": "opencode", "review": "codex"},
            mode="supervised", rounds=2, attempts=3,
        )
        args.update(kwargs)
        return draw(render.summary_panel(**args))

    def test_it_shows_what_will_happen(self):
        out = self.panel()
        self.assertIn("feature/thing", out)
        self.assertIn("claude -> opencode -> codex", out)
        self.assertIn("supervised", out)
        self.assertIn("2 rounds", out)

    def test_models_appear_when_chosen(self):
        out = self.panel(models={"execute": "glm-5.2", "plan": ""})
        self.assertIn("execute glm-5.2", out)
        self.assertNotIn("plan ", out.split("execute glm-5.2")[1])

    def test_the_source_file_appears(self):
        self.assertIn("spec.md", self.panel(source="docs/prompts/spec.md"))


class Tables(unittest.TestCase):
    def test_backends_are_tabulated(self):
        from agent_orchestrator import config
        out = draw(render.backend_table(config.DEFAULT_BACKENDS), width=140)
        for name in ("claude", "codex", "opencode"):
            self.assertIn(name, out)
        self.assertIn("BACKEND", out)

    def test_settings_are_tabulated(self):
        out = draw(render.settings_table({"mode": "supervised", "repo": ""},
                                         ["mode", "repo"]))
        self.assertIn("supervised", out)
        self.assertIn("(auto)", out)

    def test_prompt_files_are_tabulated(self):
        item = mock.Mock(name="scope", label="docs/prompts/scope.md",
                         source="docs/prompts")
        item.name = "scope"
        out = draw(render.prompt_table([item]))
        self.assertIn("@scope", out)
        self.assertIn("docs/prompts", out)

    def test_help_is_tabulated(self):
        out = draw(render.help_table([("/runs", "every run"), ("", "")]))
        self.assertIn("/runs", out)
        self.assertIn("every run", out)


class Banner(unittest.TestCase):
    def test_the_wordmark_is_ascii(self):
        for line in render.WORDMARK:
            self.assertTrue(all(ord(c) < 128 for c in line))

    def test_a_wide_terminal_gets_the_wordmark(self):
        out = "".join(draw(p) for p in render.banner(100, "2.0.0"))
        self.assertIn("AI AGENT ORCHESTRATOR", out)
        self.assertIn("v2.0.0", out)
        self.assertIn(render.WORDMARK[0].strip(), out)

    def test_a_narrow_terminal_gets_a_plain_title(self):
        out = "".join(draw(p, width=30) for p in render.banner(30, "2.0.0"))
        self.assertIn("AI AGENT ORCHESTRATOR", out)
        self.assertNotIn(render.WORDMARK[0].strip(), out)


class PromptFallbacks(unittest.TestCase):
    """Without a terminal, questionary cannot run; input() must take over."""

    def setUp(self):
        self.asker = prompts.Asker(force_plain=True)

    def test_a_line_returns_what_was_typed(self):
        with mock.patch("builtins.input", return_value="  codex  "):
            self.assertEqual(self.asker.line("agent", "claude"), "codex")

    def test_an_empty_line_keeps_the_default(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(self.asker.line("agent", "claude"), "claude")

    def test_end_of_input_keeps_the_default(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.asker.line("agent", "claude"), "claude")

    def test_confirm_defaults_to_yes_when_asked_to(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(self.asker.confirm("Run it?", default=True))
            self.assertFalse(self.asker.confirm("Delete it?", default=False))

    def test_confirm_reads_a_yes(self):
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(self.asker.confirm("Delete it?", default=False))

    def test_select_falls_back_to_typing(self):
        with mock.patch("builtins.input", return_value="codex"):
            self.assertEqual(
                self.asker.select("agent", ["claude", "codex"], "claude"), "codex"
            )

    def test_select_with_no_options_still_asks(self):
        with mock.patch("builtins.input", return_value="typed"):
            self.assertEqual(self.asker.select("model", [], ""), "typed")

    def test_the_command_prompt_reads_a_line(self):
        with mock.patch("builtins.input", return_value="/runs"):
            self.assertEqual(self.asker.command(), "/runs")

    def test_a_pipe_is_not_treated_as_interactive(self):
        with mock.patch.object(prompts.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertFalse(prompts.interactive())


if __name__ == "__main__":
    unittest.main()


class InputBox(unittest.TestCase):
    """The prompt is drawn as a box around the line prompt_toolkit owns."""

    def capture(self, fn, *args):
        buffer = io.StringIO()
        console = Console(theme=render.THEME, file=buffer, width=60,
                          force_terminal=True, legacy_windows=False)
        with mock.patch.object(render, "console", console):
            fn(*args)
        return buffer.getvalue()

    def test_the_top_edge_carries_the_hint(self):
        out = self.capture(render.box_top, "type a task or /help")
        self.assertIn("type a task or /help", out)

    def test_both_edges_are_drawn(self):
        top = self.capture(render.box_top, "")
        bottom = self.capture(render.box_bottom)
        self.assertTrue(top.strip())
        self.assertTrue(bottom.strip())

    def test_the_edges_are_the_same_width(self):
        top = ANSI_RE.sub("", self.capture(render.box_top, "")).strip("\n")
        bottom = ANSI_RE.sub("", self.capture(render.box_bottom)).strip("\n")
        self.assertEqual(len(top), len(bottom))


class StatusBar(unittest.TestCase):
    def test_it_shows_the_agents_and_the_repo(self):
        bar = render.status_bar([("claude", True), ("codex", False)], "my-api", 3, 1)
        text = bar.plain
        self.assertIn("claude", text)
        self.assertIn("codex", text)
        self.assertIn("my-api", text)
        self.assertIn("3 runs", text)
        self.assertIn("1 awaiting approval", text)

    def test_counts_are_omitted_when_zero(self):
        text = render.status_bar([("claude", True)], "api", 0, 0).plain
        self.assertNotIn("run", text)
        self.assertNotIn("awaiting", text)
