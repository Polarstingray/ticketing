import { describe, it, expect } from "vitest";
import {
  describeActivity,
  formatDate,
  formatTokens,
  formatUsd,
  isReservedTag,
  totalTokens,
} from "./constants";

describe("isReservedTag", () => {
  it("treats claude:* and repo:* prefixes as reserved", () => {
    expect(isReservedTag("claude:model")).toBe(true);
    expect(isReservedTag("repo:ticketing")).toBe(true);
  });

  it("treats the exact control tags as reserved", () => {
    expect(isReservedTag("dangerous")).toBe(true);
    expect(isReservedTag("fix")).toBe(true);
  });

  it("leaves ordinary tags editable", () => {
    expect(isReservedTag("backend")).toBe(false);
    expect(isReservedTag("repository")).toBe(false); // not the repo: prefix
    expect(isReservedTag("fixme")).toBe(false); // not exactly "fix"
  });
});

describe("totalTokens", () => {
  it("sums all four token buckets", () => {
    expect(
      totalTokens({
        input_tokens: 1,
        output_tokens: 2,
        cache_read_tokens: 3,
        cache_write_tokens: 4,
      })
    ).toBe(10);
  });

  it("treats missing buckets as zero", () => {
    expect(totalTokens({ input_tokens: 5 })).toBe(5);
    expect(totalTokens({})).toBe(0);
  });
});

describe("formatTokens / formatUsd", () => {
  it("formats token counts with thousands separators", () => {
    expect(formatTokens(1234567)).toBe("1,234,567");
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(null)).toBe("0");
  });

  it("formats USD to fractional-cent precision", () => {
    expect(formatUsd(0.0123)).toBe("$0.0123");
    expect(formatUsd(12.3456)).toBe("$12.3456");
    expect(formatUsd(null)).toBe("$0.0000");
  });
});

describe("describeActivity", () => {
  it("describes creation and comments", () => {
    expect(describeActivity({ action: "created" })).toBe("created the ticket");
    expect(describeActivity({ action: "commented" })).toBe("commented");
  });

  it("names the assignee when known and falls back to an id", () => {
    expect(describeActivity({ action: "assigned", detail: { name: "Ada" } })).toBe(
      "assigned it to Ada"
    );
    expect(describeActivity({ action: "assigned", detail: { to: 7 } })).toBe(
      "assigned it to #7"
    );
  });

  it("uses human labels for status and priority changes", () => {
    expect(
      describeActivity({ action: "status_changed", detail: { from: "open", to: "resolved" } })
    ).toBe("changed status from Open to Resolved");
    expect(
      describeActivity({ action: "priority_changed", detail: { from: "low", to: "high" } })
    ).toBe("changed priority from Low to High");
  });

  it("summarizes tag additions and removals", () => {
    expect(
      describeActivity({ action: "tags_changed", detail: { added: ["a"], removed: ["b"] } })
    ).toBe("added a and removed b");
    expect(describeActivity({ action: "tags_changed", detail: {} })).toBe("changed tags");
  });

  it("humanizes an unknown action by de-underscoring it", () => {
    expect(describeActivity({ action: "some_new_thing" })).toBe("some new thing");
  });
});

describe("formatDate", () => {
  it("returns an em dash for empty or invalid input", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("")).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
  });

  it("treats a timezone-less ISO string as UTC (not local time)", () => {
    // A naive timestamp and its explicit-UTC form must render identically;
    // if the naive one were parsed as local time the two would diverge.
    expect(formatDate("2026-01-02T03:04:05")).toBe(formatDate("2026-01-02T03:04:05Z"));
  });
});
