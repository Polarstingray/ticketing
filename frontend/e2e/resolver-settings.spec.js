import { test, expect } from "@playwright/test";

async function loginAs(page, username, password) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[type="password"]').fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

// Below 900px the nav (including "Log out") lives in a hamburger drawer that's
// closed by default; open it first so the links/buttons inside are clickable.
async function openMobileNavIfCollapsed(page) {
  const hamburger = page.getByRole("button", { name: "Menu" });
  if (await hamburger.isVisible()) {
    await hamburger.click();
  }
}

test("resolver manager: admin edits the global default, it persists", async ({ page }) => {
  await loginAs(page, "admin", "adminpass123");
  await expect(page).toHaveURL(/\/tickets$/);

  // The admin-only "Resolvers" nav link deep-links to the manager.
  await openMobileNavIfCollapsed(page);
  await page.getByRole("link", { name: "Resolvers" }).click();
  await expect(page).toHaveURL(/\/admin\/resolver-settings$/);
  await expect(page.getByRole("heading", { name: "Resolvers", level: 1 })).toBeVisible();

  // Global default is selected by default; secrets are read-only.
  await expect(page.getByText(/Editing settings for/)).toContainText("Global default");
  await expect(page.getByText("Stingray API key")).toBeVisible();
  await expect(page.getByText(/managed in \.env/).first()).toBeVisible();

  // Edit a tunable and save.
  await page.locator("#max_attempts").fill("6");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved")).toBeVisible();

  // A reload reflects the persisted value.
  await page.reload();
  await expect(page.locator("#max_attempts")).toHaveValue("6");
});

test("resolver manager: roster lists a resolver and edits are scoped to it", async ({ page }) => {
  await loginAs(page, "admin", "adminpass123");
  await expect(page).toHaveURL(/\/tickets$/);

  // Provision a resolver bot (admin call) and have it send one heartbeat as
  // itself, so it shows up in the roster with live fields.
  const uniq = Date.now();
  const botRes = await page.request.post("/api/users/resolver-bot", {
    data: { username: `gemini_${uniq}`, display_name: `gemini ${uniq}` },
  });
  expect(botRes.status()).toBe(201);
  const { user_id, api_key } = await botRes.json();

  const hb = await page.request.post("/api/resolvers/heartbeat", {
    headers: { "X-API-Key": api_key },
    data: {
      label: ".env.gemini",
      name: `gemini-${uniq}`,
      agent: "opencode",
      model: "google/gemini-2.5-flash",
      effective_config: { max_attempts: 4, agent_model: "google/gemini-2.5-flash" },
    },
  });
  expect(hb.status()).toBe(200);

  // The roster shows the resolver by its reported name + env-file label.
  await openMobileNavIfCollapsed(page);
  await page.getByRole("link", { name: "Resolvers" }).click();
  const row = page.getByRole("button", { name: new RegExp(`gemini-${uniq}`) });
  await expect(row).toBeVisible();
  // Scoped to this row: other resolver bots created elsewhere in the suite can
  // share the same ".env.gemini" label.
  await expect(row.getByText(".env.gemini")).toBeVisible();

  // Select it and save a value scoped to THIS resolver.
  await row.click();
  await expect(page.getByText(/Editing settings for/)).toContainText(`gemini-${uniq}`);
  await page.locator("#max_attempts").fill("8");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved")).toBeVisible();

  // The override is stored under this bot's id, independent of the global row.
  const scoped = await page.request.get(`/api/resolver-settings?bot_user_id=${user_id}`);
  expect((await scoped.json()).settings.max_attempts).toBe(8);
});

test("resolver manager: a non-admin is redirected away", async ({ page }) => {
  await loginAs(page, "admin", "adminpass123");
  await expect(page).toHaveURL(/\/tickets$/);
  const uniq = Date.now();
  const username = `member_${uniq}`;
  const res = await page.request.post("/api/users", {
    data: {
      username,
      display_name: username,
      email: `${username}@example.com`,
      password: "member123",
      role: "member",
    },
  });
  expect(res.status()).toBe(201);

  await openMobileNavIfCollapsed(page);
  await page.getByRole("button", { name: "Log out" }).click();
  await loginAs(page, username, "member123");
  await expect(page).toHaveURL(/\/tickets$/);

  // The <Protected adminOnly> guard bounces a member back to /tickets, and the
  // admin-only nav link is not rendered.
  await page.goto("/admin/resolver-settings");
  await expect(page).toHaveURL(/\/tickets$/);
  await expect(page.getByRole("link", { name: "Resolvers" })).toHaveCount(0);
});
