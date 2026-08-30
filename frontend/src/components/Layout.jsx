import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useNotifications } from "../notifications/NotificationsContext";
import ChatWidget from "./ChatWidget";
import Sidebar from "./Sidebar";
import {
  BellIcon,
  DashboardIcon,
  GuideIcon,
  HookIcon,
  MenuIcon,
  NewIcon,
  RobotIcon,
  SettingsIcon,
  StingrayIcon,
  UserIcon,
  UsersIcon,
} from "./icons";
import styles from "../styles/Layout.module.css";

export default function Layout() {
  const { user, logout } = useAuth();
  const { unreadCount } = useNotifications();
  const navigate = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const hamburgerRef = useRef(null);

  // Close the drawer whenever the route changes.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  // Lock body scroll while the drawer is open.
  useEffect(() => {
    document.body.style.overflow = menuOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [menuOpen]);

  // Close the drawer on Escape; return focus to the hamburger button.
  useEffect(() => {
    if (!menuOpen) return;
    function onKey(e) {
      if (e.key === "Escape") {
        setMenuOpen(false);
        hamburgerRef.current?.focus();
      }
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
          ref={hamburgerRef}
          className={styles.hamburger}
          aria-label="Menu"
          aria-expanded={menuOpen}
          aria-controls="main-nav"
          onClick={() => setMenuOpen((o) => !o)}
        >
          <MenuIcon size={20} />
        </button>
        <div className={styles.brand}>
          <StingrayIcon size={20} className={styles.logo} />
          <span className={styles.brandText}>Stingray Tickets</span>
        </div>
        <nav id="main-nav" className={`${styles.nav} ${menuOpen ? styles.navOpen : ""}`}>
          <NavLink to="/tickets" className={linkClass}>
            <DashboardIcon size={16} className={styles.navIcon} />
            Tickets
          </NavLink>
          <NavLink to="/tickets/new" className={linkClass}>
            <NewIcon size={16} className={styles.navIcon} />
            New
          </NavLink>
          <NavLink to="/notifications" className={linkClass}>
            <BellIcon size={16} className={styles.navIcon} />
            Notifications
            {unreadCount > 0 && (
              <span className={styles.badge}>
                {unreadCount > 99 ? "99+" : unreadCount}
              </span>
            )}
          </NavLink>
          <NavLink to="/guide" className={linkClass}>
            <GuideIcon size={16} className={styles.navIcon} />
            Guide
          </NavLink>
          {/* Below: items that live in the sidebar on desktop. Hidden here at
              desktop widths (.sidebarOnly) so they aren't shown twice; the
              sidebar takes over at that breakpoint. Kept in this same drawer
              nav so mobile has a single unified menu, not a second one. */}
          <div className={styles.navDivider} aria-hidden="true" />
          {user?.role === "admin" && (
            <NavLink
              to="/admin/users"
              className={({ isActive }) => `${linkClass({ isActive })} ${styles.sidebarOnly}`}
            >
              <UsersIcon size={16} className={styles.navIcon} />
              Users
            </NavLink>
          )}
          {user?.role === "admin" && (
            <NavLink
              to="/admin/resolver-settings"
              className={({ isActive }) => `${linkClass({ isActive })} ${styles.sidebarOnly}`}
            >
              <RobotIcon size={16} className={styles.navIcon} />
              Resolvers
            </NavLink>
          )}
          <NavLink
            to="/profile"
            className={({ isActive }) => `${linkClass({ isActive })} ${styles.sidebarOnly}`}
          >
            <UserIcon size={16} className={styles.navIcon} />
            Profile
          </NavLink>
          <NavLink
            to="/settings"
            end
            className={({ isActive }) => `${linkClass({ isActive })} ${styles.sidebarOnly}`}
          >
            <SettingsIcon size={16} className={styles.navIcon} />
            Settings
          </NavLink>
          <NavLink
            to="/settings/webhooks"
            className={({ isActive }) => `${linkClass({ isActive })} ${styles.sidebarOnly}`}
          >
            <HookIcon size={16} className={styles.navIcon} />
            Webhooks
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
      <div className={styles.body}>
        <Sidebar />
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
      {/* Present on every authenticated page; renders nothing when the
          deployment has no model configured. */}
      <ChatWidget />
    </div>
  );
}
