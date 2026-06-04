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
