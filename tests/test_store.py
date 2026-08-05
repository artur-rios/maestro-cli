import json
import os
import tempfile
import unittest
from pathlib import Path

from maestro import config
from maestro.models import Status, Workflow
from maestro.store import Store, log_path_for


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("MAESTRO_HOME")
        os.environ["MAESTRO_HOME"] = self.tmp.name

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("MAESTRO_HOME", None)
        else:
            os.environ["MAESTRO_HOME"] = self._old_home
        self.tmp.cleanup()

    def make(self, prompt="task"):
        return Workflow(
            prompt=prompt,
            repo_path="/tmp/repo",
            base_branch="main",
            agents={"plan": "claude", "execute": "claude", "review": "claude"},
        )


class Persistence(StoreTestCase):
    def test_workflow_survives_a_reload(self):
        store = Store()
        wf = self.make("build a thing")
        wf.status = Status.EXECUTING
        store.add(wf)

        reopened = Store()
        loaded = reopened.get(wf.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.prompt, "build a thing")
        self.assertEqual(loaded.status, Status.EXECUTING)

    def test_all_is_newest_first(self):
        store = Store()
        first = self.make("first")
        first.created_at = 100
        second = self.make("second")
        second.created_at = 200
        store.add(first)
        store.add(second)
        self.assertEqual([w.prompt for w in store.all()], ["second", "first"])

    def test_remove_deletes_from_disk(self):
        store = Store()
        wf = self.make()
        store.add(wf)
        store.remove(wf.id)
        self.assertIsNone(Store().get(wf.id))

    def test_corrupt_state_file_does_not_crash(self):
        config.state_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(Store().all(), [])

    def test_records_from_an_older_schema_are_skipped(self):
        store = Store()
        good = self.make("good")
        store.add(good)
        raw = json.loads(config.state_path().read_text(encoding="utf-8"))
        raw["workflows"].append({"id": "old", "prompt": "missing fields"})
        config.state_path().write_text(json.dumps(raw), encoding="utf-8")

        reopened = Store()
        self.assertEqual([w.id for w in reopened.all()], [good.id])


class LogPaths(StoreTestCase):
    def test_log_path_is_created_under_the_workflow_directory(self):
        path = log_path_for("abc123", "execute", 2)
        self.assertTrue(path.parent.is_dir())
        self.assertEqual(path.name, "execute-round2.log")
        self.assertEqual(path.parent.name, "abc123")


class Backends(StoreTestCase):
    def test_defaults_written_on_first_load(self):
        loaded = config.load_backends()
        self.assertIn("claude", loaded)
        self.assertIn("codex", loaded)
        self.assertIn("opencode", loaded)
        self.assertTrue(config.backends_path().exists())

    def test_user_edits_are_preserved_and_builtins_restored(self):
        config.save_backends({"mycli": {"bin": "mycli", "args": ["{prompt}"]}})
        loaded = config.load_backends()
        self.assertIn("mycli", loaded)
        self.assertIn("claude", loaded)

    def test_user_override_of_a_builtin_wins(self):
        config.save_backends({"claude": {"bin": "claude-next", "args": ["{prompt}"]}})
        self.assertEqual(config.load_backends()["claude"]["bin"], "claude-next")

    def test_keys_added_since_the_file_was_written_are_filled_in(self):
        """A file from an older version must still gain new default keys."""
        config.save_backends(
            {
                "claude": {"bin": "claude", "args": ["-p", "{prompt}"], "timeout": 1800},
                "opencode": {"bin": "opencode", "args": ["run", "{prompt}"]},
            }
        )
        loaded = config.load_backends()
        self.assertIn("search_globs", loaded["claude"])
        self.assertIn("search_globs", loaded["opencode"])
        # The user's own non-template values are untouched.
        self.assertEqual(loaded["claude"]["timeout"], 1800)
        self.assertEqual(loaded["claude"]["bin"], "claude")

    def test_a_user_edit_still_beats_the_default_for_the_same_key(self):
        config.save_backends({"claude": {"bin": "claude", "search_globs": ["/my/path"]}})
        self.assertEqual(config.load_backends()["claude"]["search_globs"], ["/my/path"])

    def test_every_builtin_ships_search_globs(self):
        for name, backend in config.load_backends().items():
            self.assertTrue(backend.get("search_globs"), f"{name} has no search_globs")

    def test_corrupt_backends_file_falls_back_to_defaults(self):
        config.backends_path().write_text("nonsense", encoding="utf-8")
        self.assertEqual(config.load_backends()["claude"]["bin"], "claude")

    def test_home_dir_honours_the_environment_override(self):
        self.assertEqual(config.home_dir(), Path(self.tmp.name))

    def test_codex_uses_sandbox_flags_not_the_nonexistent_full_auto(self):
        codex = config.load_backends()["codex"]
        self.assertNotIn("--full-auto", codex["execute_args"])
        self.assertEqual(codex["args"], ["exec", "-C", "{cwd}", "-s", "read-only"])
        self.assertEqual(
            codex["execute_args"], ["exec", "-C", "{cwd}", "-s", "workspace-write"]
        )
        self.assertEqual(codex["prompt_via"], "stdin")

    def test_an_old_config_file_gains_stdin_delivery(self):
        """The bug: a file written before the fix still had {prompt} in args."""
        config.save_backends(
            {"opencode": {"bin": "opencode", "args": ["run", "{prompt}"]}}
        )
        loaded = config.load_backends()["opencode"]
        self.assertEqual(loaded["prompt_via"], "stdin")

    def test_legacy_templates_are_replaced_not_merged(self):
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
        self.assertEqual(loaded["args"], config.DEFAULT_BACKENDS["opencode"]["args"])
        self.assertEqual(loaded["timeout"], 999)  # unrelated edits survive

    def test_migration_is_written_back_to_disk(self):
        config.save_backends({"codex": {"bin": "codex", "args": ["exec", "{prompt}"]}})
        config.load_backends()
        on_disk = json.loads(config.backends_path().read_text(encoding="utf-8"))
        self.assertNotIn("{prompt}", " ".join(on_disk["codex"]["args"]))
        self.assertIn("{cwd}", " ".join(on_disk["codex"]["args"]))

    def test_a_file_from_an_older_version_is_upgraded_wholesale(self):
        """Templates without {prompt} can still be stale, e.g. missing --agent."""
        config.backends_path().write_text(
            json.dumps({"opencode": {"bin": "opencode",
                                     "args": ["run", "--dir", "{cwd}"],
                                     "execute_args": ["run", "--dir", "{cwd}"]}}),
            encoding="utf-8",
        )
        loaded = config.load_backends()["opencode"]
        self.assertIn("--agent", loaded["execute_args"])
        self.assertIn("build", loaded["execute_args"])

        on_disk = json.loads(config.backends_path().read_text(encoding="utf-8"))
        self.assertEqual(on_disk[config.VERSION_KEY], config.CONFIG_VERSION)

    def test_a_current_file_is_left_alone(self):
        config.save_backends(config.load_backends())
        before = config.backends_path().read_text(encoding="utf-8")
        config.load_backends()
        self.assertEqual(config.backends_path().read_text(encoding="utf-8"), before)

    def test_a_custom_backend_may_still_use_argv(self):
        """The migration only touches the three built-ins."""
        config.save_backends({"mycli": {"bin": "mycli", "args": ["--task", "{prompt}"]}})
        self.assertEqual(
            config.load_backends()["mycli"]["args"], ["--task", "{prompt}"]
        )


class BinaryResolution(StoreTestCase):
    def test_path_lookup_wins(self):
        backend = {"bin": "python", "args": ["{prompt}"]}
        self.assertIsNotNone(config.find_binary(backend))

    def test_falls_back_to_search_globs_when_not_on_path(self):
        installed = Path(self.tmp.name) / "vendor" / "v1"
        installed.mkdir(parents=True)
        binary = installed / "mycli.exe"
        binary.write_text("stub", encoding="utf-8")

        backend = {
            "bin": "definitely-not-on-path-12345",
            "search_globs": [str(Path(self.tmp.name) / "vendor" / "*" / "mycli.exe")],
        }
        self.assertEqual(config.find_binary(backend), str(binary))
        self.assertTrue(config.is_installed(backend))

    def test_newest_match_wins_so_updates_are_followed(self):
        vendor = Path(self.tmp.name) / "vendor"
        old = vendor / "v1"
        new = vendor / "v2"
        for directory in (old, new):
            directory.mkdir(parents=True)
            (directory / "mycli.exe").write_text("stub", encoding="utf-8")
        os.utime(old / "mycli.exe", (1000, 1000))
        os.utime(new / "mycli.exe", (2000, 2000))

        backend = {
            "bin": "definitely-not-on-path-12345",
            "search_globs": [str(vendor / "*" / "mycli.exe")],
        }
        self.assertEqual(config.find_binary(backend), str(new / "mycli.exe"))

    def test_missing_everywhere_is_not_installed(self):
        backend = {
            "bin": "definitely-not-on-path-12345",
            "search_globs": [str(Path(self.tmp.name) / "nowhere" / "*" / "x")],
        }
        self.assertIsNone(config.find_binary(backend))
        self.assertFalse(config.is_installed(backend))

    def test_directories_are_not_mistaken_for_binaries(self):
        vendor = Path(self.tmp.name) / "vendor"
        (vendor / "mycli.exe").mkdir(parents=True)
        backend = {
            "bin": "definitely-not-on-path-12345",
            "search_globs": [str(vendor / "*")],
        }
        self.assertIsNone(config.find_binary(backend))

    def test_backend_without_search_globs_still_works(self):
        self.assertIsNone(config.find_binary({"bin": "definitely-not-on-path-12345"}))
        self.assertIsNone(config.find_binary({}))


if __name__ == "__main__":
    unittest.main()
