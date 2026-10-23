import type { Plugin } from 'vite';

/**
 * Dark mode without a second stylesheet: every hex color in style.css becomes
 * a CSS variable whose light value is the original and whose dark value
 * inverts the lightness, so text, surfaces and accents keep their contrast.
 * Surfaces, borders and text turn neutral black and grey; only accent colors
 * keep their hue. Shadows and overlays stay dark. The theme follows the system unless
 * <html data-theme="light|dark"> picks one.
 */

// Hand-tuned dark values where the formula is not good enough (light hex → dark hex).
const OVERRIDES: Record<string, string> = {
  '#fff': '#161616',
  '#ffffff': '#161616',
  '#f3f7f5': '#0a0a0a',
  '#fafcfb': '#121212',
  '#f6faf8': '#121212',
  '#eef2ef': '#0f0f0f',
  // The logo keeps its colours in dark mode (the same as public/favicon.svg).
  '#1f5c4e': '#9fd6c2',
  '#7cc4a7': '#3f8f74',
  '#163b37': '#ececec',
};

type Rgba = [number, number, number, number];

function parse(hex: string): Rgba {
  let digits = hex.slice(1);
  if (digits.length <= 4) digits = [...digits].map(d => d + d).join('');
  const value = (i: number) => parseInt(digits.slice(i, i + 2), 16) / 255;
  return [value(0), value(2), value(4), digits.length === 8 ? value(6) : 1];
}

function toHsl([r, g, b]: Rgba): [number, number, number] {
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min, s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h / 6, s, l];
}

function fromHsl(h: number, s: number, l: number): [number, number, number] {
  if (s === 0) return [l, l, l];
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const channel = (t: number) => {
    t = (t + 1) % 1;
    return t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
  };
  return [channel(h + 1 / 3), channel(h), channel(h - 1 / 3)];
}

const hex2 = (value: number) => Math.round(Math.min(1, Math.max(0, value)) * 255).toString(16).padStart(2, '0');

export function darkColor(hex: string): string {
  const key = hex.toLowerCase();
  if (OVERRIDES[key]) return OVERRIDES[key];
  const rgba = parse(key);
  const [h, s, l] = toHsl(rgba);
  const alpha = rgba[3];
  // Translucent dark colors are shadows or scrims: keep them dark, a bit stronger.
  if (alpha < 1 && l < 0.45) return `#000000${hex2(Math.min(1, alpha * 1.8))}`;
  // Invert lightness into 0.09–0.90 so surfaces are not pure black and text not pure white.
  const nextL = 0.09 + (1 - l) * 0.81;
  // Surfaces, borders and text (very light, very dark or muted colors) become
  // neutral greys; accents keep their hue.
  const nextS = l > 0.8 || l < 0.3 || s < 0.25 ? 0 : s;
  const [r, g, b] = fromHsl(h, nextS, nextL);
  return `#${hex2(r)}${hex2(g)}${hex2(b)}${alpha < 1 ? hex2(alpha) : ''}`;
}

const COLOR = /#[0-9a-fA-F]{3,8}\b/g;

export function themeCss(css: string): string {
  const names = new Map<string, string>();
  const name = (hex: string) => {
    const key = hex.toLowerCase();
    if (!names.has(key)) names.set(key, `--c${names.size}`);
    return names.get(key)!;
  };
  // Only replace colors inside declaration blocks, never in selectors.
  const themed = css.replace(/\{([^{}]*)\}/g, (_, body: string) =>
    `{${body.replace(COLOR, hex => [3, 4, 6, 8].includes(hex.length - 1) ? `var(${name(hex)})` : hex)}}`);
  const light = [...names].map(([hex, variable]) => `${variable}:${hex}`).join(';');
  const dark = [...names].map(([hex, variable]) => `${variable}:${darkColor(hex)}`).join(';');
  return `:root{${light};color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){${dark};color-scheme:dark}}
:root[data-theme=dark]{${dark};color-scheme:dark}
${themed}`;
}

export default function darkTheme(): Plugin {
  return {
    name: 'ugen-dark-theme',
    enforce: 'pre',
    transform(code, id) {
      return id.endsWith('/src/style.css') ? { code: themeCss(code), map: null } : null;
    },
  };
}
