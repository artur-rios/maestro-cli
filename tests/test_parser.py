"""Turning a typed line into an intent."""

import pytest

from maestro.shell.parser import (
    Command,
    ParseError,
    TaskIntent,
    parse,
)


@pytest.mark.parametrize("line", ["", "   ", "\t", None])
def test_given_a_blank_line_when_parsed_then_there_is_nothing_to_do(line):
    assert parse(line) is None


class TestCommands:
    def test_given_a_slash_when_parsed_then_it_is_a_command(self):
        result = parse("/runs")
        assert isinstance(result, Command)
        assert result.name == "runs"
        assert result.args == []

    def test_given_arguments_when_parsed_then_they_are_split(self):
        result = parse("/show 7f2a1c")
        assert result.name == "show"
        assert result.args == ["7f2a1c"]

    def test_given_an_upper_case_name_when_parsed_then_it_is_lowered(self):
        assert parse("/RUNS").name == "runs"

    def test_given_a_quoted_argument_when_parsed_then_it_stays_together(self):
        result = parse('/reject 7f2a "not what I asked for"')
        assert result.args == ["7f2a", "not what I asked for"]

    def test_given_several_arguments_when_arg_is_read_then_they_are_joined(self):
        assert parse("/set mode auto").arg == "mode auto"

    @pytest.mark.parametrize("line", ["/", "/   "])
    def test_given_a_bare_slash_when_parsed_then_it_is_an_error(self, line):
        with pytest.raises(ParseError):
            parse(line)

    def test_given_an_unbalanced_quote_when_parsed_then_the_words_are_split_on_spaces(self):
        result = parse('/reject 7f2a "unclosed')
        assert result.name == "reject"
        assert result.args == ["7f2a", '"unclosed']


class TestTasks:
    def test_given_plain_text_when_parsed_then_it_is_a_task(self):
        result = parse("add rate limiting to the API")
        assert isinstance(result, TaskIntent)
        assert result.text == "add rate limiting to the API"
        assert result.ref is None
        assert result.overrides == {}

    def test_given_a_reference_when_parsed_then_it_is_recognised(self):
        result = parse("@scope-spec")
        assert result.ref == "scope-spec"
        assert result.text == ""

    def test_given_text_after_a_reference_when_parsed_then_it_is_kept(self):
        result = parse("@scope-spec also add tests")
        assert result.ref == "scope-spec"
        assert result.text == "also add tests"

    def test_given_a_bare_at_when_parsed_then_it_is_an_error(self):
        with pytest.raises(ParseError):
            parse("@")

    def test_given_an_at_later_in_the_line_when_parsed_then_it_is_just_text(self):
        result = parse("email me at @home")
        assert result.ref is None
        assert "@home" in result.text


class TestFlags:
    def test_given_a_flag_when_parsed_then_it_is_taken_out_of_the_task(self):
        result = parse("add rate limiting --mode auto")
        assert result.text == "add rate limiting"
        assert result.overrides == {"mode": "autonomous"}

    def test_given_the_equals_form_when_parsed_then_it_works_too(self):
        assert parse("do it --mode=auto").overrides["mode"] == "autonomous"

    def test_given_flags_anywhere_in_the_line_when_parsed_then_all_are_taken(self):
        result = parse("--execute codex fix the login bug --rounds 3")
        assert result.text == "fix the login bug"
        assert result.overrides["execute"] == "codex"
        assert result.overrides["rounds"] == 3

    def test_given_several_flags_when_parsed_then_they_combine(self):
        result = parse("do it --plan claude --execute opencode --review codex")
        assert result.overrides == {"plan": "claude", "execute": "opencode", "review": "codex"}

    def test_given_an_upper_case_flag_name_when_parsed_then_it_is_lowered(self):
        assert parse("do it --MODE auto").overrides == {"mode": "autonomous"}

    @pytest.mark.parametrize("typed, stored", [
        ("auto", "autonomous"), ("autonomous", "autonomous"), ("AUTO", "autonomous"),
        ("super", "supervised"), ("supervised", "supervised"), ("manual", "supervised"),
    ])
    def test_given_a_short_mode_form_when_parsed_then_it_is_stored_in_full(self, typed, stored):
        assert parse(f"x --mode {typed}").overrides["mode"] == stored

    def test_given_a_bad_mode_when_parsed_then_it_is_rejected(self):
        with pytest.raises(ParseError):
            parse("x --mode sideways")

    @pytest.mark.parametrize("bad", ["0", "-1", "two", "1.5"])
    def test_given_a_count_that_is_not_a_positive_whole_number_when_parsed_then_it_is_rejected(self, bad):
        with pytest.raises(ParseError):
            parse(f"x --rounds {bad}")

    def test_given_a_text_flag_when_parsed_then_its_value_is_kept_as_typed(self):
        assert parse("x --branch Fix/Thing").overrides == {"branch": "Fix/Thing"}

    def test_given_an_unknown_flag_when_parsed_then_it_is_refused_not_swallowed(self):
        """A typo must not silently become part of the prompt."""
        with pytest.raises(ParseError) as ctx:
            parse("add rate limiting --modes auto")
        assert "--modes" in str(ctx.value)

    @pytest.mark.parametrize("line", ["do it --mode", "do it --mode --rounds 2", "do it --mode="])
    def test_given_a_flag_without_a_value_when_parsed_then_it_is_refused(self, line):
        with pytest.raises(ParseError) as ctx:
            parse(line)
        assert "needs a value" in str(ctx.value)

    def test_given_flags_with_a_reference_when_parsed_then_both_are_read(self):
        result = parse("@spec --mode auto --attempts 5")
        assert result.ref == "spec"
        assert result.overrides["attempts"] == 5

    def test_given_a_line_of_only_flags_when_parsed_then_there_is_nothing_to_do(self):
        with pytest.raises(ParseError):
            parse("--mode auto")

    def test_given_a_lone_double_dash_in_prose_when_parsed_then_it_is_an_error_not_prose(self):
        with pytest.raises(ParseError):
            parse("compare -- these things")
