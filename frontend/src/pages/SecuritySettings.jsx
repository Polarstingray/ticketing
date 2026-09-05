import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import styles from "../styles/SecuritySettings.module.css";

// Settings that affect the app's security posture. `kind` drives how each
// field is (de)serialized to/from the settings blob the API stores — see
// ResolverSettings.jsx for the same convention (list/bool/number here; no
// map/workers kinds are needed for this panel).
const SECTIONS = [
  {
    title: "Webhooks",
    fields: [
      {
        name: "webhook_allowed_hosts",
        label: "SSRF-exempt hosts",
        kind: "list",
        hint: "Comma-separated exact hostnames exempted from the private/loopback address check — scheme, port, and the localhost/.internal/.local suffix check still apply.",
      },
      {
        name: "allow_insecure_webhooks",
        label: "Allow plain http",
        kind: "bool",
        hint: "Overrides ALLOW_INSECURE_WEBHOOKS for this deployment.",
      },
      {
        name: "dispatcher_paused",
        label: "Pause delivery",
        kind: "bool",
        hint: "Stops the dispatcher from claiming or sending anything, without a restart.",
      },
      {
        name: "max_webhooks_per_user",
        label: "Max webhooks / user",
        kind: "number",
      },
    ],
  },
  {
    title: "Ticket lease TTL policy window",
    fields: [
      {
        name: "min_lease_ttl",
        label: "Minimum (s)",
        kind: "number",
        hint: "Can only tighten the hard floor, never go below it.",
      },
      {
        name: "max_lease_ttl",
        label: "Maximum (s)",
        kind: "number",
        hint: "Can only tighten the hard ceiling, never exceed it.",
      },
      {
        name: "default_lease_ttl",
        label: "Default (s)",
        kind: "number",
        hint: "Used when a claim omits ttl_seconds. Must fall within [minimum, maximum] above.",
      },
    ],
  },
];

const ALL_FIELDS = SECTIONS.flatMap((s) => s.fields);

function toForm(field, value) {
  switch (field.kind) {
    case "bool":
      return !!value;
    case "list":
      return Array.isArray(value) ? value.join(", ") : "";
    default:
      return value === null || value === undefined ? "" : String(value);
  }
}

function fromForm(field, raw) {
  switch (field.kind) {
    case "bool":
      return !!raw;
    case "number": {
      const n = parseInt(raw, 10);
      return Number.isNaN(n) ? 0 : n;
    }
    case "list":
      return String(raw)
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    default:
      return String(raw);
  }
}

// `err.status === 401 && err.message === "reauth_required"` is how
// require_recent_admin (backend/auth.py) distinguishes "not admin" (403,
// handled by the route guard in App.jsx before this page even renders) from
// "admin, but the session is stale" — this page is the only place in the
// frontend that needs to tell the two apart.
function isReauthRequired(err) {
  return err && err.status === 401 && err.message === "reauth_required";
}

function ReauthPrompt({ onSuccess }) {
  const { user, login } = useAuth();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(user?.username, password);
      onSuccess();
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.reauth}>
      <h2 className={styles.h2}>Confirm it&rsquo;s you</h2>
      <p className="muted">
        Security settings need a fresh login. Enter your password to continue as{" "}
        <strong>{user?.username}</strong>.
      </p>
      <form onSubmit={onSubmit}>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            autoComplete="current-password"
          />
        </div>
        {error && <div className="error">{error}</div>}
        <button className="primary" type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Confirming…" : "Confirm"}
        </button>
      </form>
    </div>
  );
}

export default function SecuritySettings() {
  const [form, setForm] = useState(null);
  const [meta, setMeta] = useState({ updated_at: null, updated_by: null });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [needsReauth, setNeedsReauth] = useState(false);

  function hydrate(res) {
    const f = {};
    for (const field of ALL_FIELDS) f[field.name] = toForm(field, res.settings[field.name]);
    setForm(f);
    setMeta({ updated_at: res.updated_at, updated_by: res.updated_by });
  }

  async function load() {
    setError("");
    try {
      hydrate(await api.getSecuritySettings());
      setNeedsReauth(false);
    } catch (err) {
      if (isReauthRequired(err)) {
        setNeedsReauth(true);
      } else {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function update(name, value) {
    setSaved(false);
    setForm((f) => ({ ...f, [name]: value }));
  }

  async function save() {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const values = {};
      for (const field of ALL_FIELDS) values[field.name] = fromForm(field, form[field.name]);
      const res = await api.updateSecuritySettings(values);
      hydrate(res);
      setSaved(true);
    } catch (err) {
      if (isReauthRequired(err)) {
        setNeedsReauth(true);
      } else {
        setError(err.message);
      }
    } finally {
      setSaving(false);
    }
  }

  if (needsReauth) {
    return (
      <div className={styles.wrap}>
        <ReauthPrompt onSuccess={() => { setLoading(true); load(); }} />
      </div>
    );
  }

  return (
    <div className={styles.wrap}>
      <h1>Security settings</h1>
      <p className="muted" style={{ marginTop: 0 }}>
        Settings that affect the app&rsquo;s security posture. Editing here requires a fresh
        login — it stays gated even mid-session, since these are exactly the settings an
        attacker holding a hijacked-but-valid admin session would most want to weaken
        quietly.
      </p>

      {error && <div className="error">{error}</div>}

      {loading || !form ? (
        <div className="muted">Loading…</div>
      ) : (
        <>
          {SECTIONS.map((section) => (
            <div className="card" key={section.title}>
              <h2 className={styles.h2}>{section.title}</h2>
              {section.fields.map((field) => (
                <div className={styles.field} key={field.name}>
                  <label className={styles.label} htmlFor={field.name}>
                    {field.label}
                    {field.hint && <span className={styles.hint}>{field.hint}</span>}
                  </label>
                  <div className={styles.control}>
                    {field.kind === "bool" ? (
                      <div className={styles.checkbox}>
                        <input
                          id={field.name}
                          type="checkbox"
                          checked={!!form[field.name]}
                          onChange={(e) => update(field.name, e.target.checked)}
                        />
                        <span className="muted">Enabled</span>
                      </div>
                    ) : (
                      <input
                        id={field.name}
                        type={field.kind === "number" ? "number" : "text"}
                        value={form[field.name]}
                        onChange={(e) => update(field.name, e.target.value)}
                      />
                    )}
                  </div>
                </div>
              ))}
            </div>
          ))}

          <div className={styles.actions}>
            <button className="primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
            {saved && <span className={styles.saved}>Saved</span>}
            {meta.updated_at && (
              <span className={styles.updated}>
                Last updated {new Date(meta.updated_at).toLocaleString()}
                {meta.updated_by ? ` by user #${meta.updated_by}` : ""}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
