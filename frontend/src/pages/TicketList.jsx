import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { PriorityBadge, StatusBadge, TypeBadge } from "../components/Badges";
import {
  PRIORITIES,
  PRIORITY_LABELS,
  STATUSES,
  STATUS_LABELS,
  TYPES,
  TYPE_LABELS,
  formatDate,
} from "../constants";
import styles from "../styles/TicketList.module.css";

const PAGE_SIZE = 50;

export default function TicketList() {
  const [tickets, setTickets] = useState([]);
  const [total, setTotal] = useState(0);
  const [users, setUsers] = useState([]);
  const [filters, setFilters] = useState({
    status: "",
    type: "",
    assigned_to: "",
    priority: "",
    archived: "",
  });
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  // Users list is best-effort (only admins can fetch it); used to label assignees
  // and populate the assignee filter.
  useEffect(() => {
    api
      .listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  // (Re)load the first page whenever filters change.
  useEffect(() => {
    setLoading(true);
    api
      .listTickets({ ...filters, limit: PAGE_SIZE, offset: 0 })
      .then((res) => {
        setTickets(res.items);
        setTotal(res.total);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters]);

  function loadMore() {
    setLoadingMore(true);
    api
      .listTickets({ ...filters, limit: PAGE_SIZE, offset: tickets.length })
      .then((res) => {
        setTickets((prev) => [...prev, ...res.items]);
        setTotal(res.total);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoadingMore(false));
  }

  const userName = (id) => {
    const u = users.find((x) => x.id === id);
    return u ? u.display_name : id ? `#${id}` : "Unassigned";
  };

  function setFilter(key, value) {
    setFilters((f) => ({ ...f, [key]: value }));
  }

  return (
    <div>
      <div className={styles.head}>
        <h1>
          Tickets {!loading && <span className={styles.count}>({total})</span>}
        </h1>
        <Link to="/tickets/new">
          <button className="primary">New ticket</button>
        </Link>
      </div>

      <div className={styles.filters}>
        <select value={filters.type} onChange={(e) => setFilter("type", e.target.value)}>
          <option value="">All types</option>
          {TYPES.map((t) => (
            <option key={t} value={t}>
              {TYPE_LABELS[t]}
            </option>
          ))}
        </select>
        <select value={filters.status} onChange={(e) => setFilter("status", e.target.value)}>
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABELS[s]}
            </option>
          ))}
        </select>
        <select
          value={filters.priority}
          onChange={(e) => setFilter("priority", e.target.value)}
        >
          <option value="">All priorities</option>
          {PRIORITIES.map((p) => (
            <option key={p} value={p}>
              {PRIORITY_LABELS[p]}
            </option>
          ))}
        </select>
        <select
          value={filters.assigned_to}
          onChange={(e) => setFilter("assigned_to", e.target.value)}
        >
          <option value="">Any assignee</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.display_name}
            </option>
          ))}
        </select>
        <label className={styles.archivedToggle}>
          <input
            type="checkbox"
            checked={filters.archived === "true"}
            onChange={(e) => setFilter("archived", e.target.checked ? "true" : "")}
          />
          Show archived
        </label>
        <button
          onClick={() =>
            setFilters({
              status: "",
              type: "",
              assigned_to: "",
              priority: "",
              archived: "",
            })
          }
        >
          Clear
        </button>
      </div>

      {error && <div className="error">{error}</div>}
      {loading ? (
        <div className="muted">Loading…</div>
      ) : tickets.length === 0 ? (
        <div className={`card ${styles.empty}`}>No tickets match these filters.</div>
      ) : (
        <div className={styles.list}>
          {tickets.map((t) => (
            <Link key={t.id} to={`/tickets/${t.id}`} className={styles.rowLink}>
              <div className={styles.row}>
                <div className={styles.rowMain}>
                  <div className={styles.rowTitle}>
                    <span className={styles.id}>#{t.id}</span>
                    {t.title}
                  </div>
                  <div className={styles.rowMeta}>
                    <TypeBadge type={t.type} />
                    {t.archived && <span className={styles.tag}>Archived</span>}
                    {t.tags?.map((tag) => (
                      <span key={tag} className={styles.tag}>
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
                <div className={styles.rowSide}>
                  <PriorityBadge priority={t.priority} />
                  <StatusBadge status={t.status} />
                  <span className={styles.assignee}>{userName(t.assigned_to)}</span>
                  <span className={styles.date}>{formatDate(t.updated_at)}</span>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      {!loading && tickets.length < total && (
        <div className={styles.loadMore}>
          <button onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : `Load more (${total - tickets.length} more)`}
          </button>
        </div>
      )}
    </div>
  );
}
