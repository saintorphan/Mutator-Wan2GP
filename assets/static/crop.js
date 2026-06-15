/* Mutator v0.2 — draggable crop canvas (single-track per-clip editor).
 *
 * Adapted from ImageSuite's modify_canvas, stripped to CROP ONLY: an 8-handle
 * rectangle with aspect-ratio presets, drawn over a source frame. Unlike
 * ImageSuite this canvas does NOT bake a PNG — it emits the crop rectangle as
 * integer coordinates in SOURCE pixels for ffmpeg's crop=w:h:x:y to apply.
 *
 * Delivery: this file is the inline <script> body of a self-contained
 *   <iframe srcdoc> assembled by ui/crop.py. It runs INSIDE the iframe, so
 *   `document` is the iframe document and `parent.*` is the Gradio page.
 *
 * Bridge contract (binding — must match the spec / plugin.py / ui/crop.py):
 *   JS -> Py : on crop change (debounced 120ms) write to the hidden Gradio
 *              Textbox #mut_crop_to_py via setHidden():
 *                  {"seg_id":"s1","x":120,"y":64,"w":1280,"h":720}
 *              x/y/w/h are integers in SOURCE pixels.
 *   Py -> JS : the plugin injects a one-shot hidden <iframe srcdoc> whose
 *              <script> calls the parked handles below:
 *                  parent.window.__mut_crop_setframe({frame, seg_id, src_w, src_h})
 *                  parent.window.__mut_crop_exportnow()   // synchronous flush
 *
 * Accent colour: #00d9ff (Mutator cyan), NOT the ImageSuite pink.
 */
(function () {
  "use strict";

  /* ----------------------------------------------------------------------- *
   * Constants + state                                                       *
   * ----------------------------------------------------------------------- */
  var TO_PY = "mut_crop_to_py";            // hidden Textbox elem_id (JS -> Py)
  var ACCENT = "#00d9ff";                  // Mutator cyan
  var ASPMAP = { "free": 0, "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16 };
  var MIN = 8;                             // minimum crop size (source px)

  var W = 0, H = 0;                        // source bitmap dimensions (px)
  var hasBg = false;                       // a frame is loaded
  var baseImg = null;                      // the source frame <img>
  var SEG_ID = "";                         // which segment this crop belongs to
  var baseScale = 1, viewScale = 1;        // CSS-only zoom (bitmap stays native)

  // crop rectangle in SOURCE (bitmap) coordinates — exactly what ffmpeg wants.
  var crop = { x: 0, y: 0, w: 0, h: 0 };
  var aspect = 0;                          // 0 = free, else target w/h ratio
  var dragging = null, dragStart = null;   // active handle / drag baseline

  // DOM refs (resolved or built in boot()).
  var wrap = null, stage = null, bg = null, disp = null, empty = null, aspbar = null;
  var bgx = null, dx = null;

  /* ----------------------------------------------------------------------- *
   * DOM skeleton — build it if ui/crop.py didn't provide one (self-contained)*
   * ----------------------------------------------------------------------- */
  function el(tag, attrs, css) {
    var n = document.createElement(tag);
    if (attrs) { for (var k in attrs) { if (attrs.hasOwnProperty(k)) n.setAttribute(k, attrs[k]); } }
    if (css) { n.style.cssText = css; }
    return n;
  }

  function injectStyle() {
    if (document.getElementById("mut-crop-style")) { return; }
    var s = el("style", { id: "mut-crop-style" });
    s.textContent = [
      "*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,sans-serif}",
      "html,body{height:100%}",
      "body{background:#15151b;color:#ddd;overflow:hidden;user-select:none;-webkit-user-select:none}",
      "#mut-crop-root{display:flex;flex-direction:column;height:100%;width:100%}",
      "#mut-crop-bar{flex:0 0 auto;display:flex;align-items:center;gap:6px;",
      "  padding:7px 9px;background:#1d1d25;border-bottom:1px solid #333;flex-wrap:wrap}",
      "#mut-crop-bar .lbl{font-size:10px;letter-spacing:.08em;color:#8a8a9a;",
      "  font-weight:700;text-transform:uppercase;margin-right:2px}",
      "#mut-crop-bar button{background:#2a2a35;border:1px solid #3a3a48;color:#cfcfe0;",
      "  border-radius:6px;padding:5px 9px;font-size:11px;cursor:pointer;line-height:1.1}",
      "#mut-crop-bar button.on{background:" + ACCENT + ";border-color:" + ACCENT + ";color:#06121a;font-weight:700}",
      "#mut-crop-bar .dims{margin-left:auto;font-size:11px;color:#9aa;font-variant-numeric:tabular-nums}",
      "#mut-crop-wrap{flex:1;position:relative;overflow:auto;min-height:0;",
      "  background:#101015 repeating-conic-gradient(#1a1a22 0% 25%,#141419 0% 50%) 0/24px 24px}",
      "#mut-crop-stage{position:relative;margin:10px auto;",
      "  box-shadow:0 0 0 1px #000,0 6px 24px rgba(0,0,0,.5)}",
      "#mut-crop-stage canvas{position:absolute;top:0;left:0;display:block}",
      "#mut-crop-disp{position:relative;cursor:crosshair;touch-action:none}",
      "#mut-crop-empty{position:absolute;inset:0;display:flex;align-items:center;",
      "  justify-content:center;color:#555;font-size:13px;text-align:center;",
      "  pointer-events:none;line-height:1.6}"
    ].join("\n");
    document.head.appendChild(s);
  }

  function buildSkeleton() {
    injectStyle();
    var root = document.getElementById("mut-crop-root");
    if (!root) {
      root = el("div", { id: "mut-crop-root" });
      document.body.appendChild(root);
    }

    aspbar = document.getElementById("mut-crop-bar");
    if (!aspbar) {
      aspbar = el("div", { id: "mut-crop-bar" });
      var lab = el("span", null);
      lab.className = "lbl";
      lab.textContent = "Aspect";
      aspbar.appendChild(lab);
      var presets = ["free", "1:1", "4:3", "3:4", "16:9", "9:16"];
      for (var i = 0; i < presets.length; i++) {
        var b = el("button", { "data-asp": presets[i], "type": "button" });
        b.textContent = presets[i] === "free" ? "Free" : presets[i];
        if (presets[i] === "free") { b.className = "on"; }
        aspbar.appendChild(b);
      }
      var reset = el("button", { "id": "mut-crop-reset", "type": "button", "title": "Select the whole frame" });
      reset.textContent = "Reset";
      aspbar.appendChild(reset);
      var dims = el("span", { "id": "mut-crop-dims" });
      dims.className = "dims";
      aspbar.appendChild(dims);
      root.appendChild(aspbar);
    }

    wrap = document.getElementById("mut-crop-wrap");
    if (!wrap) {
      wrap = el("div", { id: "mut-crop-wrap" });
      stage = el("div", { id: "mut-crop-stage" });
      bg = el("canvas", { id: "mut-crop-bg" });
      disp = el("canvas", { id: "mut-crop-disp" });
      empty = el("div", { id: "mut-crop-empty" });
      empty.innerHTML = "No frame yet — open <b>Crop</b> on the selected clip.";
      stage.appendChild(bg);
      stage.appendChild(disp);
      stage.appendChild(empty);
      wrap.appendChild(stage);
      root.appendChild(wrap);
    } else {
      stage = document.getElementById("mut-crop-stage");
      bg = document.getElementById("mut-crop-bg");
      disp = document.getElementById("mut-crop-disp");
      empty = document.getElementById("mut-crop-empty");
    }

    bgx = bg.getContext("2d");
    dx = disp.getContext("2d");
  }

  /* ----------------------------------------------------------------------- *
   * Sizing / view (CSS zoom only; the bitmap stays at native source res)    *
   * ----------------------------------------------------------------------- */
  function setSize(w, h) {
    W = w; H = h;
    bg.width = w; bg.height = h;
    disp.width = w; disp.height = h;
    fitView();
  }

  function fitView() {
    var availW = (stage.parentNode.clientWidth || 600) - 20;
    var availH = (stage.parentNode.clientHeight || 480) - 20;
    var sw = W > 0 ? availW / W : 1;
    var sh = H > 0 ? availH / H : 1;
    baseScale = Math.min(1, sw, sh);
    if (!(baseScale > 0)) { baseScale = 1; }
    viewScale = 1;
    applyView();
  }

  function applyView() {
    var s = baseScale * viewScale;
    stage.style.width = (W * s) + "px";
    stage.style.height = (H * s) + "px";
    bg.style.width = "100%"; bg.style.height = "100%";
    disp.style.width = "100%"; disp.style.height = "100%";
  }

  /* ----------------------------------------------------------------------- *
   * Source-pixel mapping (the load-bearing function)                        *
   * Pointer -> bitmap/source coords: divide pointer offset by the CSS rect,  *
   * multiply by W/H, clamp to [0,W]/[0,H]. NO transform — bitmap == source.  *
   * ----------------------------------------------------------------------- */
  function pos(e) {
    var r = disp.getBoundingClientRect();
    var t = (e.touches && e.touches[0]) ? e.touches[0] : e;
    var x = r.width > 0 ? (t.clientX - r.left) / r.width * W : 0;
    var y = r.height > 0 ? (t.clientY - r.top) / r.height * H : 0;
    return { x: Math.max(0, Math.min(W, x)), y: Math.max(0, Math.min(H, y)) };
  }

  /* ----------------------------------------------------------------------- *
   * Handles + hit-testing                                                   *
   * ----------------------------------------------------------------------- */
  function handlePts() {
    var x0 = crop.x, y0 = crop.y, x1 = crop.x + crop.w, y1 = crop.y + crop.h;
    var mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
    return [
      { x: x0, y: y0, h: "nw" }, { x: mx, y: y0, h: "n" }, { x: x1, y: y0, h: "ne" },
      { x: x1, y: my, h: "e" }, { x: x1, y: y1, h: "se" }, { x: mx, y: y1, h: "s" },
      { x: x0, y: y1, h: "sw" }, { x: x0, y: my, h: "w" }
    ];
  }

  function hitHandle(p) {
    var ds = (baseScale * viewScale) || 1;
    var r = 11 / ds;
    var pts = handlePts();
    for (var i = 0; i < pts.length; i++) {
      if (Math.abs(p.x - pts[i].x) < r && Math.abs(p.y - pts[i].y) < r) { return pts[i].h; }
    }
    if (p.x >= crop.x && p.x <= crop.x + crop.w && p.y >= crop.y && p.y <= crop.y + crop.h) { return "move"; }
    return null;
  }

  /* ----------------------------------------------------------------------- *
   * Aspect constraint + clamp                                               *
   * ----------------------------------------------------------------------- */
  function clampCrop() {
    if (crop.w < MIN) { crop.w = MIN; }
    if (crop.h < MIN) { crop.h = MIN; }
    if (crop.w > W) { crop.w = W; }
    if (crop.h > H) { crop.h = H; }
    if (crop.x < 0) { crop.x = 0; }
    if (crop.y < 0) { crop.y = 0; }
    if (crop.x + crop.w > W) { crop.x = W - crop.w; }
    if (crop.y + crop.h > H) { crop.y = H - crop.h; }
  }

  // Re-fit the rectangle to the locked ratio. `h` is the handle being dragged:
  // horizontal edges derive height from width, vertical edges derive width from
  // height, corners lock width and derive height (toward the drag direction).
  function applyAspectResize(h) {
    if (!aspect) { return; }
    var cx = crop.x + crop.w / 2, cy = crop.y + crop.h / 2;
    if (h === "e" || h === "w") {
      var nh = crop.w / aspect; crop.y = cy - nh / 2; crop.h = nh;
    } else if (h === "n" || h === "s") {
      var nw = crop.h * aspect; crop.x = cx - nw / 2; crop.w = nw;
    } else {
      var nh2 = crop.w / aspect;
      if (h === "nw" || h === "ne") { crop.y = (crop.y + crop.h) - nh2; }
      crop.h = nh2;
    }
    clampCrop();
  }

  function setAspect(a) {
    aspect = a || 0;
    if (aspect && hasBg) {
      var cx = crop.x + crop.w / 2, cy = crop.y + crop.h / 2;
      var nw = crop.w, nh = nw / aspect;
      if (nh > H) { nh = H; nw = nh * aspect; }
      if (nw > W) { nw = W; nh = nw / aspect; }
      crop.w = nw; crop.h = nh; crop.x = cx - nw / 2; crop.y = cy - nh / 2;
      clampCrop();
      render();
      pushExport();
    }
  }

  /* ----------------------------------------------------------------------- *
   * Render: source frame on #bg, crop overlay (mask + rect + thirds + grips) *
   * ----------------------------------------------------------------------- */
  function paintBg() {
    bgx.clearRect(0, 0, W, H);
    if (baseImg) { bgx.drawImage(baseImg, 0, 0, W, H); }
  }

  function drawCropOverlay() {
    dx.clearRect(0, 0, W, H);
    if (!hasBg) { return; }
    // dim everything outside the crop rect
    dx.save();
    dx.fillStyle = "rgba(0,0,0,.5)";
    dx.fillRect(0, 0, W, crop.y);
    dx.fillRect(0, crop.y + crop.h, W, H - (crop.y + crop.h));
    dx.fillRect(0, crop.y, crop.x, crop.h);
    dx.fillRect(crop.x + crop.w, crop.y, W - (crop.x + crop.w), crop.h);
    dx.restore();

    var ds = (baseScale * viewScale) || 1;
    var lw = Math.max(1, 1.5 / ds);
    var hs = Math.max(3, 6 / ds);

    dx.save();
    // rect border (accent cyan)
    dx.strokeStyle = ACCENT;
    dx.lineWidth = lw;
    dx.strokeRect(crop.x, crop.y, crop.w, crop.h);
    // rule-of-thirds guides
    dx.strokeStyle = "rgba(255,255,255,.35)";
    dx.lineWidth = Math.max(0.5, 0.75 / ds);
    for (var i = 1; i < 3; i++) {
      dx.beginPath();
      dx.moveTo(crop.x + crop.w * i / 3, crop.y);
      dx.lineTo(crop.x + crop.w * i / 3, crop.y + crop.h);
      dx.stroke();
      dx.beginPath();
      dx.moveTo(crop.x, crop.y + crop.h * i / 3);
      dx.lineTo(crop.x + crop.w, crop.y + crop.h * i / 3);
      dx.stroke();
    }
    // 8 handles
    dx.fillStyle = ACCENT;
    handlePts().forEach(function (p) {
      dx.beginPath();
      dx.rect(p.x - hs, p.y - hs, hs * 2, hs * 2);
      dx.fill();
    });
    dx.restore();
  }

  function syncDims() {
    var d = document.getElementById("mut-crop-dims");
    if (!d) { return; }
    if (!hasBg) { d.textContent = ""; return; }
    d.textContent = Math.round(crop.w) + " × " + Math.round(crop.h)
      + " @ " + Math.round(crop.x) + "," + Math.round(crop.y);
  }

  function render() {
    paintBg();
    drawCropOverlay();
    syncDims();
  }

  /* ----------------------------------------------------------------------- *
   * Drag interaction (window-bound move/up so drags can leave the canvas)   *
   * ----------------------------------------------------------------------- */
  function down(e) {
    if (!hasBg) { return; }
    if (e.button !== undefined && e.button !== 0) { return; }
    var p = pos(e);
    var h = hitHandle(p);
    if (!h) {
      // outside the rect: start a fresh crop from this point
      h = "se";
      crop.x = p.x; crop.y = p.y; crop.w = 1; crop.h = 1;
    }
    e.preventDefault();
    dragging = h;
    dragStart = { p: p, crop: { x: crop.x, y: crop.y, w: crop.w, h: crop.h } };
  }

  function move(e) {
    if (!dragging) { return; }
    e.preventDefault();
    var p = pos(e);
    var s = dragStart.crop;
    var ddx = p.x - dragStart.p.x, ddy = p.y - dragStart.p.y;
    if (dragging === "move") {
      crop.x = s.x + ddx; crop.y = s.y + ddy;
      clampCrop();
    } else {
      var x0 = s.x, y0 = s.y, x1 = s.x + s.w, y1 = s.y + s.h;
      if (dragging.indexOf("w") >= 0) { x0 = Math.min(p.x, x1 - MIN); }
      if (dragging.indexOf("e") >= 0) { x1 = Math.max(p.x, x0 + MIN); }
      if (dragging.indexOf("n") >= 0) { y0 = Math.min(p.y, y1 - MIN); }
      if (dragging.indexOf("s") >= 0) { y1 = Math.max(p.y, y0 + MIN); }
      crop.x = x0; crop.y = y0; crop.w = x1 - x0; crop.h = y1 - y0;
      if (aspect) { applyAspectResize(dragging); }
      clampCrop();
    }
    render();
  }

  function up() {
    if (!dragging) { return; }
    dragging = null;
    pushExport();
  }

  /* ----------------------------------------------------------------------- *
   * View controls: wheel zoom (CSS only — bitmap untouched)                 *
   * ----------------------------------------------------------------------- */
  function wheel(e) {
    if (!hasBg) { return; }
    e.preventDefault();
    viewScale = Math.max(0.2, Math.min(8, viewScale * (e.deltaY < 0 ? 1.1 : 0.9)));
    applyView();
    render();
  }

  /* ----------------------------------------------------------------------- *
   * Bridge — JS -> Python hidden Gradio Textbox (#mut_crop_to_py)           *
   * Native value-setter write to parent.document + bubbling input/change.    *
   * The ONLY reliable JS->Gradio path; ported verbatim from ImageSuite.      *
   * ----------------------------------------------------------------------- */
  function setHidden(id, val) {
    try {
      var pd = parent.document;
      var e = pd.querySelector("#" + id + " textarea") || pd.querySelector("#" + id + " input");
      if (!e) { return; }
      var proto = (e.tagName === "TEXTAREA")
        ? parent.HTMLTextAreaElement.prototype
        : parent.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(e, val);
      e.dispatchEvent(new Event("input", { bubbles: true }));
      e.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (err) { /* parent not reachable yet */ }
  }

  // Export = the crop rect as integer SOURCE-pixel coords (NOT a baked PNG).
  function buildExport() {
    return JSON.stringify({
      seg_id: SEG_ID,
      x: Math.round(crop.x),
      y: Math.round(crop.y),
      w: Math.round(crop.w),
      h: Math.round(crop.h)
    });
  }

  function exportNow() {
    if (!hasBg) { return; }
    setHidden(TO_PY, buildExport());
  }

  var exportTimer = null;
  function pushExport() {
    if (!hasBg) { return; }
    syncDims();
    clearTimeout(exportTimer);
    exportTimer = setTimeout(exportNow, 120);
  }

  /* ----------------------------------------------------------------------- *
   * Py -> JS — parked background receiver for the source frame              *
   * The plugin injects a one-shot hidden iframe calling                     *
   *   parent.window.__mut_crop_setframe({frame, seg_id, src_w, src_h})      *
   * ----------------------------------------------------------------------- */
  function setBg(payload) {
    if (!payload || !payload.frame) { return; }
    SEG_ID = payload.seg_id != null ? String(payload.seg_id) : "";
    var im = new Image();
    im.onload = function () {
      baseImg = im;
      // Bitmap == source: naturalWidth/Height ARE the source pixel space the
      // crop coords are emitted in. Prefer the probed src_w/h, fall back to
      // the decoded frame's natural size.
      var w = (payload.src_w && payload.src_w > 0) ? payload.src_w : im.naturalWidth;
      var h = (payload.src_h && payload.src_h > 0) ? payload.src_h : im.naturalHeight;
      setSize(w, h);
      crop = { x: 0, y: 0, w: W, h: H };
      aspect = 0;
      syncAspUI();
      hasBg = true;
      if (empty) { empty.style.display = "none"; }
      render();
      pushExport();
    };
    im.onerror = function () { /* bad data-URI; leave the empty state */ };
    im.src = payload.frame;
  }

  function syncAspUI() {
    if (!aspbar) { return; }
    var btns = aspbar.querySelectorAll("button[data-asp]");
    for (var i = 0; i < btns.length; i++) {
      var a = ASPMAP[btns[i].getAttribute("data-asp")] || 0;
      var on = (aspect === 0)
        ? (a === 0)
        : (Math.abs(a - aspect) < 1e-6);
      btns[i].classList.toggle("on", on);
    }
  }

  function resetCrop() {
    if (!hasBg) { return; }
    crop = { x: 0, y: 0, w: W, h: H };
    aspect = 0;
    syncAspUI();
    render();
    pushExport();
  }

  /* ----------------------------------------------------------------------- *
   * Wiring                                                                   *
   * ----------------------------------------------------------------------- */
  function wire() {
    disp.addEventListener("mousedown", down);
    disp.addEventListener("touchstart", down, { passive: false });
    window.addEventListener("mousemove", move);
    window.addEventListener("touchmove", move, { passive: false });
    window.addEventListener("mouseup", up);
    window.addEventListener("touchend", up);
    wrap.addEventListener("wheel", wheel, { passive: false });

    if (aspbar) {
      var btns = aspbar.querySelectorAll("button[data-asp]");
      for (var i = 0; i < btns.length; i++) {
        (function (b) {
          b.addEventListener("click", function () {
            var grp = aspbar.querySelectorAll("button[data-asp]");
            for (var j = 0; j < grp.length; j++) { grp[j].classList.toggle("on", grp[j] === b); }
            setAspect(ASPMAP[b.getAttribute("data-asp")] || 0);
          });
        })(btns[i]);
      }
      var rb = document.getElementById("mut-crop-reset");
      if (rb) { rb.addEventListener("click", resetCrop); }
    }

    // On layout resize do NOT reassign canvas.width/height (that clears the
    // bitmap) — only recompute the fit scale; zoom/pixels are preserved.
    window.addEventListener("resize", function () {
      if (!hasBg) { return; }
      var availW = (stage.parentNode.clientWidth || 600) - 20;
      var availH = (stage.parentNode.clientHeight || 480) - 20;
      var sw = W > 0 ? availW / W : 1;
      var sh = H > 0 ? availH / H : 1;
      baseScale = Math.min(1, sw, sh);
      if (!(baseScale > 0)) { baseScale = 1; }
      applyView();
      render();
    });
  }

  /* ----------------------------------------------------------------------- *
   * Boot — build the DOM, wire, park the Py->JS handles on parent.window    *
   * ----------------------------------------------------------------------- */
  function boot() {
    buildSkeleton();
    wire();
    // Park the receivers the plugin's injector iframe calls.
    try { parent.window.__mut_crop_setframe = setBg; } catch (e) { /* */ }
    try { parent.window.__mut_crop_exportnow = exportNow; } catch (e2) { /* */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
