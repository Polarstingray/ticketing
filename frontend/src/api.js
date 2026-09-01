// Thin fetch wrapper. All calls are same-origin under /api (Vite proxy in dev,
// nginx proxy in prod) and include the session cookie.
const BASE = "/api";

async function request(method, path, body) {
  const opts = {
    method,
    credentials: "include",
    headers: {},
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opts);

  if (res.status === 204) return null;

  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = data && data.detail ? data.detail : res.statusText;
    const err = new Error(typeof detail === "string" ? detail : "Request failed");
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/**
 * Stream a Server-Sent Events response, invoking onEvent({ event, data }) per frame.
 *
 * Why not EventSource: it is GET-only and cannot carry a request body, and a chat
 * turn is a POST with the question in it. So this reads the response body itself.
 *
 * Frames arrive split across arbitrary chunk boundaries, so bytes are buffered
 * and only whole frames (terminated by a blank line) are parsed — a naive
 * per-chunk parse drops any event unlucky enough to straddle a boundary.
 *
 * Returns when the stream ends. Pass `signal` to abort it (the caller navigating
 * away, or hitting stop); an abort resolves quietly rather than throwing, since
 * it is a user action and not a failure.
 */
async function stream(path, body, onEvent, { signal } = {}) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") return;
    throw err;
  }

  // Gates (ownership, ticket access, budget, provider unconfigured) are checked
  // before the stream opens, so a failure here is still a real HTTP status with
  // a JSON body — parse it the same way request() does.
  if (!res.ok) {
    let data = null;
    try {
      data = JSON.parse(await res.text());
    } catch {
      data = null;
    }
    const detail = data && data.detail ? data.detail : res.statusText;
    const err = new Error(typeof detail === "string" ? detail : "Request failed");
    err.status = res.status;
    throw err;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const flush = (block) => {
    let event = null;
    let data = null;
    block.split("\n").forEach((line) => {
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) {
        try {
          data = JSON.parse(line.slice(6));
        } catch {
          data = null;
        }
      }
    });
    if (event) onEvent({ event, data });
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split = buffer.indexOf("\n\n");
      while (split !== -1) {
        flush(buffer.slice(0, split));
        buffer = buffer.slice(split + 2);
        split = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim()) flush(buffer); // a final frame with no trailing blank line
  } catch (err) {
    if (err.name !== "AbortError") throw err;
  }
}

/**
 * Build a query string from a params object, dropping empty values.
 *
 * Array values are appended once per element rather than joined, which is what
 * makes repeatable params work: { tag: ["a", "b"] } -> "?tag=a&tag=b", the shape
 * the backend's List[str] filters expect.
 */
function qs(params) {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v === "" || v === null || v === undefined) return;
    if (Array.isArray(v)) {
      v.forEach((item) => {
        if (item !== "" && item !== null && item !== undefined) q.append(k, item);
      });
    } else {
      q.append(k, v);
    }
  });
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  get: (path) => request("GET", path),
  // Unauthenticated (the Login page needs it before a session exists).
  // { read_only, demo_username, demo_password } — the latter two are null
  // unless the deployment opted into showing them (the public demo).
  appConfig: () => request("GET", "/app-config"),
  post: (path, body) => request("POST", path, body),
  patch: (path, body) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),

  // Convenience helpers
  login: (username, password) => request("POST", "/auth/login", { username, password }),
  logout: () => request("POST", "/auth/logout"),
  me: () => request("GET", "/auth/me"),

  // Returns a paginated envelope: { items, total, limit, offset }.
  listTickets: (params = {}) => request("GET", "/tickets" + qs(params)),
  // Every tag on a ticket the caller can see, with usage counts:
  // { items: [{ tag, count }] }. Feeds the filter panel's tag picker.
  listTicketTags: (params = {}) => request("GET", "/tickets/tags" + qs(params)),
  getTicket: (id) => request("GET", `/tickets/${id}`),
  createTicket: (body) => request("POST", "/tickets", body),
  updateTicket: (id, body) => request("PATCH", `/tickets/${id}`, body),
  deleteTicket: (id) => request("DELETE", `/tickets/${id}`),
  listActivity: (id) => request("GET", `/tickets/${id}/activity`),
  listAgentRuns: (id) => request("GET", `/tickets/${id}/agent-runs`),
  // { own, children: [{ ticket_id, title, totals }], total } — own cost plus the
  // cost of every delegated child (tickets tagged parent:<id>).
  costRollup: (id) => request("GET", `/tickets/${id}/cost-rollup`),
  archiveTicket: (id) => request("POST", `/tickets/${id}/archive`),
  unarchiveTicket: (id) => request("POST", `/tickets/${id}/unarchive`),

  // Saved dashboard views: a named filter query string, scoped to the caller.
  listSavedViews: () => request("GET", "/saved-views"),
  createSavedView: (body) => request("POST", "/saved-views", body),
  updateSavedView: (id, body) => request("PATCH", `/saved-views/${id}`, body),
  deleteSavedView: (id) => request("DELETE", `/saved-views/${id}`),

  // Webhooks. The signing `secret` comes back ONLY from createWebhook and
  // rotateWebhookSecret — every read path returns `secret_prefix` instead, so
  // there is nowhere to re-fetch it from if the user misses it.
  listWebhooks: (params = {}) => request("GET", "/webhooks" + qs(params)),
  createWebhook: (body) => request("POST", "/webhooks", body),
  getWebhook: (id) => request("GET", `/webhooks/${id}`),
  updateWebhook: (id, body) => request("PATCH", `/webhooks/${id}`, body),
  deleteWebhook: (id) => request("DELETE", `/webhooks/${id}`),
  rotateWebhookSecret: (id) => request("POST", `/webhooks/${id}/rotate-secret`),
  // Paginated envelope { items, total, limit, offset }; params: state, ticket_id,
  // limit, offset.
  listWebhookDeliveries: (id, params = {}) =>
    request("GET", `/webhooks/${id}/deliveries` + qs(params)),
  // Re-arms a delivery row for another attempt; the worker does the sending.
  redeliverWebhookDelivery: (id, deliveryId) =>
    request("POST", `/webhooks/${id}/deliveries/${deliveryId}/redeliver`),

  listComments: (id, params = {}) =>
    request("GET", `/tickets/${id}/comments` + qs(params)),
  addComment: (id, body) => request("POST", `/tickets/${id}/comments`, { body }),
  editComment: (id, commentId, body) =>
    request("PATCH", `/tickets/${id}/comments/${commentId}`, { body }),
  deleteComment: (id, commentId) =>
    request("DELETE", `/tickets/${id}/comments/${commentId}`),

  // Notifications (the bell/inbox). listNotifications returns
  // { items, total, unread_count, limit, offset }.
  listNotifications: (params = {}) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== "" && v !== null && v !== undefined) q.append(k, v);
    });
    const qs = q.toString();
    return request("GET", "/notifications" + (qs ? `?${qs}` : ""));
  },
  unreadCount: () => request("GET", "/notifications/unread_count"),
  markNotificationRead: (id) => request("POST", `/notifications/${id}/read`),
  markAllNotificationsRead: () => request("POST", "/notifications/read_all"),
  // Clears the caller's unread notifications for one ticket (the "opening the
  // ticket is the read gesture" path). Returns the new { unread_count }.
  markTicketNotificationsRead: (id) =>
    request("POST", `/notifications/read_by_ticket/${id}`),
  deleteNotification: (id) => request("DELETE", `/notifications/${id}`),
  bulkDeleteNotifications: (ids) =>
    request("POST", "/notifications/bulk_delete", { ids }),

  // Notification preferences (the settings panel). Both return the full matrix
  // { items: [{ type, channel, enabled }] }.
  getNotificationPreferences: () => request("GET", "/preferences"),
  updateNotificationPreferences: (items) => request("PUT", "/preferences", { items }),

  // Resolver settings (admin). Non-secret tunables the resolver daemon overlays
  // on top of its .env at sweep start. Returns { bot_user_id, settings, secrets,
  // updated_at, updated_by }; updateResolverSettings sends a partial values obj.
  // Scope: omit bot_user_id for the global default, send it for a specific
  // resolver. Test `!= null` (not truthiness) so a bot with user id 0 scopes to
  // itself instead of silently writing the global row.
  getResolverSettings: (botUserId) =>
    request("GET", "/resolver-settings" + (botUserId != null ? `?bot_user_id=${botUserId}` : "")),
  updateResolverSettings: (values, botUserId) =>
    request(
      "PUT",
      "/resolver-settings" + (botUserId != null ? `?bot_user_id=${botUserId}` : ""),
      values
    ),
  // The resolver-manager roster: each resolver bot + its live self-reported state.
  listResolvers: () => request("GET", "/resolvers"),
  // The agent registry (admin): every worker that has ever sent a heartbeat,
  // our resolver bots and third-party agents alike. Read-only — an external
  // agent carries its own config, so there is nothing here to edit.
  listAgents: () => request("GET", "/agents"),

  // Chat assistant (optional — hidden entirely when chatConfig().enabled is
  // false). Returns { enabled, model, daily_usd_limit, spent_today_usd }.
  chatConfig: () => request("GET", "/chat/config"),
  listConversations: () => request("GET", "/chat/conversations"),
  createConversation: (ticketId) =>
    request("POST", "/chat/conversations", { ticket_id: ticketId ?? null }),
  getConversation: (id) => request("GET", `/chat/conversations/${id}`),
  deleteConversation: (id) => request("DELETE", `/chat/conversations/${id}`),
  // Streams the answer. onEvent receives { event, data } for each SSE frame:
  // "token" ({ text }), "tool_call" ({ name, args }), "tool_result"
  // ({ name, summary }), "done" ({ message_id, usage, meta, spent_today_usd,
  // title }) and "error" ({ detail, status }). An error *frame* is a mid-turn
  // failure; a pre-stream refusal throws with an HTTP status instead.
  //
  // `done.meta` is the same blob the server stored on the message, so a turn
  // rendered live and the same turn re-fetched later have identical shape.
  // `flush()` above dispatches on the event name alone, so these needed no
  // change there.
  sendChatMessage: (id, body, onEvent, opts) =>
    stream(`/chat/conversations/${id}/messages`, body, onEvent, opts),

  listUsers: () => request("GET", "/users"),
  createUser: (body) => request("POST", "/users", body),
  updateUser: (id, body) => request("PATCH", `/users/${id}`, body),
  deleteUser: (id) => request("DELETE", `/users/${id}`),

  // API keys (multiple per user; plaintext returned only by createApiKey).
  listApiKeys: (userId) => request("GET", `/users/${userId}/api-keys`),
  createApiKey: (userId, body) => request("POST", `/users/${userId}/api-keys`, body),
  revokeApiKey: (userId, keyId) =>
    request("POST", `/users/${userId}/api-keys/${keyId}/revoke`),
};
