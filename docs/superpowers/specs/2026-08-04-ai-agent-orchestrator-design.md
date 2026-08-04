# AI Agent Orchestrator — Design

Date: 2026-08-04

## Purpose

A terminal script with an interactive menu that builds and runs agent workflows.
The user supplies a prompt, picks which AI CLI plans, which executes, and which
reviews and merges, then watches the runs and their statuses from the menu. The
same CLI may fill more than one role.

Supported backends: `claude` (Claude Code), `codex`, `opencode`.

## Decisions

| Question | Decision |
| --- | --- |
| How agents are driven | Headless one-shot: each step is a non-interactive CLI invocation whose stdout is captured and chained into the next step |
| What execute works on | A branch in a real target repo; review reads the diff; merge is a real `git merge` |
| Menu behaviour during a run | Runs in a background thread; the menu stays usable; state persisted to JSON so runs survive a restart |
| Merge gate | Per-workflow mode: `supervised` (human confirms the merge) or `autonomous` (AI approval merges directly) |
| Reviewer requests changes | Feed the review back to the execute agent and retry, up to a configurable number of rounds (default 2) |

## Architecture

```
orchestrator.py                 entry point: python orchestrator.py
agent_orchestrator/
  backends.py    AgentBackend: build argv for a role, run it, capture output
  config.py      load/save backends.json + defaults; discover installed CLIs
  models.py      Workflow, Step, status enums as dataclasses with JSON round-trip
  store.py       persistence: state.json + logs/, guarded by a lock
  gitops.py      branch / worktree / commit / diff / merge over subprocess git
  runner.py      the state machine, executed in a background thread
  menu.py        all terminal I/O
tests/           unittest, stdlib only
```

Two invariants keep the modules separable:

- `menu.py` is the only module that prints or reads input.
- `runner.py` is the only module that mutates workflow state.

No third-party dependencies. Python 3.11+.

## Backend adapters

A backend is a named CLI plus a per-role command template, stored in
`~/.ai-agent-orchestrator/backends.json` so flags can be corrected without
editing code:

```json
{
  "claude":   { "bin": "claude", "prompt_via": "stdin", "args": ["-p"],
                "execute_args": ["-p", "--permission-mode", "acceptEdits"] },
  "codex":    { "bin": "codex", "prompt_via": "stdin",
                "args": ["exec", "-C", "{cwd}", "-s", "read-only"],
                "execute_args": ["exec", "-C", "{cwd}", "-s", "workspace-write"],
                "search_globs": ["%LOCALAPPDATA%/Programs/OpenAI/Codex/bin/codex.exe"] },
  "opencode": { "bin": "opencode", "prompt_via": "stdin",
                "args": ["run", "--dir", "{cwd}"] }
}
```

Plan and review steps use `args`; execute uses `execute_args`, falling back to
`args` when absent. Every invocation runs under a configurable timeout, with
stdout and stderr streamed line by line into its own log file under
`logs/<workflow-id>/`, named for the role, round, checklist item and attempt.
Streaming rather than buffering is what lets a running step be followed live;
naming per item is what stops concurrent checklist steps overwriting one
another.

A timeout kills the process but does not by itself fail an execute step.
Progress is measured in commits: work that was committed before the process
overran is kept and the overrun recorded as a warning. Planning and review
commit nothing, so a timeout there ends the run.

The prompt travels on stdin rather than as an argument. A command-line argument
cannot carry newlines through a Windows batch wrapper such as `opencode.cmd`:
cmd.exe truncates it at the first line break, so the agent receives only the
first line of a fifteen-line instruction and answers as though it were given no
task at all. Passing the prompt as `input` also keeps the child on a pipe
rather than the terminal, so a background agent cannot consume the keystrokes
meant for the menu.

`{cwd}` expands to the workflow's worktree. It is passed explicitly because not
every CLI honours the process working directory -- opencode roots itself at its
own project detection and will otherwise edit an unrelated repository.

Config files written by earlier versions are upgraded on load: a built-in
backend whose templates still contain `{prompt}` has those templates replaced
by the current defaults and the file is rewritten. Custom backends and other
user edits are preserved.

Binaries are located by PATH first, then by each backend's optional
`search_globs`, newest match first. This covers a CLI that is installed but
absent from PATH -- notably Codex on Windows, whose installer uses a directory
named after the version. Backends that cannot be located anywhere are still
listed in the picker, marked *(not installed)*, rather than hidden.

## Workflow model

Creation asks, in order: the task, target repo path, base branch, branch type
and name, plan agent, execute agent, review agent, mode (`supervised` or
`autonomous`), maximum rework rounds, and attempts per step.

The task is either typed at the prompt or read from a file, which suits long
descriptions and specs that already live in the repo. The file is previewed
with its size before use; a missing file, a directory, an empty file or
non-UTF-8 content is refused with a readable message. The source path is kept
on the workflow.

Status machine:

```
pending -> planning -> executing -> reviewing
                          ^            |
                          |            v  REQUEST_CHANGES, rounds remaining
                          +------------+
                                       |  APPROVE
                                       v
      supervised: awaiting_approval --(user confirms)--> merging -> merged
      autonomous: merging -> merged
                                       |  rounds exhausted
                                       v
                              changes_requested / failed
```

Each step records the agent used, status, start and end times, exit code, log
path, and round number. Nothing is deleted: a failed run keeps its branch and
its logs.

## Prompt chaining

- **Plan** receives the original prompt plus repo context and returns an
  implementation plan ending in a numbered checklist under a `TASKS:` heading.
  Only that list is parsed into tasks; a plan without one falls back to a
  single execution.
- **Execute** runs once per checklist item, receiving the original prompt, the
  plan, the checklist with completed items ticked, and the one item to do. The
  runner also commits any remaining dirty files itself, so work is not lost
  when the agent forgets to commit.
- **Continuation.** After each invocation the runner compares the branch's
  commit count against the count before. No new commits means the work was not
  done -- typically the agent ran out of turns mid-exploration -- so it is
  re-invoked with the worktree's `git status` and commit log and asked to
  finish, up to `max_attempts` times. Re-invoking is preferred over resuming a
  CLI session: the worktree is the real state, and session-resume flags select
  the *most recent* session, which is wrong when workflows run concurrently.
- **Review** receives the original prompt, the plan, and `git diff base...branch`.
  It must end with a line reading exactly `VERDICT: APPROVE` or
  `VERDICT: REQUEST_CHANGES`, followed by its reasoning.
- **Rework** re-runs execute with the review feedback appended, then re-reviews.

Verdict parsing is a strict regex over the last matching line. Anything
unparseable counts as REQUEST_CHANGES, so an ambiguous reviewer never causes an
automatic merge.

## Git integration

`gitops` verifies up front that the target is a git repository with a clean
working tree. Each workflow gets a branch `aiorch/<slug>-<shortid>` created from
the base branch, checked out into its own `git worktree` under
`~/.ai-agent-orchestrator/worktrees/<id>` so concurrent runs cannot collide.

Merge is `git merge --no-ff` onto the base branch in the main checkout. A
conflict sends the run to `failed` and leaves the conflict in place; the
orchestrator never forces a resolution. Worktree cleanup is offered after a
merge, never performed automatically.

## Menu

```
AI Agent Orchestrator
  1. New workflow
  2. List workflows          id, prompt snippet, mode, status, current step, elapsed
  3. View workflow           numbered picker; per-step detail, verdict, logs
  4. Tail a step log         live follow, Ctrl-C returns to the menu
  5. Approve / reject merge  lists runs awaiting approval; shows the diffstat first
  6. Manage a run            resume, cancel, logs, worktree, delete
  7. Configure backends
  8. History                 clear merged, finished, or all runs
  0. Exit
```

Runs persist until deleted, so their logs and results stay readable. Resuming
continues an unfinished run from where it stopped: committed checklist items
are kept, a stalled one is requeued with its attempts reset, and no rework
round is consumed. Planning is skipped whenever a plan already exists, since
replanning would discard the checklist and redo committed work. Clearing the
history never touches git branches, and never removes a run still in progress.

Plain numbered input via `input()`. No curses, so it works in any Windows
terminal. The list view refreshes whenever the user returns to the menu; nothing
repaints in the background while the user is typing.

## Error handling

Every failure surfaces as a status and a message rather than a traceback: CLI
not found, non-zero exit, timeout, dirty repository, merge conflict, unparseable
verdict. Ctrl-C at the menu exits cleanly; because state lives on disk,
interrupted runs are visible on the next launch.

## Testing

A stdlib `unittest` suite covers verdict parsing, state machine transitions,
argv construction from templates, and JSON round-tripping. Git and subprocess
calls are faked, so the suite runs without any AI CLI installed.
