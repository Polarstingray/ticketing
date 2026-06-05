"""Email notifications.

Best-effort and fully optional: if SMTP isn't configured the helpers no-op (with a
log line) and never raise into the request path. Sends are dispatched through
FastAPI's BackgroundTasks so the HTTP response isn't blocked on the mail server.
"""
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Iterable, List

from sqlalchemy.orm import Session

from models import Ticket, User, UserRole

log = logging.getLogger("stingray.notifications")


# --- Config ------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USERNAME = _env("SMTP_USERNAME")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM", "stingray@localhost")
SMTP_STARTTLS = _env("SMTP_STARTTLS", "true").lower() == "true"
SMTP_SSL = _env("SMTP_SSL", "false").lower() == "true"
# Used to build clickable ticket links in emails. No trailing slash.
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "").rstrip("/")


def email_enabled() -> bool:
    return bool(SMTP_HOST)


# --- Low-level send ----------------------------------------------------------

def send_email(to: List[str], subject: str, body: str) -> None:
    """Send a plain-text email. Swallows all errors (logs them)."""
    recipients = [addr for addr in to if addr]
    if not recipients:
        return
    if not email_enabled():
        log.info("email disabled (SMTP_HOST unset); would have emailed %s: %s", recipients, subject)
        return

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if SMTP_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        with server:
            if SMTP_STARTTLS and not SMTP_SSL:
                server.starttls()
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        log.info("sent email to %s: %s", recipients, subject)
    except Exception:  # noqa: BLE001 — notifications must never break a request
        log.exception("failed to send email to %s: %s", recipients, subject)


def _ticket_link(ticket: Ticket) -> str:
    if PUBLIC_BASE_URL:
        return f"\n\nView it: {PUBLIC_BASE_URL}/tickets/{ticket.id}"
    return ""


# --- High-level notifications ------------------------------------------------

def notify_assignment(background, ticket: Ticket, assignee: User, actor: User) -> None:
    """Email the assignee that a ticket was assigned to them."""
    if assignee is None or actor is not None and assignee.id == actor.id:
        return
    actor_name = actor.display_name if actor else "Someone"
    subject = f"You were assigned ticket #{ticket.id}: {ticket.title}"
    body = (
        f"Hi {assignee.display_name},\n\n"
        f"{actor_name} assigned ticket #{ticket.id} ({ticket.type}) to you:\n\n"
        f"  {ticket.title}\n"
        f"  priority: {ticket.priority}  status: {ticket.status}"
        f"{_ticket_link(ticket)}\n"
    )
    background.add_task(send_email, [assignee.email], subject, body)


def notify_new_ticket_admins(background, db: Session, ticket: Ticket, actor: User) -> None:
    """Email all admins (except the actor) that a new ticket was filed."""
    admins: Iterable[User] = (
        db.query(User).filter(User.role == UserRole.admin.value).all()
    )
    actor_id = actor.id if actor else None
    recipients = [a.email for a in admins if a.id != actor_id and a.email]
    if not recipients:
        return
    actor_name = actor.display_name if actor else "Someone"
    subject = f"New ticket #{ticket.id} filed: {ticket.title}"
    body = (
        f"{actor_name} filed a new {ticket.type} ticket:\n\n"
        f"  #{ticket.id} {ticket.title}\n"
        f"  priority: {ticket.priority}  status: {ticket.status}"
        f"{_ticket_link(ticket)}\n"
    )
    background.add_task(send_email, recipients, subject, body)
