import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { PriorityBadge, StatusBadge, TypeBadge } from "../components/Badges";
import CodeBlockViewer from "../components/CodeBlockViewer";
import {
  PRIORITIES,
  PRIORITY_LABELS,
  STATUSES,
  STATUS_LABELS,
  describeActivity,
  formatDate,
  isReservedTag,
} from "../constants";
import styles from "../styles/TicketDetail.module.css";

export default function TicketDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();

  const [ticket, setTicket] = useState(null);
  const [users, setUsers] = useState([]);
  const [comments, setComments] = useState([]);
  const [activity, setActivity] = useState([]);
  const [commentBody, setCommentBody] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const userName = (uid) => {
    const u = users.find((x) => x.id === uid);
    return u ? u.display_name : uid ? `#${uid}` : "Unassigned";
  };

  async function load() {
    setLoading(true);
    try {
      const [t, c, a] = await Promise.all([
        api.getTicket(id),
        api.listComments(id),
        api.listActivity(id),
      ]);
      setTicket(t);
      setComments(c);
      setActivity(a);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function reloadActivity() {
    try {
      setActivity(await api.listActivity(id));
    } catch {
      /* non-critical */
    }
  }

  useEffect(() => {
    api
      .listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const canModify =
    ticket &&
    user &&
    (user.role === "admin" ||
      user.id === ticket.created_by ||
      user.id === ticket.assigned_to);
  const canDelete = user && user.role === "admin";

  async function patch(changes) {
    setError("");
    try {
      const updated = await api.updateTicket(id, changes);
      setTicket(updated);
      reloadActivity();
    } catch (e) {
      setError(e.message);
    }
  }

  // Only free (non-reserved) tags are user-editable. Reserved control tags
  // (claude:*, repo:*, dangerous, fix) are shown read-only; the backend
  // preserves them and rejects any attempt to set them, so we patch with the
  // free tags only.
  const freeTags = (ticket?.tags ?? []).filter((t) => !isReservedTag(t));
  const reservedTags = (ticket?.tags ?? []).filter(isReservedTag);

  function saveFreeTags(next) {
    // Dedupe while preserving order.
    const deduped = [...new Set(next)];
    patch({ tags: deduped });
  }

  function addTagsFromInput() {
    const parts = tagInput
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    setTagInput("");
    if (parts.length === 0) return;
    if (parts.some(isReservedTag)) {
      setError("Reserved tags (claude:*, repo:*, dangerous, fix) can't be set here.");
      return;
    }
    saveFreeTags([...freeTags, ...parts]);
  }

  function removeTag(tag) {
    saveFreeTags(freeTags.filter((t) => t !== tag));
  }

  async function submitComment(e) {
    e.preventDefault();
    if (!commentBody.trim()) return;
    try {
      const c = await api.addComment(id, commentBody.trim());
      setComments((prev) => [...prev, c]);
      setCommentBody("");
      reloadActivity();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleDelete() {
    if (!window.confirm("Delete this ticket permanently?")) return;
    try {
      await api.deleteTicket(id);
      navigate("/tickets");
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleArchive() {
    setError("");
    try {
      const updated = await api.archiveTicket(id);
      setTicket(updated);
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleUnarchive() {
    setError("");
    try {
      const updated = await api.unarchiveTicket(id);
      setTicket(updated);
    } catch (e) {
      setError(e.message);
    }
  }

  if (loading) return <div className="muted">Loading…</div>;
  if (!ticket) return <div className="error">{error || "Ticket not found"}</div>;

  return (
    <div className={styles.layout}>
      <div className={styles.main}>
        <div className={styles.titleRow}>
          <h1>
            <span className={styles.id}>#{ticket.id}</span> {ticket.title}
          </h1>
        </div>
        <div className={styles.badges}>
          <TypeBadge type={ticket.type} />
          <StatusBadge status={ticket.status} />
          <PriorityBadge priority={ticket.priority} />
          {ticket.archived && <span className={styles.tag}>Archived</span>}
          {reservedTags.map((t) => (
            <span key={t} className={styles.systemTag} title="System tag — managed by automation">
              {t}
            </span>
          ))}
          {canModify
            ? freeTags.map((t) => (
                <span key={t} className={styles.editableTag}>
                  {t}
                  <button
                    type="button"
                    className={styles.tagRemove}
                    aria-label={`Remove tag ${t}`}
                    onClick={() => removeTag(t)}
                  >
                    ×
                  </button>
                </span>
              ))
            : freeTags.map((t) => (
                <span key={t} className={styles.tag}>
                  {t}
                </span>
              ))}
          {canModify && (
            <input
              className={styles.tagInput}
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === ",") {
                  e.preventDefault();
                  addTagsFromInput();
                }
              }}
              onBlur={addTagsFromInput}
              placeholder="Add tag…"
            />
          )}
        </div>

        {ticket.description && (
          <div className={styles.description}>{ticket.description}</div>
        )}

        {ticket.type === "code_review" && ticket.code_blocks?.length > 0 && (
          <div className={styles.section}>
            <h2>Code</h2>
            {ticket.code_blocks.map((b, i) => (
              <CodeBlockViewer key={i} block={b} />
            ))}
          </div>
        )}

        <div className={styles.section}>
          <h2>Comments</h2>
          {comments.length === 0 && <div className="muted">No comments yet.</div>}
          <div className={styles.comments}>
            {comments.map((c) => (
              <div key={c.id} className={styles.comment}>
                <div className={styles.commentHead}>
                  <span className={styles.commentAuthor}>{userName(c.author)}</span>
                  <span className={styles.commentDate}>{formatDate(c.created_at)}</span>
                </div>
                <div className={styles.commentBody}>{c.body}</div>
              </div>
            ))}
          </div>
          <form onSubmit={submitComment} className={styles.commentForm}>
            <textarea
              value={commentBody}
              onChange={(e) => setCommentBody(e.target.value)}
              placeholder="Add a comment…"
            />
            <div className={styles.commentActions}>
              <button className="primary" type="submit" disabled={!commentBody.trim()}>
                Comment
              </button>
            </div>
          </form>
        </div>

        <div className={styles.section}>
          <h2>Activity</h2>
          {activity.length === 0 ? (
            <div className="muted">No activity yet.</div>
          ) : (
            <ul className={styles.activity}>
              {activity.map((a) => (
                <li key={a.id} className={styles.activityItem}>
                  <span className={styles.activityDot} />
                  <span className={styles.activityText}>
                    <strong>{userName(a.actor)}</strong> {describeActivity(a)}
                  </span>
                  <span className={styles.activityDate}>{formatDate(a.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {error && <div className="error">{error}</div>}
      </div>

      <aside className={styles.sidebar}>
        <div className="card">
          <div className="field">
            <label>Status</label>
            <select
              value={ticket.status}
              disabled={!canModify}
              onChange={(e) => patch({ status: e.target.value })}
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label>Priority</label>
            <select
              value={ticket.priority}
              disabled={!canModify}
              onChange={(e) => patch({ priority: e.target.value })}
            >
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {PRIORITY_LABELS[p]}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label>Assignee</label>
            <select
              value={ticket.assigned_to ?? ""}
              disabled={!canModify}
              onChange={(e) =>
                patch({ assigned_to: e.target.value ? Number(e.target.value) : null })
              }
            >
              <option value="">Unassigned</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.display_name}
                </option>
              ))}
            </select>
          </div>

          <dl className={styles.meta}>
            <dt>Reporter</dt>
            <dd>{userName(ticket.created_by)}</dd>
            <dt>Created</dt>
            <dd>{formatDate(ticket.created_at)}</dd>
            <dt>Updated</dt>
            <dd>{formatDate(ticket.updated_at)}</dd>
            <dt>Due</dt>
            <dd>{formatDate(ticket.due_date)}</dd>
          </dl>

          {!canModify && (
            <div className="muted" style={{ fontSize: 12 }}>
              You can comment but not edit this ticket.
            </div>
          )}
          {canModify && ticket.status === "closed" && !ticket.archived && (
            <button style={{ width: "100%", marginTop: 12 }} onClick={handleArchive}>
              Archive ticket
            </button>
          )}
          {canModify && ticket.archived && (
            <button style={{ width: "100%", marginTop: 12 }} onClick={handleUnarchive}>
              Unarchive ticket
            </button>
          )}
          {canDelete && (
            <button className="danger" style={{ width: "100%", marginTop: 12 }} onClick={handleDelete}>
              Delete ticket
            </button>
          )}
        </div>
      </aside>
    </div>
  );
}
