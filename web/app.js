"use strict";
// Zero-build explainability SPA. Pure client of the /api contract.
// Demonstrates the platform's default output: a decomposed, source-linked,
// confidence-gated scorecard — NOT a single normative verdict.

// API base: same-origin by default; set window.DZ_API_BASE (config.js) for a split
// static-frontend + separate-API deployment (e.g. Render).
const API = (typeof window !== "undefined" && window.DZ_API_BASE) || "";
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return n;
};
const fmt = (x, d = 2) => (x === null || x === undefined ? "n/a" : Number(x).toFixed(d));

// Consistent official name formatting: "Last, First (Party)". Strips honorifics and any
// embedded "[D-CT-2]" junk; handles single-token names and Jr./Sr./III suffixes. Pass
// party to append it (use null/"" -> "(Unknown)"); omit party to show just the name.
function formatName(name, party) {
  if (!name) return "Unknown";
  // Use RegExp(string) rather than /literals/ so the static paren-balance guard parses cleanly.
  let s = String(name)
    .replace(new RegExp("\\[[^\\]]*\\]", "g"), "")
    .replace(new RegExp("\\b(Rep|Sen|Gov|President|Senator|Representative|Dr)\\.?\\s+", "gi"), "")
    .replace(new RegExp("\\s+", "g"), " ").trim();
  let formatted;
  if (s.includes(",")) {
    formatted = s.replace(new RegExp("\\s*,\\s*"), ", ");
  } else {
    const parts = s.split(" ");
    if (parts.length <= 1) {
      formatted = s;
    } else {
      const suffixes = ["Jr.", "Sr.", "Jr", "Sr", "II", "III", "IV"];
      let last = parts[parts.length - 1];
      let rest = parts.slice(0, -1);
      if (suffixes.includes(last) && rest.length >= 2) {
        last = rest[rest.length - 1] + " " + last;
        rest = rest.slice(0, -1);
      }
      formatted = `${last}, ${rest.join(" ")}`;
    }
  }
  if (party === undefined) return formatted;
  return `${formatted} (${party || "Unknown"})`;
}

// Build a <select> from [[value, label], ...] with the given value pre-selected.
const selectEl = (options, selected = "") => {
  const s = document.createElement("select");
  for (const [value, label] of options) {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    if (value === selected) o.selected = true;
    s.appendChild(o);
  }
  return s;
};

// Plain-language explainers, surfaced as inline "i" tooltips next to key terms.
const TIPS = {
  composite:
    "0 to 100: how fully an action met the goal it set for itself, scaled by our confidence in the " +
    "evidence. For an official, the average over their scored actions, weighted by their share of credit.",
  attribution:
    "The share of an outcome credited to this official, based on their role and how pivotal they were. " +
    "Most of any outcome stays unattributed to any single person.",
  coverage:
    "Of the actions tied to this official, the share we could actually score. The rest read " +
    "\u201Cinsufficient evidence\u201D, meaning we could not isolate the effect.",
  confidence:
    "How sure we are the result is real, from the strength of the method, data, and attribution. " +
    "When confidence is low, we withhold the score.",
  insufficient:
    "We could not separate this policy's effect from everything else happening at the time, so we " +
    "report no score rather than guess.",
};
const tip = (key) => el("span", {
  class: "tip", tabindex: "0", role: "img",
  "aria-label": "help", "data-tip": typeof key === "string" && TIPS[key] ? TIPS[key] : key,
}, "i");
const FACTUAL = ["outcome", "evidence", "attribution", "alignment", "dataquality", "durability"];
// Neutral default: composite = confidence-scaled achievement (outcome + durability).
// Other components are shown for context but excluded by default (they live in confidence).
const DEFAULT_WEIGHTS = { outcome: 1, durability: 1, evidence: 0, attribution: 0, alignment: 0, dataquality: 0 };

async function getJSON(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function postJSON(path, body) {
  const r = await fetch(API + path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function sensitivityCard(euId, unit) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, "Sensitivity analysis (robustness across evaluation horizons)"));
  let s;
  try {
    s = await getJSON(`/api/evaluation-units/${euId}/sensitivity`);
  } catch (e) {
    card.appendChild(el("div", { class: "muted", style: "font-size:13px" }, "Sensitivity not available for this unit."));
    return card;
  }
  const robust = s.sign_stable;
  card.appendChild(el("div", { class: "gate-banner " + (robust ? "scored" : "gated") },
    s.summary));
  card.appendChild(el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "lag (months)"), el("th", {}, "evaluation point"),
      el("th", { class: "right" }, `delta toward goal (${unit})`),
      el("th", { class: "right" }, "std. effect z"), el("th", {}, "distinguishable?"))),
    el("tbody", {}, ...s.points.map((p) =>
      el("tr", { style: p.is_registered ? "background:rgba(79,156,249,.10)" : "" },
        el("td", {}, `${p.lag_months}${p.is_registered ? " (registered)" : ""}`),
        el("td", {}, p.eval_period),
        el("td", { class: "right mono", style: `color:${p.delta_toward_goal >= 0 ? "var(--good)" : "var(--bad)"}` }, fmt(p.delta_toward_goal, 1)),
        el("td", { class: "right mono" }, fmt(p.z, 2)),
        el("td", {}, p.significant ? el("span", { class: "badge scored" }, "yes") : el("span", { class: "pill" }, "within noise")))))));
  card.appendChild(el("p", { class: "muted", style: "font-size:12px" },
    "A directionally stable, significant effect across horizons is strong evidence; a sign that " +
    "flips with the horizon indicates the result depends on an analyst choice and should temper confidence."));
  return card;
}

async function disputesCard(euId) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, "Challenge / appeal this score"));
  card.appendChild(el("p", { class: "muted", style: "font-size:13px" },
    "Anyone may dispute a score. Resolution is not editorial. It triggers an independent, " +
    "deterministic re-run and publishes whether the score changed (a public diff). Every step " +
    "is recorded on the append-only audit chain."));

  const listWrap = el("div", {});
  async function refresh() {
    listWrap.innerHTML = "";
    const disputes = await getJSON(`/api/disputes?eu_id=${euId}`);
    if (!disputes.length) {
      listWrap.appendChild(el("div", { class: "muted", style: "font-size:13px" }, "No disputes filed."));
    }
    for (const d of disputes) {
      const row = el("div", { style: "border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0" },
        el("div", {}, el("span", { class: "pill" }, d.status.replaceAll("_", " ")),
          el("span", { class: "muted", style: "margin-left:8px" }, `#${d.id} · ${d.filer}`)),
        el("div", { style: "margin:6px 0;font-size:13px" }, d.claim),
        d.public_diff ? el("div", { class: "mono", style: "font-size:12px;color:var(--good)" }, d.public_diff.summary) : null);
      if (d.status === "open") {
        row.appendChild(el("button", {
          style: "margin-top:6px", onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = "Re-running…";
            await postJSON(`/api/disputes/${d.id}/resolve`);
            await refresh();
          },
        }, "Resolve via reproducible re-run"));
      }
      listWrap.appendChild(row);
    }
  }

  const filer = el("input", { type: "text", placeholder: "your name / org", style: "width:100%;margin:4px 0;padding:6px;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:6px" });
  const claim = el("textarea", { placeholder: "what do you dispute and why? (5 to 1000 characters)", rows: "2", style: "width:100%;margin:4px 0;padding:6px;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:6px" });
  // Honeypot: hidden from real users; bots that fill it are rejected server-side.
  const hp = el("input", { type: "text", name: "website", tabindex: "-1", autocomplete: "off",
    style: "position:absolute;left:-9999px;width:1px;height:1px;opacity:0", "aria-hidden": "true" });
  const note = el("div", { class: "muted", style: "font-size:12px;margin-top:4px" });
  const submit = el("button", {
    onclick: async () => {
      const text = claim.value.trim();
      if (text.length < 5) { note.textContent = "Please enter at least 5 characters."; return; }
      try {
        await postJSON("/api/disputes", { eu_id: euId, filer: filer.value || "anonymous", claim: text, website: hp.value });
        claim.value = ""; filer.value = ""; note.textContent = "";
        await refresh();
      } catch (e) { note.textContent = "Could not file: " + e.message; }
    },
  }, "File challenge");
  card.appendChild(filer);
  card.appendChild(claim);
  card.appendChild(hp);
  card.appendChild(submit);
  card.appendChild(note);
  card.appendChild(el("h3", { style: "margin-top:16px" }, "Disputes"));
  card.appendChild(listWrap);
  await refresh();
  return card;
}

// Lightweight modal overlay for drill-down detail (no framework). Pass a title + body node.
function openModal(title, body) {
  const close = () => back.remove();
  const x = el("button", { class: "x", "aria-label": "close", onclick: close }, "✕");
  const modal = el("div", { class: "modal", onclick: (e) => e.stopPropagation() },
    x, el("h3", {}, title), body);
  const back = el("div", { class: "modal-back", onclick: close }, modal);
  back.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  document.body.appendChild(back);
  back.tabIndex = -1; back.focus();
}

function spinner(msg = "Loading…") {
  return el("div", { class: "spin-wrap" },
    el("div", { class: "spinner" }),
    el("div", {}, msg),
    el("div", { class: "muted", style: "font-size:12px" },
      "First load can take a moment while the server wakes up."));
}

async function renderLanding() {
  const app = $("#app");
  app.innerHTML = "";
  const hero = el("div", { class: "hero" },
    el("img", { class: "mark", src: "/logo.png", alt: "DegreeZero" }),
    el("h1", {}, "DegreeZero"),
    el("div", { class: "sub" },
      "Did a public action meet the goal it set for itself? We measure the outcome against each " +
      "policy's own stated objective, with sources you can check."),
    el("div", { class: "ctas" },
      el("a", { class: "cta", href: "#/officials" }, "Explore officials"),
      el("a", { class: "cta ghost", href: "#/actions" }, "Browse actions"),
      el("a", { class: "cta ghost", href: "#/about" }, "How it works")));
  app.appendChild(hero);

  // Real credibility stats (skipped quietly if the API is still waking up).
  try {
    const s = await getJSON("/api/stats");
    hero.appendChild(el("div", { class: "statbar" },
      el("div", { class: "s" }, el("div", { class: "n" }, String(s.scored)), el("div", { class: "l" }, "actions scored")),
      el("div", { class: "s" }, el("div", { class: "n" }, String(s.officials)), el("div", { class: "l" }, "officials tracked")),
      el("div", { class: "s" }, el("div", { class: "n" }, String(s.sources)), el("div", { class: "l" }, "official sources")),
      el("div", { class: "s" }, el("div", { class: "n" }, String(s.actions_considered)), el("div", { class: "l" }, "actions considered"))));
    if (s.last_updated) {
      hero.appendChild(el("div", { class: "freshness" }, "Data last updated " + s.last_updated.slice(0, 10)));
    }
  } catch (e) { /* stats are best-effort */ }

  // Three-step gist.
  hero.appendChild(el("div", { class: "steps" },
    el("div", { class: "step" }, el("span", { class: "num" }, "1"), el("b", {}, "A policy sets a goal"),
      el("p", {}, "We take the objective the law, order, or budget stated for itself.")),
    el("div", { class: "step" }, el("span", { class: "num" }, "2"), el("b", {}, "We measure the result"),
      el("p", {}, "Official data versus a defensible baseline, with the credit shared by causal role.")),
    el("div", { class: "step" }, el("span", { class: "num" }, "3"), el("b", {}, "We score it, or abstain"),
      el("p", {}, "A 0 to 100 score with confidence and sources, or \u201Cinsufficient evidence\u201D when we cannot tell."))));
}

async function renderSources() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("h2", { style: "margin:6px 0" }, "Sources"));
  app.appendChild(el("p", { class: "muted" },
    "Every source that feeds a score, with its provenance tier. Tier 0 is the action record, " +
    "Tier 1 is official statistics, Tier 2 is official analysis, Tier 3 is a verified mirror."));
  const rows = await getJSON("/api/sources");
  app.appendChild(el("div", { class: "card" },
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "source"), el("th", {}, "tier"), el("th", {}, "endpoint"))),
      el("tbody", {}, ...rows.map((d) =>
        el("tr", {},
          el("td", {}, d.name),
          el("td", {}, d.tier_label),
          el("td", { class: "mono", style: "font-size:12px" },
            el("a", { href: d.base_url, target: "_blank", rel: "noopener" }, d.base_url))))))));
}

const GLOSSARY = [
  ["Composite score", TIPS.composite, "composite = confidence x achievement of the stated goal"],
  ["Attribution (role share)", TIPS.attribution, "share = authority x pivotality, normalized with a large unattributable residual"],
  ["Coverage", TIPS.coverage, "coverage = scored actions / total attributable actions"],
  ["Confidence", TIPS.confidence, "confidence = design x data x attribution x model x sensitivity"],
  ["Insufficient evidence", TIPS.insufficient, null],
  ["Baseline", "What the metric would likely have done without the action, used to net out other forces.", null],
  ["Pre-registration", "The metric and method are fixed and hashed before outcomes are fetched, so results cannot be cherry-picked.", null],
];
async function renderGlossary() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("h2", { style: "margin:6px 0" }, "Glossary"));
  app.appendChild(el("p", { class: "muted" }, "Plain-language definitions, with the math where it applies."));
  for (const [term, def, eq] of GLOSSARY) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, term),
      el("p", { style: "margin:0" }, def),
      eq ? el("div", { class: "eq" }, eq) : null));
  }
}

const NAV = [["#/officials", "Officials"], ["#/actions", "Actions"], ["#/coverage", "Coverage"],
             ["#/integrity", "Integrity"], ["#/about", "About"]];
function renderNav() {
  const nav = $("#nav");
  if (!nav) return;
  nav.innerHTML = "";
  const h = location.hash || "";
  for (const [href, label] of NAV) {
    const base = href.slice(1);  // e.g. "/officials"
    const active = h.startsWith("#" + base) || h.startsWith(href);
    nav.appendChild(el("a", { href, class: active ? "active" : "" }, label));
  }
}

async function renderAuditStatus() {
  const node = $("#audit-status");
  if (!node) return;
  try {
    const a = await getJSON("/api/audit/verify");
    node.textContent = a.audit_chain_ok ? "✓ Audit chain verified" : "✕ Audit chain broken";
    node.className = "audit-badge " + (a.audit_chain_ok ? "ok" : "bad");
  } catch (e) {
    node.textContent = "";
    node.className = "audit-badge";
  }
}

function statusBadge(status) {
  return el("span", { class: "badge " + status }, status.replaceAll("_", " "));
}

async function renderList() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("h2", { style: "margin:6px 0" }, "Actions"));
  app.appendChild(el("p", { class: "muted" },
    "Each row is a public action (a law, executive order, rule, or budget) scored against the goal it " +
    "set for itself. Click any to see the full breakdown and sources. When we cannot separate the " +
    "policy's effect from everything else at the time, we mark it \u201Cinsufficient evidence\u201D " +
    "rather than guess."));
  const units = await getJSON("/api/evaluation-units");
  for (const u of units) {
    app.appendChild(el("div", { class: "list-item", onclick: () => { location.hash = `#/eu/${u.id}`; } },
      el("div", {},
        el("div", { class: "title" }, u.title),
        el("div", { class: "muted mono" }, (u.public_law ? `Public Law ${u.public_law}` : `Action #${u.id}`))),
      el("div", { style: "text-align:right" },
        statusBadge(u.status),
        el("div", { class: "muted", style: "margin-top:6px;font-size:12px" },
          `confidence ${u.confidence === null ? "n/a" : (u.confidence * 100).toFixed(1) + "%"}`,
          u.composite !== null ? ` · composite ${fmt(u.composite, 1)}` : " · composite suppressed"))));
  }
}

function componentBar(c) {
  const pct = Math.max(0, Math.min(100, Number(c.value)));
  return el("div", { class: "bar-wrap" },
    el("div", { class: "comp-name" }, c.name, c.is_value_laden ? el("small", {}, "value-laden") : el("small", {}, "factual")),
    el("div", { class: "bar" + (c.is_value_laden ? " value-laden" : "") }, el("span", { style: `width:${pct}%` })),
    el("div", { class: "right mono" }, fmt(c.value, 1)));
}

function gateBanner(card) {
  const s = card.score;
  if (!s) {
    return el("div", { class: "gate-banner none" },
      `Not scoreable: ${card.evaluation_unit.non_scoreable_reason || "no operational metric or outcome."} ` +
      "This is reported as absence of evidence, not as a low score.");
  }
  if (s.gated) {
    return el("div", { class: "gate-banner gated" },
      `Insufficient evidence. Confidence ${(s.confidence * 100).toFixed(1)}% is below the ` +
      `${(s.publish_threshold * 100).toFixed(0)}% threshold, so no score is issued. ` +
      "The full breakdown below is still shown.",
      tip("insufficient"));
  }
  return el("div", { class: "gate-banner scored" },
    `Composite ${fmt(s.composite, 1)} of 100, scaled by ${(s.confidence * 100).toFixed(1)}% confidence.`,
    tip("composite"));
}

function valueWeightPanel(card) {
  // Demonstrates: values live with the USER, not the engine. Recomputes a composite
  // client-side from displayed factual components. Respects the gate. Watermarked.
  const comps = Object.fromEntries(card.components.map((c) => [c.name, Number(c.value)]));
  const present = FACTUAL.filter((n) => n in comps);
  const weights = Object.fromEntries(present.map((n) => [n, DEFAULT_WEIGHTS[n] ?? 0]));
  const out = el("div", {});
  const mark = el("div", {});
  const result = el("div", { class: "mono", style: "margin-top:10px;font-size:14px" });

  const isNeutral = () => present.every((n) => Math.abs(weights[n] - (DEFAULT_WEIGHTS[n] ?? 0)) < 1e-9);

  function recompute() {
    // Watermark only when the user departs from the neutral (equal-weight) default.
    if (isNeutral()) {
      mark.className = "muted";
      mark.style.fontSize = "12px";
      mark.textContent = "Neutral default: confidence-scaled achievement (outcome + durability). Other components shown for context live inside confidence.";
    } else {
      mark.className = "watermark";
      mark.style.fontSize = "12px";
      mark.textContent = "Custom value weights applied (not the neutral default).";
    }
    const total = present.reduce((a, n) => a + weights[n], 0) || 1;
    const weighted = present.reduce((a, n) => a + (weights[n] / total) * comps[n], 0);
    const gated = card.score && card.score.gated;
    if (!card.score) {
      result.textContent = "Non-scoreable: no composite can be formed.";
    } else if (gated) {
      result.innerHTML = `Composite still <b>suppressed</b> (insufficient evidence) regardless of weights. ` +
        `Indicative weighted mean (not published): ${weighted.toFixed(1)}`;
    } else {
      result.innerHTML = `Custom-weighted composite: <b>${(card.score.confidence * weighted).toFixed(1)}</b> ` +
        `(confidence-scaled).`;
    }
  }

  const wrap = el("div", { class: "weights" });
  for (const n of present) {
    const w0 = DEFAULT_WEIGHTS[n] ?? 0;
    const val = el("span", { class: "right mono" }, w0.toFixed(1));
    const slider = el("input", { type: "range", min: "0", max: "3", step: "0.1", value: String(w0) });
    slider.addEventListener("input", () => { weights[n] = Number(slider.value); val.textContent = Number(slider.value).toFixed(1); recompute(); });
    wrap.appendChild(el("label", {}, el("span", {}, n), slider, val));
  }
  out.appendChild(mark);
  out.appendChild(wrap);
  out.appendChild(result);
  recompute();
  return out;
}

async function renderDetail(id) {
  const app = $("#app");
  app.innerHTML = ""; app.appendChild(spinner());
  const card = await getJSON(`/api/evaluation-units/${id}`);
  app.innerHTML = "";

  app.appendChild(el("a", { class: "back", href: "#/actions" }, "← all actions"));
  app.appendChild(el("h2", { style: "margin:6px 0" }, card.action.title));
  app.appendChild(el("div", { class: "muted mono" },
    [card.action.type, card.action.public_law_number ? "Public Law " + card.action.public_law_number : null,
     card.action.domain, card.action.enacted_date ? "enacted " + card.action.enacted_date : null]
      .filter(Boolean).join(" · ")));
  app.appendChild(el("div", { style: "margin:10px 0" }, statusBadge(card.evaluation_unit.status)));

  app.appendChild(gateBanner(card));

  // Narrative
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Why this scored this way"),
    el("div", { class: "narrative" }, card.narrative)));

  // Components vector
  if (card.components.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Score breakdown", tip("composite")),
      ...card.components.map(componentBar)));
  }

  // Objective + metric
  if (card.objective) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Stated objective (the goal this action set for itself)"),
      el("div", { class: "row" }, el("span", { class: "k" }, "objective level"), el("span", { class: "v" }, card.objective.level)),
      el("p", { class: "muted", style: "max-height:160px;overflow:auto" }, card.objective.text.slice(0, 1200) + (card.objective.text.length > 1200 ? "…" : "")),
      el("div", { class: "src" }, el("a", { href: card.objective.source_url, target: "_blank" }, "official source ↗")),
      card.metric ? el("div", { class: "row", style: "margin-top:10px" },
        el("span", { class: "k" }, "mapped metric"),
        el("span", { class: "v" }, `${card.metric.name} (${card.metric.unit}; better = ${card.metric.direction_good}) · ${card.metric.native_series_id}`)) : null));
  }

  // Outcome + baseline ensemble
  if (card.outcome) {
    const o = card.outcome;
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Outcome vs. counterfactual baseline"),
      el("div", { class: "kpi" },
        el("div", { class: "item" }, el("div", { class: "n" }, fmt(o.observed, 0)), el("div", { class: "l" }, "observed")),
        el("div", { class: "item" }, el("div", { class: "n" }, fmt(o.baseline_pooled, 0)), el("div", { class: "l" }, "baseline (pooled)")),
        el("div", { class: "item" }, el("div", { class: "n" }, fmt(o.delta, 0)), el("div", { class: "l" }, "delta")),
        el("div", { class: "item" }, el("div", { class: "n" }, fmt(o.z, 2)), el("div", { class: "l" }, "std. effect z")),
        el("div", { class: "item" }, el("div", { class: "n" }, (o.model_dependence * 100).toFixed(0) + "%"), el("div", { class: "l" }, "model dependence"))),
      el("div", { class: "muted", style: "margin:8px 0" }, `bootstrap 95% CI on delta: [${fmt(o.ci_low, 1)}, ${fmt(o.ci_high, 1)}] ${(o.ci_low <= 0 && o.ci_high >= 0) ? "(includes 0 → effect not distinguishable from noise)" : ""}`),
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "baseline method"), el("th", { class: "right" }, "value"), el("th", { class: "right" }, "95% CI"))),
        el("tbody", {}, ...card.baselines.map((b) =>
          el("tr", {}, el("td", {}, b.method), el("td", { class: "right mono" }, fmt(b.baseline_value, 1)),
            el("td", { class: "right mono" }, `[${fmt(b.ci_low, 1)}, ${fmt(b.ci_high, 1)}]`)))))));
  }

  // Attribution
  if (card.attribution.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Attribution (always leaves a large unattributable residual)", tip("attribution")),
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "role"), el("th", {}, "who"), el("th", { class: "right" }, "attribution"), el("th", { class: "right" }, "95% band"))),
        el("tbody", {}, ...card.attribution.map((a) =>
          el("tr", {},
            el("td", {}, a.is_residual ? el("span", { class: "pill" }, a.role.replaceAll("_", " ")) : a.role),
            el("td", {}, a.official_name ? formatName(a.official_name) : "n/a"),
            el("td", { class: "right mono" }, (a.attribution * 100).toFixed(1) + "%"),
            el("td", { class: "right mono" }, `[${(a.ci_low * 100).toFixed(0)}, ${(a.ci_high * 100).toFixed(0)}]%`)))))));
  }

  // User value weights (composite path demo, gate-respecting)
  if (card.components.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Your value weights (optional · default = neutral)"),
      valueWeightPanel(card)));
  }

  // Sensitivity analysis (robustness across alternative lag windows)
  if (card.metric && card.outcome) {
    app.appendChild(await sensitivityCard(card.evaluation_unit.id, card.metric.unit));
  }

  // Challenge / appeal (dispute workflow)
  app.appendChild(await disputesCard(card.evaluation_unit.id));

  // What would change the score
  if (card.what_would_change_the_score && card.what_would_change_the_score.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "What would change this score"),
      ...card.what_would_change_the_score.map((h) => el("div", { class: "hint" }, h))));
  }

  // Reproducibility + pre-registration
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Reproducibility & pre-registration (audit this)"),
    el("div", { class: "row" }, el("span", { class: "k" }, "pre-registration hash (committed before outcomes fetched)"), el("span", { class: "v mono" }, (card.evaluation_unit.prereg_hash || "n/a").slice(0, 24) + "…")),
    el("div", { class: "row" }, el("span", { class: "k" }, "pre-registered at"), el("span", { class: "v mono" }, card.evaluation_unit.prereg_at || "n/a")),
    card.run ? el("div", { class: "row" }, el("span", { class: "k" }, "reproducible run hash"), el("span", { class: "v mono" }, (card.run.reproducible_hash || "n/a").slice(0, 24) + "…")) : null,
    card.run ? el("div", { class: "row" }, el("span", { class: "k" }, "data snapshot id"), el("span", { class: "v mono" }, card.run.data_snapshot_id.slice(0, 24) + "…")) : null,
    card.run ? el("div", { class: "row" }, el("span", { class: "k" }, "methodology version · seed · git"), el("span", { class: "v mono" }, `${card.run.methodology_version} · ${card.run.seed} · ${(card.run.code_git_sha || "n/a").slice(0, 8)}`)) : null));

  // Source trail
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Source trail (every datum → official bytes + content hash)"),
    el("table", { class: "src" },
      el("thead", {}, el("tr", {}, el("th", {}, "source"), el("th", {}, "sha256"), el("th", {}, "retrieved"))),
      el("tbody", {}, ...card.source_trail.map((s) =>
        el("tr", {},
          el("td", {}, el("a", { href: s.source_url, target: "_blank" }, (s.native_identifier || s.source_url).slice(0, 48) + " ↗")),
          el("td", { class: "mono" }, s.content_hash.slice(0, 16) + "…"),
          el("td", { class: "mono" }, (s.retrieved_at || "").slice(0, 19))))))));
}

async function renderOfficials() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("h2", { style: "margin:6px 0" }, "Officials"));
  // Short, neutral framing; the depth lives in About and the per-official drill-down.
  app.appendChild(el("p", { class: "muted", style: "margin:2px 0 6px" },
    "How well an official's actions met the goals those actions set, weighted by their share of credit ",
    tip("composite"),
    ". Shown with coverage ", tip("coverage"), ". Search or filter to begin."));

  // Search/filter panel — labels left, inputs right. Party is intentionally absent
  // from the user-facing experience; filter by topic category instead.
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const search = el("input", { type: "text", placeholder: "type a name…", value: params.get("q") || "" });
  let catOptions = [["", "All categories"]];
  try {
    const cats = await getJSON("/api/categories");
    catOptions = catOptions.concat(cats.categories.map((c) => [c.key, c.label]));
  } catch (e) { /* categories are best-effort; the filter still renders */ }
  const catSel = selectEl(catOptions, params.get("category") || "");
  const typeSel = selectEl(
    [["", "All action types"], ["law", "Laws"], ["eo", "Executive orders"], ["regulation", "Regulations"], ["budget", "Budget execution"]],
    params.get("action_type") || "");
  const scoredOnly = el("input", { type: "checkbox" });
  if (params.get("scored_only") === "1") scoredOnly.checked = true;
  const showAll = el("input", { type: "checkbox" });
  if (params.get("all") === "1") showAll.checked = true;

  const apply = () => {
    const p = new URLSearchParams();
    if (search.value.trim()) p.set("q", search.value.trim());
    if (catSel.value) p.set("category", catSel.value);
    if (typeSel.value) p.set("action_type", typeSel.value);
    if (scoredOnly.checked) p.set("scored_only", "1");
    if (showAll.checked) p.set("all", "1");
    location.hash = "#/officials" + (p.toString() ? "?" + p.toString() : "");
  };
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
  for (const ctl of [catSel, typeSel, scoredOnly, showAll]) ctl.addEventListener("change", apply);

  const frow = (label, control, extra) => el("div", { class: "frow" },
    el("div", { class: "flabel" }, label, extra || null), control);
  app.appendChild(el("div", { class: "filters" },
    frow("Official", search),
    frow("Category", catSel),
    frow("Action type", typeSel),
    frow("Options", el("div", { class: "opts" },
      el("label", {}, scoredOnly, "scored only"),
      el("label", {}, showAll, "show all",
        tip("By default we hide officials whose only tie to a scored action is a negligible role " +
            "(e.g. one vote in a lopsided roll-call, <0.5%). 'Show all' includes them.")))),
    el("div", { class: "fbtns" },
      el("button", { onclick: apply }, "Search"),
      el("a", { class: "cta ghost", href: "#/officials", style: "padding:8px 14px;border-radius:6px" }, "Clear"))));

  // Do not list everyone by default. Show results only once a search or filter is applied.
  const hasQuery = ["q", "category", "action_type", "scored_only"].some((k) => params.get(k));
  if (!hasQuery) {
    app.appendChild(el("div", { class: "card", style: "text-align:center;color:var(--muted)" },
      el("p", {}, "Search by name, or pick a category or action type, to see officials."),
      el("p", { style: "font-size:13px" },
        "Tip: try ", el("a", { href: "#/officials?scored_only=1" }, "officials with a scored action"),
        " to see who has a result.")));
    return;
  }

  const qs = new URLSearchParams();
  if (params.get("q")) qs.set("q", params.get("q"));
  if (params.get("category")) qs.set("category", params.get("category"));
  if (params.get("action_type")) qs.set("action_type", params.get("action_type"));
  if (params.get("scored_only")) qs.set("scored_only", "true");
  // Hide negligible (<0.5%) involvement by default; "show all" turns the floor off.
  if (params.get("all") !== "1") qs.set("min_involvement", "0.005");
  const officials = await getJSON("/api/officials" + (qs.toString() ? "?" + qs.toString() : ""));
  app.appendChild(el("div", { class: "muted mono", style: "margin-bottom:8px" },
    `${officials.length} result(s)` + (params.get("all") === "1" ? ", including negligible-role ties" : "")));
  for (const o of officials) {
    const scoredText = o.composite !== null
      ? `composite ${fmt(o.composite, 1)} · confidence ${(o.confidence * 100).toFixed(0)}%`
      : "insufficient evidence";
    const meta = `${o.scored_actions}/${o.total_actions} scored · ` +
      `coverage ${(o.coverage * 100).toFixed(0)}% · role share ${(o.involvement * 100).toFixed(1)}%`;
    const titleRow = el("div", { class: "title" }, formatName(o.name));
    if (o.position) titleRow.appendChild(el("span", { class: "pill", style: "margin-left:8px" }, o.position));
    app.appendChild(el("div", { class: "list-item", onclick: () => { location.hash = `#/official/${o.id}`; } },
      el("div", {}, titleRow,
        el("div", { class: "muted mono" }, meta)),
      el("div", { style: "text-align:right" },
        o.composite !== null ? el("span", { class: "badge scored" }, scoredText)
          : el("span", { class: "badge insufficient_evidence" }, scoredText))));
  }
}

async function renderOfficialDetail(id) {
  const app = $("#app");
  app.innerHTML = ""; app.appendChild(spinner());
  const card = await getJSON(`/api/officials/${id}`);
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back", href: "#/officials" }, "← all officials"));

  const r = card.rollup;
  const o = card.official;
  const scored = r.composite !== null;
  const pct = (x) => (x === null || x === undefined ? "n/a" : (x * 100).toFixed(0) + "%");
  const who = formatName(o.name);
  const plain = scored
    ? `Across the ${r.scored_actions} of ${who}'s ${r.total_actions} attributable action(s) we could ` +
      `score, the goals those actions set were met to ${fmt(r.composite, 1)} out of 100, weighted by confidence. ` +
      `This reflects only the actions we could score.`
    : `None of ${who}'s ${r.total_actions} attributable action(s) could be isolated yet, so we ` +
      `report no score. We mark this "insufficient evidence" rather than guess.`;

  // Headline: name + big composite + plain-language summary + secondary chips.
  // Party is intentionally not shown; office (where known) and the record are.
  app.appendChild(el("div", { class: "headline" },
    el("p", { class: "name" }, formatName(o.name)),
    el("div", { class: "submeta" },
      [o.position, o.bioguide_id ? `Bioguide ${o.bioguide_id}` : "Official record"]
        .filter(Boolean).join(" · ")),
    el("div", { class: "big" },
      scored
        ? el("span", { class: "bignum scored" }, fmt(r.composite, 1))
        : el("span", { class: "bignum none" }, "Insufficient evidence"),
      scored ? el("span", { class: "ofmax" }, "/ 100 composite") : null,
      tip("composite")),
    el("p", { class: "plain" }, plain),
    el("div", { class: "chips" },
      el("span", { class: "chip" }, "coverage ", el("b", {}, pct(r.coverage)), tip("coverage")),
      el("span", { class: "chip" }, "confidence ", el("b", {}, r.confidence !== null ? pct(r.confidence) : "n/a"), tip("confidence")),
      el("span", { class: "chip" }, "scored ", el("b", {}, `${r.scored_actions}/${r.total_actions}`))),
    el("div", { style: "margin-top:12px" },
      el("a", { href: "#", onclick: (e) => {
        e.preventDefault();
        openModal("How this is calculated", el("div", {},
          el("p", { class: "narrative" }, TIPS.composite),
          el("div", { class: "eq" }, "official composite = sum(share_i x action_composite_i) / sum(share_i)\n   over the official's scored actions"),
          el("div", { class: "eq" }, "action composite = confidence x achievement\n   achievement = how fully the action's stated goal was met (0 to 100)"),
          el("p", { style: "font-size:13px" }, el("b", {}, "Attribution (share of credit). "), TIPS.attribution),
          el("div", { class: "eq" }, "share = authority x pivotality,  normalized so\n   sum(people) + unattributable residual = 1"),
          el("p", { style: "font-size:13px" }, el("b", {}, "Coverage. "), TIPS.coverage),
          el("div", { class: "eq" }, "coverage = scored actions / total attributable actions"),
          el("p", { style: "font-size:13px" }, el("b", {}, "Confidence. "), TIPS.confidence),
          el("div", { class: "eq" }, "confidence = design x data x attribution x model x sensitivity"),
          el("p", { style: "font-size:13px" }, el("b", {}, "Insufficient evidence. "), TIPS.insufficient),
          el("p", { class: "muted", style: "font-size:12px" }, r.note)));
      } }, "How is this calculated? →"))));

  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Their attributable actions"),
    el("p", { class: "muted", style: "font-size:13px;margin-top:-4px" },
      "Every action this official is credited on. Click any row to open its full, source-anchored scorecard (outcome vs. baseline, attribution, confidence, sources)."),
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "action"), el("th", {}, "role"),
        el("th", { class: "right" }, "attribution", tip("attribution")), el("th", {}, "status"),
        el("th", { class: "right" }, "composite", tip("composite")))),
      el("tbody", {}, ...card.actions.map((a) =>
        el("tr", {},
          el("td", {}, el("a", { href: `#/eu/${a.eu_id}` }, a.action_title || `EU ${a.eu_id}`)),
          el("td", {}, a.role),
          el("td", { class: "right mono" }, (a.attribution * 100).toFixed(1) + "%"),
          el("td", {}, statusBadge(a.status || "pending")),
          el("td", { class: "right mono" }, a.composite !== null ? fmt(a.composite, 1) : "n/a")))))));
}

const NODE_COLORS = { official: "#4f9cf9", action: "#2ecc71", jurisdiction: "#f1c40f", metric: "#a06fd0" };
const COLUMN_ORDER = ["official", "action", "jurisdiction", "metric"];

async function renderGraph() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("p", { class: "muted" },
    "Relationship graph: officials \u2192 the actions they're attributed to \u2192 the jurisdiction and " +
    "official outcome metric each action is evaluated against. Click an official or action node to open it."));
  const legend = el("div", { style: "display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 4px" });
  for (const t of COLUMN_ORDER) {
    legend.appendChild(el("span", { class: "muted", style: "font-size:13px" },
      el("span", { style: `display:inline-block;width:12px;height:12px;border-radius:3px;background:${NODE_COLORS[t]};margin-right:6px;vertical-align:middle` }), t));
  }
  app.appendChild(legend);

  // Default: hide tiny non-decisive-vote edges so the graph stays readable; toggle to show all.
  const showAll = (location.hash.split("?")[1] || "").includes("all=1");
  const minWeight = showAll ? 0 : 0.05;
  const toggle = el("div", { class: "muted", style: "font-size:13px;margin:4px 0 10px" },
    showAll ? "Showing all edges (incl. non-decisive votes). " : "Hiding tiny non-decisive-vote edges for readability. ",
    el("a", { href: showAll ? "#/graph" : "#/graph?all=1" }, showAll ? "Hide them" : "Show all"));
  app.appendChild(toggle);

  const g = await getJSON(`/api/graph?min_weight=${minWeight}`);
  // Deterministic layered layout: one column per node type. Generous horizontal
  // spacing + per-type label truncation + right-anchored labels so columns never collide.
  const cols = COLUMN_ORDER.map((t) => g.nodes.filter((n) => n.type === t));
  const colX = { official: 150, action: 560, jurisdiction: 960, metric: 1230 };
  const truncLen = { official: 26, action: 34, jurisdiction: 20, metric: 30 };
  const trunc = (s, n) => (s.length <= n ? s : s.slice(0, n - 1) + "\u2026");
  const rowH = 64, padY = 44, width = 1560;
  const maxRows = Math.max(1, ...cols.map((c) => c.length));
  const height = padY * 2 + maxRows * rowH;
  const pos = {};
  cols.forEach((colNodes, ci) => {
    const t = COLUMN_ORDER[ci];
    const colHeight = colNodes.length * rowH;
    const startY = (height - colHeight) / 2 + rowH / 2;
    colNodes.forEach((n, i) => { pos[n.id] = { x: colX[t], y: startY + i * rowH }; });
  });

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", "100%");
  svg.style.background = "var(--panel)";
  svg.style.border = "1px solid var(--line)";
  svg.style.borderRadius = "12px";

  for (const e of g.edges) {
    const s = pos[e.source], t = pos[e.target];
    if (!s || !t) continue;
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", s.x); line.setAttribute("y1", s.y);
    line.setAttribute("x2", t.x); line.setAttribute("y2", t.y);
    line.setAttribute("stroke", "#2a3650"); line.setAttribute("stroke-width", "1.5");
    svg.appendChild(line);
    // Place the relation label ~32% along the edge (toward the source/left side, away
    // from the long right-column node labels) and nudge it off the line.
    const f = 0.32;
    const mx = s.x + f * (t.x - s.x), my = s.y + f * (t.y - s.y);
    const lbl = document.createElementNS(NS, "text");
    lbl.setAttribute("x", mx); lbl.setAttribute("y", my - 4);
    lbl.setAttribute("fill", "#7c8aa0"); lbl.setAttribute("font-size", "10");
    lbl.setAttribute("text-anchor", "middle");
    lbl.style.pointerEvents = "none";
    lbl.textContent = e.relation;
    svg.appendChild(lbl);
  }

  for (const n of g.nodes) {
    const p = pos[n.id];
    const grp = document.createElementNS(NS, "g");
    grp.style.cursor = (n.type === "official" || n.type === "action") ? "pointer" : "default";
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", "9");
    c.setAttribute("fill", NODE_COLORS[n.type]);
    grp.appendChild(c);
    const txt = document.createElementNS(NS, "text");
    txt.setAttribute("x", p.x + 14);  // all labels extend rightward (no column collisions)
    txt.setAttribute("y", p.y + 4);
    txt.setAttribute("fill", "var(--text)"); txt.setAttribute("font-size", "12");
    txt.setAttribute("text-anchor", "start");
    txt.textContent = trunc(n.label, truncLen[n.type] || 30);
    grp.appendChild(txt);
    if (n.type === "official") grp.addEventListener("click", () => { location.hash = `#/official/${n.ref_id}`; });
    if (n.type === "action") grp.addEventListener("click", () => { location.hash = `#/eu/${n.eu_id}`; });
    svg.appendChild(grp);
  }
  const wrap = el("div", { style: "overflow-x:auto" });
  wrap.appendChild(svg);
  app.appendChild(wrap);
}

async function renderCoverage() {
  const app = $("#app");
  app.innerHTML = "";
  const c = await getJSON("/api/coverage");
  app.appendChild(el("h2", { style: "margin:6px 0" }, "Coverage"));
  app.appendChild(el("p", { class: "muted" },
    "Every action the platform has considered, including those it could not score. " +
    "\u201CInsufficient evidence\u201D is honest abstention, not a low score. The scored subset is not " +
    "a complete or representative record of any official."));
  app.appendChild(el("div", { class: "kpi" },
    el("div", { class: "item" }, el("div", { class: "n" }, String(c.total_evaluation_units)), el("div", { class: "l" }, "actions considered")),
    el("div", { class: "item" }, el("div", { class: "n", style: "color:var(--good)" }, String(c.scored)), el("div", { class: "l" }, "scored")),
    el("div", { class: "item" }, el("div", { class: "n", style: "color:var(--gate)" }, String(c.insufficient_evidence)), el("div", { class: "l" }, "insufficient evidence")),
    el("div", { class: "item" }, el("div", { class: "n", style: "color:var(--muted)" }, String(c.non_scoreable)), el("div", { class: "l" }, "non-scoreable")),
    el("div", { class: "item" }, el("div", { class: "n" }, (c.scored_share * 100).toFixed(1) + "%"), el("div", { class: "l" }, "scored share"))));

  const rows = Object.entries(c.by_action_type).map(([atype, statuses]) => {
    const tot = Object.values(statuses).reduce((a, b) => a + b, 0);
    return el("tr", {},
      el("td", {}, atype),
      el("td", { class: "right mono" }, String(statuses.scored || 0)),
      el("td", { class: "right mono" }, String(statuses.insufficient_evidence || 0)),
      el("td", { class: "right mono" }, String(tot - (statuses.scored || 0) - (statuses.insufficient_evidence || 0))),
      el("td", { class: "right mono" }, String(tot)));
  });
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "By action type"),
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "type"), el("th", { class: "right" }, "scored"),
        el("th", { class: "right" }, "insufficient"), el("th", { class: "right" }, "non-scoreable"), el("th", { class: "right" }, "total"))),
      el("tbody", {}, ...rows))));
  app.appendChild(el("p", { class: "muted", style: "font-size:12px" }, c.note));
}

async function renderAbout() {
  const app = $("#app");
  app.innerHTML = "";
  let m = {};
  try { m = await getJSON("/api/methodology"); } catch (e) { /* fall back to static copy */ }

  app.appendChild(el("h2", { style: "margin:6px 0" }, "What this is, and what it is not"));

  // The neutral framing, in plain language (the credibility spine of the platform).
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "The one question it answers"),
    el("p", { class: "narrative" },
      "For each public action, DegreeZero asks one factual question: did the measurable outcome " +
      "tied to the action's own stated objective move, relative to a defensible baseline, and how " +
      "much of that movement is credibly attributable to this official, with what confidence? " +
      "Every number links back to an official government source."),
    el("p", { class: "muted", style: "font-size:13px" },
      "The yardstick is the policy's own stated goal (statutory purpose, official summary, or its " +
      "own committed target). That makes the question party-symmetric: a jobs bill and a tax cut " +
      "are each asked the same neutral thing.")));

  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "What it deliberately avoids"),
    el("ul", { style: "line-height:1.7" },
      el("li", {}, el("b", {}, "No default \u201Cgood or bad\u201D number. "),
        "A single composite would require a hidden value function (weighing liberty, equality, " +
        "growth, and so on), which is an ideology. The default output is a decomposed, " +
        "source-linked vector; a composite is opt-in, value-laden, and shown only with a watermark."),
      el("li", {}, el("b", {}, "Not an ideology scorer, fact-checker, or pundit. "),
        "No left/right axis, no editorial labels, only numbers, intervals, and sources."),
      el("li", {}, el("b", {}, "\u201CInsufficient evidence\u201D is never a low score. "),
        "When a defensible baseline cannot separate the policy from other forces, the composite is " +
        "withheld and the action is marked insufficient evidence, which is honest abstention."))));

  if (m.philosophy) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Scoring philosophy"),
      el("p", { class: "narrative" }, m.philosophy)));
  }

  if (Array.isArray(m.bias_controls) && m.bias_controls.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "How bias is minimized (adversarial neutrality)"),
      el("ul", { style: "line-height:1.7" }, ...m.bias_controls.map((b) => el("li", {}, b)))));
  }

  const factual = m.components_factual || [];
  const valueLaden = m.components_value_laden_off_by_default || [];
  if (factual.length || valueLaden.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Factual vs. value-laden components"),
      el("p", { class: "muted", style: "font-size:13px" },
        "Factual components are combined by default; value-laden lenses are off unless you turn " +
        "them on (and any non-neutral weighting is watermarked)."),
      el("div", { class: "row" }, el("span", { class: "k" }, "factual (default)"),
        el("span", { class: "v mono" }, factual.join(", ") || "none")),
      el("div", { class: "row" }, el("span", { class: "k" }, "value-laden (opt-in)"),
        el("span", { class: "v mono" }, valueLaden.join(", ") || "none")),
      m.confidence_publish_threshold !== undefined
        ? el("div", { class: "row" }, el("span", { class: "k" }, "confidence publish threshold"),
            el("span", { class: "v mono" }, `${(m.confidence_publish_threshold * 100).toFixed(0)}%, below this the composite is withheld`))
        : null));
  }

  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "The three things that can never be fully empirical"),
    el("p", { class: "muted", style: "font-size:13px" },
      "These residues are labeled, never hidden: (1) which metric operationalizes the objective, " +
      "(2) which counterfactual baseline is right, (3) how to assign causal credit among many " +
      "actors. We shrink them (pre-registration, baseline ensembles, attribution intervals, and a " +
      "large unattributable residual) but never to zero. When a residue dominates, the answer is " +
      "\u201Cinsufficient evidence\u201D.")));

  app.appendChild(el("p", { class: "muted", style: "font-size:12px" },
    "Every published score is independently reproducible (see the Integrity tab) and the full " +
    "method is open source."));
}

async function renderIntegrity() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("h2", { style: "margin:6px 0" }, "Integrity"));
  app.appendChild(el("p", { class: "muted" },
    "Scoring is provably party-blind (the formula never reads party). This page reads party for " +
    "audit only, to watch the distribution of scored outcomes. A flagged gap prompts a human review " +
    "of metric and baseline choices. It never triggers an automated correction or changes any score."));
  const r = await getJSON("/api/integrity/party-symmetry");

  const banner = el("div", { class: "gate-banner " + (r.review_required ? "gated" : "scored") },
    r.review_required
      ? "Review flagged: a systematic gap exceeded a review threshold (see reasons below)."
      : "No systematic gap exceeds the review thresholds on the current scored set.");
  app.appendChild(banner);

  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Party-level distribution of scored outcomes (audit only)"),
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "party"), el("th", { class: "right" }, "attributed EUs"),
        el("th", { class: "right" }, "scored EUs"), el("th", { class: "right" }, "scored share"),
        el("th", { class: "right" }, "mean composite"), el("th", { class: "right" }, "mean confidence"))),
      el("tbody", {}, ...r.parties.map((p) =>
        el("tr", {},
          el("td", {}, p.abbrev),
          el("td", { class: "right mono" }, String(p.attributed_eus)),
          el("td", { class: "right mono" }, String(p.scored_eus)),
          el("td", { class: "right mono" }, (p.scored_share * 100).toFixed(0) + "%"),
          el("td", { class: "right mono" }, p.mean_composite !== null ? fmt(p.mean_composite, 1) : "n/a"),
          el("td", { class: "right mono" }, p.mean_confidence !== null ? (p.mean_confidence * 100).toFixed(0) + "%" : "n/a")))))));

  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Gap checks (vs. review thresholds)"),
    el("div", { class: "row" }, el("span", { class: "k" }, "mean-composite gap"),
      el("span", { class: "v mono" }, (r.composite_gap !== null ? fmt(r.composite_gap, 1) : "n/a (need ≥2 comparable parties)") + ` (threshold ${fmt(r.composite_gap_threshold, 0)})`)),
    el("div", { class: "row" }, el("span", { class: "k" }, "scored-share gap"),
      el("span", { class: "v mono" }, (r.scored_share_gap !== null ? (r.scored_share_gap * 100).toFixed(0) + "%" : "n/a") + ` (threshold ${(r.scored_share_gap_threshold * 100).toFixed(0)}%)`)),
    ...(r.review_reasons.length
      ? r.review_reasons.map((reason) => el("div", { class: "hint" }, reason))
      : [el("div", { class: "muted", style: "font-size:13px;margin-top:8px" }, "No review reasons flagged.")])));

  app.appendChild(el("p", { class: "muted", style: "font-size:12px" }, r.disclaimer));

  // Reproducibility self-audit (on-demand: it re-runs every published score).
  const repro = el("div", { class: "card" });
  repro.appendChild(el("h3", {}, "Reproducibility self-audit"));
  repro.appendChild(el("p", { class: "muted", style: "font-size:13px" },
    "Independently re-derives every published score from its stored inputs + pinned methodology " +
    "and confirms each reproduces its hash bit-for-bit. A mismatch indicates non-determinism or " +
    "tampering. Read-only (re-runs happen in rolled-back savepoints); re-executes scoring, so it " +
    "is run on demand."));
  const out = el("div", { style: "margin-top:8px" });
  repro.appendChild(el("button", {
    onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = "Re-running every score…";
      try {
        const a = await getJSON("/api/integrity/reproducibility");
        out.innerHTML = "";
        out.appendChild(el("div", { class: "gate-banner " + (a.all_reproduced ? "scored" : "gated") },
          a.all_reproduced
            ? `All ${a.reproduced}/${a.total} published scores reproduced bit-for-bit.`
            : `${a.mismatched} mismatch(es) and ${a.errored} inconclusive of ${a.total} scores. Please investigate.`));
        if (a.checks.some((c) => c.status !== "reproduced")) {
          out.appendChild(el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, "EU"), el("th", {}, "status"), el("th", {}, "detail"))),
            el("tbody", {}, ...a.checks.filter((c) => c.status !== "reproduced").map((c) =>
              el("tr", {}, el("td", { class: "mono" }, String(c.eu_id)),
                el("td", {}, el("span", { class: "badge insufficient_evidence" }, c.status)),
                el("td", { class: "muted", style: "font-size:12px" }, c.detail || "n/a"))))));
        }
      } catch (err) {
        out.textContent = "Error: " + err.message;
      } finally {
        e.target.disabled = false; e.target.textContent = "Run reproducibility self-audit";
      }
    },
  }, "Run reproducibility self-audit"));
  repro.appendChild(out);
  app.appendChild(repro);
}

async function route() {
  renderNav();
  const eu = location.hash.match(/#\/eu\/(\d+)/);
  const off = location.hash.match(/#\/official\/(\d+)/);
  // Show a spinner immediately so navigation (and cold starts) never look frozen.
  const isLanding = !location.hash || location.hash === "#/" || location.hash === "#";
  if (!isLanding) { $("#app").innerHTML = ""; $("#app").appendChild(spinner()); }
  renderAuditStatus();  // header badge; fire-and-forget so it never blocks the view
  try {
    if (eu) await renderDetail(eu[1]);
    else if (off) await renderOfficialDetail(off[1]);
    else if (location.hash.startsWith("#/officials")) await renderOfficials();
    else if (location.hash.startsWith("#/graph")) await renderGraph();
    else if (location.hash.startsWith("#/coverage")) await renderCoverage();
    else if (location.hash.startsWith("#/integrity")) await renderIntegrity();
    else if (location.hash.startsWith("#/about")) await renderAbout();
    else if (location.hash.startsWith("#/sources")) await renderSources();
    else if (location.hash.startsWith("#/glossary")) await renderGlossary();
    else if (location.hash.startsWith("#/actions")) await renderList();
    else await renderLanding();  // default = welcoming landing/hero
  } catch (e) {
    $("#app").innerHTML = `<div class="card">Error: ${e.message}. The API may be waking up. Retry in a moment.</div>`;
  }
}

// Boot splash / title screen shown while the app (and a possibly cold-started server) come up.
function showSplash() {
  if (document.getElementById("splash")) return;
  document.body.appendChild(el("div", { class: "splash", id: "splash" },
    el("img", { src: "/logo.png", alt: "" }),
    el("div", { class: "title" }, "DegreeZero"),
    el("div", { class: "spinner" }),
    el("div", { class: "sub" }, "Loading. First load can take a moment while the server wakes up.")));
}
function hideSplash() {
  const s = document.getElementById("splash");
  if (s) { s.classList.add("fade"); setTimeout(() => s.remove(), 400); }
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => {
  showSplash();
  route().finally(hideSplash);
});
