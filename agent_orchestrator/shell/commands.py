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
from . import logview, promptlib, render
from .parser import Command, TaskIntent
from .promptlib import PromptError
from .render import BOLD, CYAN, GREEN, GREY, RED, RESET, YELLOW, colored


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

    if source:
        print(f"  {GREY}from {source}{RESET}")
    plan = _confirm_or_edit(session, plan, repo)
    if plan is None:
        print(f"  {GREY}cancelled{RESET}")
        return

    workflow = Workflow(
        prompt=prompt,
        repo_path=str(repo),
        base_branch=base,
        agents=plan["agents"],
        models={k: v for k, v in (plan.get("models") or {}).items() if v},
        prompt_source=source,
        mode=Mode(plan["mode"]),
        max_rounds=plan["rounds"],
        max_attempts=plan["attempts"],
        branch=plan["branch"],
    )
    session.store.add(workflow)
    session.notices.seed(workflow)
    session.runner.start(workflow)
    print(colored(f"  started {workflow.id[:6]}", GREEN))

    if session.settings.get("follow", True):
        print(f"  {GREY}following - Ctrl-C returns to the prompt{RESET}\n")
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


def _confirm_or_edit(session, plan: dict, repo) -> dict | None:
    """Show what will happen; let it be changed. None means cancel."""
    while True:
        for line in render.summary_line(
            plan["branch"], plan["agents"], plan["mode"],
            plan["rounds"], plan["attempts"], plan.get("models"),
        ):
            print(line)

        if not session.settings.get("confirm", True):
            return plan

        answer = session.ask_line(
            f"{GREY}Enter to run, c to change, n to cancel{RESET}"
        ).strip().lower()
        if answer in {"", "y", "yes", "r", "run"}:
            return plan
        if answer in {"n", "no", "q", "cancel"}:
            return None
        if answer in {"c", "change", "e", "edit"}:
            plan = _edit_run(session, plan, repo)
            continue
        print(colored("  type Enter to run, c to change, or n to cancel", YELLOW))


def _edit_run(session, plan: dict, repo) -> dict:
    """Walk the settings one at a time. Enter keeps what is shown."""
    installed = [name for name, ok in backends.available() if ok]
    print(f"\n  {GREY}Enter keeps the value shown.{RESET}")
    if installed:
        print(f"  {GREY}available: {', '.join(installed)}{RESET}")

    agents = dict(plan["agents"])
    models = dict(plan.get("models") or {})
    for role, label in (
        ("plan", "plan agent"),
        ("execute", "execute agent"),
        ("review", "review agent"),
    ):
        agents[role] = _ask_until(
            session, label, agents[role], lambda v: _valid_agent(v, installed)
        )
        models[role] = _ask_model(session, agents[role], label, models.get(role, ""))
    plan["agents"] = agents
    plan["models"] = models

    plan["mode"] = _ask_until(
        session, "mode (supervised/autonomous)", plan["mode"],
        settings_mod.normalise_mode,
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

    print()
    if session.ask_confirm("  Save these as your defaults? [y/N] ", require="y"):
        _save_as_defaults(session, plan)
        print(colored("  saved", GREEN))
    return plan


def _ask_model(session, backend_name: str, role_label: str, current: str) -> str:
    """Which model that backend should use for this role. Blank = its default.

    Backends that can enumerate their models offer them; the rest take a
    typed id, since claude and codex have no list command.
    """
    backend = config.load_backends().get(backend_name) or {}
    if not backend.get("model_flag"):
        return ""

    choices = [name for name, _ in backends.list_options(backend, "model")]
    if choices:
        shown = ", ".join(choices[:8])
        more = f", +{len(choices) - 8} more" if len(choices) > 8 else ""
        print(f"    {GREY}{backend_name} models: {shown}{more}{RESET}")
    else:
        configured = backends.current_flag_value(backend, Role.PLAN, "model_flag")
        hint = f" e.g. {configured}" if configured else ""
        print(f"    {GREY}{backend_name} model - blank for its default{hint}{RESET}")

    label = role_label.replace(" agent", " model")

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
            print(colored(f"    {exc}", RED))


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
    for line in render.run_table(session.store.all(), session.runner.is_running):
        print(line)


def cmd_show(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    for line in render.run_detail(wf, session.runner.is_running):
        print(line)
    if wf.status is Status.FAILED:
        for line in logview.failure_context(wf):
            print(line)


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


# --------------------------------------------------------------- the merge gate


def _awaiting(session) -> list[Workflow]:
    return [
        w for w in session.store.all() if w.status is Status.AWAITING_APPROVAL
    ]


def cmd_pending(session, command: Command) -> None:
    waiting = _awaiting(session)
    if not waiting:
        print(f"{GREY}  nothing is waiting for approval{RESET}")
        return
    for line in render.run_table(waiting, session.runner.is_running):
        print(line)


def cmd_approve(session, command: Command) -> None:
    wf = find_run(session, command.arg, among=_awaiting(session))
    try:
        stat = gitops.diffstat(wf.repo_path, wf.base_branch, wf.branch)
    except GitError as exc:
        stat = f"(could not read the diff: {exc})"
    print(f"  merge {BOLD}{wf.branch}{RESET} into {BOLD}{wf.base_branch}{RESET}")
    print(render.indent(stat or "(no changes)"))
    if wf.review_text:
        print(f"\n  {GREY}reviewer said:{RESET}")
        print(render.indent(wf.review_text[-800:]))

    if not session.ask_confirm("  Enter to merge, n to cancel: "):
        print(f"  {GREY}left unmerged{RESET}")
        return
    session.runner.approve(wf)
    print(colored(f"  merging {wf.id[:6]}...", CYAN))


def cmd_reject(session, command: Command) -> None:
    args = list(command.args)
    wf = find_run(session, args[0] if args else "", among=_awaiting(session))
    reason = " ".join(args[1:])
    session.runner.reject(wf, reason)
    print(colored(f"  {wf.id[:6]} left for rework", YELLOW))


# ------------------------------------------------------------- managing a run


def cmd_resume(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if session.runner.is_running(wf.id):
        raise CommandError("that run is already going")
    if wf.status is Status.MERGED:
        raise CommandError("that run is already merged")
    if wf.tasks:
        print(f"{GREY}  keeping {wf.task_progress} finished items{RESET}")
    session.notices.seed(wf)
    session.runner.resume(wf)
    print(colored(f"  resumed {wf.id[:6]}", GREEN))


def cmd_retry(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if session.runner.is_running(wf.id):
        raise CommandError("that run is already going")
    session.notices.seed(wf)
    session.runner.retry(wf)
    print(colored(f"  rework round {wf.round} on {wf.id[:6]}", GREEN))


def cmd_cancel(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if not session.runner.is_running(wf.id):
        raise CommandError("that run is not going")
    session.runner.cancel(wf)
    print(colored(f"  {wf.id[:6]} will stop at the next step", YELLOW))


def cmd_rm(session, command: Command) -> None:
    wf = find_run(session, command.arg)
    if session.runner.is_running(wf.id):
        raise CommandError("that run is still going - /cancel it first")
    if not session.ask_confirm(f"  Delete {wf.id[:6]} ({wf.summary})? [y/N] ", "y"):
        return
    _forget(session, wf, drop_logs=True, drop_worktree=True)
    print(colored(f"  deleted {wf.id[:6]}", GREEN))
    print(f"{GREY}  branch '{wf.branch}' was left alone{RESET}")


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
        print(f"{GREY}  nothing to clear{RESET}")
        return

    for wf in targets[:10]:
        print(f"  {GREY}{wf.id[:6]}  {wf.status.value:<18} {wf.summary}{RESET}")
    if len(targets) > 10:
        print(f"  {GREY}... and {len(targets) - 10} more{RESET}")

    if not session.ask_confirm(
        f"  Delete {render.plural(len(targets), 'run')}? [y/N] ", "y"
    ):
        return
    for wf in targets:
        _forget(session, wf, drop_logs=True, drop_worktree=True)
    print(colored(f"  cleared {render.plural(len(targets), 'run')}", GREEN))
    print(f"{GREY}  no branches were deleted{RESET}")


def _forget(session, wf: Workflow, drop_logs: bool, drop_worktree: bool) -> None:
    if drop_worktree and wf.worktree and Path(wf.worktree).exists():
        gitops.remove_worktree(wf.repo_path, wf.worktree)
    if drop_logs:
        store_mod.discard_logs(wf.id)
    session.store.remove(wf.id)
    session.notices.forget(wf.id)


# -------------------------------------------------------------------- backends


def cmd_backends(session, command: Command) -> None:
    data = config.load_backends()
    for name, backend in sorted(data.items()):
        mark = (
            colored("installed", GREEN) if config.is_installed(backend)
            else colored("not found", RED)
        )
        print(f"  {BOLD}{name}{RESET}  {GREY}({backend.get('bin')}){RESET}  {mark}")
        print(f"{GREY}     plan/review: {' '.join(backend.get('args', []))}{RESET}")
        print(
            f"{GREY}     execute:     "
            f"{' '.join(backend.get('execute_args') or backend.get('args', []))}{RESET}"
        )
    print(f"\n{GREY}  edit {config.backends_path()} for anything not covered "
          f"by /agents and /model{RESET}")


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
        print(f"  {BOLD}{name}{RESET} {kind}s:")
        for option, note in choices:
            suffix = f"  {GREY}({note}){RESET}" if note else ""
            print(f"    {option}{suffix}")
        current_plan = backends.current_flag_value(backend, Role.PLAN, f"{kind}_flag")
        current_exec = backends.current_flag_value(backend, Role.EXECUTE, f"{kind}_flag")
        print(f"  {GREY}now: plan/review {current_plan or 'default'}, "
              f"execute {current_exec or 'default'}{RESET}")
        print(f"  {GREY}set with: /{kind}s {name} <value> [execute|read]{RESET}"
              if kind == "agent" else
              f"  {GREY}set with: /{kind} {name} <value> [execute|read]{RESET}")
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
    print(colored(f"  {name} {kind} set to {wanted}"
                  f"{' for ' + scope if scope else ''}", GREEN))


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
    print(colored(f"  {key} = {session.settings[key]}", GREEN))


def cmd_config(session, command: Command) -> None:
    print(f"  {BOLD}defaults{RESET}   {GREY}change with /set <key> <value>{RESET}")
    for key in settings_mod.EDITABLE:
        value = session.settings.get(key)
        shown = value if value not in ("", None) else f"{GREY}(auto){RESET}"
        print(f"    {key:<12}{shown}")
    roots = session.settings.get("prompt_paths", [])
    print(f"\n  {BOLD}prompt roots{RESET}  {GREY}/prompts add <path>{RESET}")
    print(f"    {GREY}always: current directory, ./docs/prompts{RESET}")
    for root in roots:
        print(f"    {root}")
    print(f"\n  {GREY}state {config.home_dir()}{RESET}")


# --------------------------------------------------------------------- prompts


def cmd_prompts(session, command: Command) -> None:
    args = list(command.args)
    if args and args[0].lower() in {"add", "rm", "remove"}:
        return _prompt_roots(session, args)

    files = session.prompt_files(refresh=True)
    if not files:
        print(f"{GREY}  no prompt files found{RESET}")
        print(f"{GREY}  looked in: "
              f"{', '.join(label for label, _, _ in promptlib.roots(session.settings, session.cwd))}"
              f"{RESET}")
        return

    current = None
    for item in files:
        if item.source != current:
            current = item.source
            print(f"  {BOLD}{current}{RESET}")
        print(f"    {CYAN}@{item.name}{RESET}  {GREY}{item.label}{RESET}")
    print(f"\n  {GREY}start one with @name, e.g. @{files[0].name}{RESET}")


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
            print(colored(f"  searching {directory} for prompts", GREEN))
        else:
            if settings_mod.remove_prompt_path(session.settings, target):
                settings_mod.save(session.settings)
                session.prompt_files(refresh=True)
                print(colored(f"  no longer searching {target}", GREEN))
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
    ("", ""),
    ("/pending", "runs waiting for your approval"),
    ("/approve <id>", "see the diff, then merge"),
    ("/reject <id> [why]", "leave it for rework"),
    ("", ""),
    ("/resume <id>", "continue where it stopped"),
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
    print()
    for left, right in HELP:
        if not left:
            print()
            continue
        print(f"  {CYAN}{left:<28}{RESET}{GREY}{right}{RESET}")
    print()


TABLE = {
    "runs": cmd_runs, "ls": cmd_runs, "list": cmd_runs,
    "show": cmd_show, "view": cmd_show,
    "logs": cmd_logs, "log": cmd_logs,
    "watch": cmd_watch, "follow": cmd_watch,
    "pending": cmd_pending, "waiting": cmd_pending,
    "approve": cmd_approve, "merge": cmd_approve,
    "reject": cmd_reject,
    "resume": cmd_resume, "continue": cmd_resume,
    "retry": cmd_retry,
    "cancel": cmd_cancel, "stop": cmd_cancel,
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
