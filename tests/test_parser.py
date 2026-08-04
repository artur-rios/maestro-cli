"""Turning a typed line into an intent."""

import unittest

from agent_orchestrator.shell.parser import (
    Command,
    ParseError,
    TaskIntent,
    parse,
)


class Blanks(unittest.TestCase):
    def test_nothing_typed_is_nothing_to_do(self):
        for line in ("", "   ", "\t", None):
            self.assertIsNone(parse(line))


class Commands(unittest.TestCase):
    def test_a_slash_makes_a_command(self):
        result = parse("/runs")
        self.assertIsInstance(result, Command)
        self.assertEqual(result.name, "runs")
        self.assertEqual(result.args, [])

    def test_arguments_are_split(self):
        result = parse("/show 7f2a1c")
        self.assertEqual(result.name, "show")
        self.assertEqual(result.args, ["7f2a1c"])

    def test_the_name_is_case_insensitive(self):
        self.assertEqual(parse("/RUNS").name, "runs")

    def test_quoted_arguments_stay_together(self):
        result = parse('/reject 7f2a "not what I asked for"')
        self.assertEqual(result.args, ["7f2a", "not what I asked for"])

    def test_arg_joins_the_rest(self):
        self.assertEqual(parse("/set mode auto").arg, "mode auto")

    def test_a_bare_slash_is_an_error(self):
        with self.assertRaises(ParseError):
            parse("/")

    def test_an_unbalanced_quote_still_parses(self):
        result = parse('/reject 7f2a "unclosed')
        self.assertEqual(result.name, "reject")


class Tasks(unittest.TestCase):
    def test_plain_text_is_a_task(self):
        result = parse("add rate limiting to the API")
        self.assertIsInstance(result, TaskIntent)
        self.assertEqual(result.text, "add rate limiting to the API")
        self.assertIsNone(result.ref)
        self.assertEqual(result.overrides, {})

    def test_a_reference_is_recognised(self):
        result = parse("@scope-spec")
        self.assertEqual(result.ref, "scope-spec")
        self.assertEqual(result.text, "")

    def test_text_after_a_reference_is_kept(self):
        result = parse("@scope-spec also add tests")
        self.assertEqual(result.ref, "scope-spec")
        self.assertEqual(result.text, "also add tests")

    def test_a_bare_at_is_an_error(self):
        with self.assertRaises(ParseError):
            parse("@")

    def test_an_at_later_in_the_line_is_just_text(self):
        result = parse("email me at @home")
        self.assertIsNone(result.ref)
        self.assertIn("@home", result.text)


class Flags(unittest.TestCase):
    def test_a_flag_is_taken_out_of_the_task(self):
        result = parse("add rate limiting --mode auto")
        self.assertEqual(result.text, "add rate limiting")
        self.assertEqual(result.overrides, {"mode": "autonomous"})

    def test_equals_form_works_too(self):
        self.assertEqual(parse("do it --mode=auto").overrides["mode"], "autonomous")

    def test_flags_may_appear_anywhere(self):
        result = parse("--execute codex fix the login bug --rounds 3")
        self.assertEqual(result.text, "fix the login bug")
        self.assertEqual(result.overrides["execute"], "codex")
        self.assertEqual(result.overrides["rounds"], 3)

    def test_several_flags_combine(self):
        result = parse("do it --plan claude --execute opencode --review codex")
        self.assertEqual(
            result.overrides,
            {"plan": "claude", "execute": "opencode", "review": "codex"},
        )

    def test_mode_accepts_the_short_forms(self):
        for typed, stored in (("auto", "autonomous"), ("autonomous", "autonomous"),
                              ("super", "supervised"), ("manual", "supervised")):
            self.assertEqual(parse(f"x --mode {typed}").overrides["mode"], stored)

    def test_a_bad_mode_is_rejected(self):
        with self.assertRaises(ParseError):
            parse("x --mode sideways")

    def test_counts_must_be_positive_whole_numbers(self):
        for bad in ("0", "-1", "two", "1.5"):
            with self.assertRaises(ParseError):
                parse(f"x --rounds {bad}")

    def test_an_unknown_flag_is_refused_not_swallowed(self):
        """A typo must not silently become part of the prompt."""
        with self.assertRaises(ParseError) as ctx:
            parse("add rate limiting --modes auto")
        self.assertIn("--modes", str(ctx.exception))

    def test_a_flag_without_a_value_is_refused(self):
        with self.assertRaises(ParseError):
            parse("do it --mode")
        with self.assertRaises(ParseError):
            parse("do it --mode --rounds 2")

    def test_flags_work_with_a_reference(self):
        result = parse("@spec --mode auto --attempts 5")
        self.assertEqual(result.ref, "spec")
        self.assertEqual(result.overrides["attempts"], 5)

    def test_a_line_of_only_flags_has_nothing_to_do(self):
        with self.assertRaises(ParseError):
            parse("--mode auto")

    def test_a_lone_double_dash_in_prose_is_an_error_not_prose(self):
        with self.assertRaises(ParseError):
            parse("compare -- these things")


if __name__ == "__main__":
    unittest.main()
