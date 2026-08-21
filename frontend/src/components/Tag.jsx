import { isReservedTag } from "../constants";
import styles from "../styles/Tag.module.css";

/**
 * One tag chip, shared by the ticket list, the ticket detail header and the
 * filter panel so a given tag looks the same everywhere.
 *
 * Reserved tags (`claude:*`, `repo:*`, `dangerous`, …) get a distinct striped,
 * dashed treatment: they aren't labels a person chose, they're resolver
 * automation state, and on a busy ticket they otherwise drown out the free tags
 * that people actually filter by. `isReservedTag` (constants.js) mirrors
 * backend/control_tags.py.
 *
 * Props:
 *   onRemove  — renders an × button (detail page, where tags are editable).
 *   muted     — de-emphasize without implying "reserved" (e.g. Archived/Overdue
 *               pseudo-tags on a list row, which aren't real tags at all).
 */
export default function Tag({ children, onRemove, muted = false, title }) {
  const label = children;
  const reserved = typeof label === "string" && isReservedTag(label);
  const className = [
    styles.tag,
    reserved && styles.reserved,
    muted && styles.muted,
    onRemove && styles.removable,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span
      className={className}
      title={title ?? (reserved ? "System tag — managed by automation" : undefined)}
    >
      {label}
      {onRemove && (
        <button
          type="button"
          className={styles.remove}
          aria-label={`Remove tag ${label}`}
          onClick={onRemove}
        >
          ×
        </button>
      )}
    </span>
  );
}
