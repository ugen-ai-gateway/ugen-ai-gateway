export type Theme = 'system' | 'light' | 'dark';
const THEME_STORAGE = 'ugen-gateway-theme';

export function loadTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch { return 'system'; }
}

/** Apply a theme; "system" follows the operating system's setting. */
export function applyTheme(theme: Theme): void {
  if (theme === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    if (theme === 'system') localStorage.removeItem(THEME_STORAGE);
    else localStorage.setItem(THEME_STORAGE, theme);
  } catch { /* the choice lasts for this page only */ }
}
