import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Blueprint design tokens (kept in sync with globals.css :root).
        ink: "#08080c",
        panel: "#0e0e16",
        accent: "#ff2e4d",
        paper: "#f3eee0",
      },
      fontFamily: {
        sans: ["Bricolage Grotesque", "system-ui", "sans-serif"],
        serif: ["Instrument Serif", "Georgia", "serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
