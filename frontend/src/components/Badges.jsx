import { useEffect, useRef, useState } from "react";
import {
  PRIORITY_LABELS,
  STATUS_LABELS,
  STATUSES,
  TYPE_LABELS,
} from "../constants";
import styles from "../styles/Badges.module.css";

export function StatusBadge({ status }) {
  return (
    <span className={`${styles.badge} ${styles[`s_${status}`] || ""}`}>
      {STATUS_LABELS[status] || status}
    </span>
  );
}

// A StatusBadge you can click to pick a different status, for changing statuses
// in bulk without opening each ticket. The menu keeps the badge palette so a
// status reads the same here as it does everywhere else.
export function StatusDropdown({ status, onChange, disabled = false }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  // Close on an outside click or Escape — the menu floats over the row, so it
  // must not survive a click meant for something underneath it.
  useEffect(() => {
    if (!open) return undefined;
    function onDocMouseDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    function onKeyDown(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // The row is a <Link>; without this every click here would navigate away.
  function swallow(e) {
    e.preventDefault();
    e.stopPropagation();
  }

  // Keyboard activation is a click as far as React is concerned, so `swallow`
  // covers Enter/Space on our own buttons. What it doesn't cover is tabbing out
  // of an open menu: focus lands on the row <Link>, where a stray Enter would
  // navigate with the menu still hanging over the list. Closing on focus-out
  // makes the menu behave for the keyboard the way it does for the mouse.
  function onBlurOut(e) {
    if (rootRef.current && !rootRef.current.contains(e.relatedTarget)) setOpen(false);
  }

  function pick(e, next) {
    swallow(e);
    setOpen(false);
    if (next !== status) onChange(next);
  }

  return (
    <span className={styles.statusDropdown} ref={rootRef} onBlur={onBlurOut}>
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Status: ${STATUS_LABELS[status] || status}. Change status`}
        className={`${styles.badge} ${styles.statusTrigger} ${styles[`s_${status}`] || ""}`}
        onClick={(e) => {
          swallow(e);
          setOpen((v) => !v);
        }}
      >
        {STATUS_LABELS[status] || status}
        <span aria-hidden="true" className={styles.chevron}>
          ▾
        </span>
      </button>
      {open && (
        <span className={styles.statusMenu} role="listbox">
          {STATUSES.map((s) => (
            <button
              key={s}
              type="button"
              role="option"
              aria-selected={s === status}
              className={styles.statusOption}
              onClick={(e) => pick(e, s)}
            >
              <span className={`${styles.badge} ${styles[`s_${s}`] || ""}`}>
                {STATUS_LABELS[s] || s}
              </span>
            </button>
          ))}
        </span>
      )}
    </span>
  );
}

// The assignee, rendered as a badge you can click to hand the ticket to someone
// else — the StatusDropdown's twin, so reassigning from the list works the same
// way changing a status does. There is no per-user palette, so the trigger and
// the options are plain pills.
export function AssigneeDropdown({ assignedTo, users, onChange, disabled = false }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  const nameOf = (id) => {
    const u = users.find((x) => x.id === id);
    return u ? u.display_name : id ? `#${id}` : "Unassigned";
  };
  const current = nameOf(assignedTo);

  // Close on an outside click or Escape — the menu floats over the row, so it
  // must not survive a click meant for something underneath it.
  useEffect(() => {
    if (!open) return undefined;
    function onDocMouseDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    function onKeyDown(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  // The row is a <Link>; without this every click here would navigate away.
  function swallow(e) {
    e.preventDefault();
    e.stopPropagation();
  }

  // Tabbing out of an open menu lands focus on the row <Link>, where a stray
  // Enter would navigate with the menu still hanging over the list. Closing on
  // focus-out makes the menu behave for the keyboard as it does for the mouse.
  function onBlurOut(e) {
    if (rootRef.current && !rootRef.current.contains(e.relatedTarget)) setOpen(false);
  }

  function pick(e, next) {
    swallow(e);
    setOpen(false);
    if (next !== assignedTo) onChange(next);
  }

  return (
    <span className={styles.statusDropdown} ref={rootRef} onBlur={onBlurOut}>
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Assignee: ${current}. Change assignee`}
        className={`${styles.badge} ${styles.statusTrigger} ${styles.assigneeTrigger}`}
        onClick={(e) => {
          swallow(e);
          setOpen((v) => !v);
        }}
      >
        <span className={styles.assigneeName}>{current}</span>
        <span aria-hidden="true" className={styles.chevron}>
          ▾
        </span>
      </button>
      {open && (
        <span className={`${styles.statusMenu} ${styles.assigneeMenu}`} role="listbox">
          <button
            type="button"
            role="option"
            aria-selected={assignedTo == null}
            className={styles.statusOption}
            onClick={(e) => pick(e, null)}
          >
            <span className={`${styles.badge} ${styles.assigneeOption}`}>Unassigned</span>
          </button>
          {users.map((u) => (
            <button
              key={u.id}
              type="button"
              role="option"
              aria-selected={u.id === assignedTo}
              className={styles.statusOption}
              onClick={(e) => pick(e, u.id)}
            >
              <span className={`${styles.badge} ${styles.assigneeOption}`}>{u.display_name}</span>
            </button>
          ))}
        </span>
      )}
    </span>
  );
}

export function PriorityBadge({ priority }) {
  return (
    <span className={`${styles.badge} ${styles[`p_${priority}`] || ""}`}>
      {PRIORITY_LABELS[priority] || priority}
    </span>
  );
}

export function TypeBadge({ type }) {
  return (
    <span className={`${styles.badge} ${styles.type}`}>
      {TYPE_LABELS[type] || type}
    </span>
  );
}
