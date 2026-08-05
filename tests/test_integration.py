"""End-to-end run against a real git repository.

The AI CLIs are replaced by a small Python script that behaves like an agent:
it reads its prompt, and for the execute role it actually edits and commits a
file. Everything else -- worktrees, diffing, the merge -- is real git.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from maestro import config, gitops
from maestro.models import Mode, Status, Verdict, Workflow
from maestro.runner import Runner
from maestro.store import Store

#: Emits a checklist when planning, implements one item per call, and (when
#: STALL_ONCE is set) wastes its first execute call without committing.
CHECKLIST_AGENT = '''\
import sys, pathlib, subprocess, os
prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
state = pathlib.Path(os.environ["AGENT_STATE"])

if "PLANNING agent" in prompt:
    print("Plan: create two files.")
    print()
    print("TASKS:")
    print("1. Create alpha.txt containing alpha.")
    print("2. Create beta.txt containing beta.")
elif "EXECUTION agent" in prompt:
    calls = int(state.read_text()) + 1 if state.exists() else 1
    state.write_text(str(calls))
    if os.environ.get("STALL_ONCE") and calls == 1:
        print("I had a look around but ran out of turns.")
        sys.exit(0)
    # The whole checklist is quoted in every prompt, so key off the
    # "Work on item N ONLY" instruction rather than the file names.
    name = "beta" if "Work on item 2" in prompt else "alpha"
    pathlib.Path(name + ".txt").write_text(name + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", "add " + name], check=True)
    print("committed " + name)
else:
    print("Matches the plan.")
    print("VERDICT: APPROVE")
'''

FAKE_AGENT = '''\
import sys, pathlib, subprocess
prompt = sys.argv[1]
if "PLANNING agent" in prompt:
    print("PLAN: create greeting.txt with the word hello")
elif "EXECUTION agent" in prompt:
    target = pathlib.Path("greeting.txt")
    target.write_text("hello\\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", "add greeting"], check=True)
    print("wrote greeting.txt and committed")
elif "REVIEW agent" in prompt:
    if "greeting.txt" not in prompt:
        print("The diff does not contain the expected file.")
        print("VERDICT: REQUEST_CHANGES")
    else:
        print("The change matches the plan.")
        print("VERDICT: APPROVE")
'''

HAS_GIT = shutil.which("git") is not None


@unittest.skipUnless(HAS_GIT, "git is not installed")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old_home = os.environ.get("MAESTRO_HOME")
        os.environ["MAESTRO_HOME"] = str(self.tmp / "home")

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("# demo\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "initial")

        agent_script = self.tmp / "fake_agent.py"
        agent_script.write_text(FAKE_AGENT, encoding="utf-8")
        config.save_backends(
            {
                "fake": {
                    "bin": sys.executable,
                    "args": [str(agent_script), "{prompt}"],
                    "timeout": 60,
                }
            }
        )

        self.store = Store()
        self.runner = Runner(self.store)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("MAESTRO_HOME", None)
        else:
            os.environ["MAESTRO_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def make(self, mode, **agents):
        staffing = {"plan": "fake", "execute": "fake", "review": "fake"}
        staffing.update(agents)
        wf = Workflow(
            prompt="add a greeting file",
            repo_path=str(self.repo),
            base_branch="main",
            agents=staffing,
            mode=mode,
        )
        self.store.add(wf)
        return wf

    def test_autonomous_workflow_lands_the_change_on_main(self):
        wf = self.make(Mode.AUTONOMOUS)
        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertIs(wf.verdict, Verdict.APPROVE)
        self.assertIn("create greeting.txt", wf.plan_text)

        # The file is really on main now, via a real merge commit.
        self.assertEqual((self.repo / "greeting.txt").read_text().strip(), "hello")
        log = self._git("log", "--oneline", "-3")
        self.assertIn("Merge feature/", log)
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD"), "main")

        for step in wf.steps:
            self.assertTrue(Path(step.log_path).exists())

    def test_one_agent_alone_can_land_the_change(self):
        """Plan and review off: the shortest workflow there is."""
        wf = self.make(Mode.AUTONOMOUS, plan="", review="")
        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertEqual([s.role.value for s in wf.steps], ["execute"])
        self.assertEqual(wf.plan_text, "")
        self.assertIsNone(wf.verdict)
        self.assertEqual((self.repo / "greeting.txt").read_text().strip(), "hello")

    def test_a_run_without_a_reviewer_still_stops_at_the_gate(self):
        wf = self.make(Mode.SUPERVISED, review="")
        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.AWAITING_APPROVAL, msg=wf.message)
        self.assertFalse((self.repo / "greeting.txt").exists())
        self.assertIn("greeting.txt", gitops.diffstat(self.repo, "main", wf.branch))

        self.runner.approve(wf)
        self.runner._threads[wf.id].join(timeout=60)

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertTrue((self.repo / "greeting.txt").exists())

    def test_supervised_workflow_does_not_touch_main_until_approved(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.AWAITING_APPROVAL, msg=wf.message)
        self.assertFalse((self.repo / "greeting.txt").exists())
        self.assertIn("greeting.txt", gitops.diffstat(self.repo, "main", wf.branch))

        self.runner.approve(wf)
        self.runner._threads[wf.id].join(timeout=60)

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertTrue((self.repo / "greeting.txt").exists())

    def test_rejecting_leaves_main_untouched_but_keeps_the_branch(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        self.runner.reject(wf, "not now")

        self.assertIs(wf.status, Status.CHANGES_REQUESTED)
        self.assertFalse((self.repo / "greeting.txt").exists())
        self.assertTrue(gitops.branch_exists(self.repo, wf.branch))

    def test_branch_follows_conventional_naming(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())

        self.assertTrue(wf.branch.startswith("feature/"), wf.branch)
        self.assertNotIn("maestro", wf.branch)
        self.assertNotIn(wf.id, wf.branch)          # no random suffix
        self.assertLessEqual(len(wf.branch.split("/")[1]), 40)
        self.assertTrue(gitops.branch_exists(self.repo, wf.branch))

    def test_two_workflows_from_one_prompt_get_distinct_branches(self):
        first = self.make(Mode.SUPERVISED)
        second = self.make(Mode.SUPERVISED)
        self.runner._run(first, threading.Event())
        self.runner._run(second, threading.Event())

        self.assertIs(first.status, Status.AWAITING_APPROVAL, msg=first.message)
        self.assertIs(second.status, Status.AWAITING_APPROVAL, msg=second.message)
        self.assertNotEqual(first.branch, second.branch)
        self.assertTrue(second.branch.endswith("-2"), second.branch)

    def test_a_user_chosen_branch_name_is_used_verbatim(self):
        wf = self.make(Mode.SUPERVISED)
        wf.branch = "fix/login-redirect"
        self.runner._run(wf, threading.Event())

        self.assertEqual(wf.branch, "fix/login-redirect")
        self.assertTrue(gitops.branch_exists(self.repo, "fix/login-redirect"))

    def test_two_workflows_run_in_isolated_worktrees(self):
        first = self.make(Mode.SUPERVISED)
        second = self.make(Mode.SUPERVISED)
        self.runner._run(first, threading.Event())
        self.runner._run(second, threading.Event())

        self.assertIs(first.status, Status.AWAITING_APPROVAL, msg=first.message)
        self.assertIs(second.status, Status.AWAITING_APPROVAL, msg=second.message)
        self.assertNotEqual(first.worktree, second.worktree)
        self.assertNotEqual(first.branch, second.branch)

    def test_a_retry_after_worktree_cleanup_gets_its_directory_back(self):
        """Cleanup then retry must not run an agent with no working directory."""
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        self.assertIs(wf.status, Status.AWAITING_APPROVAL, msg=wf.message)

        branch, worktree = wf.branch, wf.worktree
        gitops.remove_worktree(self.repo, worktree)
        wf.worktree = ""
        self.assertFalse(Path(worktree).exists())

        self.runner._reattach_worktree(wf)

        self.assertTrue(wf.worktree, "worktree was not restored")
        self.assertTrue(Path(wf.worktree).exists())
        self.assertEqual(
            subprocess.run(["git", "-C", wf.worktree, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True).stdout.strip(),
            branch,
        )

    def test_reattaching_a_live_worktree_changes_nothing(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        original = wf.worktree
        self.runner._reattach_worktree(wf)
        self.assertEqual(wf.worktree, original)

    def use_checklist_agent(self, stall_once=False):
        """Swap in the agent that plans a checklist and works item by item."""
        script = self.tmp / "checklist_agent.py"
        script.write_text(CHECKLIST_AGENT, encoding="utf-8")
        os.environ["AGENT_STATE"] = str(self.tmp / "calls.txt")
        if stall_once:
            os.environ["STALL_ONCE"] = "1"
        else:
            os.environ.pop("STALL_ONCE", None)
        self.addCleanup(lambda: os.environ.pop("AGENT_STATE", None))
        self.addCleanup(lambda: os.environ.pop("STALL_ONCE", None))
        config.save_backends(
            {"fake": {"bin": sys.executable, "args": [str(script), "{prompt}"],
                      "timeout": 60}}
        )

    def test_a_checklist_is_implemented_one_commit_per_item(self):
        self.use_checklist_agent()
        wf = self.make(Mode.AUTONOMOUS)
        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertEqual(len(wf.tasks), 2)
        self.assertEqual(wf.task_progress, "2/2")

        # Both files landed, each by its own commit.
        self.assertTrue((self.repo / "alpha.txt").exists())
        self.assertTrue((self.repo / "beta.txt").exists())
        log = self._git("log", "--oneline")
        self.assertIn("add alpha", log)
        self.assertIn("add beta", log)

    def test_an_agent_that_stalls_is_resumed_and_still_finishes(self):
        """The reported failure: exits 0, commits nothing, run dies."""
        self.use_checklist_agent(stall_once=True)
        wf = self.make(Mode.AUTONOMOUS)
        wf.max_attempts = 3
        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertEqual(wf.tasks[0].attempts, 2)  # stalled once, then did it
        self.assertTrue((self.repo / "alpha.txt").exists())
        self.assertTrue((self.repo / "beta.txt").exists())

    def test_a_permanently_stalled_agent_fails_with_a_reason(self):
        self.use_checklist_agent()
        wf = self.make(Mode.AUTONOMOUS)
        wf.max_attempts = 2
        # An agent that never commits anything at all.
        script = self.tmp / "idle_agent.py"
        script.write_text(
            "import sys\n"
            "p = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()\n"
            "print('TASKS:\\n1. Do the thing.') if 'PLANNING' in p else "
            "print('I only looked around.')\n",
            encoding="utf-8",
        )
        config.save_backends(
            {"fake": {"bin": sys.executable, "args": [str(script), "{prompt}"],
                      "timeout": 60}}
        )

        self.runner._run(wf, threading.Event())

        self.assertIs(wf.status, Status.FAILED)
        self.assertIn("2 attempt", wf.message)
        self.assertIn("committed nothing", wf.message)
        self.assertIs(wf.tasks[0].status, Status.FAILED)

    def test_an_interrupted_run_resumes_without_redoing_committed_work(self):
        """Maestro was closed mid-run; resume finishes the job."""
        self.use_checklist_agent()
        wf = self.make(Mode.AUTONOMOUS)

        # Simulate dying after the first checklist item landed.
        wf.plan_text = "Plan: create two files.\n\nTASKS:\n1. Create alpha.txt containing alpha.\n2. Create beta.txt containing beta.\n"
        from maestro.runner import parse_tasks
        wf.tasks = parse_tasks(wf.plan_text)
        self.runner._prepare_workspace(wf)
        wf.tasks[0].status = Status.DONE
        subprocess.run(["git", "-C", wf.worktree, "commit", "--allow-empty",
                        "-m", "add alpha"], check=True, capture_output=True)
        wf.status = Status.FAILED
        wf.message = "interrupted - maestro exited while this was running"
        self.store.add(wf)

        self.runner.resume(wf)
        self.runner._threads[wf.id].join(timeout=120)

        self.assertIs(wf.status, Status.MERGED, msg=wf.message)
        self.assertEqual(wf.task_progress, "2/2")
        # Item 1 was already done, so only beta.txt gets written now.
        self.assertTrue((self.repo / "beta.txt").exists())
        self.assertFalse((self.repo / "alpha.txt").exists())

    def test_deleting_a_run_leaves_its_branch_intact(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        branch = wf.branch

        self.store.remove(wf.id)

        self.assertIsNone(self.store.get(wf.id))
        self.assertTrue(gitops.branch_exists(self.repo, branch))

    def test_validate_target_rejects_a_dirty_repository(self):
        (self.repo / "scratch.txt").write_text("uncommitted", encoding="utf-8")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.validate_target(str(self.repo), "main")
        self.assertIn("uncommitted changes", str(ctx.exception))

    def test_validate_target_rejects_an_unknown_base_branch(self):
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.validate_target(str(self.repo), "nope")
        self.assertIn("does not exist", str(ctx.exception))

    def test_validate_target_rejects_a_non_repository(self):
        with self.assertRaises(gitops.GitError):
            gitops.validate_target(str(self.tmp / "home"), "main")


if __name__ == "__main__":
    unittest.main()
