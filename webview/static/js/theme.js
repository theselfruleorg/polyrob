/* One preference, supplied by the server. No browser-side preference store. */
(() => {
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: light)');
  function apply(preference) {
    root.dataset.themePreference = ['auto', 'dark', 'light'].includes(preference) ? preference : 'auto';
    const theme = root.dataset.themePreference === 'auto' ? (system.matches ? 'light' : 'dark') : root.dataset.themePreference;
    root.dataset.theme = theme;
    const scheme = document.querySelector('meta[name="color-scheme"]');
    if (scheme) scheme.content = theme;
    const chrome = document.querySelector('meta[name="theme-color"]');
    if (chrome) chrome.content = getComputedStyle(root).getPropertyValue('--color-bg-primary').trim();
  }
  apply(root.dataset.themePreference);
  system.addEventListener('change', () => apply(root.dataset.themePreference));
  document.addEventListener('console-theme', event => apply(event.detail));
})();
