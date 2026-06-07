import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";

// Holds the unread-notification count and a refresh() so the nav badge and the
// Notifications page stay in sync. Polls unread_count on an interval and on
// demand. Only polls while a user is authenticated.
const NotificationsContext = createContext({ unreadCount: 0, refresh: () => {} });

const POLL_MS = 30000;

export function NotificationsProvider({ children }) {
  const { user } = useAuth();
  const [unreadCount, setUnreadCount] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const { unread_count } = await api.unreadCount();
      setUnreadCount(unread_count);
    } catch {
      // best-effort: leave the last known count on transient failures
    }
  }, []);

  useEffect(() => {
    if (!user) {
      setUnreadCount(0);
      return undefined;
    }
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [user, refresh]);

  return (
    <NotificationsContext.Provider value={{ unreadCount, refresh }}>
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotifications() {
  return useContext(NotificationsContext);
}
