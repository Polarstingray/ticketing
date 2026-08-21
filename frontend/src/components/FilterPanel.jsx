import { useEffect, useState } from "react";
import SavedViews from "./SavedViews";
import TagPicker from "./TagPicker";
import {
  PRIORITIES,
  PRIORITY_LABELS,
  STATUSES,
  STATUS_LABELS,
  TYPES,
  TYPE_LABELS,
} from "../constants";
import { activeFilterCount, toggleTag } from "../filters";
import styles from "../styles/FilterPanel.module.css";

/**
 * The dashboard's filtering surface: search, the enum filters, the tag picker
 * and saved views.
 *
 * On a wide screen it is a persistent left rail; below ~900px it collapses to a
 * disclosure button so the ticket list still gets the full width. The open/closed
 * state is local because it is about the viewport, not the query — it must not
 * end up in the URL or a saved view.
 *
 * The search box is the one control the panel doesn't own outright: the page
 * debounces it (typing shouldn't push a history entry per keystroke), so the
 * live text comes in as `search` and is handed straight back up.
 */
export default function FilterPanel({
  filters,
  onChange,
  search,
  onSearchChange,
  users,
  tags,
  currentQuery,
  onApplyView,
  onClearAll,
}) {
  const [open, setOpen] = useState(false);
  const count = activeFilterCount(filters);

  // Escape closes the mobile drawer. Only bound while it's open, so it can't
  // swallow Escape from anything else on the page.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const set = (key) => (e) => onChange({ ...filters, [key]: e.target.value });

  return (
    <>
      <button
        type="button"
        className={styles.disclosure}
        aria-expanded={open}
        aria-controls="filter-panel"
        onClick={() => setOpen((v) => !v)}
      >
        Filters
        {count > 0 && <span className={styles.badge}>{count}</span>}
      </button>

      <aside
        id="filter-panel"
        className={open ? `${styles.panel} ${styles.panelOpen}` : styles.panel}
        aria-label="Ticket filters"
      >
        <div className={styles.panelHead}>
          <h2 className={styles.panelTitle}>
            Filters
            {count > 0 && <span className={styles.badge}>{count}</span>}
          </h2>
          {count > 0 && (
            <button type="button" className={styles.clear} onClick={onClearAll}>
              Clear all
            </button>
          )}
        </div>

        <div className={styles.section}>
          <label className={styles.field}>
            <span className={styles.label}>Search</span>
            <input
              type="search"
              placeholder="Title or description…"
              value={search}
              onChange={(e) => onSearchChange(e.target.value)}
            />
          </label>

          <label className={styles.field}>
            <span className={styles.label}>Type</span>
            <select value={filters.type} onChange={set("type")}>
              <option value="">All types</option>
              {TYPES.map((t) => (
                <option key={t} value={t}>
                  {TYPE_LABELS[t]}
                </option>
              ))}
            </select>
          </label>

          <label className={styles.field}>
            <span className={styles.label}>Status</span>
            <select value={filters.status} onChange={set("status")}>
              <option value="">All statuses</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </label>

          <label className={styles.field}>
            <span className={styles.label}>Priority</span>
            <select value={filters.priority} onChange={set("priority")}>
              <option value="">All priorities</option>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {PRIORITY_LABELS[p]}
                </option>
              ))}
            </select>
          </label>

          {/* Populated from an admin-only endpoint; members get an empty list
              and so no assignee filter at all, which is correct — they can only
              see their own tickets anyway. */}
          {users.length > 0 && (
            <label className={styles.field}>
              <span className={styles.label}>Assignee</span>
              <select value={filters.assigned_to} onChange={set("assigned_to")}>
                <option value="">Any assignee</option>
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name}
                  </option>
                ))}
              </select>
            </label>
          )}

          <label className={styles.checkbox}>
            <input
              type="checkbox"
              checked={filters.archived === "true"}
              onChange={(e) => onChange({ ...filters, archived: e.target.checked ? "true" : "" })}
            />
            Show archived
          </label>
        </div>

        <div className={styles.section}>
          <h3 className={styles.sectionTitle}>Tags</h3>
          <TagPicker
            tags={tags}
            selected={filters.tags}
            matchMode={filters.tag_match}
            onToggle={(tag) => onChange({ ...filters, tags: toggleTag(filters.tags, tag) })}
            onMatchModeChange={(mode) => onChange({ ...filters, tag_match: mode })}
          />
        </div>

        <SavedViews currentQuery={currentQuery} onApply={onApplyView} />
      </aside>
    </>
  );
}
