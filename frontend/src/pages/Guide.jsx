import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import guide from "../content/guide.md?raw";
import styles from "../styles/Guide.module.css";

// Turn a section title into a stable anchor id ("The `stingray` CLI" -> "the-stingray-cli").
function slugify(title) {
  return title
    .toLowerCase()
    .replace(/[`'"’]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// Split the bundled markdown on its H2s: everything before the first one is the
// intro, and each H2 (with its H3 sub-parts) becomes one collapsible section.
function parseGuide(md) {
  const parts = md.split(/\n(?=## )/);
  const intro = parts[0];
  const sections = parts.slice(1).map((chunk) => {
    const nl = chunk.indexOf("\n");
    const head = nl === -1 ? chunk : chunk.slice(0, nl);
    const title = head.replace(/^##\s*/, "").trim();
    return {
      title,
      slug: slugify(title),
      content: nl === -1 ? "" : chunk.slice(nl + 1),
    };
  });
  return { intro, sections };
}

// A static, in-app operator guide. The prose is bundled markdown (guide.md),
// rendered with react-markdown so it stays easy to edit and consistent with the
// repo README/resolver docs it summarizes. Each H2 is a dropdown section, with a
// sticky appendix beside them for quick access.
export default function Guide() {
  const { intro, sections } = useMemo(() => parseGuide(guide), []);
  const [open, setOpen] = useState(() => new Set());

  function setSectionOpen(slug, isOpen) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (isOpen) next.add(slug);
      else next.delete(slug);
      return next;
    });
  }

  // Appendix links open the section they point at before jumping to it —
  // otherwise the anchor would land on a collapsed <details>.
  function jumpTo(e, slug) {
    e.preventDefault();
    setSectionOpen(slug, true);
    const el = document.getElementById(slug);
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "start" });
  }

  const allOpen = sections.length > 0 && open.size === sections.length;

  return (
    <div className={styles.wrap}>
      <div className={styles.layout}>
        <aside className={styles.sidebar}>
          <h2 className={styles.sidebarTitle}>Appendix</h2>
          <ul className={styles.toc}>
            {sections.map((s) => (
              <li key={s.slug}>
                <a
                  href={`#${s.slug}`}
                  className={open.has(s.slug) ? styles.tocActive : undefined}
                  onClick={(e) => jumpTo(e, s.slug)}
                >
                  {s.title}
                </a>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className={styles.toggleAll}
            onClick={() => setOpen(allOpen ? new Set() : new Set(sections.map((s) => s.slug)))}
          >
            {allOpen ? "Collapse all" : "Expand all"}
          </button>
        </aside>

        <div className={styles.main}>
          <article className={`card ${styles.doc}`}>
            <ReactMarkdown>{intro}</ReactMarkdown>
            {sections.map((s) => (
              <details
                key={s.slug}
                id={s.slug}
                className={styles.section}
                open={open.has(s.slug)}
                onToggle={(e) => setSectionOpen(s.slug, e.currentTarget.open)}
              >
                <summary className={styles.sectionTitle}>{s.title}</summary>
                <div className={styles.sectionBody}>
                  <ReactMarkdown>{s.content}</ReactMarkdown>
                </div>
              </details>
            ))}
          </article>
        </div>
      </div>
    </div>
  );
}
