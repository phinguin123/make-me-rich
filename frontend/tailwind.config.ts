import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Pretendard → Inter → system-ui (same stack used in OrderBook.css)
        sans: [
          "Pretendard",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
        mono: [
          "Pretendard",
          "Inter",
          "ui-monospace",
          "SF Mono",
          "Menlo",
          "monospace",
        ],
      },
      colors: {
        // Korean market colour convention
        ask:  { DEFAULT: "#f04452", light: "rgba(240,68,82,0.10)"  },
        bid:  { DEFAULT: "#3182f6", light: "rgba(49,130,246,0.10)" },
        // Dashboard surface colours (dark theme)
        surface: {
          DEFAULT: "#13151f",
          card:    "#1c1f2e",
          raised:  "#242840",
          border:  "#2d3148",
        },
        content: {
          primary:   "#e2e8f0",
          secondary: "#8b95a1",
          muted:     "#4b5563",
        },
      },
      borderRadius: {
        card: "12px",
      },
      animation: {
        "flash-ask": "flashAsk 280ms ease-out forwards",
        "flash-bid": "flashBid 280ms ease-out forwards",
        "fade-in":   "fadeIn 200ms ease-out",
      },
      keyframes: {
        flashAsk: {
          "0%":   { backgroundColor: "rgba(240,68,82,0.28)"  },
          "100%": { backgroundColor: "transparent"           },
        },
        flashBid: {
          "0%":   { backgroundColor: "rgba(49,130,246,0.28)" },
          "100%": { backgroundColor: "transparent"           },
        },
        fadeIn: {
          "0%":   { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)"   },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
