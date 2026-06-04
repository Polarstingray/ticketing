import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import {
  PRIORITIES,
  PRIORITY_LABELS,
  TYPES,
  TYPE_LABELS,
} from "../constants";
import styles from "../styles/Form.module.css";

const emptyBlock = () => ({
  filename: "",
  language: "plaintext",
  line_start: 1,
  line_end: 1,
  content: "",
});

export default function TicketNew() {
  const navigate = useNavigate();
  const [users, setUsers] = useState([]);
  const [type, setType] = useState("task");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [assignedTo, setAssignedTo] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [tags, setTags] = useState("");
  const [codeBlocks, setCodeBlocks] = useState([emptyBlock()]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  function updateBlock(i, key, value) {
    setCodeBlocks((blocks) =>
      blocks.map((b, idx) => (idx === i ? { ...b, [key]: value } : b))
    );
  }

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const body = {
        type,
        title,
        description,
        priority,
        assigned_to: assignedTo ? Number(assignedTo) : null,
        due_date: dueDate ? new Date(dueDate).toISOString() : null,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      };
      if (type === "code_review") {
        body.code_blocks = codeBlocks
          .filter((b) => b.filename.trim() || b.content.trim())
          .map((b) => ({
            filename: b.filename,
            language: b.language || "plaintext",
            line_start: Number(b.line_start) || 1,
            line_end: Number(b.line_end) || Number(b.line_start) || 1,
            content: b.content,
          }));
      }
      const created = await api.createTicket(body);
      navigate(`/tickets/${created.id}`);
    } catch (err) {
      setError(err.message || "Failed to create ticket");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.formWrap}>
      <h1>New ticket</h1>
      <form onSubmit={onSubmit} className="card">
        <div className="field">
          <label>Type</label>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {TYPE_LABELS[t]}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Title</label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} required />
        </div>

        <div className="field">
          <label>Description</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={
              type === "code_review"
                ? "What should the reviewer focus on?"
                : "Describe the task…"
            }
          />
        </div>

        <div className={styles.grid2}>
          <div className="field">
            <label>Priority</label>
            <select value={priority} onChange={(e) => setPriority(e.target.value)}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {PRIORITY_LABELS[p]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Assignee</label>
            <select value={assignedTo} onChange={(e) => setAssignedTo(e.target.value)}>
              <option value="">Unassigned</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.display_name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className={styles.grid2}>
          <div className="field">
            <label>Due date</label>
            <input
              type="datetime-local"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Tags (comma-separated)</label>
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="backend, auth"
            />
          </div>
        </div>

        {type === "code_review" && (
          <div className={styles.blocks}>
            <div className={styles.blocksHead}>
              <label style={{ margin: 0 }}>Code blocks</label>
              <button
                type="button"
                onClick={() => setCodeBlocks((b) => [...b, emptyBlock()])}
              >
                + Add block
              </button>
            </div>
            {codeBlocks.map((b, i) => (
              <div key={i} className={styles.block}>
                <div className={styles.blockTop}>
                  <input
                    placeholder="filename (e.g. src/auth.py)"
                    value={b.filename}
                    onChange={(e) => updateBlock(i, "filename", e.target.value)}
                  />
                  <input
                    placeholder="language"
                    value={b.language}
                    onChange={(e) => updateBlock(i, "language", e.target.value)}
                    style={{ maxWidth: 130 }}
                  />
                  <input
                    type="number"
                    min="1"
                    placeholder="start"
                    value={b.line_start}
                    onChange={(e) => updateBlock(i, "line_start", e.target.value)}
                    style={{ maxWidth: 90 }}
                  />
                  <input
                    type="number"
                    min="1"
                    placeholder="end"
                    value={b.line_end}
                    onChange={(e) => updateBlock(i, "line_end", e.target.value)}
                    style={{ maxWidth: 90 }}
                  />
                  {codeBlocks.length > 1 && (
                    <button
                      type="button"
                      className="danger"
                      onClick={() =>
                        setCodeBlocks((blocks) => blocks.filter((_, idx) => idx !== i))
                      }
                    >
                      Remove
                    </button>
                  )}
                </div>
                <textarea
                  className={styles.mono}
                  placeholder="Paste code here…"
                  value={b.content}
                  onChange={(e) => updateBlock(i, "content", e.target.value)}
                />
              </div>
            ))}
          </div>
        )}

        {error && <div className="error">{error}</div>}
        <div className={styles.actions}>
          <button type="button" onClick={() => navigate("/tickets")}>
            Cancel
          </button>
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create ticket"}
          </button>
        </div>
      </form>
    </div>
  );
}
