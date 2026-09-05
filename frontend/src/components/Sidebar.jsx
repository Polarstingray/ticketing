import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import {
  MenuIcon,
  RobotIcon,
  SettingsIcon,
  ShieldIcon,
  UserIcon,
  UsersIcon,
  HookIcon,
} from "./icons";
import styles from "../styles/Sidebar.module.css";

export default function Sidebar() {
  const { user } = useAuth();
  const location = useLocation();
  const [expanded, setExpanded] = useState(false);
  const rootRef = useRef(null);
  const toggleRef = useRef(null);

  // Collapse back to the icon rail whenever the route changes.
  useEffect(() => {
    setExpanded(false);
  }, [location.pathname]);

  // Close on outside click or Escape, same convention as StatusDropdown/AssigneeDropdown.
  useEffect(() => {
    if (!expanded) return;
    function onMouseDown(e) {
      if (!rootRef.current?.contains(e.target)) setExpanded(false);
    }
    function onKey(e) {
      if (e.key === "Escape") {
        setExpanded(false);
        toggleRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [expanded]);

  const linkClass = ({ isActive }) =>
    isActive ? `${styles.navLink} ${styles.active}` : styles.navLink;

  return (
    <aside ref={rootRef} className={styles.sidebar}>
      <button
        ref={toggleRef}
        type="button"
        className={styles.toggle}
        aria-label="Toggle sidebar"
        aria-expanded={expanded}
        onClick={() => setExpanded((e) => !e)}
      >
        <MenuIcon size={20} />
      </button>
      <nav className={`${styles.nav} ${expanded ? styles.navExpanded : ""}`}>
        {expanded && (
          <button
            type="button"
            className={styles.close}
            aria-label="Close sidebar"
            onClick={() => setExpanded(false)}
          >
            ×
          </button>
        )}
        {user?.role === "admin" && (
          <NavLink to="/admin/users" className={linkClass} title="Users">
            <UsersIcon size={18} />
            <span className={styles.label}>Users</span>
          </NavLink>
        )}
        {user?.role === "admin" && (
          <NavLink to="/admin/resolver-settings" className={linkClass} title="Resolvers">
            <RobotIcon size={18} />
            <span className={styles.label}>Resolvers</span>
          </NavLink>
        )}
        {user?.role === "admin" && (
          <NavLink to="/admin/security-settings" className={linkClass} title="Security">
            <ShieldIcon size={18} />
            <span className={styles.label}>Security</span>
          </NavLink>
        )}
        <NavLink to="/profile" className={linkClass} title="Profile">
          <UserIcon size={18} />
          <span className={styles.label}>Profile</span>
        </NavLink>
        <NavLink to="/settings" className={linkClass} end title="Settings">
          <SettingsIcon size={18} />
          <span className={styles.label}>Settings</span>
        </NavLink>
        <NavLink to="/settings/webhooks" className={linkClass} title="Webhooks">
          <HookIcon size={18} />
          <span className={styles.label}>Webhooks</span>
        </NavLink>
      </nav>
    </aside>
  );
}
