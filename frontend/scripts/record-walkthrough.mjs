/**
 * Record the demo walkthrough as a video.
 *
 * Drives the demo container (deploy/demo/) with a real browser and captures a
 * ~100s tour: the board, filtering it down by tag and re-sorting, the resolver's
 * costed agent-run timeline, the chat assistant answering a question about that
 * ticket, the delegation rollup, and one human create → comment → resolve loop.
 *
 * This is a recording, not a test — it lives outside e2e/ so `npm run test:e2e`
 * (and CI) never picks it up. It expects the demo's seeded data, so point it at
 * a demo container, never a real instance:
 *
 *   docker build -f deploy/demo/Dockerfile -t stingray-demo .
 *   docker run -d --name stingray-demo-rec -p 3200:3000 \
 *     -e CHAT_API_URL=https://openrouter.ai/api/v1/chat/completions \
 *     -e CHAT_API_KEY="sk-or-v1-..." \
 *     -e CHAT_API_MODEL=openai/gpt-4o-mini \
 *     -e READ_ONLY=false \
 *     stingray-demo
 *   cd frontend && node scripts/record-walkthrough.mjs
 *
 * The chat beat below waits on the real assistant response, so CHAT_API_KEY
 * must be a working key when recording — without it the launcher never
 * renders (config.enabled is false) and that beat throws. READ_ONLY=false is
 * just as load-bearing: the image defaults to READ_ONLY=true (right, for the
 * public Fly demo), and the scene 5 create → comment → resolve loop 403s
 * without this override — it's only the Fly deploy that should stay read-only.
 *
 * Output: docs/video/walkthrough.webm (+ the raw Playwright capture).
 * Pacing is deliberate — the pauses are what make it watchable, so the wall-clock
 * runtime IS the video length.
 */
import { chromium } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(__dirname, "../../docs/video");

const BASE = process.env.DEMO_URL || "http://localhost:3200";
const SIZE = { width: 1280, height: 800 };

// Named beats so the pacing is tunable in one place.
const BEAT = { tick: 700, read: 1600, settle: 2400 };

const pause = (page, ms) => page.waitForTimeout(ms);

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: SIZE,
    recordVideo: { dir: OUT_DIR, size: SIZE },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();

  // --- 1. Sign in ------------------------------------------------------------
  await page.goto(`${BASE}/login`);
  await pause(page, BEAT.read);
  // Type it out — a filled-in form appearing instantly reads as a page reload.
  await page.locator('input[autocomplete="username"]').type("admin", { delay: 90 });
  await page.locator('input[type="password"]').type("demopass123", { delay: 90 });
  await pause(page, BEAT.tick);
  await page.getByRole("button", { name: "Sign in" }).click();

  // --- 2. The board: a lived-in backlog --------------------------------------
  await page.waitForURL(/\/tickets$/);
  await page.getByRole("heading", { name: /^Tickets/ }).waitFor();
  await pause(page, BEAT.settle);

  // --- 2b. Triage: narrow the board with the tag filter ----------------------
  // Two tags, ANDed — the point is that the board is navigable, not just long.
  await page.getByRole("checkbox", { name: "backend", exact: true }).check();
  await pause(page, BEAT.read);
  // repo:* is automation state, so it lives in the collapsed Workflow group.
  await page.getByRole("button", { name: /Workflow tags/i }).click();
  await pause(page, BEAT.tick);
  await page.getByRole("checkbox", { name: "repo:ticketing", exact: true }).check();
  await pause(page, BEAT.settle);
  // Sort the survivors by priority so the urgent ones surface.
  await page.getByLabel("Sort by").selectOption("priority");
  await pause(page, BEAT.settle);
  // Then drop the filters and get the whole board back.
  await page.getByRole("button", { name: /Clear all/i }).first().click();
  await pause(page, BEAT.read);

  // --- 3. The hero: the resolver's costed agent-run timeline ------------------
  await page.getByText("Review: batch the activity-feed queries").first().click();
  await page.getByRole("heading", { name: "Agent runs" }).waitFor();
  await pause(page, BEAT.read);
  // Scroll the timeline into frame and let it sit — this is the differentiator.
  await page.getByRole("heading", { name: "Agent runs" }).scrollIntoViewIfNeeded();
  await pause(page, BEAT.settle * 2);

  // --- 3b. The chat assistant: ask it about the ticket we're already on -------
  await page.getByLabel("Open the assistant").click();
  await page.getByLabel("Assistant").waitFor();
  await pause(page, BEAT.tick);
  await page
    .getByLabel("Message")
    .type("What's this ticket about, and what's the resolver's plan?", { delay: 30 });
  await pause(page, BEAT.tick);
  await page.getByRole("button", { name: "Send" }).click();
  // Streaming swaps Send for Stop; wait for the real answer to finish before
  // reading it back, then let the finished reply sit on screen.
  await page.getByRole("button", { name: "Stop" }).waitFor();
  await page.getByRole("button", { name: "Stop" }).waitFor({ state: "hidden", timeout: 60_000 });
  await pause(page, BEAT.settle * 2);
  await page.getByLabel("Close").click();
  await pause(page, BEAT.tick);

  // --- 4. Delegation: cost rolling up across the fan-out ----------------------
  await page.getByRole("link", { name: /Tickets/ }).first().click();
  await page.waitForURL(/\/tickets$/);
  await pause(page, BEAT.tick);
  await page.getByText("Harden the resolver's git-worktree isolation").first().click();
  await page.getByRole("heading", { name: "Agent runs" }).waitFor();
  await page.getByRole("heading", { name: "Agent runs" }).scrollIntoViewIfNeeded();
  await pause(page, BEAT.settle * 2);

  // --- 5. The human loop: create → comment → resolve ---------------------------
  await page.getByRole("link", { name: /Tickets/ }).first().click();
  await page.waitForURL(/\/tickets$/);
  await pause(page, BEAT.tick);
  await page.getByRole("button", { name: "New ticket" }).click();
  await page.waitForURL(/\/tickets\/new$/);
  await pause(page, BEAT.tick);

  const title = "Cache the ticket list count query";
  await page.locator("form input[required]").type(title, { delay: 45 });
  await page
    .getByPlaceholder(/Describe the task/)
    .type("COUNT(*) runs on every keystroke of the search box.", { delay: 22 });
  await page.locator('label:text-is("Priority") + select').selectOption("high");
  await pause(page, BEAT.read);
  await page.getByRole("button", { name: "Create ticket" }).click();

  await page.getByRole("heading", { name: new RegExp(title) }).waitFor();
  await pause(page, BEAT.read);

  await page
    .getByPlaceholder("Add a comment…")
    .type("Good catch — debouncing it and caching the count.", { delay: 32 });
  await page.getByRole("button", { name: "Comment" }).click();
  await pause(page, BEAT.read);

  await page.locator('label:text-is("Status") + select').selectOption("resolved");
  await pause(page, BEAT.read);
  // The activity trail records who did what, when.
  await page.getByText(/changed status .* to Resolved/).first().scrollIntoViewIfNeeded();
  await pause(page, BEAT.settle);

  // --- 6. Land back on the board ---------------------------------------------
  await page.getByRole("link", { name: /Tickets/ }).first().click();
  await page.waitForURL(/\/tickets$/);
  await pause(page, BEAT.settle);

  // Video is only flushed to disk on context.close().
  const video = page.video();
  await context.close();
  await browser.close();

  const raw = await video.path();
  const dest = path.join(OUT_DIR, "walkthrough.webm");
  fs.renameSync(raw, dest);
  const mb = (fs.statSync(dest).size / 1e6).toFixed(1);
  console.log(`Wrote ${dest} (${mb} MB)`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
