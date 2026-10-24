/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        background: '#090d16',
        surface: '#0f172a',
        surfaceHover: '#1e293b',
        borderDark: '#1e293b',
        cyanAccent: '#06b6d4',
        tealAccent: '#14b8a6',
        blueOcean: '#0284c7',

        // AquaSCADA Lab palette (from the mockup). Muted obsidian surfaces with colour
        // reserved for *state*: the values are the only saturated things on screen, so a
        // change in one reads as a change rather than as decoration. Mapped once here so
        // components never restate a hex.
        scada: {
          canvas: '#080a0f',
          surface: '#0f131c',
          elevated: '#171d2b',
          well: '#0a0d14',
          border: '#1c2436',
          borderHover: '#2a3752',
          borderFocus: '#38bdf8',
        },
        // One colour per physical quantity. Consistent everywhere the quantity appears —
        // chart series, KPI accent, actuator card — so the eye can track a channel by hue.
        telemetry: {
          do: '#38bdf8', // dissolved oxygen — cerulean
          motor: '#f59e0b', // ODrive aerator — amber
          mixer: '#14b8a6', // T-200 closed loop — teal
          pump: '#a855f7', // sulfite pump — violet
          temp: '#60a5fa', // water temperature — sky
          success: '#10b981',
          warning: '#f59e0b',
          danger: '#ef4444',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'sans-serif',
        ],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
};
