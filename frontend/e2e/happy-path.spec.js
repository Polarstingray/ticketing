import { test, expect } from "@playwright/test";

// A stable title so we can find the ticket again on the list page.
const TITLE = "Batch the activity-feed queries";

test("log in, create a ticket, comment, and resolve it", async ({ page }) => {
  // --- Login -----------------------------------------------------------------
  await page.goto("/login");
  await expect(page.getByText("Stingray Tickets")).toBeVisible();

  await page.locator('input[autocomplete="username"]').fill("admin");
  await page.locator('input[type="password"]').fill("adminpass123");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/tickets$/);
  await expect(page.getByRole("heading", { name: /^Tickets/ })).toBeVisible();

  // --- Create a ticket -------------------------------------------------------
  await page.getByRole("button", { name: "New ticket" }).click();
  await expect(page).toHaveURL(/\/tickets\/new$/);

  await page.locator("form input[required]").fill(TITLE);
  await page.getByPlaceholder(/Describe the task/).fill(
    "Listing activity issues one query per row. Please batch it."
  );
  // Labels aren't associated with inputs, so target the select by its sibling label.
  await page.locator('label:text-is("Priority") + select').selectOption("high");
  await page.getByRole("button", { name: "Create ticket" }).click();

  // Lands on the new ticket's detail page. Assert the priority carried through via
  // the sidebar select's value (unambiguous — "High" also appears as the badge text).
  await expect(page.getByRole("heading", { name: new RegExp(TITLE) })).toBeVisible();
  await expect(page.locator('label:text-is("Priority") + select')).toHaveValue("high");

  // --- Comment ---------------------------------------------------------------
  // Markdown body: the fence and the emphasis should come back rendered, not literal.
  const BODY = [
    "On it — batching with a **single** IN query.",
    "",
    "```sql",
    "SELECT * FROM activity WHERE ticket_id IN (:ids)",
    "```",
  ].join("\n");
  await page.getByPlaceholder("Add a comment…").fill(BODY);
  await page.getByRole("button", { name: "Comment" }).click();
  await expect(page.getByText("On it — batching with a")).toBeVisible();
  await expect(page.locator("strong", { hasText: "single" }).first()).toBeVisible();
  await expect(page.locator("pre", { hasText: "SELECT * FROM activity" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy" })).toBeVisible();

  // --- Resolve ---------------------------------------------------------------
  await page.locator('label:text-is("Status") + select').selectOption("resolved");
  await expect(page.locator('label:text-is("Status") + select')).toHaveValue("resolved");
  // Status change is recorded on the activity trail.
  await expect(page.getByText(/changed status .* to Resolved/).first()).toBeVisible();

  // --- Back to the list ------------------------------------------------------
  await page.getByRole("link", { name: /Tickets/ }).first().click();
  await expect(page).toHaveURL(/\/tickets$/);
  await expect(page.getByText(TITLE).first()).toBeVisible();
});
