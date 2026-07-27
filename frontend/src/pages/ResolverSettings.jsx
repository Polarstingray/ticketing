import { useEffect, useState } from "react";
import { api } from "../api";
import styles from "../styles/ResolverSettings.module.css";

// Non-secret resolver tunables, grouped into cards. `kind` drives how each field
// is rendered and (de)serialized to/from the settings blob the API stores.
//   text   -> string
//   number -> integer
//   bool   -> checkbox
//   list   -> comma-separated string <-> string[]
//   map    -> "name=path" lines       <-> { name: path }
//   workers-> "id:name:desc" lines    <-> [{ id, name, desc }]
const SECTIONS = [
  {
    title: "Models",
    fields: [
      { name: "agent_model", label: "Default model", kind: "text", hint: "Base model for every phase." },
      { name: "agent_plan_model", label: "Plan model", kind: "text", hint: "Overrides default for the plan phase." },
      { name: "agent_implement_model", label: "Implement model", kind: "text" },
      { name: "agent_review_model", label: "Review model", kind: "text" },
      { name: "agent_implement_model_easy", label: "Implement (easy)", kind: "text", hint: "Used when a plan self-rates the ticket easy." },
      { name: "agent_implement_model_hard", label: "Implement (hard)", kind: "text" },
      { name: "agent_fallback_models", label: "Fallback models", kind: "list", hint: "Comma-separated; tried in order after the primary." },
    ],
  },
  {
    title: "Sweep & attempts",
    fields: [
      { name: "max_attempts", label: "Max attempts", kind: "number", hint: "Plan/implement retries before giving up." },
      { name: "max_tickets_per_sweep", label: "Max tickets / sweep", kind: "number", hint: "0 = unlimited." },
      { name: "quota_backoff_minutes", label: "Quota backoff (min)", kind: "number" },
      { name: "audit_output_tail_bytes", label: "Audit tail bytes", kind: "number" },
    ],
  },
  {
    title: "Verification",
    fields: [
      { name: "verify_command", label: "Verify command", kind: "text", hint: "Run in the worktree before publishing (empty = skip)." },
      { name: "verify_timeout", label: "Verify timeout (s)", kind: "number" },
      { name: "verify_max_retries", label: "Verify max retries", kind: "number" },
    ],
  },
  {
    title: "Escalation",
    fields: [
      { name: "escalate_to_user_id", label: "Escalate to user id", kind: "number", hint: "0 = disabled. Hands hard tickets to this user." },
      { name: "escalate_priorities", label: "Escalate priorities", kind: "list", hint: "Comma-separated, e.g. high, critical." },
    ],
  },
  {
    title: "Delegation & repos",
    fields: [
      { name: "allow_delegation", label: "Allow delegation", kind: "bool" },
      { name: "max_delegations", label: "Max delegations", kind: "number" },
      { name: "critique_max_revisions", label: "Critique max revisions", kind: "number" },
      { name: "default_repo", label: "Default repo", kind: "text" },
      { name: "repo_map", label: "Repo map", kind: "map", hint: "One name=path per line." },
      { name: "workers", label: "Delegation roster", kind: "workers", hint: "One id:name:desc per line." },
    ],
  },
];

// --- settings blob -> editable form strings ---------------------------------
function toForm(field, value) {
  switch (field.kind) {
    case "bool":
      return !!value;
    case "list":
      return Array.isArray(value) ? value.join(", ") : "";
    case "map":
      return Object.entries(value || {})
        .map(([k, v]) => `${k}=${v}`)
        .join("\n");
    case "workers":
      return (value || []).map((w) => `${w.id}:${w.name}:${w.desc || ""}`).join("\n");
    default:
      return value === null || value === undefined ? "" : String(value);
  }
}

// --- editable form strings -> settings blob ---------------------------------
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
    case "map": {
      const map = {};
      for (const line of String(raw).split("\n")) {
        const t = line.trim();
        if (!t || !t.includes("=")) continue;
        const i = t.indexOf("=");
        map[t.slice(0, i).trim()] = t.slice(i + 1).trim();
      }
      return map;
    }
    case "workers": {
      const out = [];
      for (const line of String(raw).split("\n")) {
        const t = line.trim();
        if (!t) continue;
        const parts = t.split(":");
        const id = parseInt(parts[0], 10);
        const name = (parts[1] || "").trim();
        if (Number.isNaN(id) || !name) continue;
        out.push({ id, name, desc: parts.slice(2).join(":").trim() });
      }
      return out;
    }
    default:
      return String(raw);
  }
}

const ALL_FIELDS = SECTIONS.flatMap((s) => s.fields);

// Freshness of a resolver's last heartbeat. Sweeps run ~every 10 min, so treat
// anything within ~25 min as live, older as stale, and no heartbeat as unseen.
function relTime(ms) {
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
function freshness(lastSeen) {
  if (!lastSeen) return { cls: "never", label: "never seen" };
  const age = Date.now() - new Date(lastSeen).getTime();
  // An unparseable timestamp yields NaN, which relTime would render as "NaN d
  // ago" — treat it as no heartbeat at all.
  if (Number.isNaN(age)) return { cls: "never", label: "never seen" };
  return { cls: age < 25 * 60 * 1000 ? "live" : "stale", label: relTime(age) };
}

// A few effective-config fields worth showing in the read-only "Currently
// running" panel (what the resolver actually reported last sweep).
const RUNNING_FIELDS = [
  ["agent_model", "Model"],
  ["max_attempts", "Max attempts"],
  ["verify_command", "Verify command"],
  ["escalate_to_user_id", "Escalates to"],
  ["allow_delegation", "Delegation"],
];

export default function ResolverSettings() {
  const [roster, setRoster] = useState([]);
  const [selected, setSelected] = useState(null); // null = Global default, else bot_user_id
  const [form, setForm] = useState(null);
  const [secrets, setSecrets] = useState([]);
  const [meta, setMeta] = useState({ updated_at: null, updated_by: null });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  function hydrate(res) {
    const f = {};
    for (const field of ALL_FIELDS) f[field.name] = toForm(field, res.settings[field.name]);
    setForm(f);
    setSecrets(res.secrets || []);
    setMeta({ updated_at: res.updated_at, updated_by: res.updated_by });
  }

  async function loadRoster() {
    try {
      setRoster(await api.listResolvers());
    } catch {
      // Non-fatal: the settings form still works without the roster.
    }
  }

  async function select(botUserId) {
    setSelected(botUserId);
    setSaved(false);
    setForm(null);
    try {
      hydrate(await api.getResolverSettings(botUserId));
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        await loadRoster();
        const res = await api.getResolverSettings(); // global default first
        if (active) hydrate(res);
      } catch (e) {
        if (active) setError(e.message);
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
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
      const res = await api.updateResolverSettings(values, selected);
      hydrate(res);
      setSaved(true);
      loadRoster(); // refresh has_settings flags
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  const current = selected == null ? null : roster.find((r) => r.bot_user_id === selected);
  const scopeLabel =
    selected == null ? "Global default" : current ? current.name || current.display_name : `#${selected}`;

  return (
    <div className={styles.wrap}>
      <h1>Resolvers</h1>
      <p className="muted" style={{ marginTop: 0 }}>
        Manage each running resolver’s non-secret settings. Changes take effect on that
        resolver’s <strong>next sweep</strong>. A specific resolver inherits the
        <strong> Global default</strong> for anything it doesn’t override. Secrets stay in
        each resolver’s <code>.env</code>.
      </p>

      {error && <div className="error">{error}</div>}

      {loading ? (
        <div className="muted">Loading…</div>
      ) : (
        <>
          {/* --- Roster ------------------------------------------------------ */}
          <div className="card">
            <h2 className={styles.h2}>Resolvers</h2>
            <div className={styles.roster}>
              <button
                type="button"
                className={`${styles.rosterRow} ${styles.rosterGlobal} ${
                  selected == null ? styles.rosterActive : ""
                }`}
                onClick={() => select(null)}
              >
                <span className={styles.rosterName}>Global default</span>
                <span className="muted">applies to every resolver</span>
              </button>
              {roster.map((r) => {
                const fr = freshness(r.last_seen_at);
                return (
                  <button
                    type="button"
                    key={r.bot_user_id}
                    className={`${styles.rosterRow} ${selected === r.bot_user_id ? styles.rosterActive : ""}`}
                    onClick={() => select(r.bot_user_id)}
                  >
                    <span className={`${styles.dot} ${styles[fr.cls]}`} title={fr.label} />
                    <span className={styles.rosterName}>
                      {r.name || r.display_name}
                      {r.label && <span className={styles.rosterEnv}> {r.label}</span>}
                    </span>
                    <span className="muted">
                      {r.agent || "—"} {r.model ? `· ${r.model}` : ""}
                    </span>
                    <span className={styles.rosterMeta}>
                      {fr.label}
                      {r.has_settings ? " · overridden" : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* --- Currently running (read-only, per selected resolver) -------- */}
          {current && (
            <div className="card">
              <h2 className={styles.h2}>Currently running — {scopeLabel}</h2>
              {current.effective_config ? (
                <div className={styles.running}>
                  {RUNNING_FIELDS.map(([key, label]) => (
                    <div className={styles.runningItem} key={key}>
                      <span className="muted">{label}</span>
                      <span>{String(current.effective_config[key] ?? "—") || "—"}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted" style={{ margin: 0 }}>
                  This resolver hasn’t reported a sweep yet.
                </p>
              )}
            </div>
          )}

          <p className={styles.scope}>
            Editing settings for <strong>{scopeLabel}</strong>
            {selected != null && " (overrides the global default at its next sweep)"}.
          </p>

          {!form ? (
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
                    ) : field.kind === "map" || field.kind === "workers" ? (
                      <textarea
                        id={field.name}
                        value={form[field.name]}
                        onChange={(e) => update(field.name, e.target.value)}
                      />
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

          <div className="card">
            <h2 className={styles.h2}>Secrets</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              Managed in the server’s <code>.env</code> and never editable here.
            </p>
            {secrets.map((s) => (
              <div className={styles.secretRow} key={s.name}>
                <span>{s.label}</span>
                <span className={styles.secretVal}>•••• managed in {s.managed_in}</span>
              </div>
            ))}
          </div>

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
        </>
      )}
    </div>
  );
}
