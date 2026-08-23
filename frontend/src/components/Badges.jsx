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
