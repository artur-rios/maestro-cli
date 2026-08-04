# Prompt-driven shell — Design

Date: 2026-08-04

Supersedes the interactive numbered menu described in
`2026-08-04-ai-agent-orchestrator-design.md`. The orchestration engine is
unchanged; only the interface is replaced.

## Purpose

Drive the orchestrator from a persistent prompt rather than a numbered menu.
Typing a task starts a workflow; everything else is a slash command. Saved
prompt files can be browsed or referenced by name.

## Decisions

| Question | Decision |
| --- | --- |
| Interface shape | REPL shell with slash commands, stdlib only, no curses |
| Starting a run | Defaults resolve everything; a summary; Enter runs, `c` opens each setting for editing, `n` cancels |
| Per-run tuning | Inline flags after the task, which never change the defaults |
| Saved prompts | `/prompts` to browse, `@name` to reference |
| Background runs | Inline notices printed above the next prompt |
| The old menu | Deleted; every function has a command equivalent |

## Session

```
> add rate limiting to the scope endpoints
  feature/rate-limiting-scope-endpoints
  claude -> opencode -> codex | supervised | 2 rounds, 3 attempts
  Enter to run, c to change, n to cancel:

  started 7f2a1c

[7f2a1c] 2/4 tasks done
>
```

Anything not beginning with `/` is the task.

## Configuring a run

The summary is shown first. `c` asks for each setting in turn -- the agent for
each of the three roles, the mode, the rework rounds, the attempts per step,
and the branch -- with the current value offered as the default, so Enter keeps
it. A rejected value (an uninstalled agent, a count below one) is explained and
asked again; nothing is accepted that would fail later. The summary then
reappears, so a run never starts on settings the user has not seen in final
form.

After editing, the user is offered the chance to save those choices as their
defaults. Declining uses them for this run only.

Inline flags pre-fill the corresponding values, and `confirm off` skips the
exchange entirely.

## Commands

| Command | Does |
| --- | --- |
| `<text>` | start a workflow from what was typed |
| `@name`, `@docs/prompts/spec.md` | start one from a saved prompt file |
| `/prompts` | list discovered prompt files, grouped by source |
| `/prompts add\|rm <path>` | manage configured roots |
| `/runs`, `/show <id>`, `/logs <id>`, `/watch <id>` | inspect |
| `/approve <id>`, `/reject <id>`, `/pending` | the merge gate |
| `/resume <id>`, `/retry <id>`, `/cancel <id>`, `/rm <id>` | manage a run |
| `/clear merged\|finished\|all` | history |
| `/agents`, `/model`, `/backends` | backend, bundled agent, model |
| `/set <key> <value>` | defaults |
| `/config`, `/help`, `/exit` | the rest |

Ids match on any unique prefix. Commands that take an id fall back to the only
plausible run when there is exactly one.

## Inline overrides

`--plan --execute --review --mode --repo --base --branch --rounds --attempts`,
accepted as `--flag value` or `--flag=value`. They apply to one run and are
never written to settings. An unknown flag is an error rather than being
absorbed into the task text, so a typo cannot silently become part of a prompt.

## Prompt library

Roots, in order:

1. the current directory, non-recursive (a repository root is too noisy to walk)
2. `./docs/prompts`, recursive
3. each configured root, recursive

Extensions: `.md`, `.txt`, `.markdown`. Recursive walks stop at depth 3 and skip
dot-directories.

`@name` matches a filename stem, case-insensitively, and accepts a unique
prefix. `@some/path.md` loads that path directly. An ambiguous name lists the
candidates rather than guessing. Text after the reference is appended to the
loaded prompt as extra instruction. Loading goes through the existing file
reader, so the empty, missing, directory and non-UTF-8 checks are unchanged and
the source path is still recorded on the workflow.

## Modules

```
orchestrator.py              entry point
agent_orchestrator/
  settings.py                user defaults, stored beside backends.json
  shell/
    repl.py                  the loop, banner, notices
    parser.py                text -> intent; pure, no I/O
    commands.py              command table and handlers
    promptlib.py             discovery, @ resolution, file loading
    logview.py               step logs, streaming and following
    render.py                colours, tables, run detail
```

The engine modules (`models`, `config`, `store`, `backends`, `gitops`,
`prompts`, `runner`) are untouched. `runner` remains the only module that
mutates workflow state; the shell calls into it. `menu.py` is deleted, its
1114 lines redistributed into the modules above.

## Settings

`settings.json` beside `backends.json` holds the default repo, the three role
agents, mode, rounds, attempts, whether to confirm, the default branch type,
and the extra prompt roots. On first launch the repo defaults to the working
directory and each role to the first installed backend.

Precedence: inline flag, then setting, then built-in default.

## Notices

The shell keeps the last status it showed for each workflow. Before drawing a
prompt it compares against the store and prints one short line per change:
`[7f2a1c] task 2/4 done`, `[7f2a1c] awaiting approval`, `[7f2a1c] merged`.
Nothing prints while the user is typing. `/watch <id>` streams a run's logs
live until Ctrl-C.

## Errors

An unknown command suggests the closest match. A bad id, an ambiguous
reference, an unparseable flag, or a task typed when no backend is installed
each produce one line and return to the prompt, leaving no half-created
workflow. Ctrl-C abandons the current line; Ctrl-D exits.

## Testing

The parser is pure and carries the heaviest coverage: task versus command, flag
forms and validation, `@` references, quoting. The prompt library is tested
against a temporary tree of roots. Command handlers run against a real store
and a fake runner. The 266 existing engine tests are unaffected; the menu tests
are rewritten against the new surface.
