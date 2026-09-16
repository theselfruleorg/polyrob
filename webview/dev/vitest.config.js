// Dev-only. `include` is deliberately narrow: axe.spec.js belongs to Playwright
// (a real browser), and vitest's default glob would otherwise pick it up and run
// it in jsdom, where `@playwright/test` has no meaning.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["**/*.test.js"],
    exclude: ["node_modules/**"],
    reporters: ["default"],
  },
});
