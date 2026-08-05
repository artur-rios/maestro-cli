"""The slash commands, and starting a run from a typed task.

Handlers take the session and the parsed arguments, and return nothing; they
print through `render` and act through `runner` and `store`.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from .. import backends, config, gitops, settings as settings_mod, store as store_mod
from ..gitops import GitError
from ..models import Mode, Role, Status, Workflow
from . import logview, progress, promptlib, render
from .parser import Command, TaskIntent
from .promptlib import PromptError
from .render import console


#: Offered at the end of every model list, for an id not on it.
OTHER_MODEL = "other (type it)"


class CommandError(Exception):
    """Anything the user should see as one line, not a traceback."""


# --------------------------------------------------------------------- helpers


def find_run(session, wanted: str, among=None) -> Workflow:
    """Resolve an id prefix, or the obvious run when none was given."""
    pool = among if among is not None else session.store.all()
    if not pool:
        raise CommandError("there are no runs yet")

    wanted = (wanted or "").strip()
    if not wanted:
        if len(pool) == 1:
            return pool[0]
        raise CommandError(
            f"which run? {', '.join(w.id[:6] for w in pool[:6])}  (see /runs)"
        )

    matches = [w for w in pool if w.id.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CommandError(f"no run starts with '{wanted}' - see /runs")
    raise CommandError(f"'{wanted}' matches {len(matches)} runs - be more specific")


def _installed(name: str) -> bool:
    backend = config.load_backends().get(name)
    return bool(backend) and config.is_installed(backend)


def resolve_agents(session, overrides: dict) -> dict[str, str]:
    """Which backend fills each role, after settings and inline flags."""
    chosen: dict[str, str] = {}
    for role in ("plan", "execute", "review"):
        name = str(overrides.get(role) or session.settings.get(role) or "").strip()
        if not name:
            name = session.default_backend()
        if not name:
            raise CommandError(
                "no AI CLI was found - install claude, codex or opencode, "
                "or point 'bin' at one with /backends"
            )
        if name not in config.load_backends():
            raise CommandError(f"unknown backend '{name}' - see /backends")
        if not _installed(name):
            raise CommandError(f"'{name}' is not installed - see /backends")
        chosen[role] = name
    return chosen


# ------------------------------------------------------------------ start a run


def start_task(session, intent: TaskIntent) -> None:
    """Turn a typed task into a running workflow."""
    prompt, source = _task_text(session, intent)
    overrides = intent.overrides

    repo_raw = str(overrides.get("repo") or session.settings.get("repo") or "")
    repo_raw = repo_raw or str(session.cwd)
    try:
        repo = gitops.repo_root(repo_raw)
        base = str(overrides.get("base") or gitops.current_branch(repo))
        gitops.validate_target(str(repo), base)
    except GitError as exc:
        raise CommandError(str(exc)) from None

    plan = _defaulted_run(session, prompt, overrides, repo)

    plan = _confirm_or_edit(session, plan, repo, source)
    if plan is None:
        render.muted("cancelled")
        return

    workflow = Workflow(
        prompt=prompt,
        repo_path=str(repo),
        base_branch=base,
        agents=plan["agents"],
        models={k: v for k, v in (plan.get("models") or {}).items() if v},
        prompt_source=source,
        instructions=str(session.settings.get("instructions") or ""),
        mode=Mode(plan["mode"]),
        max_rounds=plan["rounds"],
        max_attempts=plan["attempts"],
        branch=plan["branch"],
    )
    session.store.add(workflow)
    session.notices.seed(workflow)
    session.runner.start(workflow)
    render.success(f"started {workflow.id[:6]}")

    if session.settings.get("follow", True):
        render.muted("following - Ctrl-C returns to the prompt")
        render.out()
        logview.stream(workflow, None, session.runner.is_running)
        # Whatever happened while following is now old news.
        session.notices.seed(workflow)


def _defaulted_run(session, prompt: str, overrides: dict, repo) -> dict:
    """Everything a run needs, from settings then inline flags."""
    branch_type = session.settings.get("branch_type") or None
    if overrides.get("branch"):
        branch = gitops.sanitize_branch(str(overrides["branch"]))
        if "/" not in branch:
            branch = f"{branch_type or gitops.suggest_branch_type(prompt)}/{branch}"
    else:
        branch = gitops.suggest_branch(prompt, branch_type)

    every = str(overrides.get("model") or "")
    models = {
        role: str(
            overrides.get(f"{role}-model")
            or every
            or session.settings.get(f"{role}_model")
            or ""
        )
        for role in ("plan", "execute", "review")
    }

    return {
        "agents": resolve_agents(session, overrides),
        "models": models,
        "mode": str(overrides.get("mode") or session.settings.get("mode")),
        "rounds": int(overrides.get("rounds") or session.settings.get("rounds")),
        "attempts": int(overrides.get("attempts") or session.settings.get("attempts")),
        "branch": gitops.unique_branch(repo, branch),
    }


def _confirm_or_edit(session, plan: dict, repo, source: str = "") -> dict | None:
    """Show what will happen; let it be changed. None means cancel."""
    while True:
        render.out(render.summary_panel(
            plan["branch"], plan["agents"], plan["mode"],
            plan["rounds"], plan["attempts"], plan.get("models"), source,
        ))

        if not session.settings.get("confirm", True):
            return plan

        answer = session.ask_action(
            "Start this run?",
            [
                ("run", "run it"),
                ("change", "change the settings first"),
                ("cancel", "cancel"),
            ],
            default="run",
        ).strip().lower()
        if answer in {"", "run", "y", "yes", "r"}:
            return plan
        if answer in {"cancel", "n", "no", "q"}:
            return None
        if answer in {"change", "c", "e", "edit"}:
            plan = _edit_run(session, plan, repo)
            continue
        render.warn("choose run, change or cancel")


def _edit_run(session, plan: dict, repo) -> dict:
    """Walk the settings one at a time. Enter keeps what is shown."""
    installed = [name for name, ok in backends.available() if ok]
    render.out()
    render.muted("Enter keeps the value shown.")
    if installed:
        render.muted(f"available: {', '.join(installed)}")

    agents = dict(plan["agents"])
    models = dict(plan.get("models") or {})
    for role, label in (
        ("plan", "plan agent"),
        ("execute", "execute agent"),
        ("review", "review agent"),
    ):
        chosen = (
            session.ask_select(label, installed, agents[role])
            if installed else agents[role]
        )
        try:
            # A picker cannot offer a bad value, but a typed fallback can.
            agents[role] = _valid_agent(chosen, installed)
        except ValueError as exc:
            render.error(str(exc))
            agents[role] = _ask_until(
                session, label, agents[role], lambda v: _valid_agent(v, installed)
            )
        models[role] = _ask_model(session, agents[role], label, models.get(role, ""))
    plan["agents"] = agents
    plan["models"] = models

    plan["mode"] = settings_mod.normalise_mode(
        session.ask_select("mode", ["supervised", "autonomous"], plan["mode"])
    )
    plan["rounds"] = _ask_until(
        session, "rework rounds", str(plan["rounds"]), _positive
    )
    plan["attempts"] = _ask_until(
        session, "attempts per step", str(plan["attempts"]), _positive
    )
    plan["branch"] = _ask_until(
        session, "branch", plan["branch"],
        lambda v: _valid_branch(v, repo, plan["branch"]),
    )

    render.out()
    if session.ask_confirm("Save these as your defaults?", require="y"):
        _save_as_defaults(session, plan)
        render.success("saved")
    return plan


def _ask_model(session, backend_name: str, role_label: str, current: str) -> str:
    """Which model that backend should use for this role. Blank = its default.

    Backends that can enumerate their models offer them; the rest take a
    typed id, since claude and codex have no list command.
    """
    backend = config.load_backends().get(backend_name) or {}
    if not backend.get("model_flag"):
        return ""

    choices = backends.model_choices(backend)
    label = role_label.replace(" agent", " model")

    if choices:
        # Always a picker: the first entry keeps whatever the CLI would
        # choose, the last is an escape hatch for an id not on the list.
        default_label = f"({backend_name} default)"
        options = [default_label, *choices, OTHER_MODEL]
        picked = session.ask_select(
            label, options, current if current in choices else default_label
        )
        if picked in (default_label, "", None):
            return ""
        if picked in choices:
            return picked
        if picked == OTHER_MODEL:
            render.muted(
                f"  ids you use often belong in known_models in "
                f"{config.backends_path().name}"
            )
            return session.ask_line(label, "").strip()
        # Typed straight in, which is how the plain fallback answers a
        # select. Take it as given rather than asking a second time.
        return picked.strip()

    configured = backends.current_flag_value(backend, Role.PLAN, "model_flag")
    hint = f" e.g. {configured}" if configured else ""
    render.muted(f"  {backend_name} model - blank for its default{hint}")

    def check(value: str) -> str:
        wanted = (value or "").strip()
        if not wanted:
            return ""
        if not choices or wanted in choices:
            return wanted
        near = [c for c in choices if wanted.lower() in c.lower()]
        if len(near) == 1:
            return near[0]
        if near:
            raise ValueError(f"matches {len(near)}: {', '.join(near[:5])}")
        raise ValueError(f"{backend_name} has no model '{wanted}'")

    return _ask_until(session, label, current, check)


def _ask_until(session, label: str, default: str, check):
    """Ask until `check` accepts the answer, returning what it produced."""
    while True:
        raw = session.ask_line(label, default)
        try:
            return check(raw)
        except (ValueError, CommandError) as exc:
            render.error(str(exc))


def _valid_agent(value: str, installed: list[str]) -> str:
    name = (value or "").strip()
    known = config.load_backends()
    if name not in known:
        raise ValueError(f"unknown backend '{name}' - see /backends")
    if installed and name not in installed:
        raise ValueError(f"'{name}' is not installed")
    return name


def _positive(value: str) -> int:
    text = (value or "").strip()
    if not text.isdigit() or int(text) < 1:
        raise ValueError("must be a whole number of 1 or more")
    return int(text)


def _valid_branch(value: str, repo, current: str) -> str:
    name = gitops.sanitize_branch(value)
    if not name:
        raise ValueError("a branch needs a name")
    if name == current:
        return name  # already checked to be free
    return gitops.unique_branch(repo, name)


def _save_as_defaults(session, plan: dict) -> None:
    models = plan.get("models") or {}
    session.settings.update(
        {
            "plan": plan["agents"]["plan"],
            "execute": plan["agents"]["execute"],
            "review": plan["agents"]["review"],
            "plan_model": models.get("plan", ""),
            "execute_model": models.get("execute", ""),
            "review_model": models.get("review", ""),
            "mode": plan["mode"],
            "rounds": plan["rounds"],
            "attempts": plan["attempts"],
        }
    )
    settings_mod.save(session.settings)


def _task_text(session, intent: TaskIntent) -> tuple[str, str]:
    """The prompt to run, and where it came from."""
    if not intent.ref:
        return intent.text, ""

    try:
        path = promptlib.resolve(intent.ref, session.prompt_files(), session.cwd)
        path, text = promptlib.load(path)
    except PromptError as exc:
        raise CommandError(str(exc)) from None

    if intent.text:
        # Words typed after the reference refine what the file asks for.
        text = f"{text}\n\nAdditionally: {intent.text}"
    return text, str(path)


# ----------------------------------------------------------------- inspecting


def cmd_runs(session, command: Command) -> None:
    render.out(render.run_table(session.store.all(), session.runner.is_running))


def cmd_show(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    for part in render.run_detail(wf, session.runner.is_running):
        render.out(part)
    if wf.status is Status.FAILED:
        for part in logview.failure_context(wf):
            render.out(part)


def cmd_logs(session, command: Command) -> None:
    """`/logs <id> [plan|execute|review]`, defaulting to everything."""
    args = list(command.args)
    roles = None
    if args and args[-1].lower() in {r.value for r in Role}:
        roles = {args.pop().lower()}
    wf = find_run(session, " ".join(args))
    if not wf.steps:
        raise CommandError("that run has not produced any logs yet")
    logview.stream(wf, roles, session.runner.is_running)


def cmd_watch(session, command: Command) -> None:
    running = [w for w in session.store.all() if session.runner.is_running(w.id)]
    wf = find_run(session, command.arg, among=running or None)
    logview.stream(wf, None, session.runner.is_running)


def cmd_progress(session, command: Command) -> None:
    """A live bar over the checklist, rather than the agents' chatter."""
    running = [w for w in session.store.all() if session.runner.is_running(w.id)]
    wf = find_run(session, command.arg, among=running or None)
    progress.watch(wf, session.runner.is_running)


# --------------------------------------------------------------- the merge gate


def _awaiting(session) -> list[Workflow]:
    return [
        w for w in session.store.all() if w.status is Status.AWAITING_APPROVAL
    ]


def cmd_pending(session, command: Command) -> None:
    waiting = _awaiting(session)
    if not waiting:
        render.muted("nothing is waiting for approval")
        return
    render.out(render.run_table(waiting, session.runner.is_running))


def cmd_approve(session, command: Command) -> None:
    wf = find_run(session, command.arg, among=_awaiting(session))
    try:
        stat = gitops.diffstat(wf.repo_path, wf.base_branch, wf.branch)
    except GitError as exc:
        stat = f"(could not read the diff: {exc})"
    render.out(render.merge_preview(wf, stat))

    if not session.ask_confirm("Merge it?"):
        render.muted("left unmerged")
        return
    session.runner.approve(wf)
    render.info(f"merging {wf.id[:6]}...")


def cmd_reject(session, command: Command) -> None:
    args = list(command.args)
    wf = find_run(session, args[0] if args else "", among=_awaiting(session))
    reason = " ".join(args[1:])
    session.runner.reject(wf, reason)
    render.warn(f"{wf.id[:6]} left for rework")


# ------------------------------------------------------------- managing a run


def cmd_resume(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if session.runner.is_running(wf.id):
        raise CommandError("that run is already going")
    if wf.status is Status.MERGED:
        raise CommandError("that run is already merged")
    if wf.status is Status.AWAITING_APPROVAL:
        raise CommandError("that run is waiting for you - /approve or /reject")
    if wf.tasks:
        render.muted(f"keeping {wf.task_progress} finished items")
    session.notices.seed(wf)
    session.runner.resume(wf)
    render.success(f"resumed {wf.id[:6]}")


def cmd_retry(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if session.runner.is_running(wf.id):
        raise CommandError("that run is already going")
    session.notices.seed(wf)
    session.runner.retry(wf)
    render.success(f"rework round {wf.round} on {wf.id[:6]}")


def cmd_pause(session, command: Command) -> None:
    """Stop a run for now, keeping everything it has committed."""
    wf = find_run(session, command.arg)
    if wf.status is Status.MERGED:
        raise CommandError("that run is already merged")
    if wf.status is Status.PAUSED:
        raise CommandError("that run is already paused - /resume to continue")
    session.notices.seed(wf)
    session.runner.pause(wf)
    if session.runner.is_running(wf.id):
        render.warn(f"{wf.id[:6]} will pause at the next step")
    else:
        render.success(f"paused {wf.id[:6]}")
    render.hint(f"/resume {wf.id[:6]} to carry on")


def cmd_cancel(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if not session.runner.is_running(wf.id):
        raise CommandError("that run is not going")
    session.runner.cancel(wf)
    render.warn(f"{wf.id[:6]} will stop at the next step")


def cmd_rm(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if session.runner.is_running(wf.id):
        raise CommandError("that run is still going - /cancel it first")
    if not session.ask_confirm(f"Delete {wf.id[:6]} ({wf.summary})?", "y"):
        return
    _forget(session, wf, drop_logs=True, drop_worktree=True)
    render.success(f"deleted {wf.id[:6]}")
    render.muted(f"branch '{wf.branch}' was left alone")


def cmd_clear(session, command: Command) -> None:
    scope = (command.arg or "finished").lower()
    everything = session.store.all()
    if scope == "merged":
        targets = [w for w in everything if w.status is Status.MERGED]
    elif scope in {"finished", "done"}:
        targets = [w for w in everything if w.is_terminal]
    elif scope == "all":
        targets = list(everything)
    else:
        raise CommandError("/clear takes 'merged', 'finished' or 'all'")

    targets = [w for w in targets if not session.runner.is_running(w.id)]
    if not targets:
        render.muted("nothing to clear")
        return

    render.out(render.run_table(targets[:10], session.runner.is_running))
    if len(targets) > 10:
        render.muted(f"... and {len(targets) - 10} more")

    if not session.ask_confirm(
        f"Delete {render.plural(len(targets), 'run')}?", "y"
    ):
        return
    for wf in targets:
        _forget(session, wf, drop_logs=True, drop_worktree=True)
    render.success(f"cleared {render.plural(len(targets), 'run')}")
    render.muted("no branches were deleted")


def _forget(session, wf: Workflow, drop_logs: bool, drop_worktree: bool) -> None:
    if drop_worktree and wf.worktree and Path(wf.worktree).exists():
        gitops.remove_worktree(wf.repo_path, wf.worktree)
    if drop_logs:
        store_mod.discard_logs(wf.id)
    session.store.remove(wf.id)
    session.notices.forget(wf.id)


# -------------------------------------------------------------------- backends


def cmd_backends(session, command: Command) -> None:
    render.out(render.backend_table(config.load_backends()))
    render.muted(
        f"edit {config.backends_path()} for anything /agents and /model "
        f"does not cover"
    )


def _pick_backend_setting(session, command: Command, kind: str) -> None:
    """Shared by /agents and /model."""
    data = config.load_backends()
    args = list(command.args)
    name = args[0] if args else ""
    if not name:
        supported = [n for n, b in sorted(data.items()) if b.get(f"{kind}_flag")]
        if not supported:
            raise CommandError(f"no backend supports choosing a {kind}")
        raise CommandError(f"which backend? {', '.join(supported)}")
    if name not in data:
        raise CommandError(f"unknown backend '{name}' - see /backends")

    backend = data[name]
    if not backend.get(f"{kind}_flag"):
        raise CommandError(f"{name} has no {kind} option")

    value = " ".join(args[1:]).strip()
    choices = backends.list_options(backend, kind)
    if not value:
        render.out(render.option_table(name, kind, choices))
        current_plan = backends.current_flag_value(backend, Role.PLAN, f"{kind}_flag")
        current_exec = backends.current_flag_value(backend, Role.EXECUTE, f"{kind}_flag")
        render.muted(
            f"now: plan/review {current_plan or 'default'}, "
            f"execute {current_exec or 'default'}"
        )
        command = "agents" if kind == "agent" else "model"
        render.hint(f"/{command} {name} <value> [execute|read]")
        return

    parts = value.split()
    scope = parts[-1].lower() if parts[-1].lower() in {"execute", "read"} else ""
    if scope:
        parts = parts[:-1]
    wanted = " ".join(parts)

    names = [c for c, _ in choices]
    if choices and wanted not in names:
        near = [c for c in names if wanted.lower() in c.lower()]
        if len(near) == 1:
            wanted = near[0]
        elif near:
            raise CommandError(f"'{wanted}' matches: {', '.join(near[:6])}")
        else:
            raise CommandError(f"{name} has no {kind} '{wanted}'")

    keys = {"execute": ["execute_args"], "read": ["args"]}.get(
        scope, ["args", "execute_args"]
    )
    for key in keys:
        backends.set_flag_value(backend, key, f"{kind}_flag", wanted)
    config.save_backends(data)
    render.success(
        f"{name} {kind} set to {wanted}{' for ' + scope if scope else ''}"
    )


def cmd_agents(session, command: Command) -> None:
    _pick_backend_setting(session, command, "agent")


def cmd_model(session, command: Command) -> None:
    _pick_backend_setting(session, command, "model")


# -------------------------------------------------------------------- settings


def cmd_set(session, command: Command) -> None:
    if not command.args:
        return cmd_config(session, command)
    key = command.args[0].lower()
    value = " ".join(command.args[1:])
    if not value:
        raise CommandError(f"/set {key} <value>")
    try:
        session.settings[key] = settings_mod.coerce(key, value)
    except settings_mod.SettingsError as exc:
        raise CommandError(str(exc)) from None
    settings_mod.save(session.settings)
    render.success(f"{key} = {session.settings[key]}")


def cmd_config(session, command: Command) -> None:
    render.out(render.settings_table(session.settings, settings_mod.EDITABLE))
    render.out(render.roots_panel(session.settings.get("prompt_paths", [])))
    render.muted(f"state {config.home_dir()}")


# --------------------------------------------------------------------- prompts


def cmd_prompts(session, command: Command) -> None:
    args = list(command.args)
    if args and args[0].lower() in {"add", "rm", "remove"}:
        return _prompt_roots(session, args)

    files = session.prompt_files(refresh=True)
    if not files:
        render.muted("no prompt files found")
        looked = ", ".join(
            label for label, _, _ in promptlib.roots(session.settings, session.cwd)
        )
        render.muted(f"looked in: {looked}")
        return

    render.out(render.prompt_table(files))
    render.hint(f"start one with @name, e.g. @{files[0].name}")


def _prompt_roots(session, args: list[str]) -> None:
    action = args[0].lower()
    target = " ".join(args[1:])
    if not target:
        raise CommandError(f"/prompts {action} <path>")
    try:
        if action == "add":
            directory = settings_mod.add_prompt_path(session.settings, target)
            settings_mod.save(session.settings)
            session.prompt_files(refresh=True)
            render.success(f"searching {directory} for prompts")
        else:
            if settings_mod.remove_prompt_path(session.settings, target):
                settings_mod.save(session.settings)
                session.prompt_files(refresh=True)
                render.success(f"no longer searching {target}")
            else:
                raise CommandError(f"{target} is not a configured root")
    except settings_mod.SettingsError as exc:
        raise CommandError(str(exc)) from None


# ------------------------------------------------------------------------ help

HELP = [
    ("<task>", "start a workflow from what you type"),
    ("@name [extra]", "start one from a saved prompt file"),
    ("--plan --execute --review", "which AI fills each role, this run only"),
    ("--mode --rounds --attempts", "autonomous or supervised, and the budgets"),
    ("--repo --base --branch", "where the work happens"),
    ("", ""),
    ("/runs", "every run and its status"),
    ("/show <id>", "one run in full"),
    ("/logs <id> [step]", "its logs, all steps or one"),
    ("/watch [id]", "follow a running one live"),
    ("/progress [id]", "a progress bar over its checklist"),
    ("", ""),
    ("/pending", "runs waiting for your approval"),
    ("/approve <id>", "see the diff, then merge"),
    ("/reject <id> [why]", "leave it for rework"),
    ("", ""),
    ("/pause <id>", "stop for now, keeping the work"),
    ("/resume <id>", "continue a paused or failed run"),
    ("/retry <id>", "another rework round"),
    ("/cancel <id>", "stop at the next step"),
    ("/rm <id>", "delete a run"),
    ("/clear merged|finished|all", "delete many"),
    ("", ""),
    ("/prompts [add|rm <path>]", "saved prompt files and where to find them"),
    ("/backends /agents /model", "which CLI, which agent, which model"),
    ("/set <key> <value>", "defaults for new runs"),
    ("/config", "what those defaults are"),
    ("/help /exit", ""),
]


def cmd_help(session, command: Command) -> None:
    render.out(render.help_table(HELP))


TABLE = {
    "runs": cmd_runs, "ls": cmd_runs, "list": cmd_runs,
    "show": cmd_show, "view": cmd_show,
    "logs": cmd_logs, "log": cmd_logs,
    "watch": cmd_watch, "follow": cmd_watch,
    "progress": cmd_progress, "bar": cmd_progress,
    "pending": cmd_pending, "waiting": cmd_pending,
    "approve": cmd_approve, "merge": cmd_approve,
    "reject": cmd_reject,
    "resume": cmd_resume, "continue": cmd_resume,
    "retry": cmd_retry,
    "cancel": cmd_cancel, "stop": cmd_cancel,
    "pause": cmd_pause, "hold": cmd_pause,
    "rm": cmd_rm, "delete": cmd_rm,
    "clear": cmd_clear,
    "backends": cmd_backends,
    "agents": cmd_agents, "agent": cmd_agents,
    "model": cmd_model, "models": cmd_model,
    "set": cmd_set,
    "config": cmd_config, "settings": cmd_config,
    "prompts": cmd_prompts, "prompt": cmd_prompts,
    "help": cmd_help, "?": cmd_help,
}


def dispatch(session, command: Command) -> None:
    handler = TABLE.get(command.name)
    if handler is None:
        near = difflib.get_close_matches(command.name, TABLE, n=1, cutoff=0.6)
        hint = f" - did you mean /{near[0]}?" if near else " - /help lists them"
        raise CommandError(f"no command /{command.name}{hint}")
    handler(session, command)
