"""The workflow store, log paths and the backends file."""

import json
import os
import sys
from pathlib import Path

import pytest

from maestro import config
from maestro.models import Status, Workflow
from maestro.store import Store, discard_logs, log_dir_for, log_path_for


def make(prompt="task"):
    return Workflow(
        prompt=prompt,
        repo_path="/tmp/repo",
        base_branch="main",
        agents={"plan": "claude", "execute": "claude", "review": "claude"},
    )


class TestPersistence:
    def test_given_a_saved_workflow_when_the_store_is_reopened_then_it_is_still_there(self):
        store = Store()
        wf = make("build a thing")
        wf.status = Status.EXECUTING
        store.add(wf)

        loaded = Store().get(wf.id)
        assert loaded is not None
        assert loaded.prompt == "build a thing"
        assert loaded.status == Status.EXECUTING

    def test_given_two_workflows_when_listed_then_the_newest_comes_first(self):
        store = Store()
        first = make("first")
        first.created_at = 100
        second = make("second")
        second.created_at = 200
        store.add(first)
        store.add(second)
        assert [w.prompt for w in store.all()] == ["second", "first"]

    def test_given_a_removed_workflow_when_the_store_is_reopened_then_it_is_gone(self):
        store = Store()
        wf = make()
        store.add(wf)
        store.remove(wf.id)
        assert Store().get(wf.id) is None

    def test_given_an_unknown_id_when_removed_then_nothing_happens(self):
        store = Store()
        store.add(make())
        store.remove("no-such-id")
        assert len(Store().all()) == 1

    def test_given_no_state_file_when_opened_then_the_store_is_empty(self):
        assert Store().all() == []

    def test_given_a_corrupt_state_file_when_opened_then_the_store_is_empty(self):
        config.state_path().write_text("{not json", encoding="utf-8")
        assert Store().all() == []

    def test_given_a_record_from_an_older_schema_when_opened_then_it_is_skipped(self):
        store = Store()
        good = make("good")
        store.add(good)
        raw = json.loads(config.state_path().read_text(encoding="utf-8"))
        raw["workflows"].append({"id": "old", "prompt": "missing fields"})
        raw["workflows"].append({**good.to_dict(), "id": "bad", "status": "no-such-status"})
        config.state_path().write_text(json.dumps(raw), encoding="utf-8")

        assert [w.id for w in Store().all()] == [good.id]

    def test_given_a_save_when_it_finishes_then_no_temporary_file_is_left(self):
        Store().add(make())
        assert not config.state_path().with_suffix(".json.tmp").exists()

    def test_given_an_explicit_path_when_used_then_the_state_is_written_there(self, tmp_path):
        path = tmp_path / "elsewhere.json"
        wf = make()
        Store(path).add(wf)
        assert Store(path).get(wf.id) is not None
        assert not config.state_path().exists()


class TestLogPaths:
    def test_given_a_round_when_its_log_path_is_made_then_it_sits_in_the_workflow_directory(self):
        path = log_path_for("abc123", "execute", 2)
        assert path.parent.is_dir()
        assert path.name == "execute-round2.log"
        assert path.parent == log_dir_for("abc123")

    def test_given_a_checklist_item_when_its_log_path_is_made_then_the_item_is_in_the_name(self):
        assert log_path_for("abc123", "execute", 1, task_index=3).name == "execute-round1-task3.log"

    @pytest.mark.parametrize("attempt, name", [
        (None, "execute-round1-task2.log"),
        (1, "execute-round1-task2.log"),
        (2, "execute-round1-task2-try2.log"),
    ])
    def test_given_an_attempt_when_its_log_path_is_made_then_only_retries_are_numbered(self, attempt, name):
        assert log_path_for("abc123", "execute", 1, task_index=2, attempt=attempt).name == name

    def test_given_logs_when_discarded_then_the_directory_is_removed(self):
        log_path_for("abc123", "plan", 1).write_text("x", encoding="utf-8")
        assert discard_logs("abc123") is True
        assert not log_dir_for("abc123").exists()

    def test_given_no_logs_when_discarded_then_it_reports_nothing_removed(self):
        assert discard_logs("never-ran") is False


class TestBackendsFile:
    def test_given_no_file_when_loaded_then_the_defaults_are_written(self):
        loaded = config.load_backends()
        assert {"claude", "codex", "opencode"} <= set(loaded)
        assert config.backends_path().exists()

    def test_given_a_custom_backend_when_loaded_then_it_is_kept_and_builtins_restored(self):
        config.save_backends({"mycli": {"bin": "mycli", "args": ["{prompt}"]}})
        loaded = config.load_backends()
        assert "mycli" in loaded
        assert "claude" in loaded

    def test_given_an_override_of_a_builtin_when_loaded_then_the_override_wins(self):
        config.save_backends({"claude": {"bin": "claude-next", "args": ["{prompt}"]}})
        assert config.load_backends()["claude"]["bin"] == "claude-next"

    def test_given_a_file_missing_newer_keys_when_loaded_then_they_are_filled_in(self):
        """A file from an older version must still gain new default keys."""
        config.save_backends(
            {
                "claude": {"bin": "claude", "args": ["-p", "{prompt}"], "timeout": 1800},
                "opencode": {"bin": "opencode", "args": ["run", "{prompt}"]},
            }
        )
        loaded = config.load_backends()
        assert "search_globs" in loaded["claude"]
        assert "search_globs" in loaded["opencode"]
        # The user's own non-template values are untouched.
        assert loaded["claude"]["timeout"] == 1800
        assert loaded["claude"]["bin"] == "claude"

    def test_given_a_user_value_for_a_default_key_when_loaded_then_the_user_value_wins(self):
        config.save_backends({"claude": {"bin": "claude", "search_globs": ["/my/path"]}})
        assert config.load_backends()["claude"]["search_globs"] == ["/my/path"]

    def test_given_the_builtins_when_loaded_then_each_ships_search_globs(self):
        for name, backend in config.load_backends().items():
            assert backend.get("search_globs"), f"{name} has no search_globs"

    @pytest.mark.parametrize("content", ["nonsense", "[1, 2]"])
    def test_given_an_unreadable_file_when_loaded_then_the_defaults_are_used(self, content):
        config.backends_path().write_text(content, encoding="utf-8")
        assert config.load_backends()["claude"]["bin"] == "claude"

    def test_given_a_non_object_entry_when_loaded_then_it_is_ignored(self):
        config.backends_path().write_text(
            json.dumps({config.VERSION_KEY: config.CONFIG_VERSION, "broken": "not a dict"}),
            encoding="utf-8",
        )
        assert "broken" not in config.load_backends()

    def test_given_maestro_home_when_the_home_is_asked_for_then_the_override_is_used(self, maestro_home):
        assert config.home_dir() == maestro_home
        assert maestro_home.is_dir()

    def test_given_no_maestro_home_when_the_home_is_asked_for_then_it_is_under_the_user_home(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("MAESTRO_HOME")
        monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
        assert config.home_dir() == tmp_path / ".maestro"

    def test_given_codex_when_loaded_then_it_uses_sandbox_flags_not_the_nonexistent_full_auto(self):
        codex = config.load_backends()["codex"]
        assert "--full-auto" not in codex["execute_args"]
        assert codex["args"] == ["exec", "-C", "{cwd}", "-s", "read-only"]
        assert codex["execute_args"] == ["exec", "-C", "{cwd}", "-s", "workspace-write"]
        assert codex["prompt_via"] == "stdin"

    def test_given_an_old_file_with_the_prompt_on_argv_when_loaded_then_it_gains_stdin_delivery(self):
        """The bug: a file written before the fix still had {prompt} in args."""
        config.save_backends(
            {"opencode": {"bin": "opencode", "args": ["run", "{prompt}"]}}
        )
        loaded = config.load_backends()["opencode"]
        assert loaded["prompt_via"] == "stdin"

    def test_given_legacy_templates_when_loaded_then_they_are_replaced_not_merged(self):
        """Stale args would drop --dir and put the prompt back on argv."""
        config.save_backends(
            {
                "opencode": {
                    "bin": "opencode",
                    "args": ["run", "{prompt}"],
                    "execute_args": ["run", "{prompt}"],
                    "timeout": 999,
                }
            }
        )
        loaded = config.load_backends()["opencode"]
        assert loaded["args"] == config.DEFAULT_BACKENDS["opencode"]["args"]
        assert loaded["timeout"] == 999  # unrelated edits survive

    def test_given_a_migration_when_loaded_then_it_is_written_back_to_disk(self):
        config.save_backends({"codex": {"bin": "codex", "args": ["exec", "{prompt}"]}})
        config.load_backends()
        on_disk = json.loads(config.backends_path().read_text(encoding="utf-8"))
        assert "{prompt}" not in " ".join(on_disk["codex"]["args"])
        assert "{cwd}" in " ".join(on_disk["codex"]["args"])

    def test_given_a_file_from_an_older_version_when_loaded_then_it_is_upgraded_wholesale(self):
        """Templates without {prompt} can still be stale, e.g. missing --agent."""
        config.backends_path().write_text(
            json.dumps({"opencode": {"bin": "opencode",
                                     "args": ["run", "--dir", "{cwd}"],
                                     "execute_args": ["run", "--dir", "{cwd}"]}}),
            encoding="utf-8",
        )
        loaded = config.load_backends()["opencode"]
        assert "--agent" in loaded["execute_args"]
        assert "build" in loaded["execute_args"]

        on_disk = json.loads(config.backends_path().read_text(encoding="utf-8"))
        assert on_disk[config.VERSION_KEY] == config.CONFIG_VERSION

    def test_given_a_current_file_when_loaded_then_it_is_left_alone(self):
        config.save_backends(config.load_backends())
        before = config.backends_path().read_text(encoding="utf-8")
        config.load_backends()
        assert config.backends_path().read_text(encoding="utf-8") == before

    def test_given_a_custom_backend_with_the_prompt_on_argv_when_loaded_then_it_is_not_migrated(self):
        """The migration only touches the three built-ins."""
        config.save_backends({"mycli": {"bin": "mycli", "args": ["--task", "{prompt}"]}})
        assert config.load_backends()["mycli"]["args"] == ["--task", "{prompt}"]


class TestBinaryResolution:
    @pytest.fixture
    def on_path(self, tmp_path, monkeypatch):
        """A directory that is the whole PATH, holding the named executables."""
        directory = tmp_path / "bin"
        directory.mkdir()
        monkeypatch.setenv("PATH", str(directory))

        def install(name):
            binary = directory / (name + (".exe" if sys.platform == "win32" else ""))
            binary.write_text("stub", encoding="utf-8")
            binary.chmod(0o755)
            return binary
        return install

    def test_given_a_binary_on_path_when_resolved_then_the_path_lookup_wins(self, on_path, tmp_path):
        binary = on_path("mycli")
        elsewhere = tmp_path / "vendor"
        elsewhere.mkdir()
        (elsewhere / "mycli").write_text("stub", encoding="utf-8")
        backend = {"bin": "mycli", "search_globs": [str(elsewhere / "*")]}
        assert Path(config.find_binary(backend)) == binary

    def test_given_a_binary_only_in_a_search_glob_when_resolved_then_the_glob_finds_it(self, on_path, tmp_path):
        installed = tmp_path / "vendor" / "v1"
        installed.mkdir(parents=True)
        binary = installed / "mycli.exe"
        binary.write_text("stub", encoding="utf-8")

        backend = {
            "bin": "mycli",
            "search_globs": [str(tmp_path / "vendor" / "*" / "mycli.exe")],
        }
        assert config.find_binary(backend) == str(binary)
        assert config.is_installed(backend)

    def test_given_several_glob_matches_when_resolved_then_the_newest_wins_so_updates_are_followed(
            self, on_path, tmp_path):
        vendor = tmp_path / "vendor"
        old = vendor / "v1"
        new = vendor / "v2"
        for directory in (old, new):
            directory.mkdir(parents=True)
            (directory / "mycli.exe").write_text("stub", encoding="utf-8")
        os.utime(old / "mycli.exe", (1000, 1000))
        os.utime(new / "mycli.exe", (2000, 2000))

        backend = {"bin": "mycli", "search_globs": [str(vendor / "*" / "mycli.exe")]}
        assert config.find_binary(backend) == str(new / "mycli.exe")

    def test_given_a_glob_with_an_environment_variable_when_resolved_then_it_is_expanded(
            self, on_path, tmp_path, monkeypatch):
        (tmp_path / "mycli.exe").write_text("stub", encoding="utf-8")
        monkeypatch.setenv("MYCLI_ROOT", str(tmp_path))
        backend = {"bin": "mycli", "search_globs": ["$MYCLI_ROOT/mycli.exe"]}
        assert Path(config.find_binary(backend)) == tmp_path / "mycli.exe"

    def test_given_no_match_anywhere_when_resolved_then_it_is_not_installed(self, on_path, tmp_path):
        backend = {"bin": "mycli", "search_globs": [str(tmp_path / "nowhere" / "*" / "x")]}
        assert config.find_binary(backend) is None
        assert not config.is_installed(backend)

    def test_given_a_directory_matching_the_glob_when_resolved_then_it_is_not_mistaken_for_a_binary(
            self, on_path, tmp_path):
        vendor = tmp_path / "vendor"
        (vendor / "mycli.exe").mkdir(parents=True)
        backend = {"bin": "mycli", "search_globs": [str(vendor / "*")]}
        assert config.find_binary(backend) is None

    def test_given_no_search_globs_or_no_bin_when_resolved_then_nothing_is_found(self, on_path):
        assert config.find_binary({"bin": "mycli"}) is None
        assert config.find_binary({}) is None
