// पालिका PMS — Tailwind config (base.html मा पहिले CDN मा भएको जस्तै)
// CSS बनाउने: cd tailwind && npm install && npm run build   → ../static/css/app.css
module.exports = {
  content: [
    "../templates/**/*.html",
    "../*/templates/**/*.html",
    "../static/assets/js/**/*.js",
    "../accounts/**/*.py", "../projects/**/*.py", "../usercommittee/**/*.py", "../biddings/**/*.py",
    "../quatation/**/*.py", "../letters/**/*.py", "../tenants/**/*.py",
  ],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "Noto Sans Devanagari", "ui-sans-serif", "system-ui", "sans-serif"] },
      colors: {
        brand: { 50: "#eef2fb", 100: "#dce4f6", 200: "#bccaee", 300: "#92a8e1", 400: "#6380cf", 500: "#3f5fbd",
                 600: "#2a47a3", 700: "#223a8b", 800: "#1e326f", 900: "#1b2b5c", 950: "#121d40" },
        navy: { DEFAULT: "#14224f", light: "#1f3266", dark: "#0f1a3d" },
      },
      boxShadow: {
        card: "0 1px 2px 0 rgba(16, 24, 40, .05)",
        soft: "0 4px 24px -4px rgba(16, 24, 40, .08)",
      },
    },
  },
};
