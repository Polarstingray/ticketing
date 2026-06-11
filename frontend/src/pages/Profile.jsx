import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { formatDate } from "../constants";
import styles from "../styles/Profile.module.css";

export default function Profile() {
  const { user } = useAuth();
  const [keys, setKeys] = useState([]);
  const [name, setName] = useState("");
  const [expiresInDays, setExpiresInDays] = useState("");
  const [created, setCreated] = useState(null); // the one-time plaintext key
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function loadKeys() {
    try {
      setKeys(await api.listApiKeys(user.id));
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    if (user) loadKeys();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  if (!user) return null;

  async function createKey(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError("");
    setCreated(null);
    setCopied(false);
    try {
      const body = { name: name.trim() };
      const days = parseInt(expiresInDays, 10);
      if (!Number.isNaN(days) && days > 0) body.expires_in_days = days;
      const key = await api.createApiKey(user.id, body);
      setCreated(key);
      setName("");
      setExpiresInDays("");
      loadKeys();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(keyId) {
    if (!window.confirm("Revoke this key? Anything using it will stop working immediately."))
      return;
    setError("");
    try {
      await api.revokeApiKey(user.id, keyId);
      loadKeys();
    } catch (e) {
      setError(e.message);
    }
  }

  async function copyCreated() {
    try {
      await navigator.clipboard.writeText(created.api_key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Could not copy to clipboard");
    }
  }

  function keyStatus(k) {
    if (k.revoked) return { label: "Revoked", cls: styles.stRevoked };
    if (k.expires_at && new Date(k.expires_at) <= new Date())
      return { label: "Expired", cls: styles.stExpired };
    return { label: "Active", cls: styles.stActive };
  }

  return (
    <div className={styles.wrap}>
      <h1>Profile</h1>

      <div className="card" style={{ marginBottom: 18 }}>
        <dl className={styles.info}>
          <dt>User ID</dt>
          <dd>{user.id}</dd>
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
        <h2 className={styles.h2}>API keys</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Create one key per machine/agent and use it with the <code>X-API-Key</code> header
          (e.g. Claude Code). To rotate, create a new key, swap it in, then revoke the old one.
          See <code>api_guide.md</code>.
        </p>

        {created && (
          <div className={styles.created}>
            <div className={styles.createdLabel}>
              New key <strong>{created.name}</strong> — copy it now, it won’t be shown again:
            </div>
            <div className={styles.keyRow}>
              <code className={styles.key}>{created.api_key}</code>
              <button onClick={copyCreated}>{copied ? "Copied!" : "Copy"}</button>
            </div>
          </div>
        )}

        {error && <div className="error">{error}</div>}

        {keys.length === 0 ? (
          <div className="muted">No API keys yet.</div>
        ) : (
          <table className={styles.keys}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Prefix</th>
                <th>Created</th>
                <th>Last used</th>
                <th>Expires</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => {
                const st = keyStatus(k);
                return (
                  <tr key={k.id}>
                    <td>{k.name}</td>
                    <td>
                      <code>{k.key_prefix}…</code>
                    </td>
                    <td>{formatDate(k.created_at)}</td>
                    <td>{k.last_used_at ? formatDate(k.last_used_at) : "never"}</td>
                    <td>{k.expires_at ? formatDate(k.expires_at) : "—"}</td>
                    <td>
                      <span className={`${styles.status} ${st.cls}`}>{st.label}</span>
                    </td>
                    <td>
                      {!k.revoked && (
                        <button className={styles.revoke} onClick={() => revoke(k.id)}>
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        <form onSubmit={createKey} className={styles.createForm}>
          <input
            type="text"
            placeholder="Key name (e.g. claude-code-laptop)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            type="number"
            min="1"
            placeholder="Expires (days, optional)"
            value={expiresInDays}
            onChange={(e) => setExpiresInDays(e.target.value)}
          />
          <button className="primary" type="submit" disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create key"}
          </button>
        </form>
      </div>
    </div>
  );
}
