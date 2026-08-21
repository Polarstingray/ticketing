/**
 * The dashboard's filter state, and its translation to and from the URL.
 *
 * The URL is the single source of truth for what the ticket list shows. That
 * buys three things at once: a filtered view is bookmarkable and shareable, the
 * back button restores it after visiting a ticket, and a "saved view" is just a
 * stored query string (see backend models.SavedView) rather than a second,
 * parallel representation that could drift.
 *
 * Param names match the backend's query params (routers/tickets.list_tickets),
 * so the URL's search string and the API request are near-identical — one less
 * mapping to keep in sync. The one shape difference is `tags`, which is an
 * array here and a repeated `?tag=` param on the wire.
 */

export const SORTS = ["created", "updated", "priority", "due", "title"];

export const SORT_LABELS = {
  created: "Newest",
  updated: "Recently updated",
  priority: "Priority",
  due: "Due date",
  title: "Title",
};

// Anything equal to its default is omitted from the URL, so an unfiltered list
// has a clean `/tickets` address and a saved view stores only what it changes.
export const DEFAULT_FILTERS = {
  q: "",
  type: "",
  status: "",
  priority: "",
  assigned_to: "",
  archived: "",
  tags: [],
  tag_match: "all",
  sort: "created",
  order: "desc",
};

// Keys that narrow the result set. `sort`/`order` are deliberately excluded:
// they change presentation, not membership, so they don't count as "active
// filters" and aren't cleared by "Clear all".
export const FILTER_KEYS = ["q", "type", "status", "priority", "assigned_to", "archived", "tags"];

function oneOf(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

/** Read filter state out of a URLSearchParams (or anything with .get/.getAll). */
export function paramsToFilters(searchParams) {
  const get = (k) => searchParams.get(k) || "";
  return {
    q: get("q"),
    type: get("type"),
    status: get("status"),
    priority: get("priority"),
    assigned_to: get("assigned_to"),
    archived: get("archived"),
    // Repeated ?tag=a&tag=b. Blank values are dropped so a stray `?tag=` can't
    // filter the list down to nothing.
    tags: searchParams.getAll("tag").filter((t) => t.trim() !== ""),
    // Unknown values fall back to the default rather than being passed through:
    // the backend rejects them with a 422, and a hand-edited URL shouldn't be
    // able to break the page.
    tag_match: oneOf(get("tag_match"), ["all", "any"], "all"),
    sort: oneOf(get("sort"), SORTS, "created"),
    order: oneOf(get("order"), ["asc", "desc"], "desc"),
  };
}

/** Serialize filter state back to a URLSearchParams, omitting defaults. */
export function filtersToParams(filters) {
  const params = new URLSearchParams();
  Object.entries(DEFAULT_FILTERS).forEach(([key, fallback]) => {
    const value = filters[key];
    if (key === "tags") {
      (value || []).forEach((tag) => params.append("tag", tag));
      return;
    }
    if (value !== undefined && value !== null && value !== "" && value !== fallback) {
      params.set(key, value);
    }
  });
  // tag_match only means something with more than one tag selected; keeping it
  // in the URL otherwise is noise that also makes two equivalent views compare
  // as different.
  if ((filters.tags || []).length < 2) params.delete("tag_match");
  return params;
}

/** Filter state as the params `api.listTickets` wants (`tags` -> `tag`). */
export function filtersToQuery(filters) {
  const { tags, tag_match, ...rest } = filters;
  const query = { ...rest, tag: tags };
  if ((tags || []).length < 2) delete query.tag_match;
  else query.tag_match = tag_match;
  return query;
}

/** How many distinct filters are narrowing the list right now. */
export function activeFilterCount(filters) {
  return FILTER_KEYS.reduce((n, key) => {
    const value = filters[key];
    if (key === "tags") return n + (value || []).length;
    return n + (value ? 1 : 0);
  }, 0);
}

/** Filter state with every narrowing filter cleared, but the sort preserved. */
export function clearedFilters(filters) {
  const cleared = { ...filters };
  FILTER_KEYS.forEach((key) => {
    cleared[key] = DEFAULT_FILTERS[key];
  });
  return cleared;
}

/** Toggle one tag in/out of the selection, preserving order. */
export function toggleTag(tags, tag) {
  return tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag];
}
