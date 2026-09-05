import { Fragment, useEffect, useState } from "react";
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

// Freshness of a worker's last heartbeat, sized from the cadence it reports
// rather than from a fixed number.
//
// A hardcoded window was wrong the moment sweep timers changed: a resolver that
// only heartbeats while sweeping goes quiet for the whole interval, so moving
// timers from 10 to 30 minutes made every healthy resolver display as stale. A
// worker that reports `heartbeat_seconds` is checking in on that cadence, and
// missing a few beats is the honest definition of "too quiet"; one that reports
// 0 only speaks while it sweeps, and we have no cadence to go on, so the old
// generous window stands for it.
function relTime(ms) {
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
const SWEEP_ONLY_WINDOW_MS = 45 * 60 * 1000;
const MISSED_BEATS_BEFORE_STALE = 3;

export function staleAfter(heartbeatSeconds) {
  if (!heartbeatSeconds || heartbeatSeconds <= 0) return SWEEP_ONLY_WINDOW_MS;
  return heartbeatSeconds * MISSED_BEATS_BEFORE_STALE * 1000;
}

export function freshness(lastSeen, heartbeatSeconds) {
  if (!lastSeen) return { cls: "never", label: "never seen" };
  const age = Date.now() - new Date(lastSeen).getTime();
  // An unparseable timestamp yields NaN, which relTime would render as "NaN d
  // ago" — treat it as no heartbeat at all.
  if (Number.isNaN(age)) return { cls: "never", label: "never seen" };
  return {
    cls: age < staleAfter(heartbeatSeconds) ? "live" : "stale",
    label: relTime(age),
  };
}

// Workers grouped by the host they run on, hosts in name order, with anything
// that has never reported one last. A station is only a label a worker sends,
// so this stays a display concern — nothing here assumes it is set.
export function byStation(entries) {
  const groups = new Map();
  for (const e of entries) {
    const key = e.station || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  return [...groups.entries()].sort(([a], [b]) => {
    if (a === b) return 0;
    if (!a) return 1;
    if (!b) return -1;
    return a.localeCompare(b);
  });
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
  const [agents, setAgents] = useState([]);
  const [enrollments, setEnrollments] = useState([]);
  const [enrollForm, setEnrollForm] = useState({ username: "", display_name: "" });
  const [minted, setMinted] = useState(null);   // the plaintext token, shown once
  const [enrollError, setEnrollError] = useState("");
  const [minting, setMinting] = useState(false);
  const [botKeys, setBotKeys] = useState([]);
  const [keysError, setKeysError] = useState("");
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

  async function loadEnrollments() {
    try {
      setEnrollments(await api.listEnrollments());
    } catch {
      // Same contract as the agent registry: this panel is an admin
      // convenience and must never stop the settings form rendering.
      setEnrollments([]);
    }
  }

  async function mint(e) {
    e.preventDefault();
    setEnrollError("");
    setMinted(null);
    setMinting(true);
    try {
      const created = await api.createEnrollment({
        username: enrollForm.username.trim(),
        display_name: enrollForm.display_name.trim(),
      });
      setMinted(created);
      setEnrollForm({ username: "", display_name: "" });
      loadEnrollments();
    } catch (err) {
      // A stale session is the expected failure here, not an error to
      // apologise for — explain the rule rather than just reporting a 401.
      setEnrollError(
        err && err.status === 401 && err.message === "reauth_required"
          ? "Minting needs a login from the last 15 minutes. That gate is why no API " +
            "key can create a bot — sign out and back in, then try again."
          : err.message || "Could not mint a token"
      );
    } finally {
      setMinting(false);
    }
  }

  async function revoke(id) {
    setEnrollError("");
    try {
      await api.revokeEnrollment(id);
      loadEnrollments();
    } catch (err) {
      setEnrollError(err.message || "Could not revoke");
    }
  }

  async function loadBotKeys(botUserId) {
    setKeysError("");
    if (botUserId == null) {
      setBotKeys([]);
      return;
    }
    try {
      setBotKeys(await api.listApiKeys(botUserId));
    } catch (err) {
      setBotKeys([]);
      setKeysError(err.message || "Could not list this bot's API keys");
    }
  }

  async function revokeBotKey(botUserId, keyId) {
    // Revoking is the only way to stop a resolver bot from acting, and there is
    // no undo — a new key has to be minted and redeployed to the host.
    if (
      !window.confirm(
        "Revoke this key? The resolver using it stops working immediately, and " +
          "getting it back means minting a new key and putting it on the host."
      )
    )
      return;
    setKeysError("");
    try {
      await api.revokeApiKey(botUserId, keyId);
      loadBotKeys(botUserId);
    } catch (err) {
      setKeysError(err.message || "Could not revoke");
    }
  }

  async function loadRoster() {
    try {
      setRoster(await api.listResolvers());
    } catch {
      // Non-fatal: the settings form still works without the roster.
    }
  }

  async function loadAgents() {
    try {
      // Resolver bots are already listed above as settings scopes; this panel is
      // about the workers that aren't ours, so they're filtered out here.
      const all = await api.listAgents();
      setAgents(all.filter((a) => !a.is_resolver_bot));
    } catch {
      // Non-fatal, same as the roster: liveness info is not load-bearing.
    }
  }

  async function select(botUserId) {
    setSelected(botUserId);
    setSaved(false);
    setForm(null);
    loadBotKeys(botUserId);
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
        await loadAgents();
        await loadEnrollments();
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
              {byStation(roster).map(([station, entries]) => (
                <Fragment key={station || "unassigned"}>
                  {byStation(roster).length > 1 && (
                    <div className={styles.stationHeading}>
                      {station || "no station reported"}
                    </div>
                  )}
                  {entries.map((r) => {
                const fr = freshness(r.last_seen_at, r.heartbeat_seconds);
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
                </Fragment>
              ))}
            </div>
          </div>

          {/* --- Station enrolment ------------------------------------------- */}
          <div className="card">
            <h2 className={styles.h2}>Enrol a resolver</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              Mint a one-shot token so a machine can collect the credentials for one
              named bot without ever holding an admin key. Redeem it there with{" "}
              <code>stingray station enroll &lt;token&gt;</code>. Tokens are single-use
              and expire in an hour.
            </p>

            <form onSubmit={mint} className={styles.enrollForm}>
              <div className="field">
                <label>Bot username</label>
                <input
                  value={enrollForm.username}
                  onChange={(e) =>
                    setEnrollForm({ ...enrollForm, username: e.target.value })
                  }
                  placeholder="gemini-bot"
                />
              </div>
              <div className="field">
                <label>Display name</label>
                <input
                  value={enrollForm.display_name}
                  onChange={(e) =>
                    setEnrollForm({ ...enrollForm, display_name: e.target.value })
                  }
                  placeholder="optional"
                />
              </div>
              <button
                className="primary"
                type="submit"
                disabled={minting || !enrollForm.username.trim()}
              >
                {minting ? "Minting…" : "Mint token"}
              </button>
            </form>

            {enrollError && <div className="error">{enrollError}</div>}

            {minted && (
              <div className={styles.mintedToken}>
                <strong>Copy this now — it is not shown again.</strong>
                <code>{minted.token}</code>
                <span className="muted">
                  For <strong>{minted.username}</strong>, expires{" "}
                  {new Date(minted.expires_at).toLocaleString()}.
                </span>
              </div>
            )}

            {enrollments.length === 0 ? (
              <p className="muted" style={{ marginBottom: 0 }}>
                No enrolments yet.
              </p>
            ) : (
              <div className={styles.roster}>
                {enrollments.map((en) => {
                  const spent = en.redeemed_at != null;
                  const expired =
                    !spent && new Date(en.expires_at).getTime() <= Date.now();
                  return (
                    <div key={en.id} className={`${styles.rosterRow} ${styles.agentRow}`}>
                      <span className={`${styles.dot} ${styles[spent ? "live" : expired ? "never" : "stale"]}`} />
                      <span className={styles.rosterName}>
                        {en.username}
                        <span className={styles.rosterEnv}> {en.token_prefix}…</span>
                      </span>
                      <span className="muted">
                        {spent
                          ? `redeemed${en.station ? ` on ${en.station}` : ""}`
                          : expired
                          ? "expired"
                          : `expires ${new Date(en.expires_at).toLocaleString()}`}
                      </span>
                      <span className={styles.rosterMeta}>
                        {spent ? (
                          // No revoke here on purpose: the token is already
                          // spent, and deleting this row would erase the record
                          // of how bot #N came to exist without touching the
                          // bot itself. Say so, and point at what does work.
                          <button
                            type="button"
                            onClick={() => select(en.redeemed_user_id)}
                            title="Already redeemed — revoking access means revoking this bot's API key"
                          >
                            bot #{en.redeemed_user_id} &rarr; keys
                          </button>
                        ) : (
                          <button type="button" onClick={() => revoke(en.id)}>
                            Revoke
                          </button>
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* --- External agents (read-only liveness) ------------------------ */}
          <div className="card">
            <h2 className={styles.h2}>External agents</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              Third-party workers authenticating with an <code>agent</code>-scoped API
              key. They carry their own configuration, so there is nothing to edit here —
              only who is live and when each last checked in.
            </p>
            {agents.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>
                No external agent has checked in yet.
              </p>
            ) : (
              <div className={styles.roster}>
                {agents.map((a) => {
                  const fr = freshness(a.last_seen_at, a.heartbeat_seconds);
                  return (
                    <div
                      key={a.user_id}
                      className={`${styles.rosterRow} ${styles.agentRow}`}
                    >
                      <span className={`${styles.dot} ${styles[fr.cls]}`} title={fr.label} />
                      <span className={styles.rosterName}>
                        {a.name || a.display_name || a.username}
                        {a.label && <span className={styles.rosterEnv}> {a.label}</span>}
                      </span>
                      <span className="muted">
                        {a.agent || "—"} {a.model ? `· ${a.model}` : ""}
                      </span>
                      <span className={styles.rosterMeta}>{fr.label}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* --- API keys (admin, per selected resolver) --------------------- */}
          {selected != null && (
            <div className="card">
              <h2 className={styles.h2}>API keys — {scopeLabel}</h2>
              <p className="muted" style={{ marginTop: 0 }}>
                Revoking is the only way to stop a resolver acting on this
                server. A spent enrolment cannot be withdrawn — the token is
                already used, and the record of how this bot came to exist is
                worth keeping — so this is what "revoke the bot's API key"
                means.
              </p>
              {keysError && <div className="error">{keysError}</div>}
              {botKeys.length === 0 ? (
                <p className="muted" style={{ margin: 0 }}>
                  {keysError ? "" : "This bot has no API keys."}
                </p>
              ) : (
                <div className={styles.roster}>
                  {botKeys.map((k) => (
                    <div key={k.id} className={`${styles.rosterRow} ${styles.agentRow}`}>
                      <span
                        className={`${styles.dot} ${styles[k.revoked ? "never" : "live"]}`}
                        title={k.revoked ? "revoked" : "active"}
                      />
                      <span className={styles.rosterName}>
                        {k.name}
                        <span className={styles.rosterEnv}> {k.key_prefix}…</span>
                      </span>
                      <span className="muted">
                        {k.scopes?.length ? k.scopes.join(", ") : "no scopes"}
                        {k.last_used_at
                          ? ` · last used ${relTime(Date.now() - new Date(k.last_used_at).getTime())}`
                          : " · never used"}
                      </span>
                      <span className={styles.rosterMeta}>
                        {k.revoked ? (
                          <span className="muted">revoked</span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => revokeBotKey(selected, k.id)}
                          >
                            Revoke
                          </button>
                        )}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

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
