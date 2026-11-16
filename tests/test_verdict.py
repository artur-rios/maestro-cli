"""Reading the reviewer's verdict out of its output."""

import pytest

from maestro.models import Verdict
from maestro.runner import parse_verdict


@pytest.mark.parametrize("text", [
    "Looks good.\nVERDICT: APPROVE",
    "**VERDICT: APPROVE**",
    "> VERDICT: APPROVE",
    "## VERDICT: APPROVE",
    "- VERDICT - APPROVE",
    "verdict:  approve",
])
def test_given_an_approve_line_when_parsed_then_the_verdict_is_approve(text):
    assert parse_verdict(text) is Verdict.APPROVE


@pytest.mark.parametrize("text", [
    "Missing tests.\nVERDICT: REQUEST_CHANGES",
    "Verdict - REQUEST CHANGES",
    "`VERDICT: request_changes`",
])
def test_given_a_request_changes_line_when_parsed_then_changes_are_requested(text):
    assert parse_verdict(text) is Verdict.REQUEST_CHANGES


def test_given_several_verdict_lines_when_parsed_then_the_last_one_wins():
    text = (
        "I must answer with VERDICT: APPROVE or VERDICT: REQUEST_CHANGES.\n"
        "The change is incomplete.\n"
        "VERDICT: REQUEST_CHANGES\n"
    )
    assert parse_verdict(text) is Verdict.REQUEST_CHANGES


def test_given_a_request_then_an_approve_line_when_parsed_then_the_later_approve_wins():
    assert parse_verdict("VERDICT: REQUEST_CHANGES\nfixed now\nVERDICT: APPROVE") is Verdict.APPROVE


@pytest.mark.parametrize("text", ["", "   ", "This looks fine to me!", None, "APPROVE", "VERDICT: MAYBE"])
def test_given_unparseable_output_when_parsed_then_it_never_approves(text):
    assert parse_verdict(text) is Verdict.REQUEST_CHANGES


def test_given_the_word_verdict_in_prose_when_parsed_then_it_does_not_count():
    assert parse_verdict("My verdict is that I approve of this change.") is Verdict.REQUEST_CHANGES


def test_given_a_verdict_in_the_middle_of_a_line_when_parsed_then_it_does_not_count():
    assert parse_verdict("so the VERDICT: APPROVE line comes later") is Verdict.REQUEST_CHANGES
