"""Branch naming: conventional prefix plus a short, readable description."""

import re
from unittest import mock

from maestro import gitops


class TestSlugs:
    def test_given_filler_words_when_slugified_then_they_are_dropped(self):
        # The reported bug: "create-a-new-use-case-in-which-a".
        slug = gitops.slugify(
            "Create a new Use Case in which a scope owner can add one or more "
            "custom fields to a scope"
        )
        assert slug == "use-case-scope-owner"
        assert "-a-" not in f"-{slug}-"
        assert "which" not in slug

    def test_given_a_leading_verb_when_slugified_then_it_is_dropped_because_the_prefix_implies_it(self):
        assert gitops.slugify("Add a health check endpoint") == "health-check-endpoint"
        assert gitops.slugify("Implement scope specific permissions") == "scope-specific-permissions"

    def test_given_repeated_words_when_slugified_then_each_appears_once(self):
        slug = gitops.slugify("Add scope fields to a scope for scope owners")
        assert len(slug.split("-")) == len(set(slug.split("-")))

    def test_given_only_a_verb_when_slugified_then_something_is_still_returned(self):
        assert gitops.slugify("create")

    def test_given_a_long_prompt_when_slugified_then_the_slug_stays_short(self):
        slug = gitops.slugify("Implement a comprehensive audit logging subsystem "
                              "covering every mutation across all services")
        assert len(slug) <= 40
        assert len(slug.split("-")) <= 5

    def test_given_punctuation_when_slugified_then_the_slug_is_branch_safe(self):
        slug = gitops.slugify("Fix the OAuth2 callback (500 error!) on /auth/login")
        assert re.search(r"^[a-z0-9-]+$", slug)

    def test_given_only_filler_words_when_slugified_then_something_is_still_returned(self):
        assert gitops.slugify("the a of for to")

    def test_given_an_empty_prompt_when_slugified_then_it_falls_back_to_task(self):
        assert gitops.slugify("") == "task"


class TestTypes:
    def test_given_bug_wording_when_a_type_is_suggested_then_it_is_fix(self):
        for prompt in ("Fix the broken login", "The checkout crashes on submit",
                       "Payment fails with a 500 error"):
            assert gitops.suggest_branch_type(prompt) == "fix", prompt

    def test_given_new_work_when_a_type_is_suggested_then_it_is_feature(self):
        for prompt in ("Create a new use case for scope owners",
                       "Add a health check endpoint"):
            assert gitops.suggest_branch_type(prompt) == "feature", prompt

    def test_given_a_hint_inside_a_longer_word_when_a_type_is_suggested_then_it_does_not_match(self):
        # "ci" lives inside "specific"; this used to come back as a chore.
        assert gitops.suggest_branch_type("Implement scope specific permissions") == "feature"
        assert gitops.suggest_branch_type("Add the latest pricing table") == "feature"

    def test_given_refactor_docs_or_chore_wording_when_a_type_is_suggested_then_it_matches(self):
        assert gitops.suggest_branch_type("Refactor the config loader") == "refactor"
        assert gitops.suggest_branch_type("Update the README") == "docs"
        assert gitops.suggest_branch_type("Bump the dependency versions") == "chore"


class TestFullNames:
    def test_given_a_prompt_when_a_branch_is_suggested_then_it_has_the_conventional_shape(self):
        name = gitops.suggest_branch("Add a health check endpoint to the API")
        assert name.startswith("feature/")
        assert name == "feature/health-check-endpoint-api"

    def test_given_an_explicit_type_when_a_branch_is_suggested_then_the_type_wins(self):
        name = gitops.suggest_branch("Add a health check endpoint", "fix")
        assert name.startswith("fix/")

    def test_given_a_verb_matching_the_type_when_a_branch_is_suggested_then_it_is_not_repeated(self):
        assert gitops.suggest_branch("Fix the broken login redirect that crashes") == "fix/broken-login-redirect-crashes"
        assert gitops.suggest_branch("Refactor the config loader to use pydantic") == "refactor/config-loader-use-pydantic"


class TestComposing:
    """Typing a name must not silently discard the chosen type."""

    PROMPT = "Implement scope specific permissions"

    def test_given_a_blank_name_when_composed_then_the_suggestion_is_kept(self):
        assert gitops.compose_branch("feature", "", self.PROMPT) == "feature/scope-specific-permissions"

    def test_given_a_plain_name_when_composed_then_it_goes_under_the_chosen_type(self):
        # The reported bug: this used to produce a bare "my-branch".
        assert gitops.compose_branch("feature", "scope-permissions", self.PROMPT) == "feature/scope-permissions"

    def test_given_a_type_other_than_the_suggested_one_when_composed_then_it_is_kept(self):
        assert gitops.compose_branch("chore", "scope-permissions", self.PROMPT) == "chore/scope-permissions"

    def test_given_an_explicit_prefix_when_composed_then_it_overrides_the_chosen_type(self):
        assert gitops.compose_branch("feature", "fix/login-redirect", self.PROMPT) == "fix/login-redirect"

    def test_given_a_name_with_spaces_and_punctuation_when_composed_then_it_is_sanitised(self):
        assert gitops.compose_branch("feature", "My New Thing!", self.PROMPT) == "feature/My-New-Thing"

    def test_given_only_whitespace_when_composed_then_it_counts_as_blank(self):
        assert gitops.compose_branch("fix", "   ", self.PROMPT) == gitops.suggest_branch(self.PROMPT, "fix")

    def test_given_a_use_case_prompt_when_a_branch_is_suggested_then_the_subject_survives_without_the_verb(self):
        assert gitops.suggest_branch("Create a new use case for scope owners") == "feature/use-case-scope-owners"

    def test_given_a_prompt_when_a_branch_is_suggested_then_it_has_no_random_suffix(self):
        name = gitops.suggest_branch("Add a health check endpoint")
        assert not re.search(r"-[0-9a-f]{8}$", name)


class TestSanitising:
    def test_given_spaces_and_junk_when_sanitised_then_they_become_hyphens(self):
        assert gitops.sanitize_branch("feature/My New Thing!!") == "feature/My-New-Thing"

    def test_given_repeated_separators_when_sanitised_then_they_are_collapsed_and_trimmed(self):
        assert gitops.sanitize_branch("//feature//a--b--/") == "feature/a-b"

    def test_given_blank_input_when_sanitised_then_it_falls_back_to_task(self):
        assert gitops.sanitize_branch("   ") == "task"

    def test_given_a_valid_name_when_sanitised_then_it_is_left_alone(self):
        assert gitops.sanitize_branch("fix/login-redirect") == "fix/login-redirect"


class TestUniqueness:
    def test_given_a_free_name_when_made_unique_then_it_is_returned_unchanged(self):
        with mock.patch.object(gitops, "branch_exists", return_value=False):
            assert gitops.unique_branch("/r", "feature/x") == "feature/x"

    def test_given_taken_names_when_made_unique_then_the_next_free_number_is_appended(self):
        taken = {"feature/x", "feature/x-2"}
        with mock.patch.object(
            gitops, "branch_exists", side_effect=lambda r, n: n in taken
        ):
            assert gitops.unique_branch("/r", "feature/x") == "feature/x-3"

    def test_given_every_numbered_name_taken_when_made_unique_then_a_random_suffix_is_used(self):
        with mock.patch.object(gitops, "branch_exists", return_value=True):
            name = gitops.unique_branch("/r", "feature/x")
        assert re.fullmatch(r"feature/x-[0-9a-f]{6}", name)
