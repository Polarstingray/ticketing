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

  listComments: (id) => request("GET", `/tickets/${id}/comments`),
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
