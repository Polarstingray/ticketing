export const STATUSES = ["open", "in_review", "changes_requested", "resolved", "closed"];
export const PRIORITIES = ["low", "medium", "high", "critical"];
export const TYPES = ["code_review", "task"];

export const STATUS_LABELS = {
  open: "Open",
  in_review: "In Review",
  changes_requested: "Changes Requested",
  resolved: "Resolved",
  closed: "Closed",
};

export const PRIORITY_LABELS = {
  low: "Low",
  medium: "Medium",
  high: "High",
  critical: "Critical",
};

export const TYPE_LABELS = {
  code_review: "Code Review",
  task: "Task",
};

// Human-readable predicate for an activity entry (the actor name is prepended by
// the caller). `detail` shape depends on the action — see backend activity.py.
export function describeActivity(entry) {
  const d = entry.detail || {};
  switch (entry.action) {
    case "created":
      return "created the ticket";
    case "assigned":
      return `assigned it to ${d.name ?? (d.to ? `#${d.to}` : "someone")}`;
    case "unassigned":
      return "unassigned it";
    case "status_changed":
      return `changed status from ${STATUS_LABELS[d.from] ?? d.from} to ${
        STATUS_LABELS[d.to] ?? d.to
      }`;
    case "priority_changed":
      return `changed priority from ${PRIORITY_LABELS[d.from] ?? d.from} to ${
        PRIORITY_LABELS[d.to] ?? d.to
      }`;
    case "commented":
      return "commented";
    default:
      return entry.action.replace(/_/g, " ");
  }
}

export function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
