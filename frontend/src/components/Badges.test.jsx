import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { StatusBadge, PriorityBadge, StatusDropdown, TypeBadge } from "./Badges";

describe("badges", () => {
  it("renders a human label for a known status", () => {
    render(<StatusBadge status="in_review" />);
    expect(screen.getByText("In Review")).toBeInTheDocument();
  });

  it("falls back to the raw value for an unknown status", () => {
    render(<StatusBadge status="weird" />);
    expect(screen.getByText("weird")).toBeInTheDocument();
  });

  it("renders priority and type labels", () => {
    render(
      <>
        <PriorityBadge priority="high" />
        <TypeBadge type="code_review" />
      </>
    );
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText("Code Review")).toBeInTheDocument();
  });
});

describe("StatusDropdown", () => {
  function renderDropdown(props = {}) {
    const onChange = props.onChange || vi.fn();
    render(
      <>
        <button type="button">outside</button>
        <StatusDropdown status="open" onChange={onChange} {...props} />
      </>
    );
    return { onChange, trigger: screen.getByRole("button", { name: /Change status/i }) };
  }

  it("opens a listbox of every status and reports the pick", () => {
    const { onChange, trigger } = renderDropdown();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "Open",
      "In Review",
      "Changes Requested",
      "Resolved",
      "Closed",
    ]);

    fireEvent.click(screen.getByRole("option", { name: "Resolved" }));
    expect(onChange).toHaveBeenCalledWith("resolved");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("does not fire a change when the current status is re-picked", () => {
    const { onChange, trigger } = renderDropdown();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("option", { name: "Open" }));

    expect(onChange).not.toHaveBeenCalled();
    // Still closes — the user made a choice, it just wasn't a change.
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes on Escape", () => {
    const { trigger } = renderDropdown();
    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes on a mousedown outside it", () => {
    const { trigger } = renderDropdown();
    fireEvent.click(trigger);
    fireEvent.mouseDown(screen.getByRole("button", { name: "outside" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes when focus leaves, but not while it moves inside", () => {
    const { trigger } = renderDropdown();
    fireEvent.click(trigger);

    // Tabbing from the trigger onto an option keeps the menu up.
    const option = screen.getByRole("option", { name: "Resolved" });
    fireEvent.focusOut(trigger, { relatedTarget: option });
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    // Tabbing past the last option closes it, so a stray Enter on whatever is
    // focused next can't act with the menu still hanging over the page.
    fireEvent.focusOut(option, {
      relatedTarget: screen.getByRole("button", { name: "outside" }),
    });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("will not open while disabled", () => {
    const { trigger } = renderDropdown({ disabled: true });
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
