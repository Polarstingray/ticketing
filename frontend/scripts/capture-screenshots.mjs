/**
 * Capture the README screenshots from the real UI.
 *
 * Drives the demo container (deploy/demo/) rather than the E2E stack, because
 * the demo seed is *curated* — a lived-in board with a realistic tag spread,
 * several users, and a resolver-worked ticket — whereas the E2E database holds
 * whatever two or three tickets a test happened to create. For a screenshot the
 * data is the subject, so it should be the good data.
 *
 * This is a capture, not a test — it lives outside e2e/ so `npm run test:e2e`
 * never picks it up, and so a test run can no longer overwrite committed assets.
 *
 *   docker build -f deploy/demo/Dockerfile -t stingray-demo .
 *   docker run -d --name stingray-demo-rec -p 3200:3000 stingray-demo
 *   cd frontend && node scripts/capture-screenshots.mjs
 *
 * Output: docs/img/*.png
 */
import { chromium } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(__dirname, "../../docs/img");

const BASE = process.env.DEMO_URL || "http://localhost:3200";
const SIZE = { width: 1280, height: 860 };
const USER = process.env.DEMO_USER || "admin";
const PASS = process.env.DEMO_PASS || "demopass123";

const settle = (page) => page.waitForTimeout(600);

async function shot(page, name, opts = {}) {
  const file = path.join(OUT_DIR, `${name}.png`);
  await settle(page);
  await page.screenshot({ path: file, ...opts });
  const kb = (fs.statSync(file).size / 1024).toFixed(0);
  console.log(`  ${name}.png (${kb} KB)`);
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: SIZE, deviceScaleFactor: 1 });
  const page = await context.newPage();

  console.log(`Capturing from ${BASE}`);

  // --- Login -----------------------------------------------------------------
  await page.goto(`${BASE}/login`);
  await page.locator('input[autocomplete="username"]').fill(USER);
  await page.locator('input[type="password"]').fill(PASS);
  await shot(page, "login");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/tickets$/);

  // --- The board, with the filter rail ---------------------------------------
  await page.getByRole("heading", { name: /^Tickets/ }).waitFor();
  await shot(page, "tickets", { fullPage: true });

  // --- Filtering: a free tag AND a workflow tag ------------------------------
  // This pair is chosen deliberately: `resolver` is a free tag and
  // `repo:ticketing` is reserved automation state, so the shot shows both groups
  // of the picker at once — including the Workflow group expanded — and still
  // narrows to a real result set rather than an empty one.
  await page.getByRole("checkbox", { name: "resolver", exact: true }).check();
  await settle(page);
  await page.getByRole("button", { name: /Workflow tags/i }).click();
  await settle(page);
  await page.getByRole("checkbox", { name: "repo:ticketing", exact: true }).check();
  await page.getByLabel("Sort by").selectOption("priority");
  // Checking a tag low in the list scrolls the rail's own scroll container;
  // rewind it so the shot shows the panel header (active count + Clear all).
  await page.locator("#filter-panel").evaluate((el) => (el.scrollTop = 0));
  await shot(page, "filtering", { fullPage: true });

  // --- Ticket detail + the resolver's costed timeline -------------------------
  // Same ticket the walkthrough video features: it is the one the resolver
  // actually worked, so it carries the per-phase agent runs.
  await page.goto(`${BASE}/tickets`);
  await page.getByRole("heading", { name: /^Tickets/ }).waitFor();
  await page.getByText("Review: batch the activity-feed queries").first().click();
  await page.waitForURL(/\/tickets\/\d+$/);
  await shot(page, "ticket-detail", { fullPage: true });

  const runs = page.getByRole("heading", { name: "Agent runs" });
  await runs.waitFor();
  await runs.scrollIntoViewIfNeeded();
  await shot(page, "resolver-cost", { fullPage: true });

  await context.close();
  await browser.close();
  console.log(`Wrote to ${OUT_DIR}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
