import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useNotifications } from "../notifications/NotificationsContext";
import ChatWidget from "./ChatWidget";
import { StingrayIcon } from "./icons";
import styles from "../styles/Layout.module.css";

export default function Layout() {
  const { user, logout } = useAuth();
  const { unreadCount } = useNotifications();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  const linkClass = ({ isActive }) =>
    isActive ? `${styles.navLink} ${styles.active}` : styles.navLink;

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <StingrayIcon size={20} className={styles.logo} /> Stingray Tickets
        </div>
        <nav className={styles.nav}>
          <NavLink to="/tickets" className={linkClass}>
            Tickets
          </NavLink>
          <NavLink to="/tickets/new" className={linkClass}>
            New
          </NavLink>
          {user?.role === "admin" && (
            <NavLink to="/admin/users" className={linkClass}>
              Users
            </NavLink>
          )}
          {user?.role === "admin" && (
            <NavLink to="/admin/resolver-settings" className={linkClass}>
              Resolvers
            </NavLink>
          )}
          <NavLink to="/notifications" className={linkClass}>
            <span className={styles.bell}>🔔</span>
            Notifications
            {unreadCount > 0 && (
              <span className={styles.badge}>
                {unreadCount > 99 ? "99+" : unreadCount}
              </span>
            )}
          </NavLink>
          <NavLink to="/profile" className={linkClass}>
            Profile
          </NavLink>
          <NavLink to="/settings" className={linkClass} end>
            Settings
          </NavLink>
          <NavLink to="/settings/webhooks" className={linkClass}>
            Webhooks
          </NavLink>
          <NavLink to="/guide" className={linkClass}>
            Guide
          </NavLink>
        </nav>
        <div className={styles.userbox}>
          <span className={styles.username}>{user?.display_name}</span>
          <button onClick={handleLogout}>Log out</button>
        </div>
      </header>
      <main className={styles.content}>
        <Outlet />
      </main>
      {/* Present on every authenticated page; renders nothing when the
          deployment has no model configured. */}
      <ChatWidget />
    </div>
  );
}
