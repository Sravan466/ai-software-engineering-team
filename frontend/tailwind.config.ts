import type { Config } from "tailwindcss";

// The design system lives in CSS custom properties (app/globals.css). Tailwind
// is kept only for layout utilities, so its theme mirrors those tokens rather
// than defining a second, competing palette.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        panel: "var(--bg-panel)",
        raised: "var(--bg-raised)",
        ink: "var(--ink)",
        "ink-2": "var(--ink-2)",
        "ink-3": "var(--ink-3)",
        line: "var(--line)",
        accent: "var(--accent)",
        ok: "var(--ok)",
        run: "var(--run)",
        bad: "var(--bad)",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      borderRadius: {
        DEFAULT: "var(--r)",
        lg: "var(--r-lg)",
        xl: "var(--r-xl)",
      },
    },
  },
  plugins: [],
};

export default config;
