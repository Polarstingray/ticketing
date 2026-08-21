import { describe, expect, it } from "vitest";
import {
  DEFAULT_FILTERS,
  activeFilterCount,
  clearedFilters,
  filtersToParams,
  filtersToQuery,
  paramsToFilters,
  toggleTag,
} from "./filters";

const params = (s) => new URLSearchParams(s);

describe("paramsToFilters", () => {
  it("defaults everything on an empty query string", () => {
    expect(paramsToFilters(params(""))).toEqual(DEFAULT_FILTERS);
  });

  it("collects repeated tag params into an array", () => {
    expect(paramsToFilters(params("tag=bug&tag=ui")).tags).toEqual(["bug", "ui"]);
  });

  it("drops blank tag values", () => {
    expect(paramsToFilters(params("tag=&tag=bug&tag=%20")).tags).toEqual(["bug"]);
  });

  it("falls back to defaults for values the backend would reject", () => {
    const f = paramsToFilters(params("sort=bogus&order=sideways&tag_match=some"));
    expect(f).toMatchObject({ sort: "created", order: "desc", tag_match: "all" });
  });

  it("reads the recognized sort and order through", () => {
    const f = paramsToFilters(params("sort=priority&order=asc"));
    expect(f).toMatchObject({ sort: "priority", order: "asc" });
  });
});

describe("filtersToParams", () => {
  it("omits defaults so an unfiltered view has a clean URL", () => {
    expect(filtersToParams(DEFAULT_FILTERS).toString()).toBe("");
  });

  it("round-trips a populated filter set", () => {
    const original = {
      ...DEFAULT_FILTERS,
      q: "crash",
      status: "open",
      tags: ["bug", "ui"],
      tag_match: "any",
      sort: "priority",
      order: "asc",
    };
    expect(paramsToFilters(filtersToParams(original))).toEqual(original);
  });

  it("emits one tag param per selected tag", () => {
    const p = filtersToParams({ ...DEFAULT_FILTERS, tags: ["bug", "ui"] });
    expect(p.getAll("tag")).toEqual(["bug", "ui"]);
  });

  it("omits tag_match below two tags, where it means nothing", () => {
    const one = filtersToParams({ ...DEFAULT_FILTERS, tags: ["bug"], tag_match: "any" });
    expect(one.has("tag_match")).toBe(false);

    const two = filtersToParams({ ...DEFAULT_FILTERS, tags: ["bug", "ui"], tag_match: "any" });
    expect(two.get("tag_match")).toBe("any");
  });
});

describe("filtersToQuery", () => {
  it("renames tags to the backend's repeatable tag param", () => {
    const q = filtersToQuery({ ...DEFAULT_FILTERS, tags: ["bug", "ui"] });
    expect(q.tag).toEqual(["bug", "ui"]);
    expect(q.tags).toBeUndefined();
  });

  it("carries tag_match only when more than one tag is selected", () => {
    expect(
      filtersToQuery({ ...DEFAULT_FILTERS, tags: ["bug"], tag_match: "any" }).tag_match
    ).toBeUndefined();
    expect(
      filtersToQuery({ ...DEFAULT_FILTERS, tags: ["bug", "ui"], tag_match: "any" }).tag_match
    ).toBe("any");
  });
});

describe("activeFilterCount", () => {
  it("is zero for an untouched filter set", () => {
    expect(activeFilterCount(DEFAULT_FILTERS)).toBe(0);
  });

  it("counts each selected tag separately", () => {
    expect(activeFilterCount({ ...DEFAULT_FILTERS, status: "open", tags: ["a", "b"] })).toBe(3);
  });

  it("does not count sort or order, which do not narrow the list", () => {
    expect(activeFilterCount({ ...DEFAULT_FILTERS, sort: "priority", order: "asc" })).toBe(0);
  });
});

describe("clearedFilters", () => {
  it("drops the filters but keeps the sort", () => {
    const cleared = clearedFilters({
      ...DEFAULT_FILTERS,
      q: "crash",
      tags: ["bug"],
      sort: "priority",
      order: "asc",
    });
    expect(cleared).toMatchObject({ q: "", tags: [], sort: "priority", order: "asc" });
  });
});

describe("toggleTag", () => {
  it("adds a tag at the end and removes it again", () => {
    expect(toggleTag(["a"], "b")).toEqual(["a", "b"]);
    expect(toggleTag(["a", "b"], "a")).toEqual(["b"]);
  });
});
