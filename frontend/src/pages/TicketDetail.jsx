import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { PriorityBadge, StatusBadge, TypeBadge } from "../components/Badges";
import CodeBlockViewer from "../components/CodeBlockViewer";
import Markdown from "../components/Markdown";
import MarkdownEditor from "../components/MarkdownEditor";
import Tag from "../components/Tag";
import {
  AGENT_PHASE_LABELS,
  PRIORITIES,
  PRIORITY_LABELS,
  REPO_TAG_PREFIX,
  REVIEW_MARKER,
  STATUSES,
  STATUS_LABELS,
  TAG_AWAITING_FIX,
  describeActivity,
  formatDate,
  formatTokens,
  formatUsd,
  isReservedTag,
  totalTokens,
} from "../constants";
import { useNotifications } from "../notifications/NotificationsContext";
import styles from "../styles/TicketDetail.module.css";

export default function TicketDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { refresh: refreshNotifications } = useNotifications();

  const [ticket, setTicket] = useState(null);
  const [users, setUsers] = useState([]);
  const [comments, setComments] = useState([]);
  const [commentsTotal, setCommentsTotal] = useState(0);
  const [commentsOffset, setCommentsOffset] = useState(0);
  const [commentsLoading, setCommentsLoading] = useState(false);
  const [activity, setActivity] = useState([]);
  const [agentRuns, setAgentRuns] = useState([]);
  // Delegation cost rollup: this ticket's cost plus every child's. Only surfaced
  // when the ticket actually has delegated children.
  const [rollup, setRollup] = useState(null);
  const [commentBody, setCommentBody] = useState("");
  const [editingCommentId, setEditingCommentId] = useState(null);
  const [editingBody, setEditingBody] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [requestingFix, setRequestingFix] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const userName = (uid) => {
    const u = users.find((x) => x.id === uid);
    return u ? u.display_name : uid ? `#${uid}` : "Unassigned";
  };

  async function load() {
    setLoading(true);
    try {
      const [t, commentPage, a, runs, roll] = await Promise.all([
        api.getTicket(id),
        api.listComments(id, { limit: 10, offset: 0 }),
        api.listActivity(id),
        api.listAgentRuns(id),
        // Non-critical: a rollup failure must not blank the whole page.
        api.costRollup(id).catch(() => null),
      ]);
      setTicket(t);
      setComments(commentPage.items);
      setCommentsTotal(commentPage.total);
      setCommentsOffset(commentPage.items.length);
      setActivity(a);
      setAgentRuns(runs);
      setRollup(roll);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadMoreComments() {
    if (commentsLoading) return;
    setCommentsLoading(true);
    try {
      const page = await api.listComments(id, { limit: 10, offset: commentsOffset });
      setComments((prev) => {
        const seen = new Set(prev.map((c) => c.id));
        return [...prev, ...page.items.filter((c) => !seen.has(c.id))];
      });
      setCommentsTotal(page.total);
      setCommentsOffset((prev) => prev + page.items.length);
    } catch (e) {
      setError(e.message);
    } finally {
      setCommentsLoading(false);
    }
  }

  // Opening the ticket is the "read" gesture: clear its unread notifications so
  // the list-view dot goes away. Deliberately outside load()'s try/catch and not
  // awaited — it must never delay the page or surface as a page error.
  //
  // Best-effort in one more way: a comment that arrives between the fetch and
  // this call keeps its dot until the next 30s poll.
  async function markNotificationsRead() {
    try {
      await api.markTicketNotificationsRead(id);
      refreshNotifications();
    } catch {
      /* non-critical */
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
    markNotificationsRead();
    // Both callbacks close over `id` (refreshNotifications is stable from the
    // notifications context), so `id` really is the only dependency.
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
  // (claude:*/resolver:*, repo:*, parent:*, dangerous, fix, …) are read-only; the backend
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
      setError(
        "Reserved tags (resolver:*, repo:*, parent:*, dangerous, fix, delegate) can't be set here."
      );
      return;
    }
    saveFreeTags([...freeTags, ...parts]);
  }

  function removeTag(tag) {
    saveFreeTags(freeTags.filter((t) => t !== tag));
  }

  // --- resolver fix loop --------------------------------------------------
  // After the resolver posts a review it tags the ticket resolver:awaiting-fix and
  // hands it back. Commenting `/fix` and re-assigning it to the bot makes it apply
  // its own findings as a PR — this button is that pair of steps in one click. The
  // bot to hand it back to is whoever authored the review comment, so no extra
  // endpoint (or hardcoded bot id) is needed.
  const reviewComment = [...comments]
    .reverse()
    .find((c) => (c.body || "").includes(REVIEW_MARKER));
  const awaitingFix = (ticket?.tags ?? []).includes(TAG_AWAITING_FIX);
  const hasRepoTag = (ticket?.tags ?? []).some((t) => t.startsWith(REPO_TAG_PREFIX));
  const showFixButton = Boolean(canModify && awaitingFix && reviewComment);

  async function requestFix() {
    setError("");
    setRequestingFix(true);
    try {
      const c = await api.addComment(id, "/fix");
      setComments((prev) => [...prev, c]);
      setCommentsTotal((prev) => prev + 1);
      await patch({ assigned_to: reviewComment.author });
    } catch (e) {
      setError(e.message);
    } finally {
      setRequestingFix(false);
    }
  }

  async function submitComment(e) {
    e.preventDefault();
    if (!commentBody.trim()) return;
    try {
      const c = await api.addComment(id, commentBody.trim());
      setComments((prev) => [...prev, c]);
      setCommentsTotal((prev) => prev + 1);
      setCommentBody("");
      reloadActivity();
    } catch (e) {
      setError(e.message);
    }
  }

  // A comment is editable/deletable by its author or any admin — mirrors the
  // backend permission check in routers/comments.py.
  const canModifyComment = (c) =>
    user && (user.role === "admin" || user.id === c.author);

  function startEditComment(c) {
    setEditingCommentId(c.id);
    setEditingBody(c.body);
  }

  function cancelEditComment() {
    setEditingCommentId(null);
    setEditingBody("");
  }

  async function saveEditComment(commentId) {
    if (!editingBody.trim()) return;
    setError("");
    try {
      const updated = await api.editComment(id, commentId, editingBody.trim());
      setComments((prev) => prev.map((c) => (c.id === commentId ? updated : c)));
      cancelEditComment();
      reloadActivity();
    } catch (e) {
      setError(e.message);
    }
  }

  async function deleteComment(commentId) {
    if (!window.confirm("Delete this comment?")) return;
    setError("");
    try {
      await api.deleteComment(id, commentId);
      setComments((prev) => prev.filter((c) => c.id !== commentId));
      setCommentsTotal((prev) => prev - 1);
      setCommentsOffset((prev) => Math.max(0, prev - 1));
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
          {ticket.archived && <Tag muted>Archived</Tag>}
          {agentRuns.length > 0 && (
            <span
              className={styles.costBadge}
              title={`${formatTokens(
                agentRuns.reduce((sum, r) => sum + totalTokens(r), 0)
              )} tokens across ${agentRuns.length} agent run${
                agentRuns.length === 1 ? "" : "s"
              }`}
            >
              🤖 {formatUsd(agentRuns.reduce((sum, r) => sum + (r.cost_usd || 0), 0))}
            </span>
          )}
          {reservedTags.map((t) => (
            <Tag key={t}>{t}</Tag>
          ))}
          {freeTags.map((t) => (
            <Tag key={t} onRemove={canModify ? () => removeTag(t) : undefined}>
              {t}
            </Tag>
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
          <div className={styles.description}>
            <Markdown>{ticket.description}</Markdown>
          </div>
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
                  {canModifyComment(c) && editingCommentId !== c.id && (
                    <span className={styles.commentControls}>
                      <button type="button" className="link" onClick={() => startEditComment(c)}>
                        Edit
                      </button>
                      <button type="button" className="link" onClick={() => deleteComment(c.id)}>
                        Delete
                      </button>
                    </span>
                  )}
                </div>
                {editingCommentId === c.id ? (
                  <div className={styles.commentEdit}>
                    <MarkdownEditor value={editingBody} onChange={setEditingBody} />
                    <div className={styles.commentActions}>
                      <button
                        className="primary"
                        type="button"
                        disabled={!editingBody.trim()}
                        onClick={() => saveEditComment(c.id)}
                      >
                        Save
                      </button>
                      <button type="button" onClick={cancelEditComment}>
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <Markdown>{c.body}</Markdown>
                )}
              </div>
            ))}
          </div>
          {commentsOffset < commentsTotal && (
            <button
              type="button"
              className={styles.loadMore}
              onClick={loadMoreComments}
              disabled={commentsLoading}
            >
              {commentsLoading ? "Loading…" : "Load more comments"}
            </button>
          )}
          <form onSubmit={submitComment} className={styles.commentForm}>
            <MarkdownEditor
              value={commentBody}
              onChange={setCommentBody}
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

        <div className={styles.section}>
          <h2>Agent runs</h2>
          {agentRuns.length === 0 ? (
            <div className="muted">No agent runs yet.</div>
          ) : (
            <ul className={styles.runs}>
              {agentRuns.map((r) => (
                <li key={r.id} className={styles.runItem}>
                  <span className={styles.runPhase}>
                    {AGENT_PHASE_LABELS[r.phase] ?? r.phase}
                  </span>
                  <span
                    className={`${styles.runStatus} ${
                      r.status === "failed" ? styles.runFailed : styles.runOk
                    }`}
                  >
                    {r.status}
                  </span>
                  <span className={styles.runMeta}>
                    {r.agent}
                    {r.model ? ` · ${r.model}` : ""}
                  </span>
                  <span
                    className={styles.runTokens}
                    title={`in ${formatTokens(r.input_tokens)} · out ${formatTokens(
                      r.output_tokens
                    )} · cache read ${formatTokens(
                      r.cache_read_tokens
                    )} · cache write ${formatTokens(r.cache_write_tokens)}`}
                  >
                    {formatTokens(totalTokens(r))} tok
                  </span>
                  <span className={styles.runCost}>{formatUsd(r.cost_usd)}</span>
                  <span className={styles.runDate}>
                    {formatDate(r.started_at || r.finished_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {rollup && rollup.children.length > 0 && (
            <div className={styles.rollup}>
              <div className={styles.rollupHead}>
                Delegation total ({rollup.children.length} sub-task
                {rollup.children.length === 1 ? "" : "s"}):{" "}
                <strong>{formatUsd(rollup.total.cost_usd)}</strong>{" "}
                <span className="muted">
                  ({formatTokens(
                    rollup.total.input_tokens + rollup.total.output_tokens
                  )}{" "}
                  tok across {rollup.total.run_count} run
                  {rollup.total.run_count === 1 ? "" : "s"})
                </span>
              </div>
              <ul className={styles.runs}>
                {rollup.children.map((c) => (
                  <li key={c.ticket_id} className={styles.runItem}>
                    <Link to={`/tickets/${c.ticket_id}`} className={styles.runPhase}>
                      #{c.ticket_id}
                    </Link>
                    <span className={styles.runMeta}>{c.title}</span>
                    <span className={styles.runTokens}>
                      {formatTokens(
                        c.totals.input_tokens + c.totals.output_tokens
                      )}{" "}
                      tok
                    </span>
                    <span className={styles.runCost}>
                      {formatUsd(c.totals.cost_usd)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        {error && <div className="error">{error}</div>}
      </div>

      <aside className={styles.sidebar}>
        {showFixButton && (
          <div className="card">
            <div className="field">
              <label>Resolver review</label>
              <button
                type="button"
                className="primary"
                disabled={requestingFix || !hasRepoTag}
                title={
                  hasRepoTag
                    ? "Comment /fix and assign this back to the resolver so it applies its findings"
                    : "The resolver needs a repo:<name> tag to apply fixes"
                }
                onClick={requestFix}
              >
                {requestingFix ? "Requesting…" : "Apply fixes"}
              </button>
              <div className="muted">
                {hasRepoTag
                  ? "Hands this back to the resolver with /fix — it turns the review findings into a PR."
                  : "Add a repo:<name> tag before the resolver can apply these findings."}
              </div>
            </div>
          </div>
        )}
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
