"""End-to-end run against a real git repository.

The AI CLIs are replaced by a small Python script that behaves like an agent:
it reads its prompt, and for the execute role it actually edits and commits a
file. Everything else -- worktrees, diffing, the merge -- is real git.
"""

import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from maestro import config, gitops
from maestro.models import Mode, Status, Verdict, Workflow
from maestro.runner import Runner, parse_tasks
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


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture(scope="session")
def template_repo(tmp_path_factory):
    """A repository with one commit on main, built once and copied per test.

    Six git processes per test add up on Windows, where starting one is slow;
    copying the finished directory gives every test the same fresh repo.
    """
    repo = tmp_path_factory.mktemp("template") / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.mark.skipif(not HAS_GIT, reason="git is not installed")
class TestEndToEnd:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch, template_repo):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.repo = self.tmp / "repo"
        shutil.copytree(template_repo, self.repo)

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
        yield
        for thread in list(self.runner._threads.values()):
            thread.join(timeout=60)

    def _git(self, *args):
        return _git(self.repo, *args)

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

    def test_given_an_autonomous_workflow_when_run_then_the_change_lands_on_main(self):
        wf = self.make(Mode.AUTONOMOUS)
        self.runner._run(wf, threading.Event())

        assert wf.status is Status.MERGED, wf.message
        assert wf.verdict is Verdict.APPROVE
        assert "create greeting.txt" in wf.plan_text

        # The file is really on main now, via a real merge commit.
        assert (self.repo / "greeting.txt").read_text().strip() == "hello"
        log = self._git("log", "--oneline", "-3")
        assert "Merge feature/" in log
        assert self._git("rev-parse", "--abbrev-ref", "HEAD") == "main"

        for step in wf.steps:
            assert Path(step.log_path).exists()

    def test_given_only_an_execute_agent_when_run_then_it_lands_the_change_alone(self):
        """Plan and review off: the shortest workflow there is."""
        wf = self.make(Mode.AUTONOMOUS, plan="", review="")
        self.runner._run(wf, threading.Event())

        assert wf.status is Status.MERGED, wf.message
        assert [s.role.value for s in wf.steps] == ["execute"]
        assert wf.plan_text == ""
        assert wf.verdict is None
        assert (self.repo / "greeting.txt").read_text().strip() == "hello"

    @pytest.mark.parametrize("review", ["fake", ""], ids=["reviewed", "no-reviewer"])
    def test_given_a_supervised_run_when_it_finishes_then_main_is_untouched_until_approved(self, review):
        wf = self.make(Mode.SUPERVISED, review=review)
        self.runner._run(wf, threading.Event())

        assert wf.status is Status.AWAITING_APPROVAL, wf.message
        assert not (self.repo / "greeting.txt").exists()
        assert "greeting.txt" in gitops.diffstat(self.repo, "main", wf.branch)

        self.runner.approve(wf)
        self.runner._threads[wf.id].join(timeout=60)

        assert wf.status is Status.MERGED, wf.message
        assert (self.repo / "greeting.txt").exists()

    def test_given_a_run_awaiting_approval_when_rejected_then_main_is_untouched_and_the_branch_kept(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        self.runner.reject(wf, "not now")

        assert wf.status is Status.CHANGES_REQUESTED
        assert not (self.repo / "greeting.txt").exists()
        assert gitops.branch_exists(self.repo, wf.branch)

    def test_given_a_new_run_when_its_branch_is_made_then_it_follows_conventional_naming(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())

        assert wf.branch.startswith("feature/"), wf.branch
        assert "maestro" not in wf.branch
        assert wf.id not in wf.branch          # no random suffix
        assert len(wf.branch.split("/")[1]) <= 40
        assert gitops.branch_exists(self.repo, wf.branch)

    def test_given_two_workflows_from_one_prompt_when_run_then_they_get_distinct_branches_and_worktrees(self):
        first = self.make(Mode.SUPERVISED)
        second = self.make(Mode.SUPERVISED)
        self.runner._run(first, threading.Event())
        self.runner._run(second, threading.Event())

        assert first.status is Status.AWAITING_APPROVAL, first.message
        assert second.status is Status.AWAITING_APPROVAL, second.message
        assert first.branch != second.branch
        assert second.branch.endswith("-2"), second.branch
        assert first.worktree != second.worktree

    def test_given_a_user_chosen_branch_when_run_then_it_is_used_verbatim(self):
        wf = self.make(Mode.SUPERVISED)
        wf.branch = "fix/login-redirect"
        self.runner._run(wf, threading.Event())

        assert wf.branch == "fix/login-redirect"
        assert gitops.branch_exists(self.repo, "fix/login-redirect")

    def test_given_a_cleaned_up_worktree_when_reattached_then_it_gets_its_directory_back(self):
        """Cleanup then retry must not run an agent with no working directory."""
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        assert wf.status is Status.AWAITING_APPROVAL, wf.message

        branch, worktree = wf.branch, wf.worktree
        gitops.remove_worktree(self.repo, worktree)
        wf.worktree = ""
        assert not Path(worktree).exists()

        self.runner._reattach_worktree(wf)

        assert wf.worktree, "worktree was not restored"
        assert Path(wf.worktree).exists()
        assert _git(wf.worktree, "rev-parse", "--abbrev-ref", "HEAD") == branch

    def test_given_a_live_worktree_when_reattached_then_nothing_changes(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        original = wf.worktree
        self.runner._reattach_worktree(wf)
        assert wf.worktree == original

    def use_checklist_agent(self, stall_once=False):
        """Swap in the agent that plans a checklist and works item by item."""
        script = self.tmp / "checklist_agent.py"
        script.write_text(CHECKLIST_AGENT, encoding="utf-8")
        self.monkeypatch.setenv("AGENT_STATE", str(self.tmp / "calls.txt"))
        if stall_once:
            self.monkeypatch.setenv("STALL_ONCE", "1")
        else:
            self.monkeypatch.delenv("STALL_ONCE", raising=False)
        config.save_backends(
            {"fake": {"bin": sys.executable, "args": [str(script), "{prompt}"],
                      "timeout": 60}}
        )

    def test_given_a_checklist_when_run_then_each_item_lands_in_its_own_commit(self):
        self.use_checklist_agent()
        wf = self.make(Mode.AUTONOMOUS)
        self.runner._run(wf, threading.Event())

        assert wf.status is Status.MERGED, wf.message
        assert len(wf.tasks) == 2
        assert wf.task_progress == "2/2"

        # Both files landed, each by its own commit.
        assert (self.repo / "alpha.txt").exists()
        assert (self.repo / "beta.txt").exists()
        log = self._git("log", "--oneline")
        assert "add alpha" in log
        assert "add beta" in log

    def test_given_an_agent_that_stalls_once_when_run_then_it_is_resumed_and_finishes(self):
        """The reported failure: exits 0, commits nothing, run dies."""
        self.use_checklist_agent(stall_once=True)
        wf = self.make(Mode.AUTONOMOUS)
        wf.max_attempts = 3
        self.runner._run(wf, threading.Event())

        assert wf.status is Status.MERGED, wf.message
        assert wf.tasks[0].attempts == 2  # stalled once, then did it
        assert (self.repo / "alpha.txt").exists()
        assert (self.repo / "beta.txt").exists()

    def test_given_an_agent_that_never_commits_when_run_then_it_fails_with_a_reason(self):
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

        assert wf.status is Status.FAILED
        assert "2 attempt" in wf.message
        assert "committed nothing" in wf.message
        assert wf.tasks[0].status is Status.FAILED

    def test_given_an_interrupted_run_when_resumed_then_committed_work_is_not_redone(self):
        """Maestro was closed mid-run; resume finishes the job."""
        self.use_checklist_agent()
        wf = self.make(Mode.AUTONOMOUS)

        # Simulate dying after the first checklist item landed.
        wf.plan_text = (
            "Plan: create two files.\n\nTASKS:\n"
            "1. Create alpha.txt containing alpha.\n2. Create beta.txt containing beta.\n"
        )
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

        assert wf.status is Status.MERGED, wf.message
        assert wf.task_progress == "2/2"
        # Item 1 was already done, so only beta.txt gets written now.
        assert (self.repo / "beta.txt").exists()
        assert not (self.repo / "alpha.txt").exists()

    def test_given_a_finished_run_when_deleted_then_its_branch_is_intact(self):
        wf = self.make(Mode.SUPERVISED)
        self.runner._run(wf, threading.Event())
        branch = wf.branch

        self.store.remove(wf.id)

        assert self.store.get(wf.id) is None
        assert gitops.branch_exists(self.repo, branch)

    def test_given_a_dirty_repository_when_validated_then_it_is_rejected(self):
        (self.repo / "scratch.txt").write_text("uncommitted", encoding="utf-8")
        with pytest.raises(gitops.GitError) as ctx:
            gitops.validate_target(str(self.repo), "main")
        assert "uncommitted changes" in str(ctx.value)

    def test_given_an_unknown_base_branch_when_validated_then_it_is_rejected(self):
        with pytest.raises(gitops.GitError) as ctx:
            gitops.validate_target(str(self.repo), "nope")
        assert "does not exist" in str(ctx.value)

    def test_given_a_directory_that_is_not_a_repository_when_validated_then_it_is_rejected(self):
        with pytest.raises(gitops.GitError):
            gitops.validate_target(str(self.tmp / "home"), "main")



@pytest.mark.skipif(not HAS_GIT, reason="git is not installed")
class TestGitOperations:
    """gitops against a real repository, without any agent involved."""

    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path, template_repo):
        self.repo = tmp_path / "repo"
        shutil.copytree(template_repo, self.repo)

    def commit_on(self, branch, name, text, base="main"):
        _git(self.repo, "checkout", "-q", "-b", branch, base)
        (self.repo / name).write_text(text, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", f"add {name}")
        _git(self.repo, "checkout", "-q", "main")

    def test_given_a_subdirectory_when_its_root_is_asked_for_then_the_repository_root_is_returned(self):
        (self.repo / "src").mkdir()
        assert gitops.repo_root(self.repo / "src").resolve() == self.repo.resolve()

    def test_given_a_fresh_repository_when_inspected_then_it_is_clean_on_main(self):
        assert gitops.is_clean(self.repo)
        assert gitops.current_branch(self.repo) == "main"
        assert gitops.branch_exists(self.repo, "main")
        assert not gitops.branch_exists(self.repo, "feature/nope")

    def test_given_a_taken_name_when_made_unique_then_the_next_free_number_is_used(self):
        _git(self.repo, "branch", "feature/x")
        assert gitops.unique_branch(self.repo, "feature/x") == "feature/x-2"

    def test_given_a_worktree_path_already_in_use_when_created_then_it_is_refused(self, monkeypatch):
        dest = config.worktrees_dir() / "wf1"
        dest.mkdir()
        with pytest.raises(gitops.GitError) as ctx:
            gitops.create_worktree(self.repo, "feature/a", "main", "wf1")
        assert "already exists" in str(ctx.value)

    def test_given_a_live_worktree_when_ensured_then_it_is_returned_as_it_is(self):
        made = gitops.create_worktree(self.repo, "feature/a", "main", "wf1")
        assert gitops.ensure_worktree(self.repo, "feature/a", "wf1") == made

    def test_given_a_plain_directory_in_the_worktree_spot_when_ensured_then_it_is_refused(self):
        _git(self.repo, "branch", "feature/a")
        (config.worktrees_dir() / "wf1").mkdir()
        with pytest.raises(gitops.GitError) as ctx:
            gitops.ensure_worktree(self.repo, "feature/a", "wf1")
        assert "is not a worktree" in str(ctx.value)

    def test_given_a_clean_worktree_when_committing_everything_then_no_commit_is_made(self):
        tree = gitops.create_worktree(self.repo, "feature/a", "main", "wf1")
        assert gitops.commit_all(tree, "nothing") is False
        assert gitops.commit_count(self.repo, "main", "feature/a") == 0

    def test_given_leftover_changes_when_committing_everything_then_they_land_in_one_commit(self):
        tree = gitops.create_worktree(self.repo, "feature/a", "main", "wf1")
        (tree / "left.txt").write_text("x", encoding="utf-8")
        assert "left.txt" in gitops.short_status(tree)

        assert gitops.commit_all(tree, "maestro: leftovers") is True
        assert gitops.commit_count(self.repo, "main", "feature/a") == 1
        assert gitops.has_new_commits(self.repo, "main", "feature/a")
        assert "maestro: leftovers" in gitops.recent_commits(self.repo, "main", "feature/a")

    def test_given_an_unknown_branch_when_commits_are_counted_then_it_is_zero(self):
        assert gitops.commit_count(self.repo, "main", "feature/nope") == 0

    def test_given_an_enormous_diff_when_read_then_it_is_truncated_and_says_so(self):
        self.commit_on("feature/big", "big.txt", "line\n" * 500)
        text = gitops.diff(self.repo, "main", "feature/big", max_chars=100)
        assert len(text) < 200
        assert text.endswith("[diff truncated at 100 characters]")
        assert "big.txt" in gitops.diffstat(self.repo, "main", "feature/big")

    def test_given_a_dirty_checkout_when_merging_then_it_is_refused(self):
        self.commit_on("feature/a", "a.txt", "a")
        (self.repo / "README.md").write_text("edited", encoding="utf-8")
        with pytest.raises(gitops.GitError) as ctx:
            gitops.merge(self.repo, "main", "feature/a")
        assert "uncommitted changes" in str(ctx.value)

    def test_given_another_branch_checked_out_when_merging_then_it_is_restored_afterwards(self):
        self.commit_on("feature/a", "a.txt", "a")
        _git(self.repo, "checkout", "-q", "-b", "elsewhere")
        gitops.merge(self.repo, "main", "feature/a")
        assert gitops.current_branch(self.repo) == "elsewhere"
        assert "Merge feature/a" in _git(self.repo, "log", "--oneline", "-1", "main")

    def test_given_conflicting_branches_when_merging_then_it_is_aborted_and_the_checkout_restored(self):
        self.commit_on("feature/a", "same.txt", "from a\n")
        self.commit_on("feature/b", "same.txt", "from b\n")
        gitops.merge(self.repo, "main", "feature/a")
        _git(self.repo, "checkout", "-q", "-b", "elsewhere")

        with pytest.raises(gitops.MergeConflict) as ctx:
            gitops.merge(self.repo, "main", "feature/b")

        assert "conflicts and was aborted" in str(ctx.value)
        assert gitops.current_branch(self.repo) == "elsewhere"
        assert gitops.is_clean(self.repo)
