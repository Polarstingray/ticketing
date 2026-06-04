import {
  PRIORITY_LABELS,
  STATUS_LABELS,
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
