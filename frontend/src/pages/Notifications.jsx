import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { formatDate } from "../constants";
import { useNotifications } from "../notifications/NotificationsContext";
import styles from "../styles/Notifications.module.css";

const PAGE_SIZE = 50;

const TYPE_LABELS = {
  assigned: "Assigned to you",
  commented: "New comment",
};

function describe(n) {
  switch (n.type) {
    case "assigned":
      return `${n.actor_name || "Someone"} assigned a ticket to you`;
    case "commented":
      return `${n.actor_name || "Someone"} commented on a ticket`;
    default:
      return n.type;
  }
}

export default function Notifications() {
  const { refresh } = useNotifications();
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState(() => new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.listNotifications({ limit: PAGE_SIZE, offset: 0 });
      setItems(res.items);
      setTotal(res.total);
      setSelected(new Set());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function toggle(id) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) =>
      prev.size === items.length ? new Set() : new Set(items.map((n) => n.id))
    );
  }

  async function markAllRead() {
    try {
      await api.markAllNotificationsRead();
      setItems((prev) => prev.map((n) => ({ ...n, read: true })));
      refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function deleteSelected() {
    if (selected.size === 0) return;
    try {
      await api.bulkDeleteNotifications([...selected]);
      await load();
      refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function deleteOne(id) {
    try {
      await api.deleteNotification(id);
      setItems((prev) => prev.filter((n) => n.id !== id));
      setTotal((t) => Math.max(0, t - 1));
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      refresh();
    } catch (e) {
      setError(e.message);
    }
  }

  async function onOpen(n) {
    if (n.read) return;
    try {
      await api.markNotificationRead(n.id);
      setItems((prev) => prev.map((x) => (x.id === n.id ? { ...x, read: true } : x)));
      refresh();
    } catch {
      // navigation still proceeds even if the mark-read call fails
    }
  }

  const allChecked = items.length > 0 && selected.size === items.length;

  return (
    <div>
      <div className={styles.head}>
        <h1>
          Notifications{" "}
          {!loading && <span className={styles.count}>({total})</span>}
        </h1>
        <div className={styles.actions}>
          <button onClick={markAllRead} disabled={items.length === 0}>
            Mark all read
          </button>
          <button
            className="danger"
            onClick={deleteSelected}
            disabled={selected.size === 0}
          >
            Delete selected{selected.size > 0 ? ` (${selected.size})` : ""}
          </button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {loading ? (
        <div className="muted">Loading…</div>
      ) : items.length === 0 ? (
        <div className={`card ${styles.empty}`}>You have no notifications.</div>
      ) : (
        <>
          <label className={styles.selectAll}>
            <input type="checkbox" checked={allChecked} onChange={toggleAll} />
            Select all
          </label>
          <div className={styles.list}>
            {items.map((n) => (
              <div
                key={n.id}
                className={`${styles.row} ${n.read ? "" : styles.unread}`}
              >
                <input
                  type="checkbox"
                  className={styles.check}
                  checked={selected.has(n.id)}
                  onChange={() => toggle(n.id)}
                />
                <div className={styles.body}>
                  <div className={styles.rowTitle}>
                    <span className={styles.type}>{TYPE_LABELS[n.type] ?? n.type}</span>
                    {!n.read && <span className={styles.dot} title="Unread" />}
                  </div>
                  <div className={styles.detail}>
                    {describe(n)}
                    {n.ticket_id != null && (
                      <>
                        {": "}
                        <Link to={`/tickets/${n.ticket_id}`} onClick={() => onOpen(n)}>
                          {n.ticket_title || `Ticket #${n.ticket_id}`}
                        </Link>
                      </>
                    )}
                  </div>
                  <div className={styles.date}>{formatDate(n.created_at)}</div>
                </div>
                <button className={styles.del} onClick={() => deleteOne(n.id)}>
                  Delete
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
