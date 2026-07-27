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

// Resolver agent-run phases (see backend AgentRun / resolver _emit_token_usage).
export const AGENT_PHASE_LABELS = {
  plan: "Plan",
  implement: "Implement",
  review: "Review",
};

// A run's total token spend (input + output + both cache buckets).
export function totalTokens(run) {
  return (
    (run.input_tokens || 0) +
    (run.output_tokens || 0) +
    (run.cache_read_tokens || 0) +
    (run.cache_write_tokens || 0)
  );
}

// Integer count with thousands separators, e.g. 1234567 -> "1,234,567".
export function formatTokens(n) {
  return Number(n || 0).toLocaleString();
}

// USD with enough precision to show fractions of a cent (agent runs are cheap),
// e.g. 0.0123 -> "$0.0123". Larger sums still read naturally ($12.3456).
export function formatUsd(n) {
  return "$" + Number(n || 0).toFixed(4);
}

// Reserved/control tags drive the resolver bot's automation (mirrors
// backend/control_tags.py — keep the two lists in sync, or the UI offers edits
// the backend rejects with a 422). Regular users can see but not edit these; the
// backend is the real trust boundary and rejects attempts to set them.
const RESERVED_TAG_PREFIXES = [
  "claude:",
  "resolver:",
  "repo:",
  "parent:",
  "review-by:",
];
const RESERVED_TAG_EXACT = new Set(["dangerous", "fix", "delegate"]);

export function isReservedTag(tag) {
  return (
    RESERVED_TAG_EXACT.has(tag) ||
    RESERVED_TAG_PREFIXES.some((p) => tag.startsWith(p))
  );
}

// Set by the resolver when it has posted a code review and handed the ticket
// back: the findings are on file and a `/fix` replays them as an implement plan
// (resolve_tickets.TAG_AWAIT_FIX).
export const TAG_AWAITING_FIX = "resolver:awaiting-fix";
export const REPO_TAG_PREFIX = "repo:";

// Prefix the resolver stamps on the comment carrying its findings
// (resolve_tickets.REVIEW_MARKER) — identifies which bot to hand the ticket back to.
export const REVIEW_MARKER = "🔎 **Code review**";

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
    case "tags_changed": {
      const parts = [];
      if (d.added?.length) parts.push(`added ${d.added.join(", ")}`);
      if (d.removed?.length) parts.push(`removed ${d.removed.join(", ")}`);
      return parts.length ? `${parts.join(" and ")}` : "changed tags";
    }
    default:
      return entry.action.replace(/_/g, " ");
  }
}

export function formatDate(iso) {
  if (!iso) return "—";
  // Defensive: a timezone-less ISO string (e.g. from a stale cache or a client
  // that predates the UTC-aware API) is parsed as *local* time by Date, skewing
  // the display by the viewer's offset. Treat such strings as UTC.
  if (typeof iso === "string" && /T\d{2}:\d{2}/.test(iso) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)) {
    iso = iso + "Z";
  }
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
