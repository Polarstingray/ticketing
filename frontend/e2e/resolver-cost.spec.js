import { test, expect } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Screenshot lands in docs/img/ so the README can show the resolver cost UI.
const IMG = path.resolve(__dirname, "../../docs/img");

const TITLE = "Review: batch the activity-feed queries (fix N+1)";

// The per-phase agent runs a resolver would post as it works a ticket. The
// admin session can write these (the endpoint lets admins backfill runs), so we
// don't need a live agent or the bot's key to render the costed timeline.
const RUNS = [
  { agent: "claude", phase: "plan", model: "claude-opus-4-8",
    input_tokens: 18420, output_tokens: 1960, cache_read_tokens: 12800,
    cache_write_tokens: 9400, cost_usd: 0.0731 },
  { agent: "claude", phase: "implement", model: "claude-opus-4-8",
    input_tokens: 41030, output_tokens: 6240, cache_read_tokens: 38200,
    cache_write_tokens: 15100, cost_usd: 0.2184 },
  { agent: "review-api", phase: "review", model: "claude-sonnet-5",
    input_tokens: 9870, output_tokens: 1310, cache_read_tokens: 0,
    cache_write_tokens: 0, cost_usd: 0.0492 },
];

test("resolver cost UI: agent-run timeline and cost badge", async ({ page }) => {
  // --- Login as admin --------------------------------------------------------
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill("admin");
  await page.locator('input[type="password"]').fill("adminpass123");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/tickets$/);

  // --- Create the code-review ticket the resolver "worked" -------------------
  await page.getByRole("button", { name: "New ticket" }).click();
  await page.locator("form input[required]").fill(TITLE);
  await page.getByPlaceholder(/Describe the task/).fill(
    "The detail page loads each activity's actor with its own query (N+1). " +
    "Batch them with a single load and add a covering index."
  );
  await page.locator('label:text-is("Priority") + select').selectOption("high");
  await page.getByRole("button", { name: "Create ticket" }).click();
  await expect(page.getByRole("heading", { name: new RegExp(TITLE) })).toBeVisible();

  // The ticket id is the last path segment of the detail URL.
  const ticketId = page.url().split("/").pop();

  // --- Seed the agent runs via the authenticated API -------------------------
  // page.request shares the browser's session cookie, so these POST as admin.
  for (const run of RUNS) {
    const res = await page.request.post(`/api/tickets/${ticketId}/agent-runs`, {
      data: run,
    });
    expect(res.status()).toBe(201);
  }

  // --- Reload so the detail page picks up the runs, then screenshot ----------
  await page.reload();
  const runs = page.getByRole("heading", { name: "Agent runs" }).locator("..");
  await expect(page.getByRole("heading", { name: "Agent runs" })).toBeVisible();
  // Cost badge sums the three runs (0.0731 + 0.2184 + 0.0492 = 0.3407).
  await expect(page.getByText("$0.3407")).toBeVisible();
  // Each phase label shows up in the timeline (scoped to the runs section).
  await expect(runs.getByText("Plan", { exact: true })).toBeVisible();
  await expect(runs.getByText("Implement", { exact: true })).toBeVisible();
  await expect(runs.getByText("Review", { exact: true })).toBeVisible();

  await page.screenshot({ path: path.join(IMG, "resolver-cost.png"), fullPage: true });
});
