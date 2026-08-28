import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import {
  AssigneeDropdown,
  StatusBadge,
  PriorityBadge,
  StatusDropdown,
  TypeBadge,
} from "./Badges";

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

describe("AssigneeDropdown", () => {
  const USERS = [
    { id: 5, display_name: "Ada Lovelace" },
    { id: 7, display_name: "Grace Hopper" },
  ];

  function open(assignedTo = null, props = {}) {
    const onChange = vi.fn();
    render(
      <AssigneeDropdown
        assignedTo={assignedTo}
        users={USERS}
        onChange={onChange}
        {...props}
      />
    );
    const trigger = screen.getByRole("button", { name: /Change assignee/i });
    fireEvent.click(trigger);
    return { onChange, trigger };
  }

  it("labels the trigger with the current assignee", () => {
    render(<AssigneeDropdown assignedTo={7} users={USERS} onChange={vi.fn()} />);
    expect(
      screen.getByRole("button", { name: /Assignee: Grace Hopper/i })
    ).toBeInTheDocument();
  });

  it("falls back to #id for an assignee who isn't in the users list", () => {
    render(<AssigneeDropdown assignedTo={99} users={USERS} onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Assignee: #99/i })).toBeInTheDocument();
  });

  it("offers Unassigned plus every user and reports the pick", () => {
    const { onChange, trigger } = open();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("option")).toHaveLength(USERS.length + 1);

    fireEvent.click(screen.getByRole("option", { name: "Ada Lovelace" }));

    expect(onChange).toHaveBeenCalledWith(5);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("hands the ticket back with null when Unassigned is picked", () => {
    const { onChange } = open(5);
    fireEvent.click(screen.getByRole("option", { name: "Unassigned" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("marks the current assignee selected and leaves Unassigned unselected", () => {
    open(5);
    expect(screen.getByRole("option", { name: "Ada Lovelace" })).toHaveAttribute(
      "aria-selected",
      "true"
    );
    expect(screen.getByRole("option", { name: "Unassigned" })).toHaveAttribute(
      "aria-selected",
      "false"
    );
  });

  it("selects Unassigned only for a null assignee, never for a stray undefined", () => {
    const { unmount } = render(
      <AssigneeDropdown assignedTo={null} users={USERS} onChange={vi.fn()} />
    );
    fireEvent.click(screen.getByRole("button", { name: /Change assignee/i }));
    expect(screen.getByRole("option", { name: "Unassigned" })).toHaveAttribute(
      "aria-selected",
      "true"
    );
    unmount();

    // `pick()` compares with strict `!==`, so aria-selected must be strict too:
    // an `undefined` assignedTo is not the Unassigned option.
    render(<AssigneeDropdown assignedTo={undefined} users={USERS} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Change assignee/i }));
    expect(screen.getByRole("option", { name: "Unassigned" })).toHaveAttribute(
      "aria-selected",
      "false"
    );
  });

  it("treats re-picking the current assignee as a no-op", () => {
    const { onChange } = open(7);
    fireEvent.click(screen.getByRole("option", { name: "Grace Hopper" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("closes on Escape, on an outside mousedown, and on focus-out", () => {
    const { trigger } = open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    fireEvent.focusOut(trigger, { relatedTarget: document.body });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("stays shut while the row's request is in flight", () => {
    const { trigger } = open(null, { disabled: true });
    expect(trigger).toBeDisabled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
