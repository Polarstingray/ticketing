import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TagPicker from "./TagPicker";

const FACETS = [
  { tag: "bug", count: 4 },
  { tag: "ui", count: 2 },
  { tag: "repo:ticketing", count: 9 },
  { tag: "claude:planning", count: 3 },
];

function renderPicker(props = {}) {
  const onToggle = vi.fn();
  const onMatchModeChange = vi.fn();
  render(
    <TagPicker
      tags={FACETS}
      selected={[]}
      matchMode="all"
      onToggle={onToggle}
      onMatchModeChange={onMatchModeChange}
      {...props}
    />
  );
  return { onToggle, onMatchModeChange };
}

describe("TagPicker", () => {
  it("shows free tags with their usage counts", () => {
    renderPicker();
    expect(screen.getByLabelText("bug")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("collapses workflow tags into their own group", () => {
    renderPicker();
    expect(screen.queryByLabelText("repo:ticketing")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Workflow tags/i }));
    expect(screen.getByLabelText("repo:ticketing")).toBeInTheDocument();
    expect(screen.getByLabelText("claude:planning")).toBeInTheDocument();
  });

  it("opens the workflow group when one of its tags is already selected", () => {
    // An active filter must never be hidden behind a collapsed section.
    renderPicker({ selected: ["repo:ticketing"] });
    expect(screen.getByLabelText("repo:ticketing")).toBeChecked();
  });

  it("reports a toggled tag", () => {
    const { onToggle } = renderPicker();
    fireEvent.click(screen.getByLabelText("bug"));
    expect(onToggle).toHaveBeenCalledWith("bug");
  });

  it("filters the list by the search box", () => {
    renderPicker();
    fireEvent.change(screen.getByLabelText("Find a tag"), { target: { value: "bu" } });
    expect(screen.getByLabelText("bug")).toBeInTheDocument();
    expect(screen.queryByLabelText("ui")).not.toBeInTheDocument();
  });

  it("keeps a selected tag visible even when the search excludes it", () => {
    renderPicker({ selected: ["ui"] });
    fireEvent.change(screen.getByLabelText("Find a tag"), { target: { value: "bug" } });
    expect(screen.getByLabelText("ui")).toBeInTheDocument();
  });

  it("disables the all/any toggle until two tags are selected", () => {
    renderPicker({ selected: ["bug"] });
    // Disabled rather than hidden, so it doesn't shift the layout mid-selection.
    expect(screen.getByRole("button", { name: "Any" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "All" })).toBeDisabled();
  });

  it("reports a match-mode change", () => {
    const { onMatchModeChange } = renderPicker({ selected: ["bug", "ui"] });
    fireEvent.click(screen.getByRole("button", { name: "Any" }));
    expect(onMatchModeChange).toHaveBeenCalledWith("any");
  });

  it("says so when there are no tags at all", () => {
    render(
      <TagPicker
        tags={[]}
        selected={[]}
        matchMode="all"
        onToggle={vi.fn()}
        onMatchModeChange={vi.fn()}
      />
    );
    expect(screen.getByText(/No tags on any ticket yet/i)).toBeInTheDocument();
  });
});
