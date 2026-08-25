import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import Tag from "../components/Tag";
import styles from "../styles/Webhooks.module.css";

// Mirrors models.WebhookEventType — the event types events.emit actually writes.
const EVENT_TYPES = [
  { value: "ticket.created", label: "Ticket created" },
  { value: "ticket.assigned", label: "Ticket assigned" },
  { value: "ticket.status_changed", label: "Status changed" },
  { value: "ticket.tagged", label: "Tags changed" },
  { value: "comment.created", label: "Comment added" },
  { value: "agent_run.finished", label: "Agent run finished" },
];

const DELIVERY_STATES = ["pending", "delivering", "succeeded", "failed", "skipped"];

const PAGE_SIZE = 20;

function parseTags(input) {
  return input
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

function when(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

/**
 * One webhook's delivery log: the point of the feature.
 *
 * The backend filters these rows against the webhook *owner's* ticket
 * visibility, so a row missing here is a row the owner may not see — not a bug.
 */
function DeliveryLog({ webhookId }) {
  const [page, setPage] = useState({ items: [], total: 0, offset: 0 });
  const [state, setState] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(null);

  // `alive` guards against a state update after the row is collapsed or the
  // filter changes mid-flight (same shape as Settings.jsx).
  const load = useCallback(
    async (alive = () => true) => {
      setLoading(true);
      setError("");
      try {
        const res = await api.listWebhookDeliveries(webhookId, {
          state,
          limit: PAGE_SIZE,
          offset,
        });
        if (alive()) setPage(res);
      } catch (e) {
        if (alive()) setError(e.message);
      } finally {
        if (alive()) setLoading(false);
      }
    },
    [webhookId, state, offset]
  );

  useEffect(() => {
    let active = true;
    load(() => active);
    return () => {
      active = false;
    };
  }, [load]);

  async function redeliver(deliveryId) {
    setError("");
    try {
      await api.redeliverWebhookDelivery(webhookId, deliveryId);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className={styles.log}>
      <div className={styles.logHeader}>
        <label>
          State{" "}
          <select
            aria-label="Filter deliveries by state"
            value={state}
            onChange={(e) => {
              setOffset(0);
              setState(e.target.value);
            }}
          >
            <option value="">All</option>
            {DELIVERY_STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">{page.total} deliveries</span>
      </div>

      {error && <div className="error">{error}</div>}

      {loading ? (
        <div className="muted">Loading…</div>
      ) : page.items.length === 0 ? (
        <p className="muted">No deliveries yet.</p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>When</th>
              <th>Event</th>
              <th>Ticket</th>
              <th>State</th>
              <th>Status</th>
              <th>Attempts</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {page.items.map((d) => (
              <tr key={d.id}>
                <td>{when(d.created_at)}</td>
                <td>{d.event_type}</td>
                <td>
                  {d.ticket_id ? (
                    <Link to={`/tickets/${d.ticket_id}`}>#{d.ticket_id}</Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td>
                  <span className={`${styles.state} ${styles[d.state] || ""}`}>{d.state}</span>
                  {(d.error || d.response_snippet) && (
                    <button
                      type="button"
                      className={styles.linkBtn}
                      aria-expanded={expanded === d.id}
                      onClick={() => setExpanded(expanded === d.id ? null : d.id)}
                    >
                      {expanded === d.id ? "hide" : "details"}
                    </button>
                  )}
                  {expanded === d.id && (
                    <pre className={styles.snippet}>{d.error || d.response_snippet}</pre>
                  )}
                </td>
                <td>{d.status_code ?? "—"}</td>
                <td>{d.attempt_count}</td>
                <td>
                  <button type="button" onClick={() => redeliver(d.id)}>
                    Redeliver
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className={styles.pager}>
        <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          Previous
        </button>
        <button
          type="button"
          disabled={offset + PAGE_SIZE >= page.total}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </div>
    </div>
  );
}

export default function Webhooks() {
  const [hooks, setHooks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [openLog, setOpenLog] = useState(null);

  // The plaintext secret, held in local state only. It is never re-fetched —
  // the API returns it exactly once — so dismissing this panel is final.
  const [newSecret, setNewSecret] = useState(null);

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [eventTypes, setEventTypes] = useState([]);
  const [tagInput, setTagInput] = useState("");
  const [active, setActive] = useState(true);

  async function refresh() {
    const items = await api.listWebhooks();
    setHooks(items);
  }

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const items = await api.listWebhooks();
        if (alive) setHooks(items);
      } catch (e) {
        if (alive) setError(e.message);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  function toggleEvent(value) {
    setEventTypes((prev) =>
      prev.includes(value) ? prev.filter((v) => v !== value) : [...prev, value]
    );
  }

  async function create(e) {
    e.preventDefault();
    setSaving(true);
    setFormError("");
    try {
      const created = await api.createWebhook({
        name,
        url,
        event_types: eventTypes,
        tag_filter: parseTags(tagInput),
        active,
      });
      // Keep the plaintext out of the list state: `hooks` is re-fetched from
      // read endpoints, which never carry it.
      setNewSecret({ id: created.id, name: created.name, secret: created.secret });
      setName("");
      setUrl("");
      setEventTypes([]);
      setTagInput("");
      setActive(true);
      await refresh();
    } catch (e2) {
      // The 422 from the SSRF check names the exact reason; show it verbatim.
      setFormError(e2.message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleActive(hook) {
    setError("");
    try {
      await api.updateWebhook(hook.id, { active: !hook.active });
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function rotate(hook) {
    setError("");
    try {
      const res = await api.rotateWebhookSecret(hook.id);
      setNewSecret({ id: hook.id, name: hook.name, secret: res.secret });
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function remove(hook) {
    setError("");
    try {
      await api.deleteWebhook(hook.id);
      if (openLog === hook.id) setOpenLog(null);
      await refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className={styles.wrap}>
      <h1>Webhooks</h1>
      <p className="muted" style={{ marginTop: 0 }}>
        Send ticket events to an HTTPS endpoint. Deliveries are filtered to the
        tickets you can see, and each request is signed with the webhook’s secret.
      </p>

      {error && <div className="error">{error}</div>}

      {newSecret && (
        <div className={`card ${styles.secretPanel}`} role="alert">
          <h2 className={styles.h2}>Signing secret for “{newSecret.name}”</h2>
          <p className={styles.warn}>
            This is shown <strong>once</strong>. Copy it now — it cannot be
            retrieved again, only replaced by rotating.
          </p>
          <div className={styles.secretRow}>
            <code className={styles.secret}>{newSecret.secret}</code>
            <button
              type="button"
              onClick={() => navigator.clipboard?.writeText(newSecret.secret)}
            >
              Copy
            </button>
            <button type="button" onClick={() => setNewSecret(null)}>
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="card">
        <h2 className={styles.h2}>Add a webhook</h2>
        {formError && <div className="error">{formError}</div>}
        <form onSubmit={create} className={styles.form}>
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label>
            URL
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://example.com/hooks/stingray"
              required
            />
          </label>
          <fieldset className={styles.events}>
            <legend>Events (none selected = all)</legend>
            {EVENT_TYPES.map((t) => (
              <label key={t.value} className={styles.check}>
                <input
                  type="checkbox"
                  checked={eventTypes.includes(t.value)}
                  onChange={() => toggleEvent(t.value)}
                />
                {t.label}
              </label>
            ))}
          </fieldset>
          <label>
            Tag filter (comma-separated; blank = every ticket)
            <input
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              placeholder="repo:my-app, backend"
            />
          </label>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={active}
              onChange={(e) => setActive(e.target.checked)}
            />
            Active
          </label>
          <div>
            <button className="primary" type="submit" disabled={saving}>
              {saving ? "Creating…" : "Create webhook"}
            </button>
          </div>
        </form>
      </div>

      <div className="card">
        <h2 className={styles.h2}>Your webhooks</h2>
        {loading ? (
          <div className="muted">Loading…</div>
        ) : hooks.length === 0 ? (
          <p className="muted">No webhooks yet.</p>
        ) : (
          hooks.map((hook) => (
            <div key={hook.id} className={styles.hook}>
              <div className={styles.hookHead}>
                <div>
                  <strong>{hook.name}</strong>{" "}
                  {!hook.active && <span className={styles.off}>paused</span>}
                  {hook.consecutive_failures > 0 && (
                    <span className={styles.failures}>
                      {hook.consecutive_failures} consecutive failures
                    </span>
                  )}
                  <div className={styles.url}>{hook.url}</div>
                  <div className={styles.meta}>
                    secret {hook.secret_prefix}…
                  </div>
                </div>
                <div className={styles.hookActions}>
                  <button type="button" onClick={() => toggleActive(hook)}>
                    {hook.active ? "Pause" : "Resume"}
                  </button>
                  <button type="button" onClick={() => rotate(hook)}>
                    Rotate secret
                  </button>
                  <button type="button" onClick={() => remove(hook)}>
                    Delete
                  </button>
                </div>
              </div>

              <div className={styles.chips}>
                {(hook.event_types.length ? hook.event_types : ["all events"]).map((e) => (
                  <span key={e} className={styles.chip}>
                    {e}
                  </span>
                ))}
                {hook.tag_filter.map((t) => (
                  <Tag key={t}>{t}</Tag>
                ))}
              </div>

              <button
                type="button"
                className={styles.linkBtn}
                aria-expanded={openLog === hook.id}
                onClick={() => setOpenLog(openLog === hook.id ? null : hook.id)}
              >
                {openLog === hook.id ? "▾" : "▸"} Delivery log
              </button>
              {openLog === hook.id && <DeliveryLog webhookId={hook.id} />}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
