import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";

// Holds the unread-notification count and a refresh() so the nav badge and the
// Notifications page stay in sync. Polls unread_count on an interval and on
// demand. Only polls while a user is authenticated.
//
// It also tracks which *tickets* have an unread comment notification, so the
// ticket list can dot the affected rows without every row fetching for itself.
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

  const refresh = useCallback(async () => {
    try {
      const { unread_count } = await api.unreadCount();
      setUnreadCount(unread_count);
    } catch {
      // best-effort: leave the last known count on transient failures
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

  return (
    <NotificationsContext.Provider value={{ unreadCount, unreadTicketIds, refresh }}>
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotifications() {
  return useContext(NotificationsContext);
}
