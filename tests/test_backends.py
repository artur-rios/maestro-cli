import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from maestro import backends, config
from maestro.backends import BackendError, build_argv
from maestro.models import Role

CLAUDE = {
    "bin": "claude",
    "args": ["-p", "{prompt}"],
    "execute_args": ["-p", "{prompt}", "--permission-mode", "acceptEdits"],
}


class BuildArgv(unittest.TestCase):
    def test_plan_and_review_use_read_only_args(self):
        for role in (Role.PLAN, Role.REVIEW):
            self.assertEqual(
                build_argv(CLAUDE, role, "do it"), ["claude", "-p", "do it"]
            )

    def test_execute_uses_execute_args(self):
        self.assertEqual(
            build_argv(CLAUDE, Role.EXECUTE, "do it"),
            ["claude", "-p", "do it", "--permission-mode", "acceptEdits"],
        )

    def test_execute_falls_back_to_args_when_unset(self):
        backend = {"bin": "opencode", "args": ["run", "{prompt}"]}
        self.assertEqual(
            build_argv(backend, Role.EXECUTE, "hi"), ["opencode", "run", "hi"]
        )

    def test_prompt_stays_a_single_argument(self):
        argv = build_argv(CLAUDE, Role.PLAN, 'multi word "quoted" $prompt\nnewline')
        self.assertEqual(len(argv), 3)
        self.assertIn("newline", argv[2])

    def test_placeholder_substituted_inside_a_larger_argument(self):
        backend = {"bin": "x", "args": ["--task={prompt}"]}
        self.assertEqual(build_argv(backend, Role.PLAN, "abc"), ["x", "--task=abc"])

    def test_missing_bin_is_an_error(self):
        with self.assertRaises(BackendError):
            build_argv({"args": ["{prompt}"]}, Role.PLAN, "x")

    def test_missing_args_is_an_error(self):
        with self.assertRaises(BackendError):
            build_argv({"bin": "x"}, Role.PLAN, "x")


class StdinPrompts(unittest.TestCase):
    """A multi-line prompt must never ride on the command line.

    cmd.exe truncates an argument at its first newline, so a batch wrapper such
    as opencode.cmd delivered only the opening line of the instructions.
    """

    STDIN_BACKEND = {
        "bin": "opencode",
        "prompt_via": "stdin",
        "args": ["run"],
        "execute_args": ["run"],
    }

    def test_prompt_is_absent_from_argv(self):
        argv = build_argv(self.STDIN_BACKEND, Role.PLAN, "line one\nline two")
        self.assertEqual(argv, ["opencode", "run"])

    def test_a_leftover_placeholder_is_dropped_not_passed_literally(self):
        legacy = dict(self.STDIN_BACKEND, args=["run", "{prompt}"])
        argv = build_argv(legacy, Role.PLAN, "line one\nline two")
        self.assertEqual(argv, ["opencode", "run"])
        self.assertNotIn("{prompt}", argv)

    def test_no_argument_ever_contains_a_newline(self):
        prompt = "You are the EXECUTION agent.\n\nDo the thing.\nCommit it."
        for backend in config.DEFAULT_BACKENDS.values():
            for role in (Role.PLAN, Role.EXECUTE, Role.REVIEW):
                for arg in build_argv(backend, role, prompt):
                    self.assertNotIn("\n", arg, f"newline in argv for {backend['bin']}")

    def test_every_builtin_uses_stdin(self):
        for name, backend in config.DEFAULT_BACKENDS.items():
            self.assertTrue(
                backends.sends_prompt_on_stdin(backend), f"{name} still uses argv"
            )

    def test_arg_mode_is_still_supported_for_custom_backends(self):
        custom = {"bin": "mycli", "args": ["--task", "{prompt}"]}
        self.assertEqual(
            build_argv(custom, Role.PLAN, "hello"), ["mycli", "--task", "hello"]
        )


class LiveStreaming(unittest.TestCase):
    """A real child process, watched from another thread while it runs."""

    def test_the_log_is_readable_before_the_agent_finishes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "step.log"

        script = (
            "import time, sys\n"
            "for i in range(5):\n"
            "    print('line %d' % i, flush=True)\n"
            "    time.sleep(0.3)\n"
        )
        cfg = {"chatty": {"bin": sys.executable, "args": ["-c", script], "timeout": 30}}

        seen: list[int] = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                if log.exists():
                    seen.append(len(log.read_text(encoding="utf-8").splitlines()))
                time.sleep(0.1)

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        result = backends.run("chatty", Role.PLAN, "go", tmp.name, log, cfg)
        stop.set()
        thread.join(timeout=2)

        self.assertTrue(result.ok)
        # The watcher saw the file part-written, not only complete.
        self.assertTrue(seen, "watcher never read the log")
        self.assertLess(min(seen), max(seen), f"log appeared all at once: {seen}")
        self.assertIn("line 4", log.read_text(encoding="utf-8"))


class WorkingDirectory(unittest.TestCase):
    """Agents must act on the workflow's worktree, not wherever they default to."""

    def test_cwd_placeholder_is_expanded(self):
        backend = {"bin": "opencode", "prompt_via": "stdin", "args": ["run", "--dir", "{cwd}"]}
        self.assertEqual(
            build_argv(backend, Role.PLAN, "hi", r"C:\work\tree"),
            ["opencode", "run", "--dir", r"C:\work\tree"],
        )

    def test_opencode_is_told_the_directory_explicitly(self):
        """It ignores the process cwd and would otherwise edit another repo."""
        backend = config.DEFAULT_BACKENDS["opencode"]
        for role in (Role.PLAN, Role.EXECUTE, Role.REVIEW):
            argv = build_argv(backend, role, "hi", "/tree")
            self.assertIn("--dir", argv)
            self.assertIn("/tree", argv)

    def test_codex_is_told_the_directory_explicitly(self):
        backend = config.DEFAULT_BACKENDS["codex"]
        argv = build_argv(backend, Role.EXECUTE, "hi", "/tree")
        self.assertIn("-C", argv)
        self.assertIn("/tree", argv)

    def test_no_unexpanded_placeholder_survives(self):
        for backend in config.DEFAULT_BACKENDS.values():
            for role in (Role.PLAN, Role.EXECUTE, Role.REVIEW):
                for arg in build_argv(backend, role, "hi", "/tree"):
                    self.assertNotIn("{cwd}", arg)
                    self.assertNotIn("{prompt}", arg)


class Resolve(unittest.TestCase):
    def test_unknown_backend_is_an_error(self):
        with self.assertRaises(BackendError):
            backends.resolve("nope", {"claude": CLAUDE})

    def test_known_backend_returned(self):
        self.assertEqual(backends.resolve("claude", {"claude": CLAUDE}), CLAUDE)


class FakeStdin:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, text):
        self.written.append(text)

    def close(self):
        self.closed = True


class FakePopen:
    """Just enough of Popen for the call-shape assertions below."""

    def __init__(self, lines=("hello\n",), returncode=0):
        self.stdout = io.StringIO("".join(lines))
        self.stdin = FakeStdin()
        self._returncode = returncode
        self.killed = False

    def wait(self):
        return self._returncode

    def kill(self):
        self.killed = True


class Invocation(unittest.TestCase):
    """The subprocess contract, with the process itself stubbed out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "step.log"
        self.cfg = {"claude": dict(CLAUDE, timeout=42)}

        mock.patch(
            "maestro.config.find_binary", return_value="/usr/bin/claude"
        ).start()
        self.addCleanup(mock.patch.stopall)

    def run_once(self, returncode=0, stdout="hello", stderr=""):
        self.proc = FakePopen(lines=[stdout, stderr], returncode=returncode)
        with mock.patch("subprocess.Popen", return_value=self.proc) as popen:
            result = backends.run(
                "claude", Role.PLAN, "do it", self.tmp.name, self.log, self.cfg
            )
        return result, popen.call_args

    def test_stdin_is_detached_so_agents_cannot_steal_the_menu_input(self):
        _, call = self.run_once()
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)

    def test_stdin_backend_receives_the_prompt_on_a_pipe(self):
        self.cfg["claude"]["prompt_via"] = "stdin"
        self.cfg["claude"]["args"] = ["-p"]
        _, call = self.run_once()
        self.assertEqual(call.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual("".join(self.proc.stdin.written), "do it")
        self.assertTrue(self.proc.stdin.closed)

    def test_runs_in_the_given_working_directory(self):
        _, call = self.run_once()
        self.assertEqual(call.kwargs["cwd"], self.tmp.name)

    def test_uses_the_backends_configured_timeout(self):
        with mock.patch("threading.Timer") as timer:
            self.run_once()
        self.assertEqual(timer.call_args.args[0], 42)

    def test_output_is_streamed_to_the_log_not_written_at_the_end(self):
        """The log must grow while the agent talks, so it can be followed."""
        sizes = []
        real_open = Path.open

        def watching_open(self_path, *a, **k):
            handle = real_open(self_path, *a, **k)
            original_flush = handle.flush

            def flush():
                original_flush()
                sizes.append(self_path.stat().st_size)

            handle.flush = flush
            return handle

        self.proc = FakePopen(lines=["one\n", "two\n", "three\n"])
        with mock.patch.object(Path, "open", watching_open):
            with mock.patch("subprocess.Popen", return_value=self.proc):
                backends.run(
                    "claude", Role.PLAN, "do it", self.tmp.name, self.log, self.cfg
                )

        # A size recorded after each line, each larger than the last.
        self.assertGreaterEqual(len(sizes), 4)
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[-1])

    def test_resolved_absolute_path_replaces_the_bare_name(self):
        _, call = self.run_once()
        self.assertEqual(call.args[0][0], "/usr/bin/claude")

    def test_the_run_cwd_is_what_fills_the_placeholder(self):
        self.cfg["claude"]["args"] = ["--dir", "{cwd}"]
        _, call = self.run_once()
        self.assertEqual(call.args[0], ["/usr/bin/claude", "--dir", self.tmp.name])

    def test_stdout_and_stderr_both_land_in_the_log(self):
        result, _ = self.run_once(stdout="planned", stderr="a warning")
        self.assertIn("planned", result.output)
        self.assertIn("a warning", result.output)
        self.assertIn("planned", self.log.read_text(encoding="utf-8"))

    def test_nonzero_exit_is_reported_not_raised(self):
        result, _ = self.run_once(returncode=3)
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.ok)

    def test_timeout_is_captured_as_a_result(self):
        mock.patch.stopall()  # use the real binary lookup for a real process
        script = "import time, sys\nprint('working', flush=True)\ntime.sleep(30)\n"
        cfg = {"slow": {"bin": sys.executable, "args": ["-c", script], "timeout": 1}}
        result = backends.run(
            "slow", Role.PLAN, "do it", self.tmp.name, self.log, cfg
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("timed out after 1s", logged)
        # Whatever it managed to say before the axe fell is kept.
        self.assertIn("working", logged)
        self.assertIn("working", result.output)

    def test_the_agent_is_insulated_from_console_ctrl_c(self):
        """Ctrl-C to stop following logs used to kill the running agent."""
        _, call = self.run_once()
        if os.name == "nt":
            self.assertEqual(
                call.kwargs["creationflags"], subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            self.assertTrue(call.kwargs["start_new_session"])

    def test_missing_binary_raises_a_readable_error(self):
        with mock.patch("maestro.config.find_binary", return_value=None):
            with self.assertRaises(BackendError) as ctx:
                backends.run(
                    "claude", Role.PLAN, "do it", self.tmp.name, self.log, self.cfg
                )
        self.assertIn("could not be found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
