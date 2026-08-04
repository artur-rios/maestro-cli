# AI Agent Orchestrator

A prompt-driven shell for building agent workflows. You type a task; it decides
which AI CLI **plans**, which **executes**, and which **reviews and merges**,
then runs the whole thing on a branch in the background. The same CLI can fill
all three roles.

```bash
python orchestrator.py
```

```
    ___    ____        ____             __
   /   |  /  _/       / __ \_________  / /_
  / /| |  / /        / / / / ___/ ___/ __ \
 / ___ |_/ /        / /_/ / /  / /__/ / / /
/_/  |_/___/        \____/_/   \___/_/ /_/

  AI AGENT ORCHESTRATOR  v1.0.0

  claude  codex  opencode   repo identity-manager-api
  3 runs  1 awaiting approval  /help for commands

> add rate limiting to the scope endpoints
  feature/rate-limiting-scope-endpoints
  claude -> opencode -> codex  |  supervised  |  2 rounds, 3 attempts/step
  Enter to run, c to change, n to cancel:

  started 7f2a1c

[7f2a1c] 2/4 tasks done
>
```

Anything that is not a slash command **is the task**. Everything else -- runs,
logs, approvals, settings -- is a command. Runs continue in the background, and
a short notice appears above your next prompt when one moves on.

No dependencies beyond Python 3.10+ and `git`.

## Commands

| | |
| --- | --- |
| `<task>` | start a workflow from what you type |
| `@name [extra words]` | start one from a saved prompt file |
| `/runs` `/show <id>` | what is happening, and one run in full |
| `/logs <id> [step]` `/watch [id]` | its logs; follow a running one live |
| `/pending` `/approve <id>` `/reject <id> [why]` | the merge gate |
| `/resume <id>` `/retry <id>` `/cancel <id>` | keep a run going, or stop it |
| `/rm <id>` `/clear merged\|finished\|all` | delete runs |
| `/prompts [add\|rm <path>]` | saved prompts and where to look for them |
| `/backends` `/agents` `/model` | which CLI, which bundled agent, which model |
| `/set <key> <value>` `/config` | defaults for new runs |
| `/help` `/exit` | |

Ids match on any unique prefix, and a command that takes one will assume the
obvious run when there is only a single candidate -- `/approve` alone works
when exactly one run is waiting.

## Choosing what runs

Every run is summarised before it starts, and `c` opens it up. Each question
offers the current value; pressing Enter keeps it:

```
> add rate limiting to the scope endpoints
  feature/rate-limiting-scope-endpoints
  claude -> opencode -> codex  |  supervised  |  2 rounds, 3 attempts/step
  Enter to run, c to change, n to cancel: c

  Enter keeps the value shown.
  available: claude, codex, opencode
  plan agent [claude]:
  execute agent [opencode]: claude
  review agent [codex]:
  mode (supervised/autonomous) [supervised]: auto
  rework rounds [2]: 4
  attempts per step [3]:
  branch [feature/rate-limiting-scope-endpoints]:

  Save these as your defaults? [y/N]
```

A value that will not do -- an agent that is not installed, a round count of
zero -- is explained and asked again rather than accepted. The summary then
reappears with the changes, so nothing starts until you have seen the final
shape of it.

Answering yes to the last question writes those choices to your defaults, so
the next run starts from them. Answering no uses them for this run only.

`/set confirm off` skips the whole exchange and runs immediately on defaults.

## Tuning a single run

Flags after the task skip straight past the questions for that value, apply to
that run only, and never change your defaults:

```
fix the flaky checkout test --execute codex --mode auto
@scope-spec --repo ../other-api --branch fix/scope-lookup --attempts 5
```

`--plan --execute --review --mode --repo --base --branch --rounds --attempts`,
written as `--flag value` or `--flag=value`. An unrecognised flag is refused
rather than absorbed into the task, so a typo cannot silently end up in a
prompt.

Persistent defaults live in `/config` and are changed with `/set`:

```
/set mode autonomous      /set execute opencode      /set attempts 5
/set repo D:\code\api     /set confirm off
```

## Saved prompts

A task is often written down before it is run. `/prompts` lists what it finds,
grouped by where it came from:

```
> /prompts
  current directory
    @README  README.md
  docs/prompts
    @scope-permissions  docs/prompts/scope-permissions.md
    @rate-limiting      docs/prompts/rate-limiting.md
```

Start one with `@scope-permissions`, or any unique prefix of it -- `@scope` is
enough unless it is ambiguous, in which case the candidates are listed rather
than guessed at. `@docs/prompts/spec.md` loads a path directly. Words typed
after the reference are appended as extra instruction:

```
> @scope-permissions and add integration tests for the new endpoints
```

Three roots are searched, in order: the **current directory** (flat -- a repo
root is too noisy to walk), **`./docs/prompts`** (recursive), and any root you
add with `/prompts add <path>` (recursive). Files may be `.md`, `.txt` or
`.markdown`. A missing file, a directory, an empty file, or something that is
not UTF-8 text is refused with a plain explanation, and the path is recorded on
the workflow so `/show` says where the task came from.

## How a workflow runs

```
prompt -> plan -> execute -> review -> merge
                    ^          |
                    +----------+  rework, up to N rounds
```

1. A branch is created from your base branch and checked out into a private
   `git worktree`, so parallel runs never collide.
2. The **plan** agent reads the repo, writes an implementation plan, and ends
   it with a numbered checklist of independently committable steps.
3. The **execute** agent implements the checklist **one item at a time**, each
   with its own commit. If an item ends without a commit, the agent is resumed
   and asked to finish it.
4. The **review** agent reads `git diff base...branch` and answers
   `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`.
5. On rejection, the feedback goes back to the execute agent and the cycle
   repeats until the round limit runs out.
6. On approval, the branch is merged with `git merge --no-ff`.

### One item at a time, and finishing what was started

A single headless invocation gets one shot at the whole job. Asked to implement
something use-case sized, an agent will often explore the code, run out of
turns, and exit cleanly having written nothing -- the run then fails with
everything lost. Two things address that.

**Decomposition.** The plan agent ends with a checklist:

```
TASKS:
1. Add the ScopeCustomField entity and its EF configuration.
2. Add repository methods for reading and writing custom fields.
3. Expose the endpoints on ScopeController and cover them with tests.
```

Each item gets its own invocation, its own commit, and its own line in
`/show`. An invocation faces one small job rather than the entire feature, work
that lands is kept even if a later item fails, and a retry resumes at the first
unfinished item instead of starting over. A plan without a parseable checklist
simply falls back to a single invocation, so nothing breaks if an agent ignores
the format.

**Continuation.** After each invocation the orchestrator counts commits. If the
count did not move, the work is not done: the agent is pointed back at the
worktree -- with its `git status` and the commits so far -- and asked to finish,
up to *attempts per step* times (default 3).

Commits are the measure, not the exit code. An agent that commits its work and
then overruns its timeout has still done the job, so the work is kept, the step
counts as done, and the overrun is recorded as a warning rather than failing
the run. A timeout with nothing committed simply spends an attempt like any
other unproductive one. Planning and review are different -- they commit
nothing, so a timeout there is terminal.

The default timeout is 90 minutes per invocation, because one checklist item
against a large codebase can genuinely take over half an hour. Lower it per
backend in `backends.json` if you would rather fail fast.

Continuation deliberately re-invokes rather than resuming a saved CLI session.
The worktree is the state that matters, and re-reading it works identically
across all three CLIs and stays correct when several workflows run at once,
which `--continue`-style "most recent session" flags do not.

### Branch names

Branches follow conventional naming, with the type inferred from the wording of
the task -- `feature`, `fix`, `chore`, `refactor`, `docs` or `test`. Filler
words are dropped so the description stays short and readable:

```
Create a new Use Case in which a scope owner can add custom fields to a scope
  -> feature/use-case-scope-owner

Fix the broken login redirect that crashes on submit
  -> fix/broken-login-redirect-crashes
```

Filler words and the leading verb are dropped -- the prefix already says the
change adds something -- and repeated words are collapsed.

`--branch` overrides it for one run. A bare name keeps the inferred type
(`--branch scope-lookup` -> `feature/scope-lookup`); a name with a prefix wins
outright (`--branch fix/scope-lookup`). `/set branch_type fix` changes the
default.

There is no random suffix. A numeric one (`-2`, `-3`) is appended only when the
name is genuinely already taken, such as two workflows started from the same
prompt.

### Two modes

| Mode | Behaviour |
| --- | --- |
| **Supervised** | The run stops at `awaiting_approval`. You see the verdict and diffstat, and confirm the merge yourself. |
| **Autonomous** | The reviewer's approval merges straight into the base branch, with no human step. |

A reviewer whose output cannot be parsed counts as *request changes*, so an
ambiguous review never merges on its own.

### Reading logs

`/logs <id>` shows every step in one view; add `plan`, `execute` or `review`
to narrow it, and `/watch` follows a running one. Each step is bracketed by a marker naming the agent, the round and how it
ended, so it is always clear where one step stops and the next begins:

```
------------ plan | claude | round 1 | started ------------
1. Add ScopeCustomField entity
------------ plan finished | exit 0 | 18s ------------

------------ execute | opencode | round 1 | started ------------
Wrote src/Domain/ScopeCustomField.cs
------------ execute finished | exit 0 | 1m32s ------------

  -- end of log for all steps --
  workflow 60de1425 is merged
```

Output is written to the log line by line as the agent produces it, not
buffered until the step ends, so following a log shows the agent working in
real time. Every invocation gets its own file -- `plan-round1.log`,
`execute-round1-task2.log`, `execute-round1-task2-try2.log` -- so nothing
overwrites anything else.

A run still in progress is followed live and picks up each new step as it
starts. Ctrl-C stops following without stopping the run: agents are started in
their own process group precisely so that this holds. On Windows a console
Ctrl-C reaches every process attached to that console, so without it, pressing
Ctrl-C to leave the log would kill the running agent -- it dies with
`0xC000013A` and the run fails through no fault of the agent.

Either way the view ends with an explicit marker naming the run's final state.

### Why a run failed

A failure names the agent, the step and what the agent actually said, rather
than only the symptom:

```
the opencode execute step exited cleanly but changed nothing - it likely
explored the code and stopped, or ran out of turns. Its last words: ...
```

`/show` on a failed run also prints the tail of the failing step's log
without being asked.

## Backends

Three are configured out of the box: `claude`, `codex` and `opencode`. Each is
invoked headlessly, once per step, with its output captured to a log.

Command templates live in `~/.ai-agent-orchestrator/backends.json` so you can
correct a flag without touching any code:

```json
{
  "claude": {
    "bin": "claude",
    "prompt_via": "stdin",
    "args": ["-p"],
    "execute_args": ["-p", "--permission-mode", "acceptEdits"],
    "timeout": 1800
  },
  "codex": {
    "bin": "codex",
    "prompt_via": "stdin",
    "args": ["exec", "-C", "{cwd}", "-s", "read-only"],
    "execute_args": ["exec", "-C", "{cwd}", "-s", "workspace-write"],
    "search_globs": ["%LOCALAPPDATA%/Programs/OpenAI/Codex/bin/codex.exe"]
  },
  "opencode": {
    "bin": "opencode",
    "prompt_via": "stdin",
    "args": ["run", "--dir", "{cwd}"],
    "execute_args": ["run", "--dir", "{cwd}"]
  }
}
```

`args` is used for the read-only plan and review steps; `execute_args` is used
for the step that edits files, and falls back to `args` when absent. Codex's
sandbox flags are what separate its read-only roles from the one allowed to
write. Add your own CLI by adding another entry to the file.

### Why the prompt goes on stdin

`prompt_via: stdin` is not a stylistic choice. A prompt is many lines long, and
a command-line argument cannot carry newlines through a Windows batch wrapper
such as `opencode.cmd` -- cmd.exe truncates the argument at the first line
break, so the agent receives only the opening line and replies with something
like *"Ready. What task should I execute?"*. Sending the prompt on stdin avoids
the command line entirely. The child still gets a pipe rather than the
terminal, so it cannot steal the shell's keystrokes.

A custom backend may still use `{prompt}` in its `args` if its CLI has no stdin
mode; omit `prompt_via` to get that behaviour.

### Why `{cwd}` is passed explicitly

`{cwd}` expands to the workflow's worktree. Not every CLI honours the process
working directory -- opencode roots itself at its own project detection and
will happily edit an unrelated repository -- so the directory is named on the
command line rather than assumed.

A config file written by an older version is upgraded on load: built-in
backends whose templates still carry `{prompt}` have those templates replaced
with the current ones, and the file is rewritten. Custom backends and any other
settings you changed are left alone.

### Choosing an agent or a model

`/agents <backend> [value] [execute|read]` and `/model <backend> [value]
[execute|read]` set both, separately for the read-only roles and the
implementation step. With no value they list what the CLI reports.

**Agent.** Some CLIs bundle several agents with different permissions, and
which one runs decides whether a step can edit files at all. opencode ships a
read-only `plan` and a writing `build`, so it reads with `plan` and implements
with `build`. `/agents opencode` lists whatever agents the CLI actually reports.

**Model.** Where the CLI can enumerate models they are listed (opencode reports
42); otherwise name one directly. A partial name is matched, so
`/model opencode glm-5.2 execute` is enough. Leaving it unset uses the CLI's
own default.

Both end up as ordinary flags, so the result stays inspectable:

```
run --dir {cwd} --agent build --model opencode-go/glm-5.2
```

### Finding a binary that is not on PATH

`search_globs` is a fallback for a CLI that is installed but unreachable. PATH
is tried first; if that fails, each glob is expanded (`~` and environment
variables included) and the newest match wins. This matters on Windows, where
the Codex installer drops `codex.exe` into a directory named after the version
and does not always add it to PATH -- the newest-match rule keeps working after
the tool updates itself into a new directory.

Backends whose binary cannot be located anywhere are still listed in the
picker, marked *not installed*.

Agents are run with stdin detached. They execute in background threads while
the shell owns the terminal, so an agent that read stdin would swallow the
keystrokes meant for the prompt.

## State on disk

Everything lives under `~/.ai-agent-orchestrator/` (override with `AIORCH_HOME`):

```
state.json                     every workflow and its steps
backends.json                  the command templates above
logs/<workflow-id>/            plan-round1.log, execute-round1.log, ...
worktrees/<workflow-id>/       the isolated checkout for that run
```

State is written after every transition, so runs remain visible after you quit.
A run interrupted by closing the orchestrator is reported as failed on the next
launch — its thread died with the process — but its branch and logs are kept.

## Safety

- The target repo must be a clean git checkout before a workflow starts.
- Nothing is merged in supervised mode without your explicit confirmation.
- A merge conflict is aborted rather than left in the working tree, since other
  runs share that checkout. The run fails with the command to merge by hand.
- Cancelling stops at the next step boundary; an agent already running is left
  to finish rather than killed mid-edit.
- Worktrees are only removed when you ask, and a retry after cleanup recreates
  the worktree rather than running an agent with no working directory.

## Layout

```
orchestrator.py            entry point
agent_orchestrator/
  models.py                workflows, steps, tasks, statuses
  config.py                paths and backend definitions
  settings.py              your defaults for new runs
  store.py                 JSON persistence
  backends.py              argv building and subprocess invocation
  gitops.py                branches, worktrees, diffs, merges
  prompts.py               what each agent is told
  runner.py                the state machine
  shell/
    repl.py                the prompt loop, banner, notices
    parser.py              a typed line -> an intent (pure, no I/O)
    commands.py            the command table and handlers
    promptlib.py           finding and loading saved prompts
    logview.py             streaming and following step logs
    render.py              colours, tables, run detail
tests/
```

The `shell` package is the only place that reads or prints; `runner.py` is the
only one that mutates workflow state. `parser.py` has no I/O at all, which is
what makes the fiddliest part -- telling a task from a command, and a flag from
prose -- cheap to test.

## Tests

```bash
python -m unittest discover -s tests -t .
```

335 tests. The unit tests fake git and the CLIs; the integration tests drive a
real git repository with a stub agent script, so the suite needs no AI tool
installed.
