"""Expanding a backend's command template and running it."""

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from maestro import backends, config
from maestro.backends import BackendError, RunResult, build_argv
from maestro.models import Role

CLAUDE = {
    "bin": "claude",
    "args": ["-p", "{prompt}"],
    "execute_args": ["-p", "{prompt}", "--permission-mode", "acceptEdits"],
}

ALL_ROLES = (Role.PLAN, Role.EXECUTE, Role.REVIEW)


class TestBuildArgv:
    @pytest.mark.parametrize("role", [Role.PLAN, Role.REVIEW])
    def test_given_a_reading_role_when_built_then_the_read_only_args_are_used(self, role):
        assert build_argv(CLAUDE, role, "do it") == ["claude", "-p", "do it"]

    def test_given_the_execute_role_when_built_then_the_execute_args_are_used(self):
        assert build_argv(CLAUDE, Role.EXECUTE, "do it") == [
            "claude", "-p", "do it", "--permission-mode", "acceptEdits"]

    def test_given_no_execute_args_when_built_for_execute_then_args_are_used(self):
        backend = {"bin": "opencode", "args": ["run", "{prompt}"]}
        assert build_argv(backend, Role.EXECUTE, "hi") == ["opencode", "run", "hi"]

    def test_given_a_prompt_with_spaces_quotes_and_newlines_when_built_then_it_stays_one_argument(self):
        argv = build_argv(CLAUDE, Role.PLAN, 'multi word "quoted" $prompt\nnewline')
        assert len(argv) == 3
        assert "newline" in argv[2]

    def test_given_a_placeholder_inside_a_larger_argument_when_built_then_it_is_substituted(self):
        backend = {"bin": "x", "args": ["--task={prompt}"]}
        assert build_argv(backend, Role.PLAN, "abc") == ["x", "--task=abc"]

    def test_given_no_bin_when_built_then_it_is_an_error(self):
        with pytest.raises(BackendError):
            build_argv({"args": ["{prompt}"]}, Role.PLAN, "x")

    @pytest.mark.parametrize("backend", [{"bin": "x"}, {"bin": "x", "args": []}])
    def test_given_no_args_when_built_then_it_is_an_error(self, backend):
        with pytest.raises(BackendError):
            build_argv(backend, Role.PLAN, "x")


class TestStdinPrompts:
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

    def test_given_a_stdin_backend_when_built_then_the_prompt_is_absent_from_argv(self):
        argv = build_argv(self.STDIN_BACKEND, Role.PLAN, "line one\nline two")
        assert argv == ["opencode", "run"]

    def test_given_a_leftover_placeholder_when_built_for_stdin_then_it_is_dropped_not_passed_literally(self):
        legacy = dict(self.STDIN_BACKEND, args=["run", "{prompt}"])
        argv = build_argv(legacy, Role.PLAN, "line one\nline two")
        assert argv == ["opencode", "run"]
        assert "{prompt}" not in argv

    def test_given_a_multiline_prompt_when_built_for_any_builtin_then_no_argument_has_a_newline(self):
        prompt = "You are the EXECUTION agent.\n\nDo the thing.\nCommit it."
        for backend in config.DEFAULT_BACKENDS.values():
            for role in ALL_ROLES:
                for arg in build_argv(backend, role, prompt):
                    assert "\n" not in arg, f"newline in argv for {backend['bin']}"

    def test_given_the_builtins_when_checked_then_each_sends_the_prompt_on_stdin(self):
        for name, backend in config.DEFAULT_BACKENDS.items():
            assert backends.sends_prompt_on_stdin(backend), f"{name} still uses argv"

    def test_given_a_custom_backend_without_prompt_via_when_built_then_the_prompt_goes_on_argv(self):
        custom = {"bin": "mycli", "args": ["--task", "{prompt}"]}
        assert not backends.sends_prompt_on_stdin(custom)
        assert build_argv(custom, Role.PLAN, "hello") == ["mycli", "--task", "hello"]


class TestWorkingDirectory:
    """Agents must act on the workflow's worktree, not wherever they default to."""

    def test_given_a_cwd_placeholder_when_built_then_it_is_expanded(self):
        backend = {"bin": "opencode", "prompt_via": "stdin", "args": ["run", "--dir", "{cwd}"]}
        assert build_argv(backend, Role.PLAN, "hi", r"C:\work\tree") == [
            "opencode", "run", "--dir", r"C:\work\tree"]

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_given_opencode_when_built_then_it_is_told_the_directory_explicitly(self, role):
        """It ignores the process cwd and would otherwise edit another repo."""
        argv = build_argv(config.DEFAULT_BACKENDS["opencode"], role, "hi", "/tree")
        assert argv[argv.index("--dir") + 1] == "/tree"

    def test_given_codex_when_built_then_it_is_told_the_directory_explicitly(self):
        argv = build_argv(config.DEFAULT_BACKENDS["codex"], Role.EXECUTE, "hi", "/tree")
        assert argv[argv.index("-C") + 1] == "/tree"

    def test_given_any_builtin_when_built_then_no_placeholder_survives(self):
        for backend in config.DEFAULT_BACKENDS.values():
            for role in ALL_ROLES:
                for arg in build_argv(backend, role, "hi", "/tree"):
                    assert "{cwd}" not in arg
                    assert "{prompt}" not in arg


class TestResolve:
    def test_given_an_unknown_name_when_resolved_then_it_is_an_error(self):
        with pytest.raises(BackendError):
            backends.resolve("nope", {"claude": CLAUDE})

    def test_given_a_known_name_when_resolved_then_its_definition_is_returned(self):
        assert backends.resolve("claude", {"claude": CLAUDE}) == CLAUDE

    def test_given_no_definitions_when_resolved_then_the_backends_file_is_read(self):
        assert backends.resolve("codex")["bin"] == "codex"


class TestAvailable:
    def test_given_configured_backends_when_listed_then_each_is_paired_with_whether_it_is_installed(self):
        defined = {"zed": {"bin": "zed"}, "alpha": {"bin": "alpha"}}
        with mock.patch("maestro.config.find_binary", side_effect=lambda b: "/x" if b["bin"] == "alpha" else None):
            assert backends.available(defined) == [("alpha", True), ("zed", False)]


class TestRunResult:
    @pytest.mark.parametrize("exit_code, timed_out, ok", [(0, False, True), (1, False, False), (0, True, False)])
    def test_given_an_exit_code_and_timeout_when_checked_then_only_a_clean_exit_is_ok(self, exit_code, timed_out, ok):
        assert RunResult(exit_code, "", timed_out).ok is ok


def _wait_for(predicate, timeout=10.0):
    """Poll until `predicate` holds; give up rather than hang if it never does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestLiveStreaming:
    """A real child process, watched from another thread while it runs."""

    def test_given_a_running_agent_when_its_log_is_read_then_the_output_so_far_is_there(self, tmp_path):
        log = tmp_path / "step.log"
        go = tmp_path / "go"
        # The child says one line, then waits for the test to have read it
        # before saying the last: the log was readable while it still ran.
        script = (
            "import os, sys, time\n"
            "print('line 0', flush=True)\n"
            f"while not os.path.exists({str(go)!r}):\n"
            "    time.sleep(0.01)\n"
            "print('line 1', flush=True)\n"
        )
        cfg = {"chatty": {"bin": sys.executable, "args": ["-c", script], "timeout": 30}}
        seen_early: list[str] = []

        def watcher():
            if _wait_for(lambda: log.exists() and "line 0" in log.read_text(encoding="utf-8")):
                seen_early.append(log.read_text(encoding="utf-8"))
            go.touch()

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        result = backends.run("chatty", Role.PLAN, "go", tmp_path, log, cfg)
        thread.join(timeout=10)

        assert result.ok
        assert seen_early, "the log never showed the first line while the agent ran"
        assert "line 1" not in seen_early[0]
        assert "line 1" in log.read_text(encoding="utf-8")


class FakeStdin:
    def __init__(self, fail=None):
        self.written = []
        self.closed = False
        self.fail = fail

    def write(self, text):
        if self.fail:
            raise self.fail
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


class TestInvocation:
    """The subprocess contract, with the process itself stubbed out."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.log = tmp_path / "logs" / "step.log"
        self.cfg = {"claude": dict(CLAUDE, timeout=42)}
        monkeypatch.setattr("maestro.config.find_binary", lambda backend: "/usr/bin/claude")

    def run_once(self, returncode=0, stdout="hello", stderr="", proc=None, model=""):
        self.proc = proc or FakePopen(lines=[stdout, stderr], returncode=returncode)
        with mock.patch("subprocess.Popen", return_value=self.proc) as popen:
            result = backends.run(
                "claude", Role.PLAN, "do it", str(self.tmp), self.log, self.cfg, model=model
            )
        return result, popen.call_args

    def test_given_an_argv_backend_when_run_then_stdin_is_detached_so_agents_cannot_steal_the_menu_input(self):
        _, call = self.run_once()
        assert call.kwargs["stdin"] == subprocess.DEVNULL

    def test_given_a_stdin_backend_when_run_then_the_prompt_arrives_on_a_closed_pipe(self):
        self.cfg["claude"]["prompt_via"] = "stdin"
        self.cfg["claude"]["args"] = ["-p"]
        _, call = self.run_once()
        assert call.kwargs["stdin"] == subprocess.PIPE
        assert _wait_for(lambda: self.proc.stdin.closed)
        assert "".join(self.proc.stdin.written) == "do it"

    @pytest.mark.parametrize("error", [OSError("broken pipe"), ValueError("closed file")])
    def test_given_an_agent_that_exits_before_reading_its_prompt_when_run_then_the_result_still_comes_back(
            self, error):
        self.cfg["claude"]["prompt_via"] = "stdin"
        proc = FakePopen()
        proc.stdin = FakeStdin(fail=error)
        result, _ = self.run_once(proc=proc)
        assert result.ok

    def test_given_a_working_directory_when_run_then_the_agent_runs_there(self):
        _, call = self.run_once()
        assert call.kwargs["cwd"] == str(self.tmp)

    def test_given_a_configured_timeout_when_run_then_the_watchdog_uses_it(self):
        with mock.patch("threading.Timer") as timer:
            self.run_once()
        assert timer.call_args.args[0] == 42

    def test_given_no_configured_timeout_when_run_then_the_default_applies(self):
        del self.cfg["claude"]["timeout"]
        with mock.patch("threading.Timer") as timer:
            self.run_once()
        assert timer.call_args.args[0] == config.DEFAULT_TIMEOUT

    def test_given_output_when_run_then_it_is_streamed_to_the_log_not_written_at_the_end(self):
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
                    "claude", Role.PLAN, "do it", str(self.tmp), self.log, self.cfg
                )

        # A size recorded after the header and after each line, never shrinking.
        assert len(sizes) >= 4
        assert sizes == sorted(sizes)
        assert sizes[0] < sizes[-1]

    def test_given_a_resolved_binary_when_run_then_its_absolute_path_replaces_the_bare_name(self):
        _, call = self.run_once()
        assert call.args[0][0] == "/usr/bin/claude"

    def test_given_a_cwd_placeholder_when_run_then_the_run_directory_fills_it(self):
        self.cfg["claude"]["args"] = ["--dir", "{cwd}"]
        _, call = self.run_once()
        assert call.args[0] == ["/usr/bin/claude", "--dir", str(self.tmp)]

    def test_given_a_model_when_run_then_it_is_passed_to_the_cli(self):
        self.cfg["claude"]["model_flag"] = "--model"
        _, call = self.run_once(model="opus")
        assert call.args[0][-2:] == ["--model", "opus"]

    def test_given_stdout_and_stderr_when_run_then_both_land_in_the_result_and_the_log(self):
        result, _ = self.run_once(stdout="planned\n", stderr="a warning\n")
        assert "planned" in result.output
        assert "a warning" in result.output
        logged = self.log.read_text(encoding="utf-8")
        assert logged.startswith("$ /usr/bin/claude ... (cwd=")
        assert "planned" in logged

    def test_given_a_nonzero_exit_when_run_then_it_is_reported_not_raised(self):
        result, _ = self.run_once(returncode=3)
        assert result.exit_code == 3
        assert not result.ok

    def test_given_any_agent_when_run_then_it_is_insulated_from_console_ctrl_c(self):
        """Ctrl-C to stop following logs used to kill the running agent."""
        _, call = self.run_once()
        if os.name == "nt":
            assert call.kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            assert call.kwargs["start_new_session"]

    def test_given_a_binary_that_cannot_be_found_when_run_then_a_readable_error_is_raised(self, monkeypatch):
        monkeypatch.setattr("maestro.config.find_binary", lambda backend: None)
        with pytest.raises(BackendError) as ctx:
            backends.run("claude", Role.PLAN, "do it", str(self.tmp), self.log, self.cfg)
        assert "could not be found" in str(ctx.value)

    def test_given_a_binary_that_fails_to_start_when_run_then_a_readable_error_is_raised(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("exec format error")):
            with pytest.raises(BackendError) as ctx:
                backends.run("claude", Role.PLAN, "do it", str(self.tmp), self.log, self.cfg)
        assert "failed to start" in str(ctx.value)
        assert "exec format error" in str(ctx.value)


class FireWhenLogged:
    """Stands in for the watchdog Timer: fires once the child has spoken.

    A real timeout of a second or so either makes the test slow or races the
    child's start-up on a loaded machine. This fires as soon as the agent's
    first line is in the log, which is the moment the test is about.
    """

    log: Path = Path()

    def __init__(self, interval, function):
        self.function = function
        self.daemon = True
        self._cancelled = threading.Event()

    def start(self):
        def fire():
            if _wait_for(lambda: self._cancelled.is_set()
                         or (self.log.exists() and "working" in self.log.read_text(encoding="utf-8"))):
                if not self._cancelled.is_set():
                    self.function()
        threading.Thread(target=fire, daemon=True).start()

    def cancel(self):
        self._cancelled.set()


class TestTimeout:
    def test_given_an_agent_that_overruns_when_run_then_it_is_killed_and_the_timeout_is_reported(
            self, tmp_path, monkeypatch):
        log = tmp_path / "step.log"
        FireWhenLogged.log = log
        monkeypatch.setattr(backends.threading, "Timer", FireWhenLogged)
        script = "import time\nprint('working', flush=True)\ntime.sleep(30)\n"
        cfg = {"slow": {"bin": sys.executable, "args": ["-c", script], "timeout": 7}}

        result = backends.run("slow", Role.PLAN, "do it", tmp_path, log, cfg)

        assert result.timed_out
        assert not result.ok
        logged = log.read_text(encoding="utf-8")
        assert "timed out after 7s" in logged
        # Whatever it managed to say before the axe fell is kept.
        assert "working" in logged
        assert "working" in result.output

    def test_given_an_agent_already_gone_when_the_watchdog_fires_then_the_kill_error_is_ignored(
            self, tmp_path, monkeypatch):
        fired = []

        class FireNow:
            def __init__(self, interval, function):
                self.function = function
                self.daemon = True

            def start(self):
                fired.append(True)
                self.function()

            def cancel(self):
                pass

        proc = FakePopen()
        proc.kill = mock.Mock(side_effect=OSError("no such process"))
        monkeypatch.setattr(backends.threading, "Timer", FireNow)
        monkeypatch.setattr("maestro.config.find_binary", lambda backend: "/usr/bin/claude")
        with mock.patch("subprocess.Popen", return_value=proc):
            result = backends.run("claude", Role.PLAN, "x", tmp_path, tmp_path / "s.log", {"claude": CLAUDE})
        assert fired
        assert result.timed_out
