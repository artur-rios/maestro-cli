"""Branch naming: conventional prefix plus a short, readable description."""

import unittest
import unittest.mock

from maestro import gitops


class Slugs(unittest.TestCase):
    def test_filler_words_are_dropped(self):
        # The reported bug: "create-a-new-use-case-in-which-a".
        slug = gitops.slugify(
            "Create a new Use Case in which a scope owner can add one or more "
            "custom fields to a scope"
        )
        self.assertEqual(slug, "use-case-scope-owner")
        self.assertNotIn("-a-", f"-{slug}-")
        self.assertNotIn("which", slug)

    def test_a_leading_verb_is_dropped_because_the_prefix_implies_it(self):
        self.assertEqual(gitops.slugify("Add a health check endpoint"),
                         "health-check-endpoint")
        self.assertEqual(gitops.slugify("Implement scope specific permissions"),
                         "scope-specific-permissions")

    def test_repeated_words_are_collapsed(self):
        slug = gitops.slugify("Add scope fields to a scope for scope owners")
        self.assertEqual(len(slug.split("-")), len(set(slug.split("-"))))

    def test_a_prompt_that_is_only_a_verb_still_yields_something(self):
        self.assertTrue(gitops.slugify("create"))

    def test_slug_is_short(self):
        slug = gitops.slugify("Implement a comprehensive audit logging subsystem "
                              "covering every mutation across all services")
        self.assertLessEqual(len(slug), 40)
        self.assertLessEqual(len(slug.split("-")), 5)

    def test_slug_is_branch_safe(self):
        slug = gitops.slugify("Fix the OAuth2 callback (500 error!) on /auth/login")
        self.assertRegex(slug, r"^[a-z0-9-]+$")

    def test_a_prompt_of_only_filler_still_yields_something(self):
        self.assertTrue(gitops.slugify("the a of for to"))

    def test_empty_prompt_falls_back(self):
        self.assertEqual(gitops.slugify(""), "task")


class Types(unittest.TestCase):
    def test_bug_wording_suggests_fix(self):
        for prompt in ("Fix the broken login", "The checkout crashes on submit",
                       "Payment fails with a 500 error"):
            self.assertEqual(gitops.suggest_branch_type(prompt), "fix", prompt)

    def test_new_work_suggests_feature(self):
        for prompt in ("Create a new use case for scope owners",
                       "Add a health check endpoint"):
            self.assertEqual(gitops.suggest_branch_type(prompt), "feature", prompt)

    def test_hints_match_whole_words_only(self):
        # "ci" lives inside "specific"; this used to come back as a chore.
        self.assertEqual(
            gitops.suggest_branch_type("Implement scope specific permissions"),
            "feature",
        )
        self.assertEqual(
            gitops.suggest_branch_type("Add the latest pricing table"), "feature"
        )

    def test_other_categories(self):
        self.assertEqual(gitops.suggest_branch_type("Refactor the config loader"), "refactor")
        self.assertEqual(gitops.suggest_branch_type("Update the README"), "docs")
        self.assertEqual(gitops.suggest_branch_type("Bump the dependency versions"), "chore")


class FullNames(unittest.TestCase):
    def test_conventional_shape(self):
        name = gitops.suggest_branch("Add a health check endpoint to the API")
        self.assertTrue(name.startswith("feature/"))
        self.assertEqual(name, "feature/health-check-endpoint-api")

    def test_explicit_type_wins(self):
        name = gitops.suggest_branch("Add a health check endpoint", "fix")
        self.assertTrue(name.startswith("fix/"))

    def test_the_prefix_is_not_repeated_in_the_slug(self):
        self.assertEqual(
            gitops.suggest_branch("Fix the broken login redirect that crashes"),
            "fix/broken-login-redirect-crashes",
        )
        self.assertEqual(
            gitops.suggest_branch("Refactor the config loader to use pydantic"),
            "refactor/config-loader-use-pydantic",
        )


class Composing(unittest.TestCase):
    """Typing a name must not silently discard the chosen type."""

    PROMPT = "Implement scope specific permissions"

    def test_blank_keeps_the_suggestion(self):
        self.assertEqual(
            gitops.compose_branch("feature", "", self.PROMPT),
            "feature/scope-specific-permissions",
        )

    def test_a_plain_name_goes_under_the_chosen_type(self):
        # The reported bug: this used to produce a bare "my-branch".
        self.assertEqual(
            gitops.compose_branch("feature", "scope-permissions", self.PROMPT),
            "feature/scope-permissions",
        )

    def test_the_type_is_kept_even_when_it_was_not_the_suggested_one(self):
        self.assertEqual(
            gitops.compose_branch("chore", "scope-permissions", self.PROMPT),
            "chore/scope-permissions",
        )

    def test_an_explicit_prefix_overrides_the_chosen_type(self):
        self.assertEqual(
            gitops.compose_branch("feature", "fix/login-redirect", self.PROMPT),
            "fix/login-redirect",
        )

    def test_a_typed_name_is_sanitised(self):
        self.assertEqual(
            gitops.compose_branch("feature", "My New Thing!", self.PROMPT),
            "feature/My-New-Thing",
        )

    def test_whitespace_only_counts_as_blank(self):
        self.assertEqual(
            gitops.compose_branch("fix", "   ", self.PROMPT),
            gitops.suggest_branch(self.PROMPT, "fix"),
        )

    def test_the_subject_survives_without_the_verb(self):
        self.assertEqual(
            gitops.suggest_branch("Create a new use case for scope owners"),
            "feature/use-case-scope-owners",
        )

    def test_no_random_suffix(self):
        name = gitops.suggest_branch("Add a health check endpoint")
        self.assertNotRegex(name, r"-[0-9a-f]{8}$")


class Sanitising(unittest.TestCase):
    def test_spaces_and_junk_become_hyphens(self):
        self.assertEqual(
            gitops.sanitize_branch("feature/My New Thing!!"), "feature/My-New-Thing"
        )

    def test_collapses_repeats_and_trims(self):
        self.assertEqual(gitops.sanitize_branch("//feature//a--b--/"), "feature/a-b")

    def test_empty_input_falls_back(self):
        self.assertEqual(gitops.sanitize_branch("   "), "task")

    def test_a_valid_name_is_left_alone(self):
        self.assertEqual(
            gitops.sanitize_branch("fix/login-redirect"), "fix/login-redirect"
        )


class Uniqueness(unittest.TestCase):
    def test_free_name_is_returned_unchanged(self):
        with unittest.mock.patch.object(gitops, "branch_exists", return_value=False):
            self.assertEqual(gitops.unique_branch("/r", "feature/x"), "feature/x")

    def test_taken_name_gets_a_numeric_suffix(self):
        taken = {"feature/x", "feature/x-2"}
        with unittest.mock.patch.object(
            gitops, "branch_exists", side_effect=lambda r, n: n in taken
        ):
            self.assertEqual(gitops.unique_branch("/r", "feature/x"), "feature/x-3")


if __name__ == "__main__":
    unittest.main()
