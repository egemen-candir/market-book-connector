"use strict";
/* Market Book Connector Dashboard — vanilla JS, no framework, no build step.
 * Renders the server-side view-model from /api/runs/<id>/view.
 * All dynamic text is inserted via textContent/createTextNode — never
 * innerHTML — because evaluator prose is LLM output (untrusted markup).
 * No network calls beyond the local server hosting this file.
 */

var REASON_LABELS = {
  out_of_scope: "different asset class / scope",
  insufficient_evidence: "not enough evidence in the input",
  coincidental_neighbor: "surface-similar, different phenomenon",
  direction_discordant: "opposite direction",
  family_sibling_rejected: "sibling state fits better"
};

var CITATION_KIND_LABELS = {
  observable_signature: "Observable signature",
  policy_implication: "Policy implication",
  failure_pattern: "Failure pattern",
  extracted_claim: "Cited claim"
};

var STEP_NAMES = {
  capture_raw_input: "Capture raw input",
  observation_packet_builder: "Observation packet builder",
  validate_observation_packet: "Observation packet validation",
  observation_packet_validation: "Observation packet validation",
  retrieve_candidate_entries: "Candidate retrieval",
  market_book_evaluator: "Market Book evaluator",
  validate_market_book_evaluation: "Market Book evaluation validation",
  market_book_evaluation_validation: "Market Book evaluation validation",
  export_signal_context: "Signal context export",
  render_dashboard_report: "Dashboard report render"
};

var DIRECTION_ARROWS = {
  rising: "↑", improving: "↑", strengthening: "↑",
  falling: "↓", deteriorating: "↓", weakening: "↓"
};

var NEAR_MISS_LABEL =
  "These states scored as textually similar to your observations but fell just " +
  "outside the shortlist that was sent to the evaluator. The evaluator never " +
  "judged them. The score is a similarity ranking, not a probability or a match " +
  "strength.";

/* Plain-language explanations for failed runs, keyed by failed_step. */
var FAILURE_EXPLANATIONS = {
  observation_packet_builder:
    "The builder model did not produce a usable structured reading of the " +
    "commentary, so nothing was evaluated.",
  observation_packet_validation:
    "The builder's structured reading of your commentary did not pass the " +
    "format and safety checks, so nothing was evaluated.",
  validate_observation_packet:
    "The builder's structured reading of your commentary did not pass the " +
    "format and safety checks, so nothing was evaluated.",
  retrieve_candidate_entries:
    "The search step that picks candidate states from the catalogue did not " +
    "complete, so nothing was evaluated.",
  market_book_evaluator:
    "The evaluator model did not produce a usable judgement of the candidate " +
    "states.",
  market_book_evaluation_validation:
    "The evaluator's output did not pass the format, wording, or reference " +
    "checks, so its judgement was not accepted.",
  validate_market_book_evaluation:
    "The evaluator's output did not pass the format, wording, or reference " +
    "checks, so its judgement was not accepted.",
  export_signal_context:
    "The context record could not be built or did not pass its checks.",
  signal_context_validation:
    "The context record could not be built or did not pass its checks."
};

var PROHIBITED_PHRASE_NOTE =
  "The pipeline blocks wording that reads like a trading instruction. This " +
  "often comes from phrasing in the commentary itself.";

var LIMITS_SENTENCE =
  "This is a model's reading of your text against research records. It does " +
  "not measure markets and is not a trading instruction.";

var SOURCE_LABELS = {
  curated: "Example result",
  recent: "Recent run",
  id: "Opened by ID",
  link: "Opened from link",
  submitted: "Just submitted"
};

var OUTCOME_TAGS = {
  matched: ["Match", "tag-matched"],
  no_match: ["No match", "tag-nomatch"],
  failed: ["Failed", "tag-failed"],
  legacy: ["Older format", "tag-legacy"],
  running: ["Running", "tag-running"]
};

var VALIDATOR_LABELS = {
  observation_packet: "Builder output check",
  market_book_evaluation: "Evaluator output check",
  signal_context: "Context record check"
};

var PIPELINE_CHECKS_CAPTION =
  "These checks confirm the outputs have the required structure and valid " +
  "references. They do not confirm that the interpretation is correct.";

var ROLES_FILE = "__roles_file__";   // sentinel: send no override for this role
var _rolePins = { builder: null, evaluator: null };   // from the server's roles file

var PIPELINE_ORDER = [
  "Read commentary (builder model)",
  "Structure check",
  "Find candidate states",
  "Evaluate (evaluator model)",
  "Output check",
  "Build context"
];


/* Human labels for enum values; the raw value stays visible as a small code tag. */
var POSTURE_LABELS = {
  defensive_monitoring: "Defensive monitoring",
  risk_off_monitoring: "Risk-off monitoring",
  neutral_observation: "Neutral observation",
  regime_dependent: "Regime dependent",
  caution_flagged: "Caution flagged",
  no_actionable_posture: "No actionable posture",
  unsupported: "Unsupported"
};

var CONFIDENCE_LABELS = {
  well_supported: "well supported",
  partially_supported: "partially supported",
  weakly_supported: "weakly supported",
  unsupported: "unsupported"
};

/* Prefixes the exporter writes into constraints / failure modes / follow-ups. */
var DERIVED_PREFIX_LABELS = {
  low_confidence_match: "Low-confidence match",
  weak_match: "Weak match",
  measurement_unavailable: "Measurement unavailable",
  requires_confirmation: "Requires confirmation",
  measure_feature: "Measure",
  acquire_measurement: "Acquire measurement"
};

/* The one active run: everything on screen below the input panel describes it. */
var _active = { runId: null, source: null, view: null };
var _pollTimer = null;
var _elapsedTimer = null;
var _profiles = { mock: { backend_type: "mock", timeout_seconds: 30 } };
var _defaultProfile = "mock";

/* ---------- DOM helpers (text-only, never innerHTML with data) ---------- */

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function chip(className, text) {
  return el("span", "chip " + className, text);
}

/* Small ⓘ button that opens the Guide at `topic`. */
function infoBtn(topic, about) {
  var b = el("button", "info-btn", "ⓘ");
  b.type = "button";
  b.setAttribute("data-guide", topic);
  b.setAttribute("aria-label", "About " + about);
  return b;
}

/* "Caution flagged `caution_flagged`" as a chip. */
function postureChip(value) {
  var c = el("span", "chip posture");
  c.appendChild(document.createTextNode(POSTURE_LABELS[value] || enumText(value)));
  if (POSTURE_LABELS[value]) {
    c.appendChild(document.createTextNode(" "));
    c.appendChild(el("code", "raw", value));
  }
  return c;
}

/* "low_confidence_match: entry_x" -> "Low-confidence match: entry_x" + raw code tag. */
function derivedItem(li, text) {
  var m = /^([a-z_]+):\s*(.*)$/.exec(String(text));
  if (m && DERIVED_PREFIX_LABELS[m[1]]) {
    li.appendChild(document.createTextNode(DERIVED_PREFIX_LABELS[m[1]] + ": "));
    li.appendChild(el("span", "wrap-any", m[2]));
    li.appendChild(document.createTextNode(" "));
    li.appendChild(el("code", "raw", m[1]));
  } else {
    li.appendChild(document.createTextNode(String(text)));
  }
  return li;
}

/* Heading element with a trailing ⓘ button. */
function headingWithInfo(tag, text, topic, about, className) {
  var h = el(tag, className || "", text);
  h.appendChild(document.createTextNode(" "));
  h.appendChild(infoBtn(topic, about));
  return h;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function enumText(value) {
  return String(value || "").replace(/_/g, " ");
}

/* "2026-09-01T18:31:46Z" -> "2026-09-01 18:31 UTC" (no locale surprises). */
function formatTime(iso) {
  if (!iso) return "";
  var m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(String(iso));
  return m ? m[1] + " " + m[2] + " UTC" : String(iso);
}

function fetchJSON(url) {
  return fetch(url).then(function (resp) {
    if (!resp.ok) {
      return resp.text().then(function (body) {
        throw new Error("HTTP " + resp.status + " — " + body.slice(0, 300));
      });
    }
    return resp.json();
  });
}

/* ---------- Select population ---------- */

function fillSelect(select, items, placeholder, labelFn, valueFn) {
  clear(select);
  var ph = el("option", "", placeholder);
  ph.value = "";
  select.appendChild(ph);
  items.forEach(function (item) {
    var opt = el("option", "", labelFn(item));
    opt.value = valueFn(item);
    select.appendChild(opt);
  });
}

function populateSelects() {
  fetchJSON("/api/curated-runs").then(function (data) {
    var runs = data.curated_runs || [];
    fillSelect(
      document.getElementById("curated-select"),
      runs,
      runs.length ? "(choose an example result…)" : "(none bundled)",
      function (r) {
        return r.label || r.id;
      },
      function (r) { return r.id; }
    );
    document.getElementById("curated-empty").hidden = runs.length > 0;
    if (_active.source === "curated" && _active.runId) {
      document.getElementById("curated-select").value = _active.runId;
    }
  }).catch(function () {
    fillSelect(document.getElementById("curated-select"), [],
      "(example results unavailable)", null, null);
    document.getElementById("curated-empty").hidden = false;
  });

  fetchJSON("/api/examples").then(function (data) {
    var select = document.getElementById("example-select");
    fillSelect(select, data.examples || [], "(choose an example…)",
      function (e) { return e.name; }, function (e) { return e.name; });
    select._examples = {};
    (data.examples || []).forEach(function (e) { select._examples[e.name] = e; });
  }).catch(function () {
    fillSelect(document.getElementById("example-select"), [],
      "(examples unavailable)", null, null);
  });

  fetchJSON("/api/backend-profiles").then(function (data) {
    _profiles = data.profiles || {};
    _defaultProfile = data.default || "mock";
    _rolePins = data.role_pins || { builder: null, evaluator: null };
    var names = Object.keys(_profiles);
    ["builder-backend-select", "evaluator-backend-select"].forEach(function (id) {
      var select = document.getElementById(id);
      clear(select);
      names.forEach(function (n) {
        var opt = el("option", "", profileLabel(n));
        opt.value = n;
        select.appendChild(opt);
      });
      var rf = el("option", "", "Use my roles file (no override for this step)");
      rf.value = ROLES_FILE;
      select.appendChild(rf);
      select.value = names.indexOf(_defaultProfile) >= 0 ? _defaultProfile : (names[0] || "");
    });
    updateModelsBlock();
  }).catch(function () {
    document.getElementById("time-limit-line").textContent =
      "Profile list unavailable; only the mock profile is offered.";
    updateModelsBlock();
  });

  refreshRecentRuns();
}

function refreshRecentRuns() {
  fetchJSON("/api/runs").then(function (data) {
    var runs = data.runs || [];
    var recent = document.getElementById("recent-select");
    fillSelect(
      recent,
      runs,
      runs.length ? "(reopen a recent run…)" : "(none yet)",
      function (r) {
        return [formatTime(r.created_at_utc) || "(time unknown)",
                r.status || "?", r.backend || "?"].join(" · ");
      },
      function (r) { return r.run_id; }
    );
    if (_active.source === "recent" && _active.runId) recent.value = _active.runId;
  }).catch(function () { /* recent runs are a convenience; ignore */ });
}

/* ---------- Models for this run ---------- */

function profileLabel(name) {
  var p = _profiles[name] || {};
  if (p.backend_type === "mock") return name + " — replays bundled sample output (no model call)";
  if (p.backend_type === "cli") return name + " — command-line model";
  return p.backend_type ? name + " — " + p.backend_type : name;
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) return null;
  if (seconds < 90) return seconds + " s";
  var min = Math.round(seconds / 60);
  return min + " min";
}

function roleChoice(id) {
  return document.getElementById(id).value || _defaultProfile;
}

/* What the page will send, and what it can say about it. */
function modelSelection() {
  var b = roleChoice("builder-backend-select");
  var e = roleChoice("evaluator-backend-select");
  var explicit = [b, e].filter(function (v) { return v !== ROLES_FILE; });
  var backend = explicit.length ? explicit[0] : _defaultProfile;
  return { builder: b, evaluator: e, backend: backend };
}

function roleDisplay(choice) {
  return choice === ROLES_FILE ? "roles file" : choice;
}

/* The profile a step will actually use: an explicit choice wins, then the roles
   file pin, then the run's default backend (same precedence as the pipeline). */
function effectiveProfile(choice, role, sel) {
  if (choice !== ROLES_FILE) return choice;
  return _rolePins[role] || sel.backend;
}

function updateModelsBlock() {
  var same = document.getElementById("same-profile").checked;
  var bSel = document.getElementById("builder-backend-select");
  var eSel = document.getElementById("evaluator-backend-select");
  if (same) eSel.value = bSel.value;
  eSel.disabled = same;
  var sel = modelSelection();

  // Time limit line from the selected profiles' timeout_seconds.
  var effB = effectiveProfile(sel.builder, "builder", sel);
  var effE = effectiveProfile(sel.evaluator, "evaluator", sel);
  var limits = [];
  [["Builder", effB], ["Evaluator", effE]].forEach(function (pair) {
    var t = formatDuration((_profiles[pair[1]] || {}).timeout_seconds);
    if (t) limits.push([pair[0], t]);
  });
  var line = document.getElementById("time-limit-line");
  if (limits.length === 2 && limits[0][1] === limits[1][1]) {
    line.textContent = "Each model step may take up to " + limits[0][1] + " (profile time limit).";
  } else if (limits.length) {
    line.textContent = limits.map(function (l) {
      return l[0] + " step may take up to " + l[1];
    }).join("; ") + " (profile time limit).";
  } else {
    line.textContent = "";
  }

  var usesRolesFile = sel.builder === ROLES_FILE || sel.evaluator === ROLES_FILE;
  var note = document.getElementById("roles-file-note");
  note.hidden = !usesRolesFile;
  var resolved = [];
  if (sel.builder === ROLES_FILE) resolved.push("builder: " + effB);
  if (sel.evaluator === ROLES_FILE) resolved.push("evaluator: " + effE);
  note.textContent = usesRolesFile
    ? "For a step set to “roles file”, the pipeline uses the profile pinned in the roles " +
      "file the dashboard was started with, or " + sel.backend + " if none is pinned. " +
      "For this run that means " + resolved.join(", ") + ". The saved result shows " +
      "which profile was actually used."
    : "";

  var isMock = function (c) { return (_profiles[c] || {}).backend_type === "mock"; };
  document.getElementById("mock-notice").hidden = !(isMock(effB) || isMock(effE));
}

function updateCommentaryState() {
  var ta = document.getElementById("raw-input");
  var n = ta.value.length;
  document.getElementById("raw-input-count").textContent =
    n + (n === 1 ? " character" : " characters");
  var empty = !ta.value.trim();
  var run = document.getElementById("run-button");
  run.disabled = empty;
  document.getElementById("raw-input-message").textContent = empty
    ? "Paste commentary or start from an example to enable Run." : "";
}

/* ---------- Modes ---------- */

function setMode(mode, focusTab) {
  ["explore", "evaluate"].forEach(function (m) {
    var tab = document.getElementById("tab-" + m);
    var panel = document.getElementById("mode-" + m);
    var on = m === mode;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    panel.hidden = !on;
    if (on && focusTab) tab.focus();
  });
}

/* ---------- Shared renderers ---------- */

function confidenceChip(confidence) {
  if (!confidence) return null;
  return chip("conf-" + confidence, CONFIDENCE_LABELS[confidence] || enumText(confidence));
}

/* "Support for this conclusion: <chip>" — says WHAT the confidence rates. */
function supportLine(confidence, outcome) {
  if (!confidence) return null;
  var line = el("p", "support-line");
  line.appendChild(el("span", "support-label",
    outcome === "no_match" ? "Support for not matching: "
                           : "Support for this conclusion: "));
  line.appendChild(confidenceChip(confidence));
  line.appendChild(document.createTextNode(" "));
  line.appendChild(infoBtn("confidence", "confidence"));
  return line;
}

function renderNearMiss(band) {
  var region = document.getElementById("near-miss-region");
  clear(region);
  var section = el("div", "near-miss");
  section.appendChild(headingWithInfo("h3", "Similar states not sent to the evaluator",
    "neighbors", "similar states not sent to the evaluator"));
  section.appendChild(el("p", "near-miss-label", NEAR_MISS_LABEL));
  if (!band) {
    section.appendChild(el("p", "muted",
      "The search record for this run is not available, so this list cannot be shown."));
  } else if (!band.length) {
    section.appendChild(el("p", "muted",
      "No other similar states were recorded just outside the shortlist."));
  } else {
    band.forEach(function (entry) {
      var row = el("div", "nm-row");
      var head = el("div", "nm-head");
      head.appendChild(el("span", "nm-title",
        entry.title || entry.state_id || entry.entry_id || "(unknown state)"));
      var score = entry.retrieval_score;
      var scoreSpan = el("span", "nm-score mono",
        (typeof score === "number") ? "similarity " + score.toFixed(3) : "—");
      scoreSpan.title = "Similarity ranking score, not a probability or match strength";
      head.appendChild(scoreSpan);
      row.appendChild(head);
      var closest = entry.closest_on || {};
      var closestText = closest.text_snippet || closest.record_id;
      if (closestText) {
        // Two lines on screen with a "Show all" button; no hover needed.
        row.appendChild(clampedText("Most similar record: " + closestText, "nm-closest"));
      }
      section.appendChild(row);
    });
  }
  region.appendChild(section);
}

function renderLabeledList(parent, label, items, emptyText, topic) {
  var block = el("div", "ctx-list");
  block.appendChild(topic ? headingWithInfo("h4", label, topic, "limits and caveats")
                          : el("h4", "", label));
  if (!items || !items.length) {
    block.appendChild(el("p", "muted", emptyText || "None recorded."));
  } else {
    var ul = el("ul");
    items.forEach(function (item) { ul.appendChild(derivedItem(el("li", "wrap-any"), item)); });
    block.appendChild(ul);
  }
  parent.appendChild(block);
}

function notValidatedTag() {
  return el("span", "not-validated",
    "Not validated — produced before the failure above");
}

function renderObservationPacket(body, view) {
  var input = view.input || {};
  var failed = view.outcome === "failed";
  var section = el("div", "packet" + (failed ? " packet-unvalidated" : ""));
  var h = headingWithInfo("h3", "How the builder read your commentary",
    "packet", "how the builder read your commentary");
  if (failed) {
    h.appendChild(document.createTextNode(" "));
    h.appendChild(notValidatedTag());
  }
  section.appendChild(h);
  section.appendChild(el("p", "muted small",
    "The builder model's structured reading of the text. These are statements " +
    "taken from the commentary, not measured market data."));
  if (input.summary) section.appendChild(el("p", "", input.summary));
  if (input.asset_scope && input.asset_scope.length) {
    var scope = el("p", "muted", "Asset scope: " + input.asset_scope.join(", "));
    section.appendChild(scope);
  }
  if (input.as_of) {
    var bits = [];
    if (input.as_of.date) bits.push("as of " + input.as_of.date);
    if (input.as_of.observation_horizon) bits.push(enumText(input.as_of.observation_horizon));
    if (input.as_of.timezone) bits.push(input.as_of.timezone);
    if (bits.length) section.appendChild(el("p", "muted", bits.join(" · ")));
  }
  var observations = view.observations || [];
  if (observations.length) {
    var table = el("table", "obs-table");
    if (failed) {
      var cap = el("caption", "obs-caption");
      cap.appendChild(notValidatedTag());
      table.appendChild(cap);
    }
    var thead = el("thead");
    var hrow = el("tr");
    ["#", "dimension", "direction", "magnitude", "statement"].forEach(function (h) {
      hrow.appendChild(el("th", "", h));
    });
    thead.appendChild(hrow);
    table.appendChild(thead);
    var tbody = el("tbody");
    var cols = ["#", "dimension", "direction", "magnitude", "statement"];
    observations.forEach(function (obs) {
      var tr = el("tr");
      [obs.index, obs.dimension, obs.direction, obs.magnitude, obs.statement]
        .forEach(function (value, i) {
          var td = el("td", i === 0 ? "mono" : "", value);
          td.setAttribute("data-label", cols[i]);   // pseudo-header in the stacked layout
          tr.appendChild(td);
        });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    var wrap = el("div", "table-wrap");
    wrap.appendChild(table);
    section.appendChild(wrap);
  } else {
    section.appendChild(el("p", "muted", "No observation packet available."));
  }
  body.appendChild(section);
}

function renderProvenance(body, view) {
  var prov = view.provenance || {};
  var meta = view.meta || {};
  var section = el("div", "provenance");
  section.appendChild(headingWithInfo("h3", "Files & downloads", "files", "files and downloads"));

  // Downloads only when a signal context exists; the endpoints 404 otherwise.
  var downloads = prov.downloads || {};
  if (view.context && downloads.context_json) {
    var row = el("div", "download-row");
    var aJson = el("a", "button", "Download signal context (JSON)");
    aJson.href = downloads.context_json;
    var aYaml = el("a", "button", "Download signal context (YAML)");
    aYaml.href = downloads.context_yaml;
    row.appendChild(aJson);
    row.appendChild(aYaml);
    section.appendChild(row);
    section.appendChild(el("p", "file-note",
      "What's in this file: the signal-context record — posture, caveats, " +
      "constraints, lists, and source-artifact paths. It is a compact " +
      "machine-readable summary, not a copy of this page. Some fields shown " +
      "here, such as the observation summary, may be empty in the file."));
  } else {
    section.appendChild(el("p", "file-note",
      "No signal context was produced for this run, so there is nothing to " +
      "download. Context is built only after both model outputs pass their checks."));
  }

  var artifacts = prov.source_artifacts || {};
  var names = Object.keys(artifacts);
  if (names.length) {
    var saved = el("details", "saved-files");
    var savedHead = el("summary", "small");
    savedHead.appendChild(document.createTextNode("Saved files (" + names.length + ", relative to "));
    savedHead.appendChild(el("code", "", "connector/runs/" + (view.run_id || "<run id>") + "/"));
    savedHead.appendChild(document.createTextNode(")"));
    saved.appendChild(savedHead);
    var ul = el("ul", "mono artifact-list");
    names.forEach(function (name) {
      ul.appendChild(el("li", "", name + " — " + artifacts[name]));
    });
    saved.appendChild(ul);
    section.appendChild(saved);
  }
  section.appendChild(el("p", "muted small",
    "A Markdown report (dashboard/dashboard_report.md) is also saved in the run " +
    "folder. The dashboard does not serve it, and it may omit observation details."));
  if (meta.raw_input_hash) {
    section.appendChild(el("p", "mono small wrap-any", "Input fingerprint: " + meta.raw_input_hash));
    section.appendChild(el("p", "muted small",
      "Fingerprint of the exact text used. It identifies the input; it does not verify it."));
  }
  section.appendChild(el("p", "banner inline", view.banner || ""));
  body.appendChild(section);
}

function renderContext(view) {
  var body = document.getElementById("context-body");
  clear(body);
  body.className = "";
  var ctx = view.context;
  // Context & posture lives in its own card; the lists sit in a responsive grid.
  var ctxCard = el("div", "ctx-card");
  ctxCard.appendChild(headingWithInfo("h3", "Context & posture", "posture", "posture labels"));
  body.appendChild(ctxCard);
  var outerBody = body;
  body = el("div", "ctx-grid");
  ctxCard.appendChild(body);
  if (!ctx) {
    body.appendChild(el("p", "muted",
      "No signal context was produced for this run."));
  } else {
    var posture = el("div", "ctx-posture");
    posture.appendChild(headingWithInfo("h4", "Policy posture", "posture", "posture labels"));
    var postures = ctx.policy_posture || [];
    if (postures.length) {
      postures.forEach(function (p) { posture.appendChild(postureChip(p)); });
    } else {
      posture.appendChild(el("p", "muted", "none"));
    }
    body.appendChild(posture);

    var risk = ctx.risk_posture;
    if (risk) {
      var riskBlock = el("div", "ctx-list");
      riskBlock.appendChild(el("h4", "", "Risk posture"));
      var cc = confidenceChip(risk.confidence);
      if (cc) {
        riskBlock.appendChild(cc);
        riskBlock.appendChild(el("span", "muted small",
          "(same as overall support above)"));
        riskBlock.appendChild(document.createElement("br"));
      }
      (risk.postures || []).forEach(function (p) {
        riskBlock.appendChild(postureChip(p));
      });
      body.appendChild(riskBlock);
    }

    if ((ctx.caveats || []).length &&
        (view.outcome === "matched" || view.outcome === "no_match")) {
      // Same list the "Limits of this result" block shows; do not print it twice.
      var cvBlock = el("div", "ctx-list");
      cvBlock.appendChild(headingWithInfo("h4", "Caveats (from the evaluator)", "limits",
        "limits and caveats"));
      cvBlock.appendChild(el("p", "muted", ctx.caveats.length +
        (ctx.caveats.length === 1 ? " caveat, shown" : " caveats, shown") +
        " above under “Limits of this result”."));
      body.appendChild(cvBlock);
    } else {
      renderLabeledList(body, "Caveats (from the evaluator)", ctx.caveats,
        "No caveats recorded.", "limits");
    }
    renderLabeledList(body, "Declared missing observations", ctx.missing_evidence,
      "None declared. The evaluator did not flag specific observations as " +
      "missing. Gaps described in the caveats above still apply.");
    renderLabeledList(body, "Constraints on use", ctx.constraints, "None recorded.");
    renderLabeledList(body, "Ways this reading could be wrong", ctx.failure_modes,
      "None recorded.");
    renderLabeledList(body, "Follow-up measurements listed", ctx.required_validations,
      "None listed. (This is not the pipeline's check result — see Pipeline checks.)");
  }
  body = outerBody;
  renderPipelineChecks(body, view);
  if (view.input || (view.observations || []).length) {
    renderObservationPacket(body, view);
  }
  renderSubmittedText(body, view);
  if (view.provenance) {
    renderProvenance(body, view);
  }
}

function checkMark(state) {
  var marks = { pass: ["✓", "check-pass"], fail: ["✗", "check-fail"],
                none: ["—", "check-none"] };
  var m = marks[state] || marks.none;
  var span = el("span", "check-mark " + m[1], m[0]);
  span.setAttribute("aria-hidden", "true");
  return span;
}

function renderPipelineChecks(body, view) {
  var section = el("div", "pipeline-checks");
  section.appendChild(headingWithInfo("h3", "Pipeline checks", "checks", "pipeline checks"));
  section.appendChild(el("p", "muted small", PIPELINE_CHECKS_CAPTION));

  var steps = view.steps || [];
  var validation = view.validation || {};
  var det = el("details", "checks-details");
  var stepsOk = steps.filter(function (st) {
    return String(st.status || "") === "ok" && st.valid !== false;
  }).length;
  var gates = Object.keys(VALIDATOR_LABELS).map(function (k) { return validation[k]; });
  var gatesOk = gates.filter(function (g) { return g && g.valid === true; }).length;
  var allGood = steps.length > 0 && stepsOk === steps.length && gatesOk === gates.length;
  det.open = !allGood;   // collapsed only when everything passed
  det.appendChild(el("summary", "",
    (steps.length ? stepsOk + " of " + steps.length + " steps completed"
                  : "Step record not available") +
    " · " + gatesOk + " of " + gates.length + " validators passed"));
  section.appendChild(det);
  var target = det;

  if (!steps.length) {
    target.appendChild(el("p", "muted", "Step record not available for this run."));
  } else {
    var ul = el("ul", "check-list");
    steps.forEach(function (st) {
      var status = String(st.status || "");
      var state = (status === "ok" && st.valid !== false) ? "pass"
        : (/fail|error/.test(status) || st.valid === false) ? "fail" : "none";
      var li = el("li", "check-row");
      li.appendChild(checkMark(state));
      li.appendChild(el("span", "check-name", STEP_NAMES[st.name] || st.name || "(unnamed step)"));
      li.appendChild(el("span", "check-status muted small", status || "no status"));
      ul.appendChild(li);
    });
    target.appendChild(ul);
  }

  var vul = el("ul", "check-list validators");
  Object.keys(VALIDATOR_LABELS).forEach(function (key) {
    var gate = validation[key];
    var li = el("li", "check-row");
    var state = !gate ? "none" : gate.valid === true ? "pass"
      : gate.valid === false ? "fail" : "none";
    li.appendChild(checkMark(state));
    li.appendChild(el("span", "check-name", VALIDATOR_LABELS[key]));
    var text = !gate ? "not run"
      : gate.valid === true ? "passed"
      : gate.valid === false ? "failed (" + (gate.errors || []).length + " error" +
          ((gate.errors || []).length === 1 ? "" : "s") + ")"
      : "no result";
    li.appendChild(el("span", "check-status muted small", text));
    vul.appendChild(li);
  });
  target.appendChild(el("h4", "", "Validators"));
  target.appendChild(vul);
  body.appendChild(section);
}

function renderSubmittedText(body, view) {
  var input = view.input || {};
  var section = el("div", "submitted");
  section.appendChild(headingWithInfo("h3", "Submitted commentary", "identity", "the submitted commentary"));
  if (input.raw_text) {
    var det = el("details", "input-echo");
    det.appendChild(el("summary", "", "Show the text this run evaluated (" +
      input.raw_text.length + " characters)"));
    det.appendChild(el("pre", "input-pre", input.raw_text));
    section.appendChild(det);
  } else {
    section.appendChild(el("p", "muted", "The submitted text is not available for this run."));
  }
  body.appendChild(section);
}

/* ---------- Outcome screens ---------- */

function renderRunning(runId, backend) {
  stopPolling();
  var body = document.getElementById("result-body");
  clear(body);
  body.className = "";
  var box = el("div", "running-screen");
  var head = el("div", "running-head");
  head.appendChild(el("div", "spinner"));
  head.appendChild(headingWithInfo("h3", "Evaluating…", "running", "what happens during a run"));
  box.appendChild(head);

  var idLine = el("p", "wrap-any");
  idLine.appendChild(document.createTextNode("Run "));
  var link = el("a", "mono", runId);
  link.href = "#run=" + encodeURIComponent(runId);
  idLine.appendChild(link);
  box.appendChild(idLine);

  var roles = _active.roles;
  box.appendChild(el("p", "",
    roles ? "Builder: " + roles.builder + " · Evaluator: " + roles.evaluator
          : "Backend: " + (backend || "not recorded")));

  var elapsed = el("p", "elapsed");
  box.appendChild(elapsed);
  var submittedAt = _active.submittedAt;
  var tick = function () {
    if (!submittedAt) {
      elapsed.textContent = "Elapsed time is not available after a page reload.";
      return;
    }
    var sec = Math.max(0, Math.floor((Date.now() - submittedAt) / 1000));
    elapsed.textContent = "Elapsed since you submitted: " +
      Math.floor(sec / 60) + " min " + (sec % 60 < 10 ? "0" : "") + (sec % 60) + " s";
  };
  tick();
  stopElapsed();
  if (submittedAt) _elapsedTimer = setInterval(tick, 1000);

  box.appendChild(el("h4", "", "What happens during a run"));
  var ol = el("ol", "pipeline-order");
  PIPELINE_ORDER.forEach(function (step) { ol.appendChild(el("li", "", step)); });
  box.appendChild(ol);
  box.appendChild(el("p", "muted small",
    "This list shows the order of steps, not live progress. The dashboard only " +
    "learns when the whole run finishes."));
  box.appendChild(el("p", "",
    "You can leave this page. When the run finishes it appears under Explore → Recent runs. " +
    "The dashboard cannot cancel a run."));
  body.appendChild(box);
  clear(document.getElementById("near-miss-region"));
  var ctxBody = document.getElementById("context-body");
  clear(ctxBody);
  ctxBody.appendChild(el("p", "muted", "Waiting for the run to complete…"));
  startPolling(runId);
}

function renderResult(view) {
  var body = document.getElementById("result-body");
  clear(body);
  body.className = "";
  var interp = view.interpretation || {};
  var states = view.matched_states || [];

  // 1. Outcome headline + qualified support
  var header = el("div", "result-header");
  var matched = view.outcome === "matched";
  header.appendChild(el("div",
    matched ? "badge badge-matched" : "badge badge-nomatch",
    matched ? (states.length > 1 ? "Supported matches found" : "Supported match found")
            : "No supported match found"));
  body.appendChild(header);
  var support = supportLine(interp.confidence, view.outcome);
  if (support) body.appendChild(support);

  // 2. Limits of this result (always visible)
  body.appendChild(renderLimits(view));

  // 3. No-match rationale
  if (view.outcome === "no_match" && view.no_match_rationale) {
    var callout = el("div", "rationale-callout");
    callout.appendChild(el("h4", "", "Why nothing matched"));
    callout.appendChild(el("p", "", view.no_match_rationale));
    body.appendChild(callout);
  }

  // 4. Interpretation (long summaries clamped)
  if (interp.summary || (interp.dominant_themes || []).length) {
    var interpBlock = el("div", "interp-block");
    interpBlock.appendChild(el("h3", "", "Interpretation"));
    if (interp.summary) interpBlock.appendChild(clampedText(interp.summary, "interpretation"));
    if (interp.dominant_themes && interp.dominant_themes.length) {
      var themes = el("div", "themes");
      interp.dominant_themes.forEach(function (t) { themes.appendChild(chip("theme", t)); });
      interpBlock.appendChild(themes);
    }
    body.appendChild(interpBlock);
  }

  // 5. Matched states
  if (states.length) {
    body.appendChild(el("h3", "section-title",
      states.length === 1 ? "Matched state" : "Matched states (" + states.length + ")"));
    states.forEach(function (ms) { body.appendChild(renderMatchedCard(ms)); });
  }

  // 6. Rejected candidates (7. similar states render below, in their own region)
  body.appendChild(renderNonMatches(view.non_matches || [], view.outcome));

  renderNearMiss(view.near_miss_band);
  renderContext(view);
}

/* Paragraph clamped to ~6 lines with a Show all / Show less toggle when it overflows. */
function clampedText(text, className) {
  var wrap = el("div", "clamp-wrap");
  var p = el("p", (className || "") + " clamp", text);
  wrap.appendChild(p);
  var btn = el("button", "link-button", "Show all");
  btn.type = "button";
  btn.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", function () {
    var open = p.classList.toggle("clamp-open");
    btn.textContent = open ? "Show less" : "Show all";
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  wrap.appendChild(btn);
  requestAnimationFrame(function () {
    if (p.scrollHeight > p.clientHeight + 2) btn.hidden = false;
    else p.classList.add("clamp-open");
  });
  return wrap;
}

function countBadge(n, singular, plural) {
  return el("span", "count-badge" + (n ? "" : " count-zero"),
    n + " " + (n === 1 ? singular : plural));
}

function renderLimits(view) {
  var ctx = view.context;
  var box = el("div", "limits-block");
  box.appendChild(headingWithInfo("h3", "Limits of this result", "limits", "limits of this result"));
  if (!ctx) {
    box.appendChild(el("p", "muted",
      "The context record is not available for this run, so its caveats cannot be shown."));
  } else {
    var caveats = ctx.caveats || [];
    if (caveats.length) {
      var ul = el("ul", "limits-caveats");
      caveats.forEach(function (c) { ul.appendChild(el("li", "", c)); });
      box.appendChild(ul);
    } else {
      box.appendChild(el("p", "muted", "No caveats recorded by the evaluator."));
    }
    var counts = el("p", "limits-counts");
    counts.appendChild(countBadge((ctx.constraints || []).length, "constraint on use", "constraints on use"));
    counts.appendChild(countBadge((ctx.missing_evidence || []).length,
      "declared missing observation", "declared missing observations"));
    counts.appendChild(el("span", "muted small", "details under Context & posture below"));
    box.appendChild(counts);
  }
  var fixed = el("p", "limits-fixed");
  fixed.appendChild(document.createTextNode(LIMITS_SENTENCE + " "));
  var a = el("a", "", "What results do not mean");
  a.href = "#guide-not-meaning";
  a.setAttribute("data-guide", "not-meaning");
  fixed.appendChild(a);
  box.appendChild(fixed);
  return box;
}

function renderMatchedCard(ms) {
  var card = el("article", "state-card");
  card.appendChild(headingWithInfo("h3", ms.title || ms.state_id || ms.entry_id || "(unresolved state)",
    "matched", "reading a matched state"));

  var sub = el("div", "card-sub");
  if (ms.state_id) sub.appendChild(el("code", "small wrap-any", ms.state_id));
  if (ms.match_strength) {
    var sg = el("span", "chip-group");
    sg.appendChild(chip("strength-" + ms.match_strength, "Match strength: " + ms.match_strength));
    sg.appendChild(infoBtn("confidence", "match strength"));
    sub.appendChild(sg);
  }
  if (ms.policy_posture) {
    var pg = el("span", "chip-group");
    pg.appendChild(postureChip(ms.policy_posture));
    pg.appendChild(infoBtn("posture", "posture labels"));
    sub.appendChild(pg);
  }
  card.appendChild(sub);

  if (ms.state_summary) {
    if (ms.state_summary.length > 400) {
      var det = el("details");
      det.appendChild(el("summary", "", "State description"));
      det.appendChild(el("p", "", ms.state_summary));
      card.appendChild(det);
    } else {
      card.appendChild(el("p", "", ms.state_summary));
    }
  }

  var obsBlock = el("div", "card-section");
  obsBlock.appendChild(el("h4", "", "Your observations that support this match"));
  var obsList = (ms.matched_observations || []).filter(function (o) { return o.statement; });
  if (obsList.length) {
    var ul = el("ul");
    obsList.forEach(function (o) {
      var li = el("li");
      var arrow = DIRECTION_ARROWS[o.direction] || o.direction || "";
      li.appendChild(chip("dim", (o.dimension || "?") + (arrow ? " " + arrow : "")));
      li.appendChild(document.createTextNode(" " + o.statement));
      ul.appendChild(li);
    });
    obsBlock.appendChild(ul);
  } else {
    obsBlock.appendChild(el("p", "muted", "none recorded"));
  }
  card.appendChild(obsBlock);

  // Caveats come before the citations, always visible.
  if (ms.evidence_caveats) {
    var caveat = el("div", "card-section caveat-note");
    caveat.appendChild(el("h4", "", "Evidence caveats"));
    caveat.appendChild(el("p", "", ms.evidence_caveats));
    card.appendChild(caveat);
  }

  var cits = ms.citations || [];
  var citBlock = el("details", "card-section citations");
  citBlock.open = cits.length <= 2;
  var citSum = el("summary", "",
    "Evidence from the Market Book (" + cits.length + (cits.length === 1 ? " citation)" : " citations)"));
  citSum.appendChild(document.createTextNode(" "));
  citSum.appendChild(infoBtn("citations", "evidence citations"));
  citBlock.appendChild(citSum);
  cits.forEach(function (cit) {
    var c = el("div", "citation");
    c.appendChild(el("div", "citation-kind",
      CITATION_KIND_LABELS[cit.kind] || cit.kind || "Citation"));
    if (cit.text) {
      c.appendChild(el("p", "wrap-any", cit.text));
    } else {
      var missing = el("p", "muted");
      missing.appendChild(el("code", "wrap-any", cit.record_id || ""));
      missing.appendChild(document.createTextNode(" (text unavailable)"));
      c.appendChild(missing);
    }
    citBlock.appendChild(c);
  });
  card.appendChild(citBlock);

  var fm = ms.failure_modes || [];
  if (fm.length) {
    var fmBlock = el("div", "card-section");
    fmBlock.appendChild(el("h4", "", "Known failure modes"));
    var fmUl = el("ul");
    fm.forEach(function (f) { fmUl.appendChild(el("li", "", f)); });
    fmBlock.appendChild(fmUl);
    card.appendChild(fmBlock);
  }

  return card;
}

function renderNonMatches(nonMatches, outcome) {
  var det = el("details", "non-matches");
  if (outcome === "no_match") det.open = true;
  var n = nonMatches.length;
  var sum = el("summary", "", n === 1
    ? "1 candidate the evaluator considered and rejected"
    : n + " candidates the evaluator considered and rejected");
  sum.appendChild(document.createTextNode(" "));
  sum.appendChild(infoBtn("rejections", "rejected candidates"));
  det.appendChild(sum);
  if (!n) {
    det.appendChild(el("p", "muted", "No candidates were rejected."));
    return det;
  }
  var legend = el("p", "reason-legend small");
  legend.appendChild(el("span", "muted", "Reasons: "));
  Object.keys(REASON_LABELS).forEach(function (code) {
    legend.appendChild(chip("reason-" + code, REASON_LABELS[code]));
  });
  det.appendChild(legend);
  nonMatches.forEach(function (nm) {
    var item = el("div", "nm-item");
    var head = el("div", "nm-item-head");
    head.appendChild(el("span", "nm-item-title",
      nm.title || nm.state_id || nm.entry_id || "(unresolved state)"));
    var label = REASON_LABELS[nm.reason_code];
    if (label) {
      head.appendChild(chip("reason-" + nm.reason_code, label));
    } else if (nm.reason_code) {
      head.appendChild(el("code", "small", nm.reason_code));
    }
    item.appendChild(head);
    if (nm.reason) item.appendChild(el("p", "", nm.reason));
    var surfaced = (nm.surfaced_by || [])
      .map(function (s) { return s.text_snippet || s.record_id; })
      .filter(Boolean);
    if (surfaced.length) {
      item.appendChild(el("p", "muted small wrap-any",
        "Why retrieval suggested it: " + surfaced.join("; ")));
    }
    det.appendChild(item);
  });
  return det;
}

function renderFailed(view) {
  var body = document.getElementById("result-body");
  clear(body);
  body.className = "";
  var failure = view.failure || {};
  var step = failure.failed_step || "";
  var isValidation = /^(validate_|export_)/.test(step) || /_validation$/.test(step);

  var screen = el("div", "failed-screen");
  screen.appendChild(el("div", "badge badge-failed",
    isValidation ? "Output failed validation" : "The pipeline did not complete"));
  screen.appendChild(infoBtn("failure", "failed runs"));
  var verrs = failure.validator_errors || [];
  var explanation = FAILURE_EXPLANATIONS[step] ||
    (step ? "The pipeline stopped at this step before producing a checked evaluation."
          : "The pipeline stopped before producing a checked evaluation.");
  screen.appendChild(el("p", "failure-explain", explanation));
  var prohibited = verrs.some(function (e) { return /prohibited phrase/i.test(String(e)); });
  if (prohibited) screen.appendChild(el("p", "failure-explain", PROHIBITED_PHRASE_NOTE));
  screen.appendChild(el("p", "muted small",
    "This is a software or validation outcome, not a judgment about your commentary."));

  var tech = el("details", "tech-details");
  tech.appendChild(el("summary", "", "Technical details"));
  if (step) {
    var stepLine = el("p", "");
    stepLine.appendChild(document.createTextNode("Failed step: " + (STEP_NAMES[step] || step) + " "));
    stepLine.appendChild(el("code", "small", step));
    tech.appendChild(stepLine);
  }
  if (failure.reason) {
    tech.appendChild(el("p", "wrap-any", "Reason: " + failure.reason));
  }
  if (verrs.length) {
    var vb = el("div", "card-section");
    vb.appendChild(el("h4", "", "Validator errors"));
    var ul = el("ul");
    verrs.forEach(function (e2) { ul.appendChild(el("li", "wrap-any", e2)); });
    vb.appendChild(ul);
    tech.appendChild(vb);
  }
  if (failure.stderr_tail) {
    var sb = el("div", "card-section");
    sb.appendChild(el("h4", "", "stderr (tail)"));
    sb.appendChild(el("pre", "stderr-pre", failure.stderr_tail));
    tech.appendChild(sb);
  }
  screen.appendChild(tech);
  body.appendChild(screen);

  // Honest partial artifacts: packet table, near-miss, submitted text.
  renderNearMiss(view.near_miss_band);
  renderContext(view);
}

function renderLegacy(view) {
  var body = document.getElementById("result-body");
  clear(body);
  body.className = "";
  var screen = el("div", "legacy-screen");
  screen.appendChild(el("p", "",
    "This run was saved in an older format that this dashboard cannot display."));
  var meta = view.meta || {};
  screen.appendChild(el("p", "mono small",
    (view.run_id || "") + (meta.backend ? " · backend: " + meta.backend : "")));
  body.appendChild(screen);
  clear(document.getElementById("near-miss-region"));
  var ctxBody = document.getElementById("context-body");
  clear(ctxBody);
  ctxBody.appendChild(el("p", "muted",
    "No signal context can be shown for a run in the older format."));
}

function renderLoadError(runId, message) {
  stopPolling();
  var body = document.getElementById("result-body");
  clear(body);
  body.className = "";
  var screen = el("div", "failed-screen");
  screen.appendChild(el("div", "badge badge-failed", "Could not open run " + runId));
  screen.appendChild(el("p", "wrap-any", message || "Unknown error"));
  var hint = el("p", "muted small");
  hint.appendChild(document.createTextNode(
    "Check the run ID. A run can be opened only if it is saved under "));
  hint.appendChild(el("code", "", "connector/runs/<run id>/"));
  hint.appendChild(document.createTextNode(" and has a run manifest."));
  screen.appendChild(hint);
  body.appendChild(screen);
  clear(document.getElementById("near-miss-region"));
  var ctxBody = document.getElementById("context-body");
  clear(ctxBody);
  ctxBody.appendChild(el("p", "muted", "Nothing to show."));
}

/* ---------- Run loading + polling ---------- */

/* A newly opened run starts at the top of each scrolling pane. */
function resetPaneScroll() {
  ["results-area", "panel-evaluation-result", "panel-signal-context"].forEach(function (id) {
    var node = document.getElementById(id);
    if (node) node.scrollTop = 0;
  });
}

/* Open a run as THE active run. source: curated | recent | id | link | submitted */
function openRun(runId, source) {
  runId = String(runId || "").trim();
  if (!runId) return;
  stopPolling();
  _active = { runId: runId, source: source || "id", view: null };
  resetSelectorsExcept(source);
  setHash(runId);
  renderNowViewing();
  updateGuideContext(null);
  loadView(runId);
}

/* Opening a run resets the OTHER selectors; the commentary editor is left alone. */
function resetSelectorsExcept(source) {
  if (source !== "curated") document.getElementById("curated-select").value = "";
  if (source !== "recent") document.getElementById("recent-select").value = "";
  if (source !== "id") document.getElementById("open-id-input").value = "";
  document.getElementById("open-id-message").textContent = "";
}

function setHash(runId) {
  var want = "#run=" + encodeURIComponent(runId);
  if (window.location.hash !== want) {
    try {
      history.replaceState(null, "", want);
    } catch (e) {
      window.location.hash = want;
    }
  }
}

function hashRunId() {
  var m = /^#run=(.+)$/.exec(window.location.hash || "");
  if (!m) return null;
  try { return decodeURIComponent(m[1]); } catch (e) { return null; }
}

function runLink(runId) {
  var base = window.location.href.split("#")[0];
  return base + "#run=" + encodeURIComponent(runId);
}

function renderNowViewing() {
  var bar = document.getElementById("now-viewing");
  if (!_active.runId) { bar.hidden = true; return; }
  bar.hidden = false;
  var view = _active.view;
  var meta = (view && view.meta) || {};

  var runIdNode = document.getElementById("nv-runid");
  runIdNode.textContent = _active.runId;

  var outcomeNode = document.getElementById("nv-outcome");
  clear(outcomeNode);
  var outcome = view ? view.outcome : "loading";
  // Outcome drives the accent colour of the identity bar and the result panel.
  bar.setAttribute("data-outcome", outcome);
  document.getElementById("panel-evaluation-result").setAttribute("data-outcome", outcome);
  var tag = OUTCOME_TAGS[outcome];
  if (tag) outcomeNode.appendChild(el("span", "outcome-tag " + tag[1], tag[0]));
  else if (outcome === "loading") outcomeNode.appendChild(el("span", "outcome-tag tag-legacy", "Loading…"));
  else outcomeNode.appendChild(el("span", "outcome-tag tag-failed", "Not available"));

  var details = document.getElementById("nv-details");
  clear(details);
  var bits = [SOURCE_LABELS[_active.source] || "Opened"];
  if (meta.created_at_utc) bits.push("created " + formatTime(meta.created_at_utc));
  details.textContent = bits.join(" · ");

  var roles = document.getElementById("nv-roles");
  clear(roles);
  var bp = meta.backend_profiles;
  if (bp && (bp.observation_packet_builder || bp.market_book_evaluator)) {
    roles.textContent = "Builder: " + (bp.observation_packet_builder || "?") +
      " · Evaluator: " + (bp.market_book_evaluator || "?");
  } else if (_active.roles) {
    roles.textContent = "Builder: " + _active.roles.builder +
      " · Evaluator: " + _active.roles.evaluator + " (as submitted)";
  } else if (meta.backend) {
    roles.textContent = "Backend: " + meta.backend + " (per-role detail not recorded)";
  } else if (view && view.outcome !== "unavailable") {
    roles.textContent = "Backend not recorded for this run.";
  }
  document.getElementById("copy-link-status").textContent = "";
}

function copyRunLink() {
  if (!_active.runId) return;
  var link = runLink(_active.runId);
  var status = document.getElementById("copy-link-status");
  var fallback = function () {
    // Clipboard unavailable: show the link so it can be copied by hand.
    clear(status);
    var input = el("input", "link-fallback mono");
    input.type = "text";
    input.readOnly = true;
    input.value = link;
    input.setAttribute("aria-label", "Link to this run");
    status.appendChild(input);
    input.focus();
    input.select();
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(link).then(function () {
      status.textContent = "Link copied.";
    }, fallback);
  } else {
    fallback();
  }
}

function loadView(runId) {
  fetchJSON("/api/runs/" + encodeURIComponent(runId) + "/view")
    .then(function (view) {
      if (runId !== _active.runId) return;  // a newer run was opened meanwhile
      _active.view = view;
      renderNowViewing();
      updateGuideContext(view.outcome);
      resetPaneScroll();
      if (view.outcome === "running") {
        renderRunning(runId, (view.meta || {}).backend);
      } else if (view.outcome === "matched" || view.outcome === "no_match") {
        renderResult(view);
      } else if (view.outcome === "failed") {
        renderFailed(view);
      } else if (view.outcome === "legacy") {
        renderLegacy(view);
      } else {
        renderLoadError(runId, "Unrecognized outcome: " + view.outcome);
      }
    })
    .catch(function (err) {
      if (runId !== _active.runId) return;
      _active.view = { outcome: "unavailable" };
      renderNowViewing();
      var msg = /HTTP 404/.test(err.message)
        ? "No saved run with this ID was found." : err.message;
      renderLoadError(runId, msg);
    });
}

function startPolling(runId) {
  if (_pollTimer) clearInterval(_pollTimer);   // keep the elapsed timer running
  _pollTimer = setInterval(function () {
    fetchJSON("/api/runs/" + encodeURIComponent(runId) + "/status")
      .then(function (st) {
        if (st.status === "ok" || st.status === "failed") {
          stopPolling();
          loadView(runId);
          refreshRecentRuns();
        }
      })
      .catch(function () { /* transient; keep polling */ });
  }, 2000);
}

function stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
  stopElapsed();
}

function stopElapsed() {
  if (_elapsedTimer) {
    clearInterval(_elapsedTimer);
    _elapsedTimer = null;
  }
}

/* ---------- Guide panel ---------- */

var GUIDE_SEEN_KEY = "mbc.guide.seen";
var GUIDE_DOCKED_KEY = "mbc.guide.docked";
var _wideQuery = window.matchMedia ? window.matchMedia("(min-width: 1200px)") : null;
var _guide = { open: false, invoker: null };

function storageGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}
function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* storage blocked: fine */ }
}

function guideIsWide() { return !!(_wideQuery && _wideQuery.matches); }

function applyGuideMode() {
  var aside = document.getElementById("guide");
  var backdrop = document.getElementById("guide-backdrop");
  var toggle = document.getElementById("guide-toggle");
  var wide = guideIsWide();
  document.body.classList.toggle("guide-docked", _guide.open && wide);
  document.body.classList.toggle("guide-overlay-open", _guide.open && !wide);
  aside.hidden = !_guide.open;
  backdrop.hidden = !(_guide.open && !wide);
  toggle.setAttribute("aria-expanded", _guide.open ? "true" : "false");
  if (_guide.open && !wide) {
    aside.setAttribute("role", "dialog");
    aside.setAttribute("aria-modal", "true");
  } else {
    aside.setAttribute("role", "complementary");
    aside.removeAttribute("aria-modal");
  }
}

function scrollGuideTo(topic, focusHeading) {
  var section = document.getElementById("guide-" + topic);
  var scroller = document.getElementById("guide-scroll");
  if (!section) return;
  // "Start here" keeps the Contents list in view; other topics scroll to their heading.
  scroller.scrollTop = topic === "start" ? 0 : Math.max(0, section.offsetTop - 8);
  if (focusHeading) {
    var h = section.querySelector("h3");
    if (h) {
      try { h.focus({ preventScroll: true }); } catch (e) { h.focus(); }
    }
  }
}

function openGuide(topic, invoker, opts) {
  opts = opts || {};
  var wasOpen = _guide.open;
  if (invoker) _guide.invoker = invoker;
  else if (!wasOpen) _guide.invoker = null;
  _guide.open = true;
  applyGuideMode();
  if (guideIsWide()) storageSet(GUIDE_DOCKED_KEY, "1");
  scrollGuideTo(topic || "start", opts.focus !== false);
}

function closeGuide() {
  if (!_guide.open) return;
  _guide.open = false;
  applyGuideMode();
  if (guideIsWide()) storageSet(GUIDE_DOCKED_KEY, "0");
  var back = _guide.invoker;
  _guide.invoker = null;
  if (back && document.body.contains(back)) back.focus();
  else document.getElementById("guide-toggle").focus();
}

/* Follow the active outcome: one context link at the top of the Guide. */
function updateGuideContext(outcome) {
  var p = document.getElementById("guide-context");
  clear(p);
  var map = {
    failed: ["This run failed — what that means", "failure"],
    no_match: ["No supported match — what that means", "outcomes"]
  };
  var entry = map[outcome];
  if (!entry) { p.hidden = true; return; }
  var a = el("a", "", entry[0]);
  a.href = "#guide-" + entry[1];
  a.setAttribute("data-guide", entry[1]);
  p.appendChild(a);
  p.hidden = false;
}

function isTypingTarget(node) {
  if (!node) return false;
  var tag = (node.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || node.isContentEditable;
}

document.addEventListener("click", function (e) {
  var t = e.target.closest ? e.target.closest("[data-guide]") : null;
  if (!t) return;
  e.preventDefault();   // never touch location.hash (it holds the run id)
  e.stopPropagation();  // an ⓘ inside <summary> must not toggle the details
  var insideGuide = !!t.closest("#guide");
  openGuide(t.getAttribute("data-guide"), insideGuide ? null : t);
});

document.getElementById("guide-toggle").addEventListener("click", function () {
  if (_guide.open) closeGuide();
  else openGuide("start", this);
});
document.getElementById("guide-close").addEventListener("click", closeGuide);
document.getElementById("guide-backdrop").addEventListener("click", closeGuide);

document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && _guide.open) {
    e.preventDefault();
    closeGuide();
    return;
  }
  if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey && !isTypingTarget(e.target)) {
    e.preventDefault();
    if (_guide.open) closeGuide();
    else openGuide("start", document.getElementById("guide-toggle"));
    return;
  }
  // Focus trap while the Guide is a modal overlay.
  if (e.key === "Tab" && _guide.open && !guideIsWide()) {
    var aside = document.getElementById("guide");
    var focusables = Array.prototype.filter.call(
      aside.querySelectorAll("a[href], button, summary, [tabindex]:not([tabindex='-1'])"),
      function (n) { return n.offsetParent !== null; });
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    if (!aside.contains(document.activeElement)) {
      e.preventDefault(); first.focus();
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }
});

if (_wideQuery) {
  var onWideChange = function () { if (_guide.open) applyGuideMode(); };
  if (_wideQuery.addEventListener) _wideQuery.addEventListener("change", onWideChange);
  else if (_wideQuery.addListener) _wideQuery.addListener(onWideChange);
}

function initGuide() {
  if (!storageGet(GUIDE_SEEN_KEY)) {
    storageSet(GUIDE_SEEN_KEY, "1");
    openGuide("start", null, { focus: false });   // first visit, once
  } else if (guideIsWide() && storageGet(GUIDE_DOCKED_KEY) === "1") {
    openGuide("start", null, { focus: false });   // remembered docked state
  } else {
    applyGuideMode();
  }
}

/* ---------- Events ---------- */

document.getElementById("curated-select").addEventListener("change", function () {
  if (this.value) openRun(this.value, "curated");
});

document.getElementById("recent-select").addEventListener("change", function () {
  if (this.value) openRun(this.value, "recent");
});

function openFromIdField() {
  var input = document.getElementById("open-id-input");
  var id = input.value.trim();
  var msg = document.getElementById("open-id-message");
  if (!id) {
    msg.textContent = "Type a run ID first.";
    input.focus();
    return;
  }
  openRun(id, "id");
}

document.getElementById("open-id-button").addEventListener("click", openFromIdField);
document.getElementById("open-id-input").addEventListener("keydown", function (e) {
  if (e.key === "Enter") { e.preventDefault(); openFromIdField(); }
});

document.getElementById("copy-link-button").addEventListener("click", copyRunLink);

window.addEventListener("hashchange", function () {
  var id = hashRunId();
  if (id && id !== _active.runId) openRun(id, "link");
});

document.getElementById("example-select").addEventListener("change", function () {
  var examples = this._examples || {};
  var ex = examples[this.value];
  if (ex) document.getElementById("raw-input").value = ex.body || "";
  updateCommentaryState();
});

document.getElementById("raw-input").addEventListener("input", updateCommentaryState);
document.getElementById("builder-backend-select").addEventListener("change", updateModelsBlock);
document.getElementById("evaluator-backend-select").addEventListener("change", updateModelsBlock);
document.getElementById("same-profile").addEventListener("change", updateModelsBlock);

["explore", "evaluate"].forEach(function (m, i, all) {
  var tab = document.getElementById("tab-" + m);
  tab.addEventListener("click", function () { setMode(m, false); });
  tab.addEventListener("keydown", function (e) {
    var next = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = all[(i + 1) % all.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = all[(i + all.length - 1) % all.length];
    else if (e.key === "Home") next = all[0];
    else if (e.key === "End") next = all[all.length - 1];
    if (next) { e.preventDefault(); setMode(next, true); }
  });
});

document.getElementById("run-button").addEventListener("click", function () {
  var text = document.getElementById("raw-input").value.trim();
  var runMsg = document.getElementById("run-message");
  runMsg.textContent = "";
  if (!text) {
    updateCommentaryState();          // inline message under the box; result untouched
    document.getElementById("raw-input").focus();
    return;
  }
  var sel = modelSelection();
  // Always explicit: the displayed choice is what runs (explicit override wins).
  var payload = { backend: sel.backend, raw_input_text: text };
  if (sel.builder !== ROLES_FILE) payload.builder_backend = sel.builder;
  if (sel.evaluator !== ROLES_FILE) payload.evaluator_backend = sel.evaluator;
  var button = this;
  button.disabled = true;

  fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(function (resp) {
    return resp.json().then(function (data) {
      if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
      _active = { runId: data.run_id, source: "submitted",
                  view: { outcome: "running", meta: {} },
                  roles: { builder: roleDisplay(sel.builder), evaluator: roleDisplay(sel.evaluator) },
                  submittedAt: Date.now() };
      resetSelectorsExcept("submitted");
      setHash(data.run_id);
      renderNowViewing();
      updateGuideContext(null);
      renderRunning(data.run_id, sel.backend);
      // Bring the run's identity + running screen into view.
      document.getElementById("now-viewing").scrollIntoView({ block: "start" });
    });
  }).catch(function (err) {
    runMsg.textContent = "The server did not start the run: " + err.message;
  }).then(function () { updateCommentaryState(); });
});

/* ---------- Init ---------- */

populateSelects();
setMode("explore", false);
updateModelsBlock();
updateCommentaryState();
initGuide();
(function openFromHash() {
  var id = hashRunId();
  if (id) openRun(id, "link");
})();
