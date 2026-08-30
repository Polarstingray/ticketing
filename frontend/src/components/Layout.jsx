import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useNotifications } from "../notifications/NotificationsContext";
import ChatWidget from "./ChatWidget";
import { BellIcon, StingrayIcon } from "./icons";
import styles from "../styles/Layout.module.css";

export default function Layout() {
  const { user, logout } = useAuth();
  const { unreadCount } = useNotifications();
  const navigate = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  // Close the drawer whenever the route changes.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  // Close the drawer on Escape.
  useEffect(() => {
    if (!menuOpen) return;
    function onKey(e) {
      if (e.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  async function handleLogout() {
    await logout();
    navigate("/login");
  }

  const linkClass = ({ isActive }) =>
    isActive ? `${styles.navLink} ${styles.active}` : styles.navLink;

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <button
          className={styles.hamburger}
          aria-label="Menu"
          aria-expanded={menuOpen}
          aria-controls="main-nav"
          onClick={() => setMenuOpen((o) => !o)}
        >
          <span className={styles.hamburgerLine} />
          <span className={styles.hamburgerLine} />
          <span className={styles.hamburgerLine} />
        </button>
        <div className={styles.brand}>
          <StingrayIcon size={20} className={styles.logo} />
          <span className={styles.brandText}>Stingray Tickets</span>
        </div>
        <nav id="main-nav" className={`${styles.nav} ${menuOpen ? styles.navOpen : ""}`}>
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
            <BellIcon size={16} className={styles.bell} />
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
          {/* In the mobile drawer the userbox moves here so it's reachable. */}
          <div className={styles.drawerUserbox}>
            <span className={styles.username}>{user?.display_name}</span>
            <button onClick={handleLogout}>Log out</button>
          </div>
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
