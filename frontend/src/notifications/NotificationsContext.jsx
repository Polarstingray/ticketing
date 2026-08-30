import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";

// Holds the unread-notification count and a refresh() so the nav badge and the
// Notifications page stay in sync. Polls unread_count on an interval and on
// demand. Only polls while a user is authenticated.
//
// It also tracks which *tickets* have an unread comment notification, so the
// ticket list can dot the affected rows without every row fetching for itself.
// Scoped to "commented" on purpose: an assignment already announces itself (the
// ticket lands in the assignee's queue), while a new comment on a ticket you
// have open in a list is the thing that is easy to miss.
const EMPTY_TICKET_IDS = new Set();

const NotificationsContext = createContext({
  unreadCount: 0,
  unreadTicketIds: EMPTY_TICKET_IDS,
  refresh: () => {},
});

const POLL_MS = 30000;
// The dot is a hint, not an inbox: one page of unread rows is plenty to mark up
// the list, and it keeps the poll cheap.
const UNREAD_FETCH_LIMIT = 200;

export function NotificationsProvider({ children }) {
  const { user } = useAuth();
  const [unreadCount, setUnreadCount] = useState(0);
  const [unreadTicketIds, setUnreadTicketIds] = useState(EMPTY_TICKET_IDS);
  const [pageTitleEnabled, setPageTitleEnabled] = useState(false);

  const refresh = useCallback(async () => {
    let count;
    try {
      const { unread_count } = await api.unreadCount();
      count = unread_count;
      setUnreadCount(unread_count);
    } catch {
      // best-effort: leave the last known count on transient failures
      return;
    }
    // Only the count is polled unconditionally; the row listing is fetched just
    // when there is something unread, so the steady state (an empty inbox) stays
    // at one request per poll.
    if (!count) {
      setUnreadTicketIds((prev) => (prev.size ? EMPTY_TICKET_IDS : prev));
      return;
    }
    try {
      const { items } = await api.listNotifications({
        unread: true,
        limit: UNREAD_FETCH_LIMIT,
      });
      const ids = new Set();
      (items || []).forEach((n) => {
        if (n.type === "commented" && n.ticket_id != null) ids.add(n.ticket_id);
      });
      setUnreadTicketIds(ids);
    } catch {
      // best-effort: keep the previous dots rather than clearing them on a blip
    }
  }, []);

  useEffect(() => {
    if (!user) {
      setUnreadCount(0);
      setUnreadTicketIds(EMPTY_TICKET_IDS);
      return undefined;
    }
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [user, refresh]);

  // Fetch page_title preference once per login session.
  useEffect(() => {
    if (!user) { setPageTitleEnabled(false); return; }
    api.getNotificationPreferences().then(({ items }) => {
      const pageTitleItems = items.filter((i) => i.channel === "page_title");
      // Sparse/opt-out model: no rows means enabled; enabled if any row is enabled.
      const anyEnabled = pageTitleItems.length === 0 || pageTitleItems.some((i) => i.enabled);
      setPageTitleEnabled(anyEnabled);
    }).catch(() => {});
  }, [user]);

  // Update document.title whenever unread count or page_title preference changes.
  useEffect(() => {
    const base = "Stingray Tickets";
    document.title = pageTitleEnabled && unreadCount > 0
      ? `● (${unreadCount}) ${base}`
      : base;
  }, [unreadCount, pageTitleEnabled]);

  return (
    <NotificationsContext.Provider value={{ unreadCount, unreadTicketIds, refresh, pageTitleEnabled }}>
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotifications() {
  return useContext(NotificationsContext);
}
