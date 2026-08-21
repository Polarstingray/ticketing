"""``stingray scaffold`` — generate a project outline with a ready-made backlog."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import requests

from stingray_cli import guided
from stingray_cli import scaffold as sc
from stingray_cli.common import client_from, confirm, profile_from
from stingray_cli.config import ConfigError
from stingray_client.stubs import (
    MAX_STUB_TICKETS,
    build_stub_payload,
    filed_checklist,
    stub_checklist,
)
from stingray_client.stubs import (
    epic_tag as stub_epic_tag,
)
from stingray_client.tickets import build_payload

# The adaptation pass rewrites a whole tree, so it is a strictly bigger job than
# describing a diff — give it more headroom than describe.DEFAULT_TIMEOUT. Agent
# CLI startup alone measured ~60s here, and a small-diff description took 350s.
# Timing out mid-adaptation isn't loud: it falls back to the plain template, so a
# too-tight value looks like "the AI pass did nothing".
DEFAULT_ADAPT_TIMEOUT = 1800


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser(
        "scaffold",
        help="generate a project outline plus a ticket per stub",
        description=(
            "Renders a template, optionally adapts it with a local agent, commits "
            "it, then files one ticket per STINGRAY-STUB marker plus an epic that "
            "tracks them. With --guided it also writes an ASSIGNMENT.md handout "
            "(learning goals, milestones, rubric) and writes the ticket bodies as "
            "exercises — a project shaped like a CS-class assignment."
        ),
    )
    parser.add_argument("template", nargs="?", help="template name")
    parser.add_argument("dest", nargs="?", help="target directory")
    parser.add_argument("--name", help="project name (default: the directory name)")
    parser.add_argument("--intent", metavar="TEXT",
                        help="what the project should do; drives the AI adaptation pass")
    # Deprecated alias. `review --describe` is a boolean meaning "let an agent write
    # the prose"; here the same word took a string and meant the opposite direction
    # (text in, code out). Kept working so existing invocations don't break, but it
    # warns and `--intent` is the documented spelling.
    parser.add_argument("--describe", metavar="TEXT", dest="describe_alias",
                        help=argparse.SUPPRESS)
    parser.add_argument("--guided", action="store_true",
                        help="also write an ASSIGNMENT.md handout and exercise-style "
                             "ticket bodies (a guided, class-project-shaped repo)")
    parser.add_argument("--course-level", choices=guided.COURSE_LEVELS,
                        default="intermediate", metavar="LEVEL",
                        help="how much of the design the handout gives away "
                             f"({'|'.join(guided.COURSE_LEVELS)}; default: intermediate)")
    parser.add_argument("--milestones", type=int, default=4, metavar="N",
                        help="how many milestones to group the stubs into (default: 4)")
    parser.add_argument("--no-assignment", action="store_true",
                        help="with --guided, skip the handout but still write "
                             "exercise-style ticket bodies")
    parser.add_argument("--list-templates", action="store_true")
    parser.add_argument("--no-ai", action="store_true", help="template only, no agent pass")
    parser.add_argument("--agent", metavar="NAME", help="agent to adapt with (claude|opencode)")
    parser.add_argument("--agent-timeout", type=int, metavar="SECONDS",
                        help=f"seconds for the adaptation pass (default: {DEFAULT_ADAPT_TIMEOUT})")
    parser.add_argument("--no-git", action="store_true", help="don't init/commit")
    parser.add_argument("--no-tickets", action="store_true", help="don't file tickets")
    parser.add_argument("--assign", type=int, metavar="USER_ID")
    parser.add_argument("--priority", default="medium")
    parser.add_argument("--max-tickets", type=int, default=MAX_STUB_TICKETS, metavar="N")
    parser.add_argument("--force", action="store_true", help="allow a non-empty destination")
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="scaffold and scan, but print the tickets instead of filing")
    add_connection_flags(parser)
    parser.set_defaults(func=cmd_scaffold)


def cmd_scaffold(args) -> int:
    intent = _resolve_intent(args)
    if args.list_templates:
        for template in sc.available_templates():
            print(f"{template.name:20} {template.description}")
        return 0

    if not args.template or not args.dest:
        raise ConfigError("usage: stingray scaffold TEMPLATE DEST (or --list-templates)")

    template = sc.load_template(args.template)
    dest = Path(args.dest).expanduser().resolve()
    name = args.name or dest.name

    if dest.exists() and any(dest.iterdir()) and not args.force:
        raise ConfigError(f"{dest} is not empty (pass --force to scaffold into it anyway)")

    variables = {
        "project_name": name,
        "package": name.replace("-", "_").replace(" ", "_").lower(),
        "description": intent or f"The {name} project.",
    }

    # Render into a temp dir first: an agent pass that produces a broken tree
    # should never leave a half-written project behind.
    staging = Path(tempfile.mkdtemp(prefix="stingray-scaffold-"))
    try:
        work = staging / "tree"
        work.mkdir()
        sc.render(template, work, variables)

        if not args.no_ai and intent:
            _adapt(work, template, args, variables)

        problems = sc.validate_tree(work)
        if problems:
            print("warning: the scaffold did not validate:", file=sys.stderr)
            for problem in problems[:5]:
                print(f"  {problem}", file=sys.stderr)
            print("falling back to the unmodified template", file=sys.stderr)
            shutil.rmtree(work)
            work.mkdir()
            sc.render(template, work, variables)

        dest.mkdir(parents=True, exist_ok=True)
        for item in work.iterdir():
            target = dest / item.name
            if target.exists():
                if not args.force:
                    raise ConfigError(f"{target} already exists (pass --force)")
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"scaffolded {template.name} into {dest}")

    # The handout has to be written before the commit (so .gitignore lands in it)
    # but needs the real stub list to build milestones from — hence a scan here and
    # a re-scan below. The second scan is what tickets are filed from: it sees the
    # committed tree, so every code block's line numbers are stable.
    assignment, exercises = "", {}
    if args.guided:
        assignment, exercises = _guide(dest, template, args, intent, sc.scan_stubs(dest))

    if not args.no_git:
        sc.git_init_and_commit(dest, f"scaffold: {name} from {template.name}")
        print("committed the scaffold (line numbers are now stable)")

    stubs = sc.scan_stubs(dest)
    print(f"found {len(stubs)} stub(s)")
    if not stubs or args.no_tickets:
        return 0

    if len(stubs) > args.max_tickets:
        print(f"warning: {len(stubs)} stubs exceeds --max-tickets={args.max_tickets}; "
              f"filing the first {args.max_tickets}", file=sys.stderr)
        stubs = stubs[:args.max_tickets]

    return _file_tickets(args, dest, name, template, stubs, assignment, exercises)


def _resolve_intent(args) -> str | None:
    """The adaptation intent, accepting the deprecated ``--describe`` spelling.

    Normalizes onto ``args.intent`` so everything downstream reads one attribute.
    """
    alias = getattr(args, "describe_alias", None)
    if alias and not args.intent:
        print("warning: `scaffold --describe` is deprecated; use `--intent`. "
              "(`--describe` means something different on `review`: there it is a "
              "boolean asking an agent to write the ticket's prose.)", file=sys.stderr)
        args.intent = alias
    elif alias and args.intent:
        raise ConfigError("--intent and --describe are the same option; pass only --intent")
    return args.intent


def _profile_or_none(args):
    """The profile, if one resolves.

    Rendering and adapting a scaffold are entirely local — only filing the
    tickets needs credentials — so the agent settings must still be readable
    when nobody has logged in yet.
    """
    try:
        return profile_from(args)
    except ConfigError:
        return None


def _adapt(work: Path, template: sc.Template, args, variables: dict) -> None:
    """Let a local agent adapt the rendered template to the user's intent."""
    from stingray_cli.agent import AgentError
    from stingray_cli.agent import run as run_agent

    listing = "\n".join(
        f"  {p.relative_to(work)}" for p in sorted(work.rglob("*")) if p.is_file()
    )
    prompt = "\n".join([
        f"You are adapting a '{template.name}' project scaffold in this directory to "
        f"a specific intent. The project is called '{variables['project_name']}'.",
        "",
        f"Intent: {args.intent}",
        f"Template: {template.description}",
        "",
        "Files:",
        listing,
        "",
        "Rules — follow all of them:",
        "- Rename, add or remove files and adapt function signatures to fit the intent.",
        "- Every non-trivial function body MUST be left as a stub in exactly this form:",
        f"      # {sc.STUB_MARKER}: <what to implement, one line>",
        "      # ACCEPTANCE: <how to know it is done>",
        '      raise NotImplementedError("' + sc.STUB_MARKER + '")',
        "  (use the target language's comment syntax and its equivalent of raise).",
        "- Do NOT implement business logic. The point is a skeleton to fill in by hand.",
        "- Keep entry points, imports and packaging wired so the project imports cleanly.",
        "- Leave at least three stubs.",
    ])

    if not confirm(f"Let an agent edit files in {work}?", assume_yes=args.yes):
        print("skipping the AI adaptation pass", file=sys.stderr)
        return

    agent, model, timeout = _agent_settings(args)
    try:
        run_agent(prompt, work, agent=agent, model=model, timeout=timeout, edit=True)
        print("agent adapted the scaffold")
    except AgentError as exc:
        print(f"warning: adaptation pass failed ({exc}); using the plain template",
              file=sys.stderr)


def _agent_settings(args) -> tuple[str | None, str | None, int]:
    """``(agent, model, timeout)`` for a local agent pass.

    Flag > profile > default, the same precedence as everything else. Local-agent
    settings live in the profile's ``[describe]`` block and are shared by every
    pass, so ``--intent`` and ``--guided`` can never drift onto different models.
    """
    settings = dict(getattr(_profile_or_none(args), "describe", None) or {})
    return (
        args.agent or settings.get("agent") or None,
        settings.get("model") or None,
        args.agent_timeout or int(settings.get("timeout", DEFAULT_ADAPT_TIMEOUT)),
    )


def _guide(dest: Path, template: sc.Template, args, intent: str | None,
           stubs: list[sc.Stub]) -> tuple[str, dict]:
    """Write the assignment handout and collect per-stub exercise prose.

    Returns ``(assignment_markdown, exercises)``; either may be empty. Best-effort
    throughout — a scaffold that produced stubs is already useful, so nothing here
    is allowed to fail the command.
    """
    from stingray_cli.agent import AgentError
    from stingray_cli.agent import run as run_agent

    assignment, exercises = "", {}

    if not args.no_ai and stubs:
        prompt = guided.assignment_prompt(
            project_name=args.name or dest.name,
            template_description=template.description,
            template_notes=template.notes,
            intent=intent,
            stubs=stubs,
            level=args.course_level,
            milestones=args.milestones,
        )
        if confirm(f"Let an agent write the assignment in {dest}?", assume_yes=args.yes):
            agent, model, timeout = _agent_settings(args)
            try:
                run_agent(prompt, dest, agent=agent, model=model,
                          timeout=timeout, edit=True)
            except AgentError as exc:
                print(f"warning: the assignment pass failed ({exc}); "
                      "writing a generated handout instead", file=sys.stderr)
            else:
                exercises = guided.read_exercises(dest)
                path = dest / guided.ASSIGNMENT_FILE
                if path.is_file():
                    try:
                        assignment = path.read_text(encoding="utf-8").strip()
                    except (OSError, UnicodeDecodeError) as exc:
                        print(f"warning: could not read {guided.ASSIGNMENT_FILE} ({exc})",
                              file=sys.stderr)

    if not assignment:
        assignment = guided.deterministic_assignment(
            project_name=args.name or dest.name,
            template_description=template.description,
            template_notes=template.notes,
            intent=intent,
            stubs=stubs,
            milestones=args.milestones,
        )

    if args.no_assignment:
        # The handout was still generated — the epic mirrors it — but the learner
        # asked not to have the file in their tree.
        (dest / guided.ASSIGNMENT_FILE).unlink(missing_ok=True)
        return assignment, exercises

    try:
        (dest / guided.ASSIGNMENT_FILE).write_text(assignment + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write {guided.ASSIGNMENT_FILE} ({exc})", file=sys.stderr)
        return assignment, exercises

    guided.ensure_gitignored(dest, guided.ASSIGNMENT_FILE)
    print(f"wrote {guided.ASSIGNMENT_FILE} (gitignored; also mirrored onto the epic)")
    return assignment, exercises


def _file_tickets(args, dest: Path, name: str, template: sc.Template,
                  stubs: list[sc.Stub], assignment: str = "",
                  exercises: dict | None = None) -> int:
    exercises = exercises or {}
    header = (
        # The handout is gitignored, so the epic is the copy that survives.
        [guided.epic_summary(assignment), "", "---", ""] if assignment else
        [f"Scaffolded from the `{template.name}` template.",
         template.notes or template.description, ""]
    )
    epic_description = "\n".join(header + [
        f"**{len(stubs)} stub(s) to implement**",
        "",
        stub_checklist(stubs),
        "",
        f"Each stub is marked `{sc.STUB_MARKER}:` in the source and has its own ticket.",
    ])

    epic_payload = build_payload(
        type="task",
        title=(f"{name}: guided project ({len(stubs)} stubs)" if assignment
               else f"{name}: scaffold ({len(stubs)} stubs)"),
        description=epic_description,
        priority=args.priority,
        tags=["scaffold"],
        root=dest,
        repo=name,
        assign=args.assign,
    )

    if args.dry_run:
        print(f"\n[dry-run] epic: {epic_payload['title']}")
        for stub in stubs:
            print(f"[dry-run]   {stub.path}:{stub.line} — {stub.summary}")
        return 0

    if not confirm(f"File 1 epic + {len(stubs)} stub ticket(s)?", assume_yes=args.yes):
        print("aborted; the scaffold is still on disk", file=sys.stderr)
        return 1

    client, profile = client_from(args)
    try:
        epic = client.create_ticket(**epic_payload)
    except requests.HTTPError as exc:
        resp = exc.response
        print(f"error: filing the epic failed "
              f"({resp.status_code if resp is not None else '?'})\n"
              f"{resp.text if resp is not None else exc}", file=sys.stderr)
        return 1
    epic_id = epic["id"]
    print(f"created epic #{epic_id}: {epic['title']}")

    epic_tag = stub_epic_tag(epic_id)
    filed: list[tuple[int, str]] = []
    for stub in stubs:
        title, body = guided.exercise_for(exercises, stub)
        payload = build_stub_payload(
            dest, name, stub,
            epic_id=epic_id,
            priority=args.priority,
            assign=args.assign,
            repo=name,
            body=body,
        )
        if title:
            payload["title"] = f"{name}: {title}"[:200]
        try:
            child = client.create_ticket(**payload)
        except requests.HTTPError as exc:
            print(f"warning: could not file a ticket for {stub.path}:{stub.line}: {exc}",
                  file=sys.stderr)
            continue
        filed.append((child["id"], child["title"]))
        print(f"  #{child['id']} {stub.path}:{stub.line}")

    # Link the children back, and tag the epic with its own id so one query
    # returns parent + children. (Note the server's tag filter is a substring
    # match, so `epic:4` also matches `epic:42` — filter client-side.)
    links = filed_checklist(filed)
    try:
        client.update_ticket(
            epic_id,
            description=epic_description + "\n\n**Filed tickets**\n\n" + links,
            tags=["scaffold", epic_tag, f"repo:{name}"],
        )
    except requests.HTTPError as exc:
        print(f"warning: could not link the children onto the epic: {exc}", file=sys.stderr)

    print(f"\n{profile.web_url}/tickets/{epic_id}")
    return 0
