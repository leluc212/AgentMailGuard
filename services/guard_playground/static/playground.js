// Guard Playground client helpers. Tour keys and presenter mode are added by later tasks.
// Evidence links, back links and stepper stages jump to a target inside a collapsed
// <details>: open every enclosing <details>, then move focus to the target.
"use strict";

(function () {
  function reveal(target) {
    for (let el = target; el; el = el.parentElement) {
      if (el.tagName === "DETAILS") el.open = true;
    }
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: false });
  }

  document.addEventListener("click", function (event) {
    const link = event.target.closest("a.ev-ref, a.back-ref, a.stage-link");
    if (!link) return;
    const hash = link.getAttribute("href") || "";
    if (hash.charAt(0) !== "#") return;
    const target = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (!target) return;
    event.preventDefault();
    reveal(target);
    if (history.replaceState) history.replaceState(null, "", hash);
  });
})();

// Custom_Email (D12): which input the Run button uses, chunk fields add/remove (max 5).
(function () {
  const MAX_CHUNKS = 5;

  function setSource(value) {
    const field = document.getElementById("input-source");
    if (field && field.value !== value) {
      field.value = value;
      field.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const label = document.getElementById("input-source-label");
    if (label) label.textContent = value === "custom" ? "Input: your own email" : "Input: selected case";
  }

  function renumber() {
    const fields = document.querySelectorAll("#chunk-fields .chunk-field");
    fields.forEach(function (f, i) {
      const n = i + 1;
      const area = f.querySelector("textarea");
      const label = f.querySelector("label");
      const button = f.querySelector("[data-remove-chunk]");
      area.id = "chunk-" + n;
      label.setAttribute("for", area.id);
      label.textContent = "KB chunk " + n;
      button.textContent = "Remove chunk " + n;
    });
    const add = document.getElementById("add-chunk");
    if (add) add.disabled = fields.length >= MAX_CHUNKS;
    const summary = document.querySelector("#custom-email .advanced summary");
    if (summary) summary.textContent = "Advanced: knowledge base chunks (" + fields.length + "/5)";
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-add-chunk]")) {
      const box = document.getElementById("chunk-fields");
      if (!box || box.children.length >= MAX_CHUNKS) return;
      const div = document.createElement("div");
      div.className = "chunk-field";
      div.innerHTML = '<label></label><textarea name="custom_chunk" form="run-form" rows="3"></textarea>' +
        '<button type="button" class="link" data-remove-chunk></button>';
      box.appendChild(div);
      renumber();
      div.querySelector("textarea").focus();
      setSource("custom");
    } else if (event.target.closest("[data-remove-chunk]")) {
      event.target.closest(".chunk-field").remove();
      renumber();
      const add = document.getElementById("add-chunk");
      if (add) add.focus();
    } else if (event.target.closest(".case-item")) {
      setSource("case");
    }
  });

  document.addEventListener("input", function (event) {
    if (event.target.closest("#custom-email")) setSource("custom");
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail && event.detail.target && event.detail.target.id === "custom-email") {
      setSource("custom");
    }
  });
  // An out-of-band swap replaces #custom-email itself; htmx reports it on the new element.
  document.addEventListener("htmx:oobAfterSwap", function (event) {
    if (event.detail && event.detail.target && event.detail.target.id === "custom-email") {
      setSource("custom");
    }
  });
})();

// Presenter_Mode (Requirement 19) and Demo_Tour (Requirement 17).
(function () {
  const $ = (id) => document.getElementById(id);

  // Presenter mode: off on every page load (19.1); bigger text, collapse guarded prompts (19.5).
  document.addEventListener("click", function (event) {
    const toggle = event.target.closest("#presenter-toggle");
    if (!toggle) return;
    const on = !document.documentElement.classList.contains("presenter");
    document.documentElement.classList.toggle("presenter", on);
    toggle.setAttribute("aria-pressed", String(on));
    toggle.textContent = "Presenter mode: " + (on ? "on" : "off");
    if (on) document.querySelectorAll("details.prompt").forEach((d) => { d.open = false; });
  });
  document.addEventListener("htmx:afterSwap", function () {
    if (document.documentElement.classList.contains("presenter")) {
      document.querySelectorAll("details.prompt").forEach((d) => { d.open = false; });
    }
  });

  const dataEl = $("tour-data");
  if (!dataEl) return;
  const steps = JSON.parse(dataEl.textContent);
  let index = -1;
  let generation = 0; // a result of an older step is dropped (17.10)

  function setSelect(name, value) {
    const el = document.querySelector('#run-form [name="' + name + '"]');
    if (el) el.value = value;
  }

  function setMode(mode) {
    const radio = document.querySelector('#run-form input[name="mode"][value="' + mode + '"]');
    if (radio && !radio.disabled) radio.checked = true;
    return radio && !radio.disabled && radio.checked;
  }

  function show(i) {
    index = Math.max(0, Math.min(steps.length - 1, i));
    generation += 1;
    const step = steps[index];
    $("tour-panel").hidden = false;
    $("tour-count").textContent = "Step " + (index + 1) + " of " + steps.length;
    $("tour-point").textContent = step.talking_point;
    $("tour-prev").disabled = index === 0;
    $("tour-next").disabled = index === steps.length - 1;
    const compare = step.presets.length === 2;
    const cmp = document.querySelector('#run-form input[name="compare"][value="' + (compare ? "1" : "0") + '"]');
    if (cmp) cmp.checked = true;
    if (compare) { setSelect("left_preset", step.presets[0]); setSelect("right_preset", step.presets[1]); }
    else setSelect("single_preset", step.presets[0]);
    const modeOk = setMode(step.mode);
    $("results").innerHTML = '<p class="note">No run yet. Press Run.</p>';
    const blocked = $("tour-blocked");
    blocked.hidden = true;
    if (!modeOk) {
      blocked.hidden = false;
      blocked.textContent = "Run unavailable for this step: it needs Live mode, which is off.";
    }
    if (step.case_id) {
      const source = $("input-source");
      if (source) source.value = "case";
      const label = $("input-source-label");
      if (label) label.textContent = "Input: selected case";
      htmx.ajax("GET", "/guard/cases/" + encodeURIComponent(step.case_id), { target: "#case-detail" });
    } else {
      $("custom-subject").value = step.subject;
      $("custom-body").value = step.body;
      $("custom-body").dispatchEvent(new Event("input", { bubbles: true }));
    }
    document.querySelector("#run-form").dispatchEvent(new Event("change", { bubbles: true }));
  }

  function exitTour() {
    index = -1;
    $("tour-panel").hidden = true;
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("#tour-start")) show(0);
    else if (event.target.closest("#tour-next")) show(index + 1);
    else if (event.target.closest("#tour-prev")) show(index - 1);
    else if (event.target.closest("#tour-exit")) exitTour();
  });

  document.addEventListener("keydown", function (event) {
    if (index < 0 || event.ctrlKey || event.altKey || event.metaKey) return;
    const t = event.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
    const key = event.key.toLowerCase();
    if (key === "n" && index < steps.length - 1) show(index + 1);
    else if (key === "p" && index > 0) show(index - 1);
  });

  // Drop a result that arrives after the presenter moved to another step (17.10).
  document.addEventListener("htmx:beforeRequest", function (event) {
    event.detail.elt.dataset.tourGeneration = String(generation);
  });
  document.addEventListener("htmx:beforeSwap", function (event) {
    const g = event.detail.requestConfig && event.detail.requestConfig.elt
      ? event.detail.requestConfig.elt.dataset.tourGeneration : undefined;
    const target = event.detail.target;
    if (index >= 0 && g !== undefined && Number(g) !== generation && target &&
        (target.id === "results" || target.closest && target.closest("#results"))) {
      event.detail.shouldSwap = false;
    }
  });
})();

// Drop zone highlight (the native file input already accepts drops and the keyboard).
(function () {
  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      const zone = event.target.closest && event.target.closest(".dropzone__label");
      if (zone) zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      const zone = event.target.closest && event.target.closest(".dropzone__label");
      if (zone) zone.classList.remove("dragover");
    });
  });
})();
