import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import styles from "../styles/Login.module.css";

export default function Login() {
  const { user, login, loading } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Absent on every deployment but the public demo — a fetch failure is left
  // as null and simply renders no hint, since this is a courtesy, not
  // something the login form depends on to function.
  const [appConfig, setAppConfig] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api.appConfig()
      .then((cfg) => { if (!cancelled) setAppConfig(cfg); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (!loading && user) return <Navigate to="/tickets" replace />;

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(username, password);
      navigate("/tickets");
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <form className={styles.box} onSubmit={onSubmit}>
        <div className={styles.title}>
          <span>🐟</span> Stingray Tickets
        </div>
        <div className="field">
          <label>Username</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
          />
        </div>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        {error && <div className="error">{error}</div>}
        <button className="primary" type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {appConfig?.demo_username && (
        <div className={`card ${styles.demoHint}`}>
          {appConfig.read_only && (
            <div className="muted">
              This is a read-only public demo — nothing you do here writes.
            </div>
          )}
          <div>
            Log in with <code>{appConfig.demo_username}</code> /{" "}
            <code>{appConfig.demo_password}</code>
          </div>
        </div>
      )}
    </div>
  );
}
