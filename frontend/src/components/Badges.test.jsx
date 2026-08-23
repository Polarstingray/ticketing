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
  function open(status = "open", props = {}) {
    const onChange = vi.fn();
    render(<StatusDropdown status={status} onChange={onChange} {...props} />);
    const trigger = screen.getByRole("button", { name: /Change status/i });
    fireEvent.click(trigger);
    return { onChange, trigger };
  }

  it("opens a listbox of every status and reports the pick", () => {
    const { onChange, trigger } = open();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("option", { name: "Resolved" }));

    expect(onChange).toHaveBeenCalledWith("resolved");
    // The menu closes behind the choice.
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("treats re-picking the current status as a no-op", () => {
    const { onChange } = open("in_review");
    fireEvent.click(screen.getByRole("option", { name: "In Review" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes on Escape and on an outside mousedown", () => {
    open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Change status/i }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes when focus leaves it, so the row link can't be activated behind an open menu", () => {
    const { trigger } = open();
    fireEvent.focusOut(trigger, { relatedTarget: document.body });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("stays shut while the row's request is in flight", () => {
    const { trigger } = open("open", { disabled: true });
    expect(trigger).toBeDisabled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
