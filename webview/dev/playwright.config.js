// Rendered contrast + overflow matrix for both token themes and viewport widths.
import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: ".", testMatch: "axe.spec.js", forbidOnly: !!process.env.CI,
  reporter: [["list"]], workers: 2,
  use: {
    ...devices["Desktop Chrome"], bypassCSP: true,
    launchOptions: process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {},
  },
  projects: [390, 1320].flatMap(width => ["dark", "light"].map(theme => ({
    name: `${theme}-${width}`, use: { viewport: { width, height: 844 }, colorScheme: theme },
  }))),
});
