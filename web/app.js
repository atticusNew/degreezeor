"use strict";
// Zero-build explainability SPA. Pure client of the /api contract.
// Demonstrates the platform's default output: a decomposed, source-linked,
// confidence-gated scorecard — NOT a single normative verdict.

const API = ""; // same origin (FastAPI serves both)
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
const fmt = (x, d = 2) => (x === null || x === undefined ? "—" : Number(x).toFixed(d));
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

async function disputesCard(euId) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, "Challenge / appeal this score"));
  card.appendChild(el("p", { class: "muted", style: "font-size:13px" },
    "Anyone may dispute a score. Resolution is not editorial — it triggers an independent, " +
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
  const claim = el("textarea", { placeholder: "what do you dispute and why?", rows: "2", style: "width:100%;margin:4px 0;padding:6px;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:6px" });
  const submit = el("button", {
    onclick: async () => {
      if (!claim.value.trim()) return;
      await postJSON("/api/disputes", { eu_id: euId, filer: filer.value || "anonymous", claim: claim.value });
      claim.value = ""; filer.value = "";
      await refresh();
    },
  }, "File challenge");
  card.appendChild(filer);
  card.appendChild(claim);
  card.appendChild(submit);
  card.appendChild(el("h3", { style: "margin-top:16px" }, "Disputes"));
  card.appendChild(listWrap);
  await refresh();
  return card;
}

async function renderAuditStatus() {
  try {
    const a = await getJSON("/api/audit/verify");
    const node = $("#audit-status");
    node.textContent = a.audit_chain_ok ? "● audit chain verified" : "● AUDIT CHAIN BROKEN";
    node.className = "mono " + (a.audit_chain_ok ? "audit-ok" : "audit-bad");
  } catch (e) { /* ignore */ }
}

function statusBadge(status) {
  return el("span", { class: "badge " + status }, status.replaceAll("_", " "));
}

async function renderList() {
  const app = $("#app");
  app.innerHTML = "";
  app.appendChild(el("p", { class: "muted" },
    "Each entry is an Evaluation Unit: an action measured against its own stated objective. " +
    "The platform abstains (\"insufficient evidence\") rather than over-claim when a defensible " +
    "baseline cannot separate the policy from concurrent shocks."));
  const units = await getJSON("/api/evaluation-units");
  for (const u of units) {
    app.appendChild(el("div", { class: "list-item", onclick: () => { location.hash = `#/eu/${u.id}`; } },
      el("div", {},
        el("div", { class: "title" }, u.title),
        el("div", { class: "muted mono" }, (u.public_law ? `Public Law ${u.public_law}` : `EU #${u.id}`))),
      el("div", { style: "text-align:right" },
        statusBadge(u.status),
        el("div", { class: "muted", style: "margin-top:6px;font-size:12px" },
          `confidence ${u.confidence === null ? "—" : (u.confidence * 100).toFixed(1) + "%"}`,
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
      `Non-scoreable: ${card.evaluation_unit.non_scoreable_reason || "no operational metric / outcome."} ` +
      "This is reported as absence of evidence — never as a low score.");
  }
  if (s.gated) {
    return el("div", { class: "gate-banner gated" },
      `INSUFFICIENT EVIDENCE — confidence ${(s.confidence * 100).toFixed(1)}% is below the ` +
      `${(s.publish_threshold * 100).toFixed(0)}% publish threshold. No composite verdict is issued. ` +
      "The full decomposition below is still shown for transparency.");
  }
  return el("div", { class: "gate-banner scored" },
    `Composite ${fmt(s.composite, 1)}/100 (confidence-scaled, factual components only). ` +
    `Confidence ${(s.confidence * 100).toFixed(1)}%.`);
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
      mark.textContent = "⚠ CUSTOM VALUE WEIGHTS — value-laden, not the neutral default.";
    }
    const total = present.reduce((a, n) => a + weights[n], 0) || 1;
    const weighted = present.reduce((a, n) => a + (weights[n] / total) * comps[n], 0);
    const gated = card.score && card.score.gated;
    if (!card.score) {
      result.textContent = "Non-scoreable: no composite can be formed.";
    } else if (gated) {
      result.innerHTML = `Composite still <b>suppressed</b> (insufficient evidence) regardless of weights. ` +
        `Indicative weighted mean (NOT published): ${weighted.toFixed(1)}`;
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
  app.innerHTML = "Loading…";
  const card = await getJSON(`/api/evaluation-units/${id}`);
  app.innerHTML = "";

  app.appendChild(el("a", { class: "back", href: "#/" }, "← all evaluation units"));
  app.appendChild(el("h2", { style: "margin:6px 0" }, card.action.title));
  app.appendChild(el("div", { class: "muted mono" },
    `${card.action.type.toUpperCase()} · ${card.action.public_law_number ? "Public Law " + card.action.public_law_number : ""} · ${card.action.domain || ""} · enacted ${card.action.enacted_date || "—"}`));
  app.appendChild(el("div", { style: "margin:10px 0" }, statusBadge(card.evaluation_unit.status)));

  app.appendChild(gateBanner(card));

  // Narrative
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Why this scored this way"),
    el("div", { class: "narrative" }, card.narrative)));

  // Components vector
  if (card.components.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Decomposed score vector (default output — not a single verdict)"),
      ...card.components.map(componentBar)));
  }

  // Objective + metric
  if (card.objective) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Stated objective (the yardstick — the action's OWN goal)"),
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
      el("h3", {}, "Attribution (always leaves a large unattributable residual)"),
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "role"), el("th", {}, "who"), el("th", { class: "right" }, "attribution"), el("th", { class: "right" }, "95% band"))),
        el("tbody", {}, ...card.attribution.map((a) =>
          el("tr", {},
            el("td", {}, a.is_residual ? el("span", { class: "pill" }, a.role.replaceAll("_", " ")) : a.role),
            el("td", {}, a.official_name || "—"),
            el("td", { class: "right mono" }, (a.attribution * 100).toFixed(1) + "%"),
            el("td", { class: "right mono" }, `[${(a.ci_low * 100).toFixed(0)}, ${(a.ci_high * 100).toFixed(0)}]%`)))))));
  }

  // User value weights (composite path demo, gate-respecting)
  if (card.components.length) {
    app.appendChild(el("div", { class: "card" },
      el("h3", {}, "Your value weights (optional · default = neutral)"),
      valueWeightPanel(card)));
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
    el("div", { class: "row" }, el("span", { class: "k" }, "pre-registration hash (committed before outcomes fetched)"), el("span", { class: "v mono" }, (card.evaluation_unit.prereg_hash || "—").slice(0, 24) + "…")),
    el("div", { class: "row" }, el("span", { class: "k" }, "pre-registered at"), el("span", { class: "v mono" }, card.evaluation_unit.prereg_at || "—")),
    card.run ? el("div", { class: "row" }, el("span", { class: "k" }, "reproducible run hash"), el("span", { class: "v mono" }, (card.run.reproducible_hash || "—").slice(0, 24) + "…")) : null,
    card.run ? el("div", { class: "row" }, el("span", { class: "k" }, "data snapshot id"), el("span", { class: "v mono" }, card.run.data_snapshot_id.slice(0, 24) + "…")) : null,
    card.run ? el("div", { class: "row" }, el("span", { class: "k" }, "methodology version · seed · git"), el("span", { class: "v mono" }, `${card.run.methodology_version} · ${card.run.seed} · ${(card.run.code_git_sha || "—").slice(0, 8)}`)) : null));

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
  app.appendChild(el("p", { class: "muted" },
    "Official-level roll-up: the attribution-weighted mean composite over each official's " +
    "SCORED actions — always shown with coverage. \u201CInsufficient evidence\u201D means none of " +
    "their actions cleared the confidence gate (never a low score)."));
  const officials = await getJSON("/api/officials");
  for (const o of officials) {
    const scoredText = o.composite !== null
      ? `composite ${fmt(o.composite, 1)} · confidence ${(o.confidence * 100).toFixed(0)}%`
      : "insufficient evidence";
    app.appendChild(el("div", { class: "list-item", onclick: () => { location.hash = `#/official/${o.id}`; } },
      el("div", {}, el("div", { class: "title" }, o.name || `Official #${o.id}`),
        el("div", { class: "muted mono" }, `${o.scored_actions}/${o.total_actions} actions scored · coverage ${(o.coverage * 100).toFixed(0)}%`)),
      el("div", { style: "text-align:right" },
        o.composite !== null ? el("span", { class: "badge scored" }, scoredText)
          : el("span", { class: "badge insufficient_evidence" }, scoredText))));
  }
}

async function renderOfficialDetail(id) {
  const app = $("#app");
  app.innerHTML = "Loading…";
  const card = await getJSON(`/api/officials/${id}`);
  app.innerHTML = "";
  app.appendChild(el("a", { class: "back", href: "#/officials" }, "← all officials"));
  app.appendChild(el("h2", { style: "margin:6px 0" }, card.official.name));
  if (card.official.bioguide_id) app.appendChild(el("div", { class: "muted mono" }, `Bioguide ${card.official.bioguide_id}`));

  const r = card.rollup;
  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Roll-up (attribution-weighted, with coverage)"),
    el("div", { class: "kpi" },
      el("div", { class: "item" }, el("div", { class: "n" }, r.composite !== null ? fmt(r.composite, 1) : "—"), el("div", { class: "l" }, "composite")),
      el("div", { class: "item" }, el("div", { class: "n" }, r.confidence !== null ? (r.confidence * 100).toFixed(0) + "%" : "—"), el("div", { class: "l" }, "confidence")),
      el("div", { class: "item" }, el("div", { class: "n" }, `${r.scored_actions}/${r.total_actions}`), el("div", { class: "l" }, "scored / total")),
      el("div", { class: "item" }, el("div", { class: "n" }, (r.coverage * 100).toFixed(0) + "%"), el("div", { class: "l" }, "coverage"))),
    r.composite === null ? el("div", { class: "gate-banner gated", style: "margin-top:10px" },
      "INSUFFICIENT EVIDENCE — none of this official's attributable actions cleared the confidence gate. This is not a low score.") : null,
    el("p", { class: "muted", style: "font-size:12px" }, r.note)));

  app.appendChild(el("div", { class: "card" },
    el("h3", {}, "Attributable actions"),
    el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "action"), el("th", {}, "role"), el("th", { class: "right" }, "attribution"), el("th", {}, "status"), el("th", { class: "right" }, "composite"))),
      el("tbody", {}, ...card.actions.map((a) =>
        el("tr", {},
          el("td", {}, el("a", { href: `#/eu/${a.eu_id}` }, a.action_title || `EU ${a.eu_id}`)),
          el("td", {}, a.role),
          el("td", { class: "right mono" }, (a.attribution * 100).toFixed(1) + "%"),
          el("td", {}, statusBadge(a.status || "pending")),
          el("td", { class: "right mono" }, a.composite !== null ? fmt(a.composite, 1) : "—")))))));
}

async function route() {
  await renderAuditStatus();
  const eu = location.hash.match(/#\/eu\/(\d+)/);
  const off = location.hash.match(/#\/official\/(\d+)/);
  try {
    if (eu) await renderDetail(eu[1]);
    else if (off) await renderOfficialDetail(off[1]);
    else if (location.hash.startsWith("#/officials")) await renderOfficials();
    else await renderList();
  } catch (e) {
    $("#app").innerHTML = `<div class="card">Error: ${e.message}. Is the API running?</div>`;
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
