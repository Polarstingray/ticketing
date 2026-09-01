import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useNotifications } from "../notifications/NotificationsContext";
import styles from "../styles/Settings.module.css";

// The backend returns the full matrix; these drive row/column order + labels.
const TYPES = [
  { value: "assigned", label: "Assigned to you" },
  { value: "commented", label: "New comment" },
];
const CHANNELS = [
  { value: "in_app", label: "In-app" },
  { value: "email", label: "Email" },
  { value: "page_title", label: "Page title" },
];

function key(type, channel) {
  return `${type}:${channel}`;
}

export default function Settings() {
  const { refreshPreferences } = useNotifications();
  // Map of "type:channel" -> enabled (bool).
  const [prefs, setPrefs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const res = await api.getNotificationPreferences();
        if (!active) return;
        const map = {};
        for (const it of res.items) map[key(it.type, it.channel)] = it.enabled;
        setPrefs(map);
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

  function toggle(type, channel) {
    setSaved(false);
    setPrefs((p) => ({ ...p, [key(type, channel)]: !p[key(type, channel)] }));
  }

  const items = useMemo(() => {
    if (!prefs) return [];
    return Object.entries(prefs).map(([k, enabled]) => {
      const [type, channel] = k.split(":");
      return { type, channel, enabled };
    });
  }, [prefs]);

  async function save() {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const res = await api.updateNotificationPreferences(items);
      const map = {};
      for (const it of res.items) map[key(it.type, it.channel)] = it.enabled;
      setPrefs(map);
      setSaved(true);
      refreshPreferences();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <h1>Settings</h1>

      <div className="card">
        <h2 className={styles.h2}>Notifications</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Choose how you’re notified for each event. Everything is on by default;
          turn off any channel you don’t want.
        </p>

        {error && <div className="error">{error}</div>}

        {loading ? (
          <div className="muted">Loading…</div>
        ) : (
          <>
            <table className={styles.grid}>
              <thead>
                <tr>
                  <th>Event</th>
                  {CHANNELS.map((c) => (
                    <th key={c.value} className={styles.channel}>
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {TYPES.map((t) => (
                  <tr key={t.value}>
                    <td className={styles.typeName}>{t.label}</td>
                    {CHANNELS.map((c) => (
                      <td key={c.value} className={styles.toggle}>
                        <input
                          type="checkbox"
                          aria-label={`${t.label} — ${c.label}`}
                          checked={!!prefs[key(t.value, c.value)]}
                          onChange={() => toggle(t.value, c.value)}
                        />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>

            <div className={styles.actions}>
              <button className="primary" onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
              {saved && <span className={styles.saved}>Saved</span>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
