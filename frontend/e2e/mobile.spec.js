import { test, expect } from "@playwright/test";

// Only run in the mobile project (Pixel 5 viewport).
test.skip(({ isMobile }) => !isMobile, "mobile-only spec");

async function login(page) {
  await page.goto("/login");
  await page.locator('input[autocomplete="username"]').fill("admin");
  await page.locator('input[type="password"]').fill("adminpass123");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/tickets$/);
}

test("ticket list: no horizontal overflow", async ({ page }) => {
  await login(page);

  const overflow = await page.evaluate(() => {
    return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
  });
  expect(overflow).toBe(false);
});

test("ticket list: titles are not clipped to nothing", async ({ page }) => {
  await login(page);

  const titles = page.locator("[class*='rowTitle']");
  const count = await titles.count();
  // Fail loudly if there are no tickets — the test environment needs seed data.
  expect(count).toBeGreaterThan(0);
  const box = await titles.first().boundingBox();
  expect(box?.width).toBeGreaterThan(50);
});

test("hamburger opens and closes nav drawer", async ({ page }) => {
  await login(page);

  const hamburger = page.getByRole("button", { name: "Menu" });
  await expect(hamburger).toBeVisible();

  // Drawer is closed initially.
  await expect(hamburger).toHaveAttribute("aria-expanded", "false");

  // Open the drawer.
  await hamburger.click();
  await expect(hamburger).toHaveAttribute("aria-expanded", "true");

  // Nav links are visible.
  await expect(page.getByRole("link", { name: "Tickets" }).first()).toBeVisible();

  // Clicking a nav link closes the drawer.
  await page.getByRole("link", { name: "Profile" }).click();
  await expect(page).toHaveURL(/\/profile/);
  await expect(hamburger).toHaveAttribute("aria-expanded", "false");
});

test("hamburger: Escape closes the nav drawer", async ({ page }) => {
  await login(page);

  const hamburger = page.getByRole("button", { name: "Menu" });
  await hamburger.click();
  await expect(hamburger).toHaveAttribute("aria-expanded", "true");

  await page.keyboard.press("Escape");
  await expect(hamburger).toHaveAttribute("aria-expanded", "false");
});

test("ticket detail: no horizontal overflow", async ({ page }) => {
  await login(page);

  // Navigate into the first ticket if one exists.
  const firstLink = page.locator("[class*='rowLink']").first();
  const count = await page.locator("[class*='rowLink']").count();
  if (count === 0) {
    test.skip();
    return;
  }
  await firstLink.click();
  await expect(page).toHaveURL(/\/tickets\/\d+/);

  const overflow = await page.evaluate(() => {
    return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1;
  });
  expect(overflow).toBe(false);
});
