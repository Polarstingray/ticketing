import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import {
  AssigneeDropdown,
  PriorityBadge,
  StatusDropdown,
  TypeBadge,
} from "../components/Badges";
import FilterPanel from "../components/FilterPanel";
import Tag from "../components/Tag";
import {
  PRIORITY_LABELS,
  STATUS_LABELS,
  TYPE_LABELS,
  formatDate,
} from "../constants";
import {
  SORTS,
  SORT_LABELS,
  activeFilterCount,
  clearedFilters,
  filtersToParams,
  filtersToQuery,
  paramsToFilters,
} from "../filters";
import { useNotifications } from "../notifications/NotificationsContext";
import styles from "../styles/TicketList.module.css";

const PAGE_SIZE = 50;

// Row height is a viewing preference, not part of the query, so it lives in
// localStorage rather than the URL — a shared link shouldn't impose the sender's
// display density on the recipient.
const DENSITY_KEY = "stingray.ticketList.density";

function loadDensity() {
  try {
    return localStorage.getItem(DENSITY_KEY) === "compact" ? "compact" : "comfortable";
  } catch {
    // Private-mode / disabled storage: fall back to the default rather than
    // taking the whole list down.
    return "comfortable";
  }
}

export default function TicketList() {
  const [searchParams, setSearchParams] = useSearchParams();
  // Tickets someone else has commented on that the current user hasn't read yet;
  // the provider polls this, so the list just reads it.
  const { unreadTicketIds } = useNotifications();
  // The URL is the source of truth for filters, so there is no filter useState
  // to keep in sync with it. Derive on every render; it's cheap.
  const filters = useMemo(() => paramsToFilters(searchParams), [searchParams]);
  const currentQuery = searchParams.toString();

  const [tickets, setTickets] = useState([]);
  const [total, setTotal] = useState(0);
  const [users, setUsers] = useState([]);
  const [tagFacets, setTagFacets] = useState([]);
  // Search box is debounced into the URL so each keystroke doesn't refetch.
  const [search, setSearch] = useState(filters.q);
  const [density, setDensity] = useState(loadDensity);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  const applyFilters = useCallback(
    (next, { replace = false } = {}) => setSearchParams(filtersToParams(next), { replace }),
    [setSearchParams]
  );

  // Users list is best-effort (only admins can fetch it); used to label assignees
  // and populate the assignee filter.
  useEffect(() => {
    api
      .listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  // Tag facets follow the archived toggle: the picker should offer the tags of
  // the tickets currently in scope, not tags you can't reach from here.
  useEffect(() => {
    api
      .listTicketTags({ archived: filters.archived })
      .then((res) => setTagFacets(res.items))
      .catch(() => setTagFacets([]));
  }, [filters.archived]);

  // Keep the box in step when the query changes from outside it — applying a
  // saved view, "Clear all", or the back button.
  useEffect(() => {
    setSearch(filters.q);
  }, [filters.q]);

  // Debounce the search box (~300ms) into the URL, which triggers a refetch.
  // `replace` so a typed word leaves one history entry, not one per character.
  useEffect(() => {
    if (search === filters.q) return undefined;
    const id = setTimeout(() => {
      applyFilters({ ...filters, q: search }, { replace: true });
    }, 300);
    return () => clearTimeout(id);
  }, [search, filters, applyFilters]);

  // (Re)load the first page whenever the query changes.
  useEffect(() => {
    setLoading(true);
    api
      .listTickets({ ...filtersToQuery(filters), limit: PAGE_SIZE, offset: 0 })
      .then((res) => {
        setTickets(res.items);
        setTotal(res.total);
        setError("");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters]);

  function loadMore() {
    setLoadingMore(true);
    api
      .listTickets({ ...filtersToQuery(filters), limit: PAGE_SIZE, offset: tickets.length })
      .then((res) => {
        setTickets((prev) => [...prev, ...res.items]);
        setTotal(res.total);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoadingMore(false));
  }

  function changeDensity(next) {
    setDensity(next);
    try {
      localStorage.setItem(DENSITY_KEY, next);
    } catch {
      // Non-persistent is fine; the toggle still works for this session.
    }
  }

  // Row-level status/archive edits, so a sweep through the list doesn't mean
  // opening every ticket. `busyId` keeps a row from being double-submitted while
  // its request is in flight.
  const [busyId, setBusyId] = useState(null);

  // Mass-select state
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkStatus, setBulkStatus] = useState("");
  const [bulkAssignee, setBulkAssignee] = useState("");

  // Clear selection whenever the active query changes (filter / sort change).
  useEffect(() => {
    setSelectedIds(new Set());
  }, [currentQuery]);

  // A row edit can outlive the list: navigate to a ticket mid-request and the
  // response lands after unmount. Guard the state updates rather than let the
  // late setState fire on a dead component.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function changeStatus(id, status) {
    setBusyId(id);
    try {
      const updated = await api.updateTicket(id, { status });
      if (!mounted.current) return;
      setTickets((prev) => prev.map((t) => (t.id === id ? { ...t, ...updated } : t)));
      setError("");
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusyId(null);
    }
  }

  async function archive(id) {
    setBusyId(id);
    try {
      await api.archiveTicket(id);
      if (!mounted.current) return;
      if (filters.archived) {
        // Archived tickets are in scope here, so keep the row and just re-badge it.
        setTickets((prev) => prev.map((t) => (t.id === id ? { ...t, archived: true } : t)));
      } else {
        // It has left the current query — drop it, and keep the count honest so
        // "Load more" doesn't offer a page that isn't there.
        setTickets((prev) => prev.filter((t) => t.id !== id));
        setTotal((n) => Math.max(0, n - 1));
      }
      setError("");
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusyId(null);
    }
  }

  async function changeAssignee(id, userId) {
    setBusyId(id);
    try {
      const updated = await api.updateTicket(id, { assigned_to: userId });
      if (!mounted.current) return;
      setTickets((prev) => prev.map((t) => (t.id === id ? { ...t, ...updated } : t)));
      setError("");
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusyId(null);
    }
  }

  function toggleSelect(id) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  const allSelected = tickets.length > 0 && tickets.every((t) => selectedIds.has(t.id));
  const someSelected = !allSelected && tickets.some((t) => selectedIds.has(t.id));

  function toggleSelectAll() {
    if (allSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(tickets.map((t) => t.id)));
    }
  }

  async function applyBulk() {
    const ids = [...selectedIds];
    const body = { ids };
    if (bulkStatus) body.status = bulkStatus;
    if (bulkAssignee !== "") {
      body.assigned_to = bulkAssignee === "null" ? null : Number(bulkAssignee);
    }
    if (!body.status && !("assigned_to" in body)) {
      setError("Select a status or assignee to apply");
      return;
    }
    setBulkBusy(true);
    try {
      const result = await api.bulkUpdateTickets(body);
      if (!mounted.current) return;
      setTickets((prev) =>
        prev.map((t) => {
          const up = result.updated.find((u) => u.id === t.id);
          return up ? { ...t, ...up } : t;
        })
      );
      if (result.failed.length > 0) {
        setError(`${result.failed.length} ticket(s) could not be updated.`);
      } else {
        setError("");
      }
      setSelectedIds(new Set());
      setBulkStatus("");
      setBulkAssignee("");
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBulkBusy(false);
    }
  }

  const userName = (id) => {
    const u = users.find((x) => x.id === id);
    return u ? u.display_name : id ? `#${id}` : "Unassigned";
  };

  const activeCount = activeFilterCount(filters);

  // One removable chip per active filter, so the current query is legible
  // without opening the panel and any single part of it can be dropped.
  const chips = [];
  if (filters.q) chips.push({ key: "q", label: `“${filters.q}”`, clear: { q: "" } });
  if (filters.type)
    chips.push({ key: "type", label: TYPE_LABELS[filters.type], clear: { type: "" } });
  if (filters.status)
    chips.push({ key: "status", label: STATUS_LABELS[filters.status], clear: { status: "" } });
  if (filters.priority)
    chips.push({
      key: "priority",
      label: PRIORITY_LABELS[filters.priority],
      clear: { priority: "" },
    });
  if (filters.assigned_to)
    chips.push({
      key: "assignee",
      label: userName(Number(filters.assigned_to)),
      clear: { assigned_to: "" },
    });
  if (filters.archived) chips.push({ key: "archived", label: "Archived", clear: { archived: "" } });
  filters.tags.forEach((tag) =>
    chips.push({
      key: `tag:${tag}`,
      label: tag,
      clear: { tags: filters.tags.filter((t) => t !== tag) },
    })
  );

  return (
    <div className={styles.layout}>
      <FilterPanel
        filters={filters}
        onChange={applyFilters}
        search={search}
        onSearchChange={setSearch}
        users={users}
        tags={tagFacets}
        currentQuery={currentQuery}
        onApplyView={(query) => setSearchParams(new URLSearchParams(query))}
        onClearAll={() => applyFilters(clearedFilters(filters))}
      />

      <div className={styles.main}>
        <div className={styles.head}>
          <h1>
            Tickets {!loading && <span className={styles.count}>({total})</span>}
          </h1>
          <div className={styles.headControls}>
            <label className={styles.sort}>
              <select
                value={filters.sort}
                aria-label="Sort by"
                onChange={(e) => applyFilters({ ...filters, sort: e.target.value })}
              >
                {SORTS.map((s) => (
                  <option key={s} value={s}>
                    {SORT_LABELS[s]}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className={styles.orderToggle}
                aria-label={
                  filters.order === "desc"
                    ? "Sorted descending; switch to ascending"
                    : "Sorted ascending; switch to descending"
                }
                onClick={() =>
                  applyFilters({
                    ...filters,
                    order: filters.order === "desc" ? "asc" : "desc",
                  })
                }
              >
                {filters.order === "desc" ? "↓" : "↑"}
              </button>
            </label>
            <div className={styles.density} role="group" aria-label="Row density">
              {["comfortable", "compact"].map((mode) => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={density === mode}
                  className={density === mode ? styles.densityOn : undefined}
                  onClick={() => changeDensity(mode)}
                >
                  {mode === "comfortable" ? "Comfortable" : "Compact"}
                </button>
              ))}
            </div>
            <Link to="/tickets/new">
              <button className="primary">New ticket</button>
            </Link>
          </div>
        </div>

        {chips.length > 0 && (
          <div className={styles.chips}>
            {chips.map((chip) => (
              <button
                key={chip.key}
                type="button"
                className={styles.chip}
                aria-label={`Remove filter ${chip.label}`}
                onClick={() => applyFilters({ ...filters, ...chip.clear })}
              >
                {chip.label}
                <span aria-hidden="true">×</span>
              </button>
            ))}
            <button
              type="button"
              className={styles.chipClear}
              onClick={() => applyFilters(clearedFilters(filters))}
            >
              Clear all
            </button>
          </div>
        )}

        {error && <div className="error">{error}</div>}

        {selectedIds.size > 0 && (
          <div className={styles.bulkBar}>
            <span className={styles.bulkCount}>{selectedIds.size} selected</span>
            <select
              value={bulkStatus}
              onChange={(e) => setBulkStatus(e.target.value)}
              aria-label="Bulk status"
              disabled={bulkBusy}
            >
              <option value="">— Status —</option>
              {Object.entries(STATUS_LABELS).map(([v, label]) => (
                <option key={v} value={v}>{label}</option>
              ))}
            </select>
            {users.length > 0 && (
              <select
                value={bulkAssignee}
                onChange={(e) => setBulkAssignee(e.target.value)}
                aria-label="Bulk assignee"
                disabled={bulkBusy}
              >
                <option value="">— Assignee —</option>
                <option value="null">Unassigned</option>
                {users.map((u) => (
                  <option key={u.id} value={String(u.id)}>{u.display_name}</option>
                ))}
              </select>
            )}
            <button
              type="button"
              className="primary"
              disabled={bulkBusy || (!bulkStatus && bulkAssignee === "")}
              onClick={applyBulk}
            >
              {bulkBusy ? "Applying…" : "Apply"}
            </button>
            <button
              type="button"
              disabled={bulkBusy}
              onClick={() => setSelectedIds(new Set())}
            >
              Clear selection
            </button>
          </div>
        )}

        {loading ? (
          <div className="muted">Loading…</div>
        ) : tickets.length === 0 ? (
          activeCount > 0 ? (
            <div className={`card ${styles.empty}`}>No tickets match these filters.</div>
          ) : (
            <div className={`card ${styles.empty}`}>
              <p>No tickets yet.</p>
              <p>
                <Link to="/tickets/new">Create your first ticket</Link> to get started.
              </p>
            </div>
          )
        ) : (
          <div className={styles.list}>
            <div className={styles.selectAllRow}>
              <input
                type="checkbox"
                className={styles.checkbox}
                checked={allSelected}
                ref={(el) => { if (el) el.indeterminate = someSelected; }}
                onChange={toggleSelectAll}
                aria-label="Select all tickets"
              />
            </div>
            {tickets.map((t) => (
              <Link key={t.id} to={`/tickets/${t.id}`} className={styles.rowLink}>
                <div
                  className={[
                    density === "compact" ? `${styles.row} ${styles.rowCompact}` : styles.row,
                    selectedIds.has(t.id) ? styles.rowSelected : "",
                  ].filter(Boolean).join(" ")}
                >
                  <input
                    type="checkbox"
                    className={styles.checkbox}
                    checked={selectedIds.has(t.id)}
                    aria-label={`Select ticket ${t.id}`}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleSelect(t.id)}
                  />
                  <div className={styles.rowMain}>
                    <div className={styles.rowTitle}>
                      {unreadTicketIds?.has(t.id) && (
                        <span
                          className={styles.notifDot}
                          role="img"
                          aria-label="Unread comment"
                          title="Unread comment"
                        />
                      )}
                      <span className={styles.id}>#{t.id}</span>
                      {t.title}
                    </div>
                    <div className={styles.rowMeta}>
                      <TypeBadge type={t.type} />
                      {t.archived && <Tag muted>Archived</Tag>}
                      {t.tags?.map((tag) => <Tag key={tag}>{tag}</Tag>)}
                    </div>
                  </div>
                  <div className={styles.rowSide}>
                    <PriorityBadge priority={t.priority} />
                    <StatusDropdown
                      status={t.status}
                      disabled={busyId === t.id || bulkBusy}
                      onChange={(s) => changeStatus(t.id, s)}
                    />
                    {t.status === "closed" && !t.archived && (
                      <button
                        type="button"
                        className={styles.archiveBtn}
                        title="Archive ticket"
                        disabled={busyId === t.id}
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          archive(t.id);
                        }}
                      >
                        Archive
                      </button>
                    )}
                    {/* Only admins can list users, so a non-admin's `users` is
                        empty and there is nobody to pick from — fall back to the
                        read-only label rather than an empty menu. */}
                    {users.length > 0 ? (
                      <AssigneeDropdown
                        assignedTo={t.assigned_to}
                        users={users}
                        disabled={busyId === t.id || bulkBusy}
                        onChange={(uid) => changeAssignee(t.id, uid)}
                      />
                    ) : (
                      <span className={styles.assignee}>{userName(t.assigned_to)}</span>
                    )}
                    {t.due_date && (
                      <span className={styles.date}>Due {formatDate(t.due_date)}</span>
                    )}
                    {t.due_date &&
                      new Date(t.due_date) < new Date() &&
                      t.status !== "resolved" &&
                      t.status !== "closed" && <Tag muted>Overdue</Tag>}
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
    </div>
  );
}
