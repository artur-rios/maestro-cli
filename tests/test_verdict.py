import unittest

from maestro.models import Verdict
from maestro.runner import parse_verdict


class ParseVerdict(unittest.TestCase):
    def test_plain_approve(self):
        self.assertIs(parse_verdict("Looks good.\nVERDICT: APPROVE"), Verdict.APPROVE)

    def test_plain_request_changes(self):
        self.assertIs(
            parse_verdict("Missing tests.\nVERDICT: REQUEST_CHANGES"),
            Verdict.REQUEST_CHANGES,
        )

    def test_tolerates_markdown_decoration(self):
        self.assertIs(parse_verdict("**VERDICT: APPROVE**"), Verdict.APPROVE)
        self.assertIs(parse_verdict("> VERDICT: APPROVE"), Verdict.APPROVE)
        self.assertIs(parse_verdict("## VERDICT: APPROVE"), Verdict.APPROVE)

    def test_tolerates_spaces_and_case(self):
        self.assertIs(parse_verdict("verdict:  approve"), Verdict.APPROVE)
        self.assertIs(
            parse_verdict("Verdict - REQUEST CHANGES"), Verdict.REQUEST_CHANGES
        )

    def test_last_verdict_line_wins(self):
        text = (
            "I must answer with VERDICT: APPROVE or VERDICT: REQUEST_CHANGES.\n"
            "The change is incomplete.\n"
            "VERDICT: REQUEST_CHANGES\n"
        )
        self.assertIs(parse_verdict(text), Verdict.REQUEST_CHANGES)

    def test_unparseable_output_never_approves(self):
        for text in ("", "   ", "This looks fine to me!", None, "APPROVE"):
            self.assertIs(parse_verdict(text), Verdict.REQUEST_CHANGES)

    def test_verdict_word_in_prose_does_not_count(self):
        self.assertIs(
            parse_verdict("My verdict is that I approve of this change."),
            Verdict.REQUEST_CHANGES,
        )


if __name__ == "__main__":
    unittest.main()
