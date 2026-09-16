// axe-core over every console screen, at 390 x 844, with serious + critical = 0.
//
// TWO targets, one spec:
//
//   default            the mockup set (docs/design/040/web/*.html) over file://.
//                      This is the SPECIFICATION, and it is also the proof that
//                      the rig works before a single real page exists. It is
//                      PRIVATE (docs/design/ is denylisted from the public
//                      export) while this rig ships, so an absent mockup set
//                      SKIPS — a public consumer points AXE_BASE_URL at their
//                      own console instead.
//   AXE_BASE_URL=...   a running console. Point it at a live uvicorn and the
//                      same bar is applied to the real routes:
//                        AXE_BASE_URL=http://127.0.0.1:5050 npm run axe
//                      AXE_ROUTES overrides the route list (comma separated).
//
// Only serious + critical fail. `moderate` is reported and does not fail: the
// mockup set's 54 remaining nodes are all `region`, from the reviewer
// annotation, the back-link and the sources note, which sit OUTSIDE the .app
// frame by design.
import { test, expect } from "@playwright/test";
import { existsSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const AXE_SOURCE = require.resolve("axe-core");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const MOCKUPS = path.join(REPO, "docs", "design", "040", "web");

const FAIL_ON = new Set(["serious", "critical"]);

const BASE = (process.env.AXE_BASE_URL || "").replace(/\/+$/, "");
const ROUTES = (process.env.AXE_ROUTES || "/,/inbox,/work,/work#schedule,/work#apps,/work#log,/money,/money#moves,/money#cash,/money#invoices,/money#limits,/agent,/agent#identity,/agent#capabilities,/agent#memory,/agent#settings,/agent#advanced,/pending")
  .split(",")
  .map((r) => r.trim())
  .filter(Boolean);

const HAVE_MOCKUPS = existsSync(MOCKUPS);

/** [{name, url}] — the mockups by default, a live console when AXE_BASE_URL is
 *  set, and nothing at all when neither is available. */
function targets() {
  if (BASE) {
    return ROUTES.map((route) => ({ name: route, url: BASE + route }));
  }
  if (!HAVE_MOCKUPS) {
    return [];
  }
  const files = readdirSync(MOCKUPS)
    .filter((f) => f.endsWith(".html"))
    .sort();
  return files.map((f) => ({
    name: f,
    url: pathToFileURL(path.join(MOCKUPS, f)).href,
  }));
}

const TARGETS = targets();

function describeViolations(violations) {
  return violations
    .map((v) => {
      const where = v.nodes
        .slice(0, 4)
        .map((n) => `      ${n.target.join(" ")}`)
        .join("\n");
      const more =
        v.nodes.length > 4 ? `\n      ... and ${v.nodes.length - 4} more` : "";
      return `  ${v.impact} ${v.id} (${v.nodes.length} nodes) — ${v.help}\n${where}${more}`;
    })
    .join("\n");
}

if (TARGETS.length === 0) {
  test.skip(
    "no target: the mockup set is absent (it is private) and AXE_BASE_URL is unset",
    () => {},
  );
} else if (!BASE) {
  // Non-vacuity: with the mockups present, every screen must be a target. A
  // rig that silently narrowed to one file would still report green.
  test("every screen in the set is a target", () => {
    expect(TARGETS.length).toBeGreaterThanOrEqual(20);
  });
}

for (const target of TARGETS) {
  test(`no serious or critical accessibility violations: ${target.name}`, async ({
    page,
  }) => {
    const response = await page.goto(target.url, { waitUntil: "load" });
    if (response && !response.ok() && BASE) {
      throw new Error(`${target.url} answered ${response.status()}`);
    }
    if (BASE) {
      await page.evaluate(() => document.dispatchEvent(new CustomEvent('console-theme', {
        detail: matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark',
      })));
      if (target.url.endsWith('#advanced')) {
        await page.locator('#agent-adv-search').fill('MEMORY');
        await page.waitForFunction(() => document.querySelector('#agent-adv-flags table'));
      }
      // Lazy readers render after navigation; wait for requests and a paint.
      await page.waitForLoadState('networkidle');
      const size = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: innerWidth }));
      expect(size.content, `horizontal overflow: ${target.name}`).toBeLessThanOrEqual(size.viewport);
    }
    await page.addScriptTag({ path: AXE_SOURCE });
    const results = await page.evaluate(async () => {
      // eslint-disable-next-line no-undef
      return await axe.run(document, { resultTypes: ["violations"] });
    });
    const blocking = results.violations.filter((v) => FAIL_ON.has(v.impact));
    const nodes = blocking.reduce((n, v) => n + v.nodes.length, 0);
    expect(
      blocking,
      `${target.name}: ${nodes} serious/critical node(s)\n${describeViolations(
        blocking,
      )}`,
    ).toEqual([]);
  });
}

if (BASE) test('theme preference writes, applies and survives reload', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'dark-390', 'one shared test tenant');
  try {
    await page.goto(BASE + '/agent#settings');
    const control = page.locator('[data-setting-key="ui.theme"]');
    await control.locator('select').selectOption('light');
    await control.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
    const response = await page.request.get(BASE + '/api/webgate/preferences');
    const prefs = await response.json();
    expect(prefs.preferences.find(p => p.key === 'ui.theme').value).toBe('light');
    await page.screenshot({ path: '/tmp/polyrob-console-light-phone.png', fullPage: true });
  } finally {
    const response = await page.request.patch(BASE + '/api/webgate/preferences', {
      data: { key: 'ui.theme', value: 'auto' }, headers: { Origin: BASE },
    });
    expect(response.ok()).toBeTruthy();
  }
});
