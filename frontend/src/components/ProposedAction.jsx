import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import styles from "../styles/ChatWidget.module.css";

// A card the assistant put up, and the user decides on. The assistant cannot
// perform any of these itself — Confirm calls the same endpoints the rest of the
// app calls, as the signed-in user, so this feature adds no write path and no
// second copy of the authorization rules.
//
// Nothing here goes through Markdown. The assistant's prose does, by design, but
// a proposal card is the one surface where text that may have been injected into
// a ticket becomes *a button the user is being asked to click* — so it renders as
// plain data, and cannot borrow the app's own voice with bold text or a link.

const LABELS = {
  create_ticket: "File a ticket",
  add_comment: "Post a comment",
  set_status: "Change the status",
  request_fix: "Ask the resolver to apply its findings",
};

function Summary({ kind, payload }) {
  if (kind === "create_ticket") {
    return (
      <>
        <div className={styles.proposalTitle}>{payload.title}</div>
        <div className={styles.proposalFields}>
          {payload.type} · {payload.priority}
          {payload.tags?.length ? ` · ${payload.tags.join(", ")}` : ""}
        </div>
        {payload.description && (
          <div className={styles.proposalBody}>{payload.description}</div>
        )}
      </>
    );
  }
  if (kind === "add_comment") {
    return (
      <>
        <div className={styles.proposalFields}>on #{payload.ticket_id}</div>
        <div className={styles.proposalBody}>{payload.body}</div>
      </>
    );
  }
  if (kind === "set_status") {
    return (
      <div className={styles.proposalFields}>
        #{payload.ticket_id} → {payload.status}
      </div>
    );
  }
  return <div className={styles.proposalFields}>on #{payload.ticket_id}</div>;
}

export default function ProposedAction({ proposal }) {
  const { kind, payload, rationale } = proposal;
  const [state, setState] = useState("idle");
  const [error, setError] = useState("");
  const [created, setCreated] = useState(null);

  if (state === "dismissed") return null;

  async function confirm() {
    setError("");
    setState("working");
    try {
      if (kind === "create_ticket") {
        const ticket = await api.createTicket(payload);
        setCreated(ticket.id);
      } else if (kind === "add_comment") {
        await api.addComment(payload.ticket_id, payload.body);
      } else if (kind === "set_status") {
        await api.updateTicket(payload.ticket_id, { status: payload.status });
      }
      setState("done");
    } catch (err) {
      setError(err.message);
      setState("idle");
    }
  }

  // `request_fix` is a link rather than a button. Doing it properly needs the
  // author of the review comment plus the awaiting-fix/repo-tag guards, all of
  // which the "Apply fixes" button on the ticket page already has — sending the
  // user there costs one click and duplicates none of it.
  const isNavigation = kind === "request_fix";

  return (
    <div className={styles.proposal}>
      <div className={styles.proposalKind}>{LABELS[kind] || kind}</div>
      <Summary kind={kind} payload={payload} />
      {rationale && <div className={styles.proposalWhy}>{rationale}</div>}

      {state === "done" ? (
        <div className={styles.proposalDone}>
          {created ? <Link to={`/tickets/${created}`}>Filed #{created}</Link> : "Done."}
        </div>
      ) : (
        <div className={styles.proposalActions}>
          {isNavigation ? (
            <Link className={styles.proposalLink} to={`/tickets/${payload.ticket_id}`}>
              Open ticket #{payload.ticket_id}
            </Link>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={state === "working"}
              onClick={confirm}
            >
              {state === "working" ? "Working…" : "Confirm"}
            </button>
          )}
          <button type="button" onClick={() => setState("dismissed")}>
            Dismiss
          </button>
        </div>
      )}
      {error && <div className={styles.proposalError}>{error}</div>}
    </div>
  );
}
