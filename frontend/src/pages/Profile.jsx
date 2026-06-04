import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { formatDate } from "../constants";
import styles from "../styles/Profile.module.css";

export default function Profile() {
  const { user, setUser } = useAuth();
  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!user) return null;

  const key = user.api_key || "";
  const masked = key ? key.slice(0, 6) + "••••••••••••••••••••" : "—";

  async function regenerate() {
    if (
      !window.confirm(
        "Regenerate your API key? The old key will stop working immediately."
      )
    )
      return;
    setBusy(true);
    setError("");
    try {
      const { api_key } = await api.regenerateApiKey(user.id);
      setUser({ ...user, api_key });
      setRevealed(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Could not copy to clipboard");
    }
  }

  return (
    <div className={styles.wrap}>
      <h1>Profile</h1>

      <div className="card" style={{ marginBottom: 18 }}>
        <dl className={styles.info}>
          <dt>Display name</dt>
          <dd>{user.display_name}</dd>
          <dt>Username</dt>
          <dd>{user.username}</dd>
          <dt>Email</dt>
          <dd>{user.email}</dd>
          <dt>Role</dt>
          <dd>{user.role}</dd>
          <dt>Member since</dt>
          <dd>{formatDate(user.created_at)}</dd>
        </dl>
      </div>

      <div className="card">
        <h2 className={styles.h2}>API key</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Use this key with the <code>X-API-Key</code> header for programmatic access (e.g.
          Claude Code). See <code>api_guide.md</code> for usage.
        </p>
        <div className={styles.keyRow}>
          <code className={styles.key}>{revealed ? key || "—" : masked}</code>
          <button onClick={() => setRevealed((r) => !r)}>
            {revealed ? "Hide" : "Reveal"}
          </button>
          <button onClick={copy} disabled={!key}>
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
        {error && <div className="error">{error}</div>}
        <button className="danger" onClick={regenerate} disabled={busy} style={{ marginTop: 14 }}>
          {busy ? "Regenerating…" : "Regenerate API key"}
        </button>
      </div>
    </div>
  );
}
