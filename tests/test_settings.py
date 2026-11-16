"""User defaults: loading, saving and reading what was typed."""

import json
from pathlib import Path

import pytest

from maestro import settings as settings_mod
from maestro.settings import SettingsError


class TestLoadAndSave:
    def test_given_no_file_when_loaded_then_the_defaults_come_back(self):
        assert settings_mod.load() == settings_mod.DEFAULTS

    def test_given_saved_settings_when_loaded_then_they_override_the_defaults(self):
        settings_mod.save({**settings_mod.DEFAULTS, "mode": "autonomous", "rounds": 5})
        loaded = settings_mod.load()
        assert loaded["mode"] == "autonomous"
        assert loaded["rounds"] == 5
        assert loaded["attempts"] == settings_mod.DEFAULTS["attempts"]

    def test_given_unknown_keys_when_saved_and_loaded_then_they_are_dropped(self):
        settings_mod.save({"mode": "autonomous", "nonsense": 1})
        assert "nonsense" not in json.loads(settings_mod.path().read_text(encoding="utf-8"))
        settings_mod.path().write_text(json.dumps({"mode": "autonomous", "other": 2}), encoding="utf-8")
        assert "other" not in settings_mod.load()

    @pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", "\"just a string\""])
    def test_given_an_unusable_file_when_loaded_then_the_defaults_come_back(self, content):
        settings_mod.path().write_text(content, encoding="utf-8")
        assert settings_mod.load() == settings_mod.DEFAULTS

    def test_given_the_defaults_when_loaded_twice_then_the_copies_are_independent(self):
        first = settings_mod.load()
        first["prompt_paths"].append("/somewhere")
        assert settings_mod.load()["prompt_paths"] == []


class TestCoerce:
    def test_given_an_unknown_key_when_coerced_then_the_valid_keys_are_listed(self):
        with pytest.raises(SettingsError) as ctx:
            settings_mod.coerce("nonsense", "1")
        assert "not a setting" in str(ctx.value)
        assert "mode" in str(ctx.value)

    @pytest.mark.parametrize("raw", ["0", "-2", "two", ""])
    def test_given_a_count_that_is_not_positive_when_coerced_then_it_is_refused(self, raw):
        with pytest.raises(SettingsError):
            settings_mod.coerce("rounds", raw)

    def test_given_a_padded_count_when_coerced_then_it_is_a_number(self):
        assert settings_mod.coerce("attempts", " 4 ") == 4

    @pytest.mark.parametrize("raw, value", [
        ("on", True), ("YES", True), ("true", True), ("1", True),
        ("off", False), ("No", False), ("false", False), ("0", False),
    ])
    def test_given_a_switch_word_when_coerced_then_it_becomes_a_boolean(self, raw, value):
        assert settings_mod.coerce("confirm", raw) is value

    def test_given_a_word_that_is_not_a_switch_when_coerced_then_it_is_refused(self):
        with pytest.raises(SettingsError) as ctx:
            settings_mod.coerce("follow", "maybe")
        assert "on or off" in str(ctx.value)

    @pytest.mark.parametrize("raw, mode", [("auto", "autonomous"), ("Manual", "supervised")])
    def test_given_a_short_mode_when_coerced_then_it_is_spelled_out(self, raw, mode):
        assert settings_mod.coerce("mode", raw) == mode

    def test_given_an_unknown_mode_when_coerced_then_it_is_refused(self):
        with pytest.raises(SettingsError):
            settings_mod.coerce("mode", "sideways")

    def test_given_a_home_relative_repo_when_coerced_then_the_tilde_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert settings_mod.coerce("repo", "~/project") == str(tmp_path / "project")

    def test_given_a_blank_repo_when_coerced_then_it_means_the_working_directory(self):
        assert settings_mod.coerce("repo", "  ") == ""

    def test_given_a_skip_word_for_review_when_coerced_then_it_is_stored_as_off(self):
        assert settings_mod.coerce("review", "skip") == settings_mod.OFF

    def test_given_a_backend_for_execute_when_coerced_then_it_is_kept(self):
        assert settings_mod.coerce("execute", "codex") == "codex"

    def test_given_free_text_when_coerced_then_it_is_stripped(self):
        assert settings_mod.coerce("branch_type", "  fix ") == "fix"


class TestPromptPaths:
    def test_given_a_directory_when_added_then_it_is_stored_resolved_and_only_once(self, tmp_path):
        settings = {"prompt_paths": []}
        assert settings_mod.add_prompt_path(settings, f'"{tmp_path}"') == tmp_path
        settings_mod.add_prompt_path(settings, str(tmp_path))
        assert settings["prompt_paths"] == [str(tmp_path.resolve())]

    def test_given_a_missing_directory_when_added_then_it_is_refused(self, tmp_path):
        with pytest.raises(SettingsError) as ctx:
            settings_mod.add_prompt_path({}, str(tmp_path / "nope"))
        assert "no such directory" in str(ctx.value)

    def test_given_a_file_when_added_then_it_is_refused(self, tmp_path):
        file = tmp_path / "a.md"
        file.write_text("x", encoding="utf-8")
        with pytest.raises(SettingsError) as ctx:
            settings_mod.add_prompt_path({}, str(file))
        assert "is a file" in str(ctx.value)

    def test_given_a_stored_root_when_removed_by_any_spelling_then_it_is_gone(self, tmp_path):
        settings = {"prompt_paths": [str(tmp_path.resolve()), "/other"]}
        assert settings_mod.remove_prompt_path(settings, f"'{tmp_path}'") is True
        assert settings["prompt_paths"] == ["/other"]

    def test_given_a_root_that_is_not_stored_when_removed_then_nothing_changes(self, tmp_path):
        settings = {"prompt_paths": ["/other"]}
        assert settings_mod.remove_prompt_path(settings, str(tmp_path)) is False
        assert settings["prompt_paths"] == ["/other"]

    def test_given_a_path_that_cannot_be_resolved_when_removed_then_the_typed_text_is_still_matched(
            self, monkeypatch):
        def unresolvable(self, strict=False):
            raise OSError("too many links")

        monkeypatch.setattr(Path, "resolve", unresolvable)
        settings = {"prompt_paths": ["loop"]}
        assert settings_mod.remove_prompt_path(settings, "loop") is True
