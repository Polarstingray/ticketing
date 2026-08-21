import { useMemo, useState } from "react";
import Tag from "./Tag";
import { isReservedTag } from "../constants";
import styles from "../styles/FilterPanel.module.css";

/**
 * Multi-select tag filter, fed by `api.listTicketTags` facet counts.
 *
 * Two groups, not one flat list. Free tags are what people actually triage by;
 * workflow tags (`repo:*`, `claude:*`, `dangerous`, …) are resolver automation
 * state and, on a busy instance, outnumber the free tags several times over. So
 * they get their own section, collapsed by default — still reachable when you
 * do want "everything awaiting a fix", but not in the way otherwise.
 *
 * Selected tags always render, even when the search box or the collapsed
 * workflow group would hide them, so a filter can never become invisible-but-
 * active.
 */
export default function TagPicker({ tags, selected, matchMode, onToggle, onMatchModeChange }) {
  const [search, setSearch] = useState("");
  const [showWorkflow, setShowWorkflow] = useState(false);

  const { free, workflow } = useMemo(() => {
    const term = search.trim().toLowerCase();
    const visible = tags.filter(
      (t) => selected.includes(t.tag) || !term || t.tag.toLowerCase().includes(term)
    );
    return {
      free: visible.filter((t) => !isReservedTag(t.tag)),
      workflow: visible.filter((t) => isReservedTag(t.tag)),
    };
  }, [tags, selected, search]);

  // A selected workflow tag would otherwise be hidden behind the collapsed
  // section, so open it rather than silently hiding an active filter.
  const workflowSelected = workflow.some((t) => selected.includes(t.tag));
  const workflowOpen = showWorkflow || workflowSelected;

  function renderRow(facet) {
    const checked = selected.includes(facet.tag);
    return (
      <label key={facet.tag} className={styles.tagRow}>
        {/* Explicit label: the enclosing <label> also wraps the usage count, so
            its text content ("bug 4") is not the accessible name we want. */}
        <input
          type="checkbox"
          aria-label={facet.tag}
          checked={checked}
          onChange={() => onToggle(facet.tag)}
        />
        <Tag>{facet.tag}</Tag>
        <span className={styles.tagCount}>{facet.count}</span>
      </label>
    );
  }

  if (tags.length === 0) {
    return <p className={styles.emptyHint}>No tags on any ticket yet.</p>;
  }

  return (
    <div>
      <div className={styles.tagHeader}>
        <input
          type="search"
          className={styles.tagSearch}
          placeholder="Find a tag…"
          aria-label="Find a tag"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {/* Only meaningful with 2+ tags picked; disabled rather than hidden so
            it doesn't pop into existence and shift the layout mid-selection. */}
        <div
          className={styles.matchToggle}
          role="group"
          aria-label="Match all or any of the selected tags"
        >
          {["all", "any"].map((mode) => (
            <button
              key={mode}
              type="button"
              disabled={selected.length < 2}
              aria-pressed={matchMode === mode}
              className={matchMode === mode ? styles.matchOn : undefined}
              title={
                mode === "all"
                  ? "Show tickets that have every selected tag"
                  : "Show tickets that have any selected tag"
              }
              onClick={() => onMatchModeChange(mode)}
            >
              {mode === "all" ? "All" : "Any"}
            </button>
          ))}
        </div>
      </div>

      {free.length > 0 && <div className={styles.tagList}>{free.map(renderRow)}</div>}
      {free.length === 0 && search.trim() && (
        <p className={styles.emptyHint}>No tags match “{search.trim()}”.</p>
      )}

      {workflow.length > 0 && (
        <div className={styles.workflowGroup}>
          <button
            type="button"
            className={styles.groupToggle}
            aria-expanded={workflowOpen}
            onClick={() => setShowWorkflow((v) => !v)}
          >
            <span className={styles.caret}>{workflowOpen ? "▾" : "▸"}</span>
            Workflow tags
            <span className={styles.tagCount}>{workflow.length}</span>
          </button>
          {workflowOpen && <div className={styles.tagList}>{workflow.map(renderRow)}</div>}
        </div>
      )}
    </div>
  );
}
