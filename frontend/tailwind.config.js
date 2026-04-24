/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "Consolas", "monospace"],
        sans: ["-apple-system", "BlinkMacSystemFont", "Inter", "system-ui", "sans-serif"],
      },
      colors: {
        ink: "#0a0b0d",
        panel: "#111316",
        line: "#1d2025",
        muted: "#6b7280",
        accent: "#f59e0b",
      },
    },
  },
  plugins: [],
};
