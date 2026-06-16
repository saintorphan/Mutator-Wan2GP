/* Mutator — hand-rolled, no-build, vanilla-JS SINGLE-track timeline + the
 * Gradio<->browser state bridge. Delivered via WAN2GPPlugin.add_custom_js(),
 * which Wan2GP splices into the single gr.Blocks(js=...) init function that runs
 * once on app load — so we wrap in a guarded IIFE, publish window.MutTimeline,
 * and (re)mount against our own elem_ids once Gradio renders them.
 *
 * THE MODEL: one ordered lane of Segments (starts as one segment = whole
 * source). Click a segment to SELECT it (which loads ITS OWN per-clip edits into
 * the tool row + Result, server-side). Drag a segment's edges to TRIM its
 * in/out. The playhead scrubs the ruler; Splice/Rejoin/Undo/Redo are Gradio
 * buttons in the tool row that read the freshly-flushed playhead/selection.
 *
 * STATE BRIDGE (two hidden gr.Textbox pipes):
 *   outbound (browser -> Python): write the edit-state JSON into #mut_tl_to_py via
 *     the native value setter + a bubbling 'input' event, debounced on pointerup.
 *   inbound  (Python -> browser): Python returns {seq, op, edit}; #mut_tl_from_py's
 *     .change(fn=None, js=APPLY_OP_JS) hook calls applyOp(). A monotonic seq dedupes.
 *
 * Single-track subset of the Reel2Reel timeline: NO multi-track/track-heads, NO
 * audio mixer, NO transitions/markers, NO move/reposition (the lane is one
 * contiguous run), NO razor tool (Splice is a server button at the playhead).
 */
(function () {
  if (window.MutTimeline) return;

  var TL_ROOT = "mut_tl_root", TO_PY = "mut_tl_to_py", FROM_PY = "mut_tl_from_py";

  var S = {
    edit: {
      fps: 30, segments: [], selected_id: null,
      ui: { px_per_sec: 80, playhead: 0, selected: null, snap: true }
    },
    pxPerSec: 80, snap: true, lastSeqIn: -1, mounted: false,
    root: null, lane: null, ruler: null, playhead: null,
    pushTimer: null, interacting: false, pendingLoad: null
  };

  // ---- bridge ---------------------------------------------------------------
  // Native value-setter + a bubbling 'input' event is the only reliable way to
  // push a value into a Gradio textbox from JS so the .change() handler fires.
  function setNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function pushNow() {
    var ta = document.querySelector("#" + TO_PY + " textarea, #" + TO_PY + " input");
    if (!ta) return;
    S.edit.ui = S.edit.ui || {};
    S.edit.ui.px_per_sec = S.pxPerSec;
    S.edit.ui.snap = S.snap;
    S.edit.ui.playhead = ph();
    S.edit.ui.selected = S.edit.selected_id || null;
    // Mirror the canonical ui values onto the top-level keys too (both sides accept either).
    S.edit.playhead = ph();
    try { setNativeValue(ta, JSON.stringify(S.edit)); }
    catch (e) { console.error("[MUT] push", e); }
  }
  function commit() {
    if (S.pushTimer) clearTimeout(S.pushTimer);
    S.pushTimer = setTimeout(pushNow, 120);
  }
  // Synchronously flush any pending debounced state BEFORE firing a server action,
  // so the server reads the latest playhead/selection (e.g. scrub-then-Splice)
  // instead of stale state still sitting in the 120ms debounce window.
  function flushNow() {
    if (S.pushTimer) { clearTimeout(S.pushTimer); S.pushTimer = null; }
    pushNow();
  }

  function applyOp(payload) {
    if (!payload) return;
    var msg;
    try { msg = typeof payload === "string" ? JSON.parse(payload) : payload; }
    catch (e) { console.error("[MUT] bad inbound", e); return; }
    if (typeof msg.seq === "number" && msg.seq <= S.lastSeqIn) return;   // drop stale/replayed
    // Don't clobber an in-flight drag/scrub; queue and apply on pointerup.
    if (S.interacting && msg.op === "load") { S.pendingLoad = msg; return; }
    _apply(msg);
  }
  function _apply(msg) {
    if (msg.op === "load" && msg.edit) {
      // Preserve an uncommitted client-side selection across the wholesale swap
      // (a load arriving inside the commit-debounce window would otherwise drop it).
      var keep = S.edit.selected_id;
      S.edit = msg.edit;
      S.edit.ui = S.edit.ui || {};
      var present = {};
      (msg.edit.segments || []).forEach(function (c) { present[c.id] = 1; });
      var sel = (keep && present[keep])
        ? keep
        : (S.edit.selected_id || (S.edit.ui && S.edit.ui.selected) || null);
      S.edit.selected_id = sel;
      S.edit.ui.selected = sel;
      var ui = msg.edit.ui || {};
      if (ui.px_per_sec) S.pxPerSec = ui.px_per_sec;
      if (typeof ui.snap === "boolean") S.snap = ui.snap;
      if (typeof msg.seq === "number") S.lastSeqIn = msg.seq;   // consume only on a recognized op
      renderAll();
    } else {
      // Unknown op: don't consume seq (so a corrected envelope can resend at the same seq).
      console.warn("[MUT] unknown inbound op", msg && msg.op);
    }
  }
  function endInteract() {
    S.interacting = false;
    if (S.pendingLoad) { var m = S.pendingLoad; S.pendingLoad = null; _apply(m); }
  }

  // ---- geometry -------------------------------------------------------------
  function sec2px(s) { return s * S.pxPerSec; }
  function px2sec(p) { return p / S.pxPerSec; }
  function fps() { return (S.edit && S.edit.fps) || 30; }
  function ph() { return (S.edit.ui && S.edit.ui.playhead) || 0; }
  function segs() { return S.edit.segments || []; }
  function selectedId() { return S.edit.selected_id || (S.edit.ui && S.edit.ui.selected) || null; }
  // Single contiguous track: each segment's start = the running sum of prior durations.
  function totalDur() {
    return segs().reduce(function (m, c) { return m + (c.dur || 0); }, 0);
  }
  function snapVal(s) {
    if (!S.snap) return Math.max(0, s);
    var grid = 0.25, best = Math.round(s / grid) * grid, bestD = Math.abs(best - s);
    var cands = [ph()];                       // snap trim edges to the playhead…
    var run = 0;
    segs().forEach(function (c) { cands.push(run); run += (c.dur || 0); cands.push(run); });
    cands.forEach(function (e) {
      if (Math.abs(e - s) < bestD && Math.abs(e - s) < 0.2) { best = e; bestD = Math.abs(e - s); }
    });
    return Math.max(0, best);
  }
  // CSS url() with a properly-escaped string (so a quote/backslash in a path can't
  // break out of url(...) — defensive even though thumb_url is server-generated).
  function cssUrl(u) { return 'url("' + String(u).replace(/["\\]/g, "\\$&") + '")'; }

  // ---- rendering ------------------------------------------------------------
  function renderAll() {
    if (!S.mounted) return;
    S.lane.innerHTML = "";
    var run = 0, sel = selectedId();
    segs().forEach(function (c) {
      // Re-derive start/dur authoritatively from the source trim + speed each render.
      var sp = (c.speed && c.speed > 0.01) ? c.speed : 1;
      var srcLen = Math.max(0, (c.out || 0) - (c["in"] != null ? c["in"] : (c.in_ || 0)));
      c.src_len = srcLen;
      c.dur = srcLen / sp;
      c.start = run;
      run += c.dur;
      S.lane.appendChild(renderClip(c, sel));
    });
    if (!segs().length) {
      var hint = document.createElement("div");
      hint.className = "mut-empty";
      hint.innerHTML = "No clip loaded — pick a clip from the gallery, drop a file on the "
        + "<b>Source</b> player, or right-click any output → <b>Mutator</b>.";
      S.lane.appendChild(hint);
    }
    var w = Math.max(600, sec2px(totalDur()) + 200);
    if (S.ruler) S.ruler.style.width = w + "px";
    if (S.lane) S.lane.style.width = w + "px";
    drawRuler();
    placePlayhead();
  }
  function renderClip(c, sel) {
    var el = document.createElement("div");
    // mut-clip + mut-timeline-clip + data-media-src are the shared SaintorphanMenu
    // convention: sibling plugins register context items against this surface and
    // read the clip's media from data-media-src.
    el.className = "mut-clip mut-timeline-clip" + (c.id === sel ? " sel" : "")
      + (c.graded ? " graded" : "");
    el.dataset.id = c.id;
    el.setAttribute("data-media-src", c.render_path || c.thumb_url || "");
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    el.setAttribute("aria-label", (c.label || c.id) + " clip");
    el.style.transform = "translateX(" + sec2px(c.start || 0) + "px)";
    el.style.width = Math.max(8, sec2px(c.dur || 0)) + "px";
    if (c.thumb_url) {                                // tiled filmstrip background
      el.style.backgroundImage = cssUrl(c.thumb_url);
      el.style.backgroundSize = "auto 100%";
      el.style.backgroundRepeat = "repeat-x";
    }
    var lbl = document.createElement("span");
    lbl.className = "mut-label";
    lbl.textContent = (c.label || c.id);
    el.appendChild(lbl);
    if (c.graded) {                                  // visible "this clip is graded" badge
      var g = document.createElement("span");
      g.className = "mut-grade-dot"; g.textContent = "◐"; g.title = "Colour-graded clip";
      el.appendChild(g);
    }
    var hl = document.createElement("div"); hl.className = "mut-handle l";
    var hr = document.createElement("div"); hr.className = "mut-handle r";
    el.appendChild(hl); el.appendChild(hr);
    wireClip(el, c, hl, hr);
    return el;
  }
  function drawRuler() {
    if (!S.ruler) return;
    S.ruler.innerHTML = "";
    var dur = Math.ceil(totalDur()) + 4;
    var step = S.pxPerSec < 40 ? 5 : (S.pxPerSec < 90 ? 2 : 1);
    for (var s = 0; s <= dur; s += step) {
      var tick = document.createElement("div");
      tick.className = "mut-tick";
      tick.style.left = sec2px(s) + "px";
      tick.textContent = s + "s";
      S.ruler.appendChild(tick);
    }
  }
  function placePlayhead() {
    if (S.playhead) S.playhead.style.transform = "translateX(" + sec2px(ph()) + "px)";
  }
  // running timeline-start of a segment by id (re-derived from order + dur).
  function startOf(id) {
    var run = 0, found = 0;
    segs().some(function (c) {
      if (c.id === id) { found = run; return true; }
      run += (c.dur || 0); return false;
    });
    return found;
  }

  // ---- selection ------------------------------------------------------------
  function select(id) {
    S.edit.selected_id = id;
    S.edit.ui = S.edit.ui || {};
    S.edit.ui.selected = id;
    highlight();
    // Selecting a clip re-bases the stage's within-clip frame at the current
    // playhead (the plugin pushes the new clip payload separately on selection).
    pushStageSeek();
  }
  // Cheap, no-rebuild .sel toggle (a pure click must NOT renderAll — that would
  // destroy the click target between events).
  function highlight() {
    var sel = selectedId();
    if (!S.lane) return;
    S.lane.querySelectorAll(".mut-clip").forEach(function (n) {
      n.classList.toggle("sel", n.dataset.id === sel);
    });
  }

  // ---- interaction ----------------------------------------------------------
  function wireClip(el, c, hl, hr) {
    var mode = null, x0 = 0, start0 = 0, in0 = 0, out0 = 0, moved = false;
    function down(e, m) {
      e.preventDefault(); e.stopPropagation();
      mode = m;
      x0 = e.clientX;
      start0 = c.start || 0;
      in0 = (c["in"] != null ? c["in"] : (c.in_ || 0));
      out0 = c.out || 0;
      moved = false;
      S.interacting = true;                          // freeze inbound load while dragging
      // A body pointerdown SELECTS the clip (single-select); the handles trim.
      if (m === "move") select(c.id);
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up, { once: true });
    }
    function move(e) {
      // ds is a TIMELINE-seconds delta; the source in/out move by ds*speed, and the
      // on-timeline length is (out-in)/speed. (Per-clip speed makes this conversion
      // mandatory — the #1 trim trap.)
      var ds = px2sec(e.clientX - x0);
      if (Math.abs(e.clientX - x0) > 2) moved = true;
      var sp = (c.speed && c.speed > 0.01) ? c.speed : 1;
      var f = fps();
      if (mode === "l") {
        // Trim the IN-point; keep the RIGHT edge of the clip fixed on the timeline.
        var niRaw = in0 + ds * sp;
        var ni = Math.min(out0 - 1 / f, Math.max(0, niRaw));
        c["in"] = ni;
        c.in_ = ni;
        c.dur = (c.out - ni) / sp;
        c.src_len = c.out - ni;
        c.start = Math.max(0, start0 + (ni - in0) / sp);
        el.style.transform = "translateX(" + sec2px(c.start) + "px)";
        el.style.width = Math.max(8, sec2px(c.dur)) + "px";
      } else if (mode === "r") {
        // Trim the OUT-point; the left edge stays put.
        var no = Math.max((c["in"] != null ? c["in"] : c.in_) + 1 / f, out0 + ds * sp);
        c.out = no;
        var inv = (c["in"] != null ? c["in"] : (c.in_ || 0));
        c.dur = (no - inv) / sp;
        c.src_len = no - inv;
        el.style.width = Math.max(8, sec2px(c.dur)) + "px";
      }
      // mode === "move" is a pure select (no drag-to-reposition on a single track).
    }
    function up() {
      window.removeEventListener("pointermove", move);
      var didEdit = moved && (mode === "l" || mode === "r");
      mode = null;
      // A pure click (no drag) must NOT renderAll — only re-highlight, so the click
      // target survives and the server-side selection swap can drive the tool row.
      if (didEdit) renderAll(); else highlight();
      commit();
      endInteract();
    }
    el.addEventListener("pointerdown", function (e) { down(e, "move"); });
    hl.addEventListener("pointerdown", function (e) { down(e, "l"); });
    hr.addEventListener("pointerdown", function (e) { down(e, "r"); });
  }

  // ---- playhead / ruler -----------------------------------------------------
  // Within-selected-clip second for the stage: the stage previews ONLY the
  // selected clip, so seekToTimeline expects seconds measured from the clip's
  // own start (subtract the selected clip's timeline start from the global
  // playhead). Clamped at 0.
  function withinSelectedSec() {
    var s = ph() - startOf(selectedId());
    return s > 0 ? s : 0;
  }
  // Push the playhead to the stage (scrub path: timeline -> video). Guarded so
  // the timeline works standalone if the stage module isn't present.
  function pushStageSeek() {
    if (window.MutStage && window.MutStage.seekToTimeline) {
      window.MutStage.seekToTimeline(withinSelectedSec());
    }
  }
  function setPlayhead(s) {
    S.edit.ui = S.edit.ui || {};
    S.edit.ui.playhead = Math.max(0, s);
    S.edit.playhead = S.edit.ui.playhead;
    placePlayhead();
    pushStageSeek();                          // scrub drives the video
  }
  // Playback-driven update FROM the stage (video -> playhead). Move the playhead
  // ONLY: no stage seek (would bounce back into a seek), no per-frame commit.
  // `sec` is a within-selected-clip second; add the selected clip's start to get
  // the global playhead.
  function setExternalPlayhead(sec) {
    S.edit.ui = S.edit.ui || {};
    var g = startOf(selectedId()) + Math.max(0, sec || 0);
    S.edit.ui.playhead = g;
    S.edit.playhead = g;
    placePlayhead();
    updateReadout();
  }
  // Compact playhead-time readout (kept tolerant: no dedicated element required).
  function updateReadout() {
    var v = S.root && S.root.querySelector(".mut-phval");
    if (v) v.textContent = (ph()).toFixed(2) + "s";
  }
  function wireRuler() {
    if (!S.ruler) return;
    function scrub(e) {
      var rect = S.ruler.getBoundingClientRect();
      setPlayhead(Math.max(0, px2sec(e.clientX - rect.left)));   // no per-move commit
    }
    S.ruler.addEventListener("pointerdown", function (e) {
      S.interacting = true; scrub(e);
      window.addEventListener("pointermove", scrub);
      window.addEventListener("pointerup", function () {
        window.removeEventListener("pointermove", scrub);
        commit();                                    // commit once, on release
        endInteract();
      }, { once: true });
    });
  }

  // ---- zoom (compact) -------------------------------------------------------
  function setZoom(px) {
    S.pxPerSec = Math.max(8, Math.min(400, px));
    updateZoomVal();
    renderAll();
    commit();
  }
  function fit() {
    var sc = S.root && S.root.querySelector(".mut-scroll");
    var avail = (sc ? sc.clientWidth : 900) - 60;
    var dur = totalDur() || 10;
    setZoom(avail / dur);
  }
  function updateZoomVal() {
    var v = S.root && S.root.querySelector(".mut-zoomval");
    if (v) v.textContent = Math.round(S.pxPerSec) + " px/s";
    var z = S.root && S.root.querySelector(".mut-zoom");
    if (z && document.activeElement !== z) z.value = Math.round(S.pxPerSec);
  }

  // Fire a hidden/visible Gradio button by elem_id (flush state first so the Python
  // handler reads the freshly-flushed playhead/selected_id).
  function clickGr(id) {
    flushNow();
    var b = document.querySelector("#" + id + " button") || document.querySelector("#" + id);
    if (b) b.click();
  }

  // ---- mount ----------------------------------------------------------------
  function buildSkeleton(root) {
    root.innerHTML =
      '<div class="mut-tl">' +
      '  <div class="mut-tlbar">' +
      '    <button class="mut-btn" data-act="zout" title="Zoom out">−</button>' +
      '    <input type="range" class="mut-zoom" min="8" max="400" value="80" title="Zoom">' +
      '    <span class="mut-zoomval">80 px/s</span>' +
      '    <button class="mut-btn" data-act="zin" title="Zoom in">+</button>' +
      '    <button class="mut-btn" data-act="fit" title="Zoom to fit">Fit</button>' +
      '    <button class="mut-btn" data-act="snap" title="Toggle snapping">Snap</button>' +
      '  </div>' +
      '  <div class="mut-scroll">' +
      '    <div class="mut-ruler"></div>' +
      '    <div class="mut-lane"></div>' +
      '    <div class="mut-playhead"></div>' +
      '  </div>' +
      '</div>';
    var wrap = root.querySelector(".mut-tl");
    S.root = wrap;
    S.lane = wrap.querySelector(".mut-lane");
    S.ruler = wrap.querySelector(".mut-ruler");
    S.playhead = wrap.querySelector(".mut-playhead");
    wireRuler();
    var bar = wrap.querySelector(".mut-tlbar");
    if (bar) bar.addEventListener("click", function (e) {
      var el = e.target.closest("[data-act]");
      if (!el) return;
      e.preventDefault();
      switch (el.dataset.act) {
        case "zin": setZoom(S.pxPerSec * 1.3); break;
        case "zout": setZoom(S.pxPerSec / 1.3); break;
        case "fit": fit(); break;
        case "snap":
          S.snap = !S.snap; el.classList.toggle("active", S.snap); commit(); break;
      }
    });
    var zr = wrap.querySelector(".mut-zoom");
    if (zr) zr.addEventListener("input", function (e) {
      setZoom(parseInt(e.target.value, 10) || 80);
    });
    syncSnapBox();
    S.mounted = true;
    renderAll();
  }
  function syncSnapBox() {
    var b = S.root && S.root.querySelector('[data-act="snap"]');
    if (b) b.classList.toggle("active", S.snap);
  }
  function tryMount() {
    var root = document.getElementById(TL_ROOT);
    if (root && (!root.querySelector(".mut-tl") || !S.mounted)) buildSkeleton(root);
  }
  function boot() {
    tryMount();
    var tries = 0;
    (function poll() {
      if (S.mounted || tries++ > 80) return;
      requestAnimationFrame(function () { tryMount(); setTimeout(poll, 100); });
    })();
    try {
      new MutationObserver(function () {
        var root = document.getElementById(TL_ROOT);
        // Gradio re-renders the tab → our mount div is replaced; re-mount onto it.
        if (root && !root.querySelector(".mut-tl")) { S.mounted = false; tryMount(); }
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
  }

  // Public surface kept minimal — internal state (S) is intentionally NOT exposed.
  // setExternalPlayhead is the stage's playback-driven playhead update (no seek).
  window.MutTimeline = {
    applyOp: applyOp, remount: tryMount, clickGr: clickGr,
    setExternalPlayhead: setExternalPlayhead
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
