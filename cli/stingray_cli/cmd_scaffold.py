"""``stingray scaffold`` — generate a project outline with a ready-made backlog."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import requests

from stingray_cli import scaffold as sc
from stingray_cli.common import client_from, confirm
from stingray_cli.config import ConfigError
from stingray_client.tickets import build_payload

# Bound how many tickets one scaffold can file, so an enthusiastic AI pass can't
# dump 200 tickets into the tracker.
MAX_STUB_TICKETS = 30


def add_parser(sub, add_connection_flags) -> None:
    parser = sub.add_parser(
        "scaffold",
        help="generate a project outline plus a ticket per stub",
        description=(
            "Renders a template, optionally adapts it with a local agent, commits "
            "it, then files one ticket per STINGRAY-STUB marker plus an epic that "
            "tracks them."
        ),
    )
    parser.add_argument("template", nargs="?", help="template name")
    parser.add_argument("dest", nargs="?", help="target directory")
    parser.add_argument("--name", help="project name (default: the directory name)")
    parser.add_argument("--describe", metavar="TEXT",
                        help="what the project should do; drives the AI adaptation pass")
    parser.add_argument("--list-templates", action="store_true")
    parser.add_argument("--no-ai", action="store_true", help="template only, no agent pass")
    parser.add_argument("--agent", metavar="NAME", help="agent to adapt with (claude|opencode)")
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
        "description": args.describe or f"The {name} project.",
    }

    # Render into a temp dir first: an agent pass that produces a broken tree
    # should never leave a half-written project behind.
    staging = Path(tempfile.mkdtemp(prefix="stingray-scaffold-"))
    try:
        work = staging / "tree"
        work.mkdir()
        sc.render(template, work, variables)

        if not args.no_ai and args.describe:
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

    return _file_tickets(args, dest, name, template, stubs)


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
        f"Intent: {args.describe}",
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

    try:
        run_agent(prompt, work, agent=args.agent, timeout=600, edit=True)
        print("agent adapted the scaffold")
    except AgentError as exc:
        print(f"warning: adaptation pass failed ({exc}); using the plain template",
              file=sys.stderr)


def _stub_block(dest: Path, stub: sc.Stub) -> dict | None:
    from stingray_cli.gitctx import language_for
    try:
        lines = (dest / stub.path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    start = max(1, stub.block_start)
    end = min(stub.block_end, len(lines))
    if end < start:
        return None
    return {
        "filename": stub.path,
        "language": language_for(stub.path),
        "line_start": start,
        "line_end": end,
        "content": "\n".join(lines[start - 1:end]),
    }


def _file_tickets(args, dest: Path, name: str, template: sc.Template,
                  stubs: list[sc.Stub]) -> int:
    checklist = "\n".join(
        f"- [ ] `{s.path}:{s.line}` — {s.summary}" for s in stubs
    )
    epic_description = "\n".join([
        f"Scaffolded from the `{template.name}` template.",
        template.notes or template.description,
        "",
        f"**{len(stubs)} stub(s) to implement**",
        "",
        checklist,
        "",
        f"Each stub is marked `{sc.STUB_MARKER}:` in the source and has its own ticket.",
    ])

    epic_payload = build_payload(
        type="task",
        title=f"{name}: scaffold ({len(stubs)} stubs)",
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

    # `epic:<id>` is a FREE tag on purpose. The reserved `parent:<id>` would make
    # each child self-driving (the resolver auto-approves a child's plan and goes
    # straight to implement) — wrong for a backlog meant to be filled in by hand.
    epic_tag = f"epic:{epic_id}"
    filed: list[tuple[int, str]] = []
    for stub in stubs:
        block = _stub_block(dest, stub)
        description = [f"Implement the `{sc.STUB_MARKER}` at `{stub.path}:{stub.line}`.", ""]
        description.append(stub.summary)
        if stub.acceptance:
            description += ["", f"**Acceptance:** {stub.acceptance}"]
        description += ["", f"Part of epic #{epic_id}."]

        payload = build_payload(
            type="code_review" if block else "task",
            title=f"{name}: {stub.summary}"[:200],
            description="\n".join(description),
            priority=args.priority,
            tags=["scaffold", "stub", epic_tag],
            code_blocks=[block] if block else [],
            root=dest,
            repo=name,
            assign=args.assign,
        )
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
    links = "\n".join(f"- [ ] #{tid} {title}" for tid, title in filed)
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
