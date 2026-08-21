import { test, expect } from "@playwright/test";

// Unique per run so the spec is safe against a database that already has tickets
// (and against a re-run against the same one).
const RUN = Date.now().toString(36);
const TAG_A = `e2e-a-${RUN}`;
const TAG_B = `e2e-b-${RUN}`;

async function login(page) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill("admin");
  await page.locator('input[type="password"]').fill("adminpass123");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/tickets$/);
}

// Labels on the new-ticket form aren't associated with their inputs, so target
// them positionally the way happy-path.spec.js does.
const TAG_FIELD = 'label:text-is("Tags (comma-separated)") + input';

async function createTicket(page, title, tags, priority) {
  await page.goto("/tickets/new");
  await page.locator("form input[required]").fill(title);
  await page.locator(TAG_FIELD).fill(tags.join(", "));
  if (priority) {
    await page.locator('label:text-is("Priority") + select').selectOption(priority);
  }
  await page.getByRole("button", { name: "Create ticket" }).click();
  await expect(page.getByRole("heading", { name: new RegExp(title) })).toBeVisible();
}

test("filter the dashboard by several tags, share the URL, and save the view", async ({
  page,
}) => {
  await login(page);

  const both = `Both tags ${RUN}`;
  const onlyA = `Only A ${RUN}`;
  const onlyB = `Only B ${RUN}`;
  await createTicket(page, both, [TAG_A, TAG_B]);
  await createTicket(page, onlyA, [TAG_A]);
  await createTicket(page, onlyB, [TAG_B]);

  await page.goto("/tickets");
  const list = page.locator("a", { hasText: RUN });
  await expect(list).toHaveCount(3);

  // --- Select two tags; "All" is the default, so this narrows to the overlap ---
  await page.getByRole("checkbox", { name: TAG_A, exact: true }).check();
  await expect(page.locator("a", { hasText: RUN })).toHaveCount(2);

  await page.getByRole("checkbox", { name: TAG_B, exact: true }).check();
  await expect(page.getByText(both)).toBeVisible();
  await expect(page.getByText(onlyA)).toHaveCount(0);
  await expect(page.getByText(onlyB)).toHaveCount(0);

  // Both tags are in the URL, which is what makes the view shareable.
  await expect(page).toHaveURL(new RegExp(`tag=${TAG_A}.*tag=${TAG_B}`));

  // --- Any/All toggle widens to the union -------------------------------------
  await page.getByRole("button", { name: "Any", exact: true }).click();
  await expect(page.locator("a", { hasText: RUN })).toHaveCount(3);
  await page.getByRole("button", { name: "All", exact: true }).click();
  await expect(page.locator("a", { hasText: RUN })).toHaveCount(1);

  // --- The view survives a reload ---------------------------------------------
  const filteredUrl = page.url();
  await page.reload();
  await expect(page.getByText(both)).toBeVisible();
  await expect(page.locator("a", { hasText: RUN })).toHaveCount(1);

  // --- Save it, clear, and get back to it -------------------------------------
  const viewName = `Overlap ${RUN}`;
  await page.getByRole("button", { name: /Save current view/i }).click();
  await page.getByLabel(/Name for this view/i).fill(viewName);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  // exact: the delete button's aria-label is "Delete saved view <name>", which
  // Playwright's substring name matching would also resolve to.
  await expect(page.getByRole("button", { name: viewName, exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Clear all/i }).first().click();
  await expect(page.locator("a", { hasText: RUN })).toHaveCount(3);

  await page.getByRole("button", { name: viewName, exact: true }).click();
  await expect(page.locator("a", { hasText: RUN })).toHaveCount(1);
  await expect(page.getByText(both)).toBeVisible();
  await expect(page).toHaveURL(filteredUrl);
});

test("a filtered view survives opening a ticket and going back", async ({ page }) => {
  await login(page);

  const title = `Back button ${RUN}`;
  await createTicket(page, title, [TAG_A]);

  await page.goto(`/tickets?tag=${TAG_A}`);
  await page.getByText(title).click();
  await expect(page.getByRole("heading", { name: new RegExp(title) })).toBeVisible();

  await page.goBack();
  // The filter came back with the URL rather than being lost to component state.
  await expect(page).toHaveURL(new RegExp(`tag=${TAG_A}`));
  await expect(page.getByRole("checkbox", { name: TAG_A, exact: true })).toBeChecked();
});

test("sorting by priority ranks critical first, not alphabetically", async ({ page }) => {
  await login(page);

  const tag = `e2e-sort-${RUN}`;
  for (const [priority, label] of [
    ["low", `Low ${RUN}`],
    ["critical", `Critical ${RUN}`],
    ["medium", `Medium ${RUN}`],
  ]) {
    await createTicket(page, label, [tag], priority);
  }

  await page.goto(`/tickets?tag=${tag}&sort=priority`);
  const titles = page.locator("a", { hasText: RUN });
  await expect(titles).toHaveCount(3);
  await expect(titles.nth(0)).toContainText("Critical");
  await expect(titles.nth(2)).toContainText("Low");
});
