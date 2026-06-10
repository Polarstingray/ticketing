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
  listTickets: (params = {}) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== "" && v !== null && v !== undefined) q.append(k, v);
    });
    const qs = q.toString();
    return request("GET", "/tickets" + (qs ? `?${qs}` : ""));
  },
  getTicket: (id) => request("GET", `/tickets/${id}`),
  createTicket: (body) => request("POST", "/tickets", body),
  updateTicket: (id, body) => request("PATCH", `/tickets/${id}`, body),
  deleteTicket: (id) => request("DELETE", `/tickets/${id}`),
  listActivity: (id) => request("GET", `/tickets/${id}/activity`),
  listAgentRuns: (id) => request("GET", `/tickets/${id}/agent-runs`),
  archiveTicket: (id) => request("POST", `/tickets/${id}/archive`),
  unarchiveTicket: (id) => request("POST", `/tickets/${id}/unarchive`),

  listComments: (id) => request("GET", `/tickets/${id}/comments`),
  addComment: (id, body) => request("POST", `/tickets/${id}/comments`, { body }),

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
