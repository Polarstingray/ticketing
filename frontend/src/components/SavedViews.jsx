import { useEffect, useState } from "react";
import { api } from "../api";
import styles from "../styles/FilterPanel.module.css";

/**
 * The caller's named filter presets.
 *
 * A view stores the dashboard's raw query string, so applying one is just
 * pushing that string into the URL — the same thing pasting a shared link does.
 * That's why there's no separate "apply these filters" code path here.
 *
 * Props:
 *   currentQuery — the URL's current search string (without "?").
 *   onApply      — hands a stored query string back to the page.
 */
export default function SavedViews({ currentQuery, onApply }) {
  const [views, setViews] = useState([]);
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listSavedViews()
      .then(setViews)
      .catch(() => setViews([]));
  }, []);

  async function save(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setError("");
    try {
      const view = await api.createSavedView({ name: name.trim(), query: currentQuery });
      setViews((prev) => [...prev, view].sort((a, b) => a.name.localeCompare(b.name)));
      setNaming(false);
      setName("");
    } catch (err) {
      // A duplicate name (409) is the common case and is worth showing inline
      // rather than dropping the user back to a blank form.
      setError(err.message);
    }
  }

  async function remove(view) {
    setError("");
    try {
      await api.deleteSavedView(view.id);
      setViews((prev) => prev.filter((v) => v.id !== view.id));
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className={styles.section}>
      <h3 className={styles.sectionTitle}>Saved views</h3>

      {views.length === 0 && !naming && (
        <p className={styles.emptyHint}>Save a set of filters you come back to often.</p>
      )}

      {views.length > 0 && (
        <ul className={styles.viewList}>
          {views.map((view) => (
            <li key={view.id} className={styles.viewRow}>
              <button
                type="button"
                className={
                  view.query === currentQuery
                    ? `${styles.viewApply} ${styles.viewActive}`
                    : styles.viewApply
                }
                onClick={() => onApply(view.query)}
                title={view.query || "No filters"}
              >
                {view.name}
              </button>
              <button
                type="button"
                className={styles.viewDelete}
                aria-label={`Delete saved view ${view.name}`}
                onClick={() => remove(view)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      {error && <p className={styles.viewError}>{error}</p>}

      {naming ? (
        <form className={styles.viewForm} onSubmit={save}>
          <input
            autoFocus
            value={name}
            aria-label="Name for this view"
            placeholder="Name this view…"
            maxLength={60}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setNaming(false)}
          />
          <button type="submit" className="primary">
            Save
          </button>
        </form>
      ) : (
        // Saving an unfiltered view would just store "" — nothing to come back
        // to — so the affordance only appears once there is a query to keep.
        currentQuery && (
          <button type="button" className={styles.saveView} onClick={() => setNaming(true)}>
            + Save current view
          </button>
        )
      )}
    </div>
  );
}
