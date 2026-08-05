"""Command-line entry point.

Bare invocation opens the interactive shell. Every slash command is also a
subcommand here, so the same capabilities are available from a script or a
one-off shell invocation:

    orchestrator                          # the interactive shell
    orchestrator new "add rate limiting" --mode auto
    orchestrator runs
    orchestrator logs 7f2a1c
    orchestrator approve 7f2a1c
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import __version__
from .shell import commands as cmd
from .shell import render
from .shell.commands import CommandError
from .shell.parser import Command, ParseError, parse
from .shell.promptlib import PromptError
from .shell.repl import Shell

CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 96}


def _session(cwd: Path | None = None):
    """A session for one-shot use: no banner, no prompting where avoidable."""
    shell = Shell(cwd=cwd)
    return shell.session


def _run(handler, session, *args: str) -> int:
    """Invoke a shell command handler, turning its errors into exit codes."""
    try:
        handler(session, Command(name=handler.__name__, args=[a for a in args if a]))
    except (CommandError, PromptError) as exc:
        render.error(str(exc))
        return 1
    except KeyboardInterrupt:
        render.warn("cancelled")
        return 130
    return 0


@click.group(context_settings=CONTEXT, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", prog_name="orchestrator")
@click.option("--repo", type=click.Path(), default=None,
              help="Repository to act on. Defaults to the working directory.")
@click.pass_context
def cli(ctx: click.Context, repo: str | None) -> None:
    """Orchestrate AI coding agents: one plans, one executes, one reviews and merges.

    Run without a subcommand to open the interactive shell.
    """
    ctx.ensure_object(dict)
    ctx.obj["cwd"] = Path(repo) if repo else None
    if ctx.invoked_subcommand is None:
        ctx.exit(Shell(cwd=ctx.obj["cwd"]).run())


@cli.command("new", context_settings=CONTEXT)
@click.argument("task", nargs=-1, required=True)
@click.option("--plan", help="Backend that writes the plan.")
@click.option("--execute", help="Backend that implements it.")
@click.option("--review", help="Backend that reviews and merges.")
@click.option("--model", help="Model for every role.")
@click.option("--plan-model", help="Model for the plan step only.")
@click.option("--execute-model", help="Model for the execute step only.")
@click.option("--review-model", help="Model for the review step only.")
@click.option("--mode", type=click.Choice(["supervised", "autonomous", "auto"]),
              help="Whether the merge waits for you.")
@click.option("--base", help="Branch to work from.")
@click.option("--branch", help="Branch to create.")
@click.option("--rounds", type=int, help="Maximum rework rounds.")
@click.option("--attempts", type=int, help="Attempts per step before giving up.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.option("--follow/--no-follow", default=None, help="Show the logs as it runs.")
@click.pass_context
def new(ctx: click.Context, task: tuple[str, ...], yes: bool,
        follow: bool | None, **flags) -> None:
    """Start a workflow. TASK is the instruction, or @name for a saved prompt."""
    session = _session(ctx.obj.get("cwd"))
    if yes:
        session.settings["confirm"] = False
    if follow is not None:
        session.settings["follow"] = follow

    # Rebuild the line the shell parser understands, so one grammar serves both.
    line = " ".join(task)
    for key, value in flags.items():
        if value is not None:
            line += f" --{key.replace('_', '-')} {value}"

    try:
        intent = parse(line)
    except ParseError as exc:
        render.error(str(exc))
        ctx.exit(1)

    try:
        cmd.start_task(session, intent)
    except (CommandError, PromptError) as exc:
        render.error(str(exc))
        ctx.exit(1)
    except KeyboardInterrupt:
        render.warn("cancelled")
        ctx.exit(130)


@cli.command("runs", context_settings=CONTEXT)
@click.pass_context
def runs(ctx: click.Context) -> None:
    """List every run and its status."""
    ctx.exit(_run(cmd.cmd_runs, _session(ctx.obj.get("cwd"))))


@cli.command("show", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def show(ctx: click.Context, run_id: str) -> None:
    """Show one run in full."""
    ctx.exit(_run(cmd.cmd_show, _session(ctx.obj.get("cwd")), run_id))


@cli.command("logs", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.argument("step", required=False, default="",
                type=click.Choice(["", "plan", "execute", "review"]))
@click.pass_context
def logs(ctx: click.Context, run_id: str, step: str) -> None:
    """Show a run's logs, all steps or one."""
    ctx.exit(_run(cmd.cmd_logs, _session(ctx.obj.get("cwd")), run_id, step))


@cli.command("watch", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.option("--progress", "as_progress", is_flag=True,
              help="Show a progress bar instead of the log stream.")
@click.pass_context
def watch(ctx: click.Context, run_id: str, as_progress: bool) -> None:
    """Follow a running workflow live."""
    handler = cmd.cmd_progress if as_progress else cmd.cmd_watch
    ctx.exit(_run(handler, _session(ctx.obj.get("cwd")), run_id))


@cli.command("pending", context_settings=CONTEXT)
@click.pass_context
def pending(ctx: click.Context) -> None:
    """List runs waiting for approval."""
    ctx.exit(_run(cmd.cmd_pending, _session(ctx.obj.get("cwd"))))


@cli.command("approve", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def approve(ctx: click.Context, run_id: str) -> None:
    """Review the diff and merge."""
    ctx.exit(_run(cmd.cmd_approve, _session(ctx.obj.get("cwd")), run_id))


@cli.command("reject", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.argument("reason", nargs=-1)
@click.pass_context
def reject(ctx: click.Context, run_id: str, reason: tuple[str, ...]) -> None:
    """Leave a run for rework instead of merging."""
    ctx.exit(_run(cmd.cmd_reject, _session(ctx.obj.get("cwd")), run_id, *reason))


@cli.command("resume", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def resume(ctx: click.Context, run_id: str) -> None:
    """Continue a run from where it stopped."""
    ctx.exit(_run(cmd.cmd_resume, _session(ctx.obj.get("cwd")), run_id))


@cli.command("retry", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def retry(ctx: click.Context, run_id: str) -> None:
    """Run another rework round."""
    ctx.exit(_run(cmd.cmd_retry, _session(ctx.obj.get("cwd")), run_id))


@cli.command("pause", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def pause(ctx: click.Context, run_id: str) -> None:
    """Stop a run for now, keeping what it has committed."""
    ctx.exit(_run(cmd.cmd_pause, _session(ctx.obj.get("cwd")), run_id))


@cli.command("cancel", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def cancel(ctx: click.Context, run_id: str) -> None:
    """Stop a run at its next step boundary."""
    ctx.exit(_run(cmd.cmd_cancel, _session(ctx.obj.get("cwd")), run_id))


@cli.command("rm", context_settings=CONTEXT)
@click.argument("run_id", required=False, default="")
@click.pass_context
def rm(ctx: click.Context, run_id: str) -> None:
    """Delete a run from the history."""
    ctx.exit(_run(cmd.cmd_rm, _session(ctx.obj.get("cwd")), run_id))


@cli.command("clear", context_settings=CONTEXT)
@click.argument("scope", required=False, default="finished",
                type=click.Choice(["merged", "finished", "all"]))
@click.pass_context
def clear(ctx: click.Context, scope: str) -> None:
    """Delete many runs at once."""
    ctx.exit(_run(cmd.cmd_clear, _session(ctx.obj.get("cwd")), scope))


@cli.command("prompts", context_settings=CONTEXT)
@click.argument("action", required=False, default="",
                type=click.Choice(["", "add", "rm"]))
@click.argument("path", required=False, default="")
@click.pass_context
def prompts(ctx: click.Context, action: str, path: str) -> None:
    """List saved prompt files, or manage where they are looked for."""
    ctx.exit(_run(cmd.cmd_prompts, _session(ctx.obj.get("cwd")), action, path))


@cli.command("backends", context_settings=CONTEXT)
@click.pass_context
def backends(ctx: click.Context) -> None:
    """Show the configured AI CLIs."""
    ctx.exit(_run(cmd.cmd_backends, _session(ctx.obj.get("cwd"))))


@cli.command("agents", context_settings=CONTEXT)
@click.argument("backend", required=False, default="")
@click.argument("value", required=False, default="")
@click.argument("scope", required=False, default="",
                type=click.Choice(["", "execute", "read"]))
@click.pass_context
def agents(ctx: click.Context, backend: str, value: str, scope: str) -> None:
    """Show or set which bundled agent a backend runs."""
    ctx.exit(_run(cmd.cmd_agents, _session(ctx.obj.get("cwd")), backend, value, scope))


@cli.command("model", context_settings=CONTEXT)
@click.argument("backend", required=False, default="")
@click.argument("value", required=False, default="")
@click.argument("scope", required=False, default="",
                type=click.Choice(["", "execute", "read"]))
@click.pass_context
def model(ctx: click.Context, backend: str, value: str, scope: str) -> None:
    """Show or set which model a backend uses."""
    ctx.exit(_run(cmd.cmd_model, _session(ctx.obj.get("cwd")), backend, value, scope))


@cli.command("config", context_settings=CONTEXT)
@click.pass_context
def show_config(ctx: click.Context) -> None:
    """Show the defaults applied to new runs."""
    ctx.exit(_run(cmd.cmd_config, _session(ctx.obj.get("cwd"))))


@cli.command("set", context_settings=CONTEXT)
@click.argument("key")
@click.argument("value", nargs=-1, required=True)
@click.pass_context
def set_setting(ctx: click.Context, key: str, value: tuple[str, ...]) -> None:
    """Change a default, e.g. `set mode autonomous`."""
    ctx.exit(_run(cmd.cmd_set, _session(ctx.obj.get("cwd")), key, *value))


def main() -> int:
    try:
        return cli(standalone_mode=False, obj={}) or 0
    except click.ClickException as exc:
        render.error(exc.format_message())
        return exc.exit_code
    except click.exceptions.Abort:
        render.warn("cancelled")
        return 130
    except KeyboardInterrupt:
        render.warn("cancelled")
        return 130


if __name__ == "__main__":
    sys.exit(main())
