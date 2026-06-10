// Frontend runtime config. For a split deploy (static site + separate API service,
// e.g. Render), set DZ_API_BASE to the API service's base URL, e.g.
//   window.DZ_API_BASE = "https://degreezeor-api.onrender.com";
// Default "" = same origin (the API also serves the static UI in single-service/local mode).
window.DZ_API_BASE = "";

// Optional: a public contact address shown on the Contact page.
// Leave empty to show source-/methodology-based guidance instead of a mailto link.
window.DZ_CONTACT_EMAIL = "support@degree0.org";
