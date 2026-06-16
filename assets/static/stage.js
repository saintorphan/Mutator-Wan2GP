/* Mutator v0.4 — STAGE: a real video-preview player (transport + crop overlay)
 * synced bidirectionally to the single-track timeline playhead.
 *
 * Delivery: this file is a PARENT-DOCUMENT module shipped via
 *   WAN2GPPlugin.add_custom_js() (exactly like timeline.js) — NOT an iframe. It
 *   mounts into the #mut_stage_root div, so it shares `window` with timeline.js
 *   and the two sync directly (window.MutStage <-> window.MutTimeline) with no
 *   cross-iframe calls.
 *
 * The stage previews the SELECTED clip only, so the stage's playhead frame is
 * "within-clip seconds" — clipStart is treated as 0 here; timeline.js subtracts
 * the selected clip's timeline start before calling seekToTimeline().
 *
 * TIMELINE SYNC (the core feature — Reel2Reel math, single-clip simplified):
 *   Scrub  (timeline -> video): timeline.js calls MutStage.seekToTimeline(sec);
 *     off = sec*speed;  src_t = reverse ? out-off : in_+off;  clamp [in_,out];
 *     video.currentTime = src_t  (guarded on readyState; queued on metadata).
 *   Playback (video -> timeline): a rAF loop reads video.currentTime, computes
 *     tl = (currentTime - in_)/speed (forward time within the clip) and calls
 *     window.MutTimeline.setExternalPlayhead(tl). Stops at out.
 *   FEEDBACK GUARD: S.driving is set while the stage plays. setExternalPlayhead
 *     (on the timeline side) NEVER calls back into MutStage.seekToTimeline, so a
 *     playback-driven playhead update cannot bounce back into a seek.
 *
 * Crop emit (JS -> Py): unchanged contract — debounced write of
 *   {seg_id,x,y,w,h} (SOURCE px, even-rounded in Python) into #mut_crop_to_py
 *   via the native value setter (setHidden). Accent #00d9ff.
 *
 * Author: saintorphan.
 */
(function () {
  "use strict";
  if (window.MutStage) return;

  /* ----------------------------------------------------------------------- *
   * Constants                                                               *
   * ----------------------------------------------------------------------- */
  var STAGE_ROOT = "mut_stage_root";       // parent-doc mount div
  var TO_PY = "mut_crop_to_py";            // hidden Textbox elem_id (JS -> Py)
  var ACCENT = "#00d9ff";                  // Mutator cyan
  var ASPMAP = { "free": 0, "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16 };
  var MIN = 8;                             // minimum crop size (source px)

  /* ----------------------------------------------------------------------- *
   * State                                                                   *
   * ----------------------------------------------------------------------- */
  var S = {
    mounted: false,
    root: null, video: null, canvas: null, ctx: null, bar: null,
    aspbar: null, readout: null,

    // current clip payload (source-second window + transform flags)
    url: "", segId: "", in_: 0, out: 0, speed: 1, reverse: false,
    srcW: 0, srcH: 0,

    // crop rect in SOURCE (bitmap) px — exactly what ffmpeg wants.
    crop: { x: 0, y: 0, w: 0, h: 0 },
    aspect: 0,
    cropMode: false,                       // crop tool toggle (overlay hidden when off)
    hasClip: false,

    dragging: null, dragStart: null,
    driving: false,                        // true while the stage drives playback
    rafId: 0, playId: 0,                    // rAF handle + monotonic play token
    pendingSeek: null,                     // a seek queued until loadedmetadata
    exportTimer: null
  };

  /* ----------------------------------------------------------------------- *
   * Small DOM helper                                                        *
   * ----------------------------------------------------------------------- */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  /* ----------------------------------------------------------------------- *
   * Time readout (MM:SS.mmm)                                                *
   * ----------------------------------------------------------------------- */
  function fmt(t) {
    if (!(t >= 0)) t = 0;
    var m = Math.floor(t / 60);
    var s = Math.floor(t % 60);
    var ms = Math.floor((t - Math.floor(t)) * 1000);
    function p2(n) { return n < 10 ? "0" + n : "" + n; }
    function p3(n) { return n < 10 ? "00" + n : (n < 100 ? "0" + n : "" + n); }
    return p2(m) + ":" + p2(s) + "." + p3(ms);
  }
  // The on-timeline duration of the clip = source length / speed.
  function clipDur() {
    var len = Math.max(0, S.out - S.in_);
    var sp = (S.speed && S.speed > 0.01) ? S.speed : 1;
    return len / sp;
  }
  // Current within-clip timeline second derived from video.currentTime.
  function curTl() {
    if (!S.video) return 0;
    var sp = (S.speed && S.speed > 0.01) ? S.speed : 1;
    var ct = S.video.currentTime || 0;
    var tl = S.reverse ? (S.out - ct) / sp : (ct - S.in_) / sp;
    return Math.max(0, tl);
  }
  function updateReadout() {
    if (!S.readout) return;
    S.readout.textContent = fmt(curTl()) + " / " + fmt(clipDur());
  }

  /* ----------------------------------------------------------------------- *
   * Crop geometry (ported from crop.js — source-px rect over the video)     *
   * The overlay canvas backing store is sized to the video's SOURCE pixels  *
   * (videoWidth x videoHeight), so crop coords need no transform; the canvas *
   * is stretched over the displayed video rect via CSS.                     *
   * ----------------------------------------------------------------------- */
  function dispScale() {
    // SOURCE px per displayed CSS px (so a hit radius in px stays constant).
    if (!S.canvas) return 1;
    var r = S.canvas.getBoundingClientRect();
    if (r.width <= 0 || !S.srcW) return 1;
    return S.srcW / r.width;
  }
  // Pointer (client coords) -> SOURCE px, clamped to [0,W]/[0,H].
  function pos(e) {
    var r = S.canvas.getBoundingClientRect();
    var t = (e.touches && e.touches[0]) ? e.touches[0] : e;
    var x = r.width > 0 ? (t.clientX - r.left) / r.width * S.srcW : 0;
    var y = r.height > 0 ? (t.clientY - r.top) / r.height * S.srcH : 0;
    return { x: Math.max(0, Math.min(S.srcW, x)), y: Math.max(0, Math.min(S.srcH, y)) };
  }
  function handlePts() {
    var c = S.crop;
    var x0 = c.x, y0 = c.y, x1 = c.x + c.w, y1 = c.y + c.h;
    var mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
    return [
      { x: x0, y: y0, h: "nw" }, { x: mx, y: y0, h: "n" }, { x: x1, y: y0, h: "ne" },
      { x: x1, y: my, h: "e" }, { x: x1, y: y1, h: "se" }, { x: mx, y: y1, h: "s" },
      { x: x0, y: y1, h: "sw" }, { x: x0, y: my, h: "w" }
    ];
  }
  function hitHandle(p) {
    var c = S.crop;
    var r = 11 * dispScale();                 // px radius mapped to source px
    var pts = handlePts();
    for (var i = 0; i < pts.length; i++) {
      if (Math.abs(p.x - pts[i].x) < r && Math.abs(p.y - pts[i].y) < r) return pts[i].h;
    }
    if (p.x >= c.x && p.x <= c.x + c.w && p.y >= c.y && p.y <= c.y + c.h) return "move";
    return null;
  }
  function clampCrop() {
    var c = S.crop, W = S.srcW, H = S.srcH;
    if (c.w < MIN) c.w = MIN;
    if (c.h < MIN) c.h = MIN;
    if (c.w > W) c.w = W;
    if (c.h > H) c.h = H;
    if (c.x < 0) c.x = 0;
    if (c.y < 0) c.y = 0;
    if (c.x + c.w > W) c.x = W - c.w;
    if (c.y + c.h > H) c.y = H - c.h;
  }
  function applyAspectResize(h) {
    if (!S.aspect) return;
    var c = S.crop;
    var cx = c.x + c.w / 2, cy = c.y + c.h / 2;
    if (h === "e" || h === "w") {
      var nh = c.w / S.aspect; c.y = cy - nh / 2; c.h = nh;
    } else if (h === "n" || h === "s") {
      var nw = c.h * S.aspect; c.x = cx - nw / 2; c.w = nw;
    } else {
      var nh2 = c.w / S.aspect;
      if (h === "nw" || h === "ne") { c.y = (c.y + c.h) - nh2; }
      c.h = nh2;
    }
    clampCrop();
  }
  function setAspect(a) {
    S.aspect = a || 0;
    if (S.aspect && S.hasClip) {
      var c = S.crop, W = S.srcW, H = S.srcH;
      var cx = c.x + c.w / 2, cy = c.y + c.h / 2;
      var nw = c.w, nh = nw / S.aspect;
      if (nh > H) { nh = H; nw = nh * S.aspect; }
      if (nw > W) { nw = W; nh = nw / S.aspect; }
      c.w = nw; c.h = nh; c.x = cx - nw / 2; c.y = cy - nh / 2;
      clampCrop();
      render();
      pushExport();
    }
  }
  function drawCropOverlay() {
    var dx = S.ctx, c = S.crop, W = S.srcW, H = S.srcH;
    if (!dx) return;
    dx.clearRect(0, 0, W, H);
    if (!S.hasClip || !S.cropMode) return;   // overlay hidden unless crop mode
    // dim outside the crop rect
    dx.save();
    dx.fillStyle = "rgba(0,0,0,.45)";
    dx.fillRect(0, 0, W, c.y);
    dx.fillRect(0, c.y + c.h, W, H - (c.y + c.h));
    dx.fillRect(0, c.y, c.x, c.h);
    dx.fillRect(c.x + c.w, c.y, W - (c.x + c.w), c.h);
    dx.restore();

    var ds = dispScale();
    var lw = Math.max(1, 1.5 * ds);
    var hs = Math.max(3, 6 * ds);

    dx.save();
    dx.strokeStyle = ACCENT;
    dx.lineWidth = lw;
    dx.strokeRect(c.x, c.y, c.w, c.h);
    // rule-of-thirds
    dx.strokeStyle = "rgba(255,255,255,.35)";
    dx.lineWidth = Math.max(0.5, 0.75 * ds);
    for (var i = 1; i < 3; i++) {
      dx.beginPath();
      dx.moveTo(c.x + c.w * i / 3, c.y);
      dx.lineTo(c.x + c.w * i / 3, c.y + c.h);
      dx.stroke();
      dx.beginPath();
      dx.moveTo(c.x, c.y + c.h * i / 3);
      dx.lineTo(c.x + c.w, c.y + c.h * i / 3);
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
  function render() {
    // The overlay canvas is interactive (and visible) ONLY in crop mode; when
    // off it's transparent + pointer-events:none so the video plays clean.
    if (S.canvas) {
      S.canvas.style.pointerEvents = S.cropMode ? "auto" : "none";
      S.canvas.style.display = S.cropMode ? "" : "none";
    }
    drawCropOverlay();
    syncAspUI();
  }

  /* ----------------------------------------------------------------------- *
   * Crop-mode toggle (drives the crop_btn in the tool row, JS-side)         *
   * ----------------------------------------------------------------------- */
  function setCropMode(b) {
    S.cropMode = !!b;
    render();
    return S.cropMode;
  }
  function toggleCropMode() {
    return setCropMode(!S.cropMode);
  }
  // Public setAspect(name): map a preset name through ASPMAP, then re-clamp.
  function setAspectByName(name) {
    setAspect(ASPMAP[name] || 0);
  }

  /* ----------------------------------------------------------------------- *
   * Crop drag interaction (window-bound so drags can leave the canvas)      *
   * ----------------------------------------------------------------------- */
  function down(e) {
    if (!S.hasClip || !S.cropMode) return;   // no crop drag unless crop mode on
    if (e.button !== undefined && e.button !== 0) return;
    var p = pos(e);
    var h = hitHandle(p);
    if (!h) {
      h = "se";
      S.crop.x = p.x; S.crop.y = p.y; S.crop.w = 1; S.crop.h = 1;
    }
    e.preventDefault();
    S.dragging = h;
    S.dragStart = { p: p, crop: { x: S.crop.x, y: S.crop.y, w: S.crop.w, h: S.crop.h } };
  }
  function move(e) {
    if (!S.dragging) return;
    e.preventDefault();
    var p = pos(e);
    var s = S.dragStart.crop, c = S.crop;
    var ddx = p.x - S.dragStart.p.x, ddy = p.y - S.dragStart.p.y;
    if (S.dragging === "move") {
      c.x = s.x + ddx; c.y = s.y + ddy;
      clampCrop();
    } else {
      var x0 = s.x, y0 = s.y, x1 = s.x + s.w, y1 = s.y + s.h;
      if (S.dragging.indexOf("w") >= 0) x0 = Math.min(p.x, x1 - MIN);
      if (S.dragging.indexOf("e") >= 0) x1 = Math.max(p.x, x0 + MIN);
      if (S.dragging.indexOf("n") >= 0) y0 = Math.min(p.y, y1 - MIN);
      if (S.dragging.indexOf("s") >= 0) y1 = Math.max(p.y, y0 + MIN);
      c.x = x0; c.y = y0; c.w = x1 - x0; c.h = y1 - y0;
      if (S.aspect) applyAspectResize(S.dragging);
      clampCrop();
    }
    render();
  }
  function up() {
    if (!S.dragging) return;
    S.dragging = null;
    pushExport();
  }

  /* ----------------------------------------------------------------------- *
   * Bridge — JS -> Python hidden Gradio Textbox (#mut_crop_to_py)           *
   * Native value-setter + bubbling input/change is the only reliable path.  *
   * ----------------------------------------------------------------------- */
  function setHidden(id, val) {
    try {
      var e = document.querySelector("#" + id + " textarea")
        || document.querySelector("#" + id + " input");
      if (!e) return;
      var proto = (e.tagName === "TEXTAREA")
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(e, val);
      e.dispatchEvent(new Event("input", { bubbles: true }));
      e.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (err) { /* not reachable yet */ }
  }
  function buildExport() {
    return JSON.stringify({
      seg_id: S.segId,
      x: Math.round(S.crop.x),
      y: Math.round(S.crop.y),
      w: Math.round(S.crop.w),
      h: Math.round(S.crop.h)
    });
  }
  function exportNow() {
    if (!S.hasClip) return;
    setHidden(TO_PY, buildExport());
  }
  function pushExport() {
    if (!S.hasClip) return;
    clearTimeout(S.exportTimer);
    S.exportTimer = setTimeout(exportNow, 120);
  }

  /* ----------------------------------------------------------------------- *
   * Aspect-preset button UI sync                                            *
   * ----------------------------------------------------------------------- */
  function syncAspUI() {
    if (!S.aspbar) return;
    var btns = S.aspbar.querySelectorAll("button[data-asp]");
    for (var i = 0; i < btns.length; i++) {
      var a = ASPMAP[btns[i].getAttribute("data-asp")] || 0;
      var on = (S.aspect === 0) ? (a === 0) : (Math.abs(a - S.aspect) < 1e-6);
      btns[i].classList.toggle("on", on);
    }
  }

  /* ----------------------------------------------------------------------- *
   * Transport + sync                                                        *
   * ----------------------------------------------------------------------- */
  // Convert a within-clip timeline second to a SOURCE second and seek.
  function seekToTimeline(sec) {
    if (!S.video || !S.hasClip) return;
    var ph = Math.max(0, sec || 0);
    var sp = (S.speed && S.speed > 0.01) ? S.speed : 1;
    var off = ph * sp;
    var srcT = S.reverse ? (S.out - off) : (S.in_ + off);
    srcT = Math.max(S.in_, Math.min(S.out, srcT));
    if (S.video.readyState >= 1) {
      try { S.video.currentTime = srcT; } catch (e) { /* */ }
      updateReadout();
    } else {
      S.pendingSeek = srcT;                  // applied on loadedmetadata
    }
  }

  function stopRaf() {
    if (S.rafId) { cancelAnimationFrame(S.rafId); S.rafId = 0; }
  }
  // Playback loop: video drives the timeline playhead (no re-seek of the video).
  function driveLoop() {
    var myId = S.playId;
    function tick() {
      if (myId !== S.playId) return;        // a newer play/stop superseded us
      if (!S.video) return;
      var ct = S.video.currentTime || 0;
      // Stop at the clip's out point (or the reversed in point).
      if (!S.reverse && ct >= S.out - 1e-3) { pause(); seekTo(S.out); return; }
      if (S.reverse && ct <= S.in_ + 1e-3) { pause(); seekTo(S.in_); return; }
      var tl = curTl();
      if (window.MutTimeline && window.MutTimeline.setExternalPlayhead) {
        window.MutTimeline.setExternalPlayhead(tl);
      }
      updateReadout();
      S.rafId = requestAnimationFrame(tick);
    }
    S.rafId = requestAnimationFrame(tick);
  }
  // Low-level seek used by transport buttons (source seconds).
  function seekTo(srcT) {
    if (!S.video) return;
    srcT = Math.max(S.in_, Math.min(S.out, srcT));
    if (S.video.readyState >= 1) {
      try { S.video.currentTime = srcT; } catch (e) { /* */ }
    } else {
      S.pendingSeek = srcT;
    }
    var tl = curTl();
    if (window.MutTimeline && window.MutTimeline.setExternalPlayhead) {
      window.MutTimeline.setExternalPlayhead(tl);
    }
    updateReadout();
  }

  function play() {
    if (!S.video || !S.hasClip) return;
    // If we're parked at the end, restart from the clip start.
    var ct = S.video.currentTime || 0;
    if (!S.reverse && ct >= S.out - 1e-3) { seekTo(S.in_); }
    if (S.reverse && ct <= S.in_ + 1e-3) { seekTo(S.out); }
    S.driving = true;
    S.playId++;
    var p = S.video.play();
    if (p && p.catch) p.catch(function () { /* autoplay rejected */ });
    syncPlayBtn(true);
    stopRaf();
    driveLoop();
  }
  function pause() {
    if (!S.video) return;
    S.driving = false;
    S.playId++;                              // invalidate the running loop
    stopRaf();
    try { S.video.pause(); } catch (e) { /* */ }
    syncPlayBtn(false);
    updateReadout();
  }
  function toggle() {
    if (S.video && !S.video.paused) pause(); else play();
  }
  function stop() {
    pause();
    seekTo(S.reverse ? S.out : S.in_);
  }
  function stepFrames(n) {
    if (!S.video || !S.hasClip) return;
    pause();
    var fps = (S.video && S.video.dataset && +S.video.dataset.fps) || 30;
    var dt = n / fps;
    seekTo((S.video.currentTime || 0) + dt);
  }
  function home() { pause(); seekTo(S.reverse ? S.out : S.in_); }
  function end() { pause(); seekTo(S.reverse ? S.in_ : S.out); }

  function syncPlayBtn(playing) {
    if (!S.bar) return;
    var b = S.bar.querySelector('[data-act="play"]');
    if (b) b.innerHTML = playing ? "&#10074;&#10074;" : "&#9654;";
  }

  /* ----------------------------------------------------------------------- *
   * loadClip — the Py -> JS payload entrypoint                              *
   * ----------------------------------------------------------------------- */
  function loadClip(payload) {
    if (!S.mounted) { tryMount(); }
    if (!S.mounted || !payload) return;
    var sameUrl = (payload.url === S.url);
    S.url = payload.url || "";
    S.segId = payload.seg_id != null ? String(payload.seg_id) : "";
    S.in_ = +payload["in"] || 0;
    S.out = +payload.out || 0;
    S.speed = (+payload.speed > 0.01) ? +payload.speed : 1;
    S.reverse = !!payload.reverse;
    S.srcW = +payload.src_w || 0;
    S.srcH = +payload.src_h || 0;

    // Init the crop rect from the payload (or full frame).
    if (payload.crop && payload.crop.w && payload.crop.h) {
      S.crop = {
        x: +payload.crop.x || 0, y: +payload.crop.y || 0,
        w: +payload.crop.w || 0, h: +payload.crop.h || 0
      };
      S.aspect = 0;
    } else {
      S.crop = { x: 0, y: 0, w: S.srcW, h: S.srcH };
      S.aspect = 0;
    }

    // Size the overlay backing store to SOURCE px (no transform needed).
    if (S.canvas) {
      if (S.srcW > 0 && S.srcH > 0) { S.canvas.width = S.srcW; S.canvas.height = S.srcH; }
    }

    S.hasClip = (!!S.url) && S.srcW > 0 && S.srcH > 0;
    pause();

    if (!sameUrl && S.url) {
      S.video.src = S.url;
      // seek to in_ once the metadata is ready (so videoWidth/Height are known).
      S.pendingSeek = S.reverse ? S.out : S.in_;
      try { S.video.load(); } catch (e) { /* */ }
    } else if (S.url) {
      // Same source: seek to the clip in-point straight away.
      seekTo(S.reverse ? S.out : S.in_);
    }

    if (S.hasClip) {
      var emptyEl = S.root && S.root.querySelector(".mut-stage-empty");
      if (emptyEl) emptyEl.style.display = "none";
    }
    render();
    pushExport();
    updateReadout();
  }

  /* ----------------------------------------------------------------------- *
   * Mount — build the stage DOM into #mut_stage_root                        *
   * ----------------------------------------------------------------------- */
  function buildSkeleton(root) {
    root.innerHTML = "";
    var wrap = el("div", "mut-stage");

    var disp = el("div", "mut-stage-disp");
    var video = document.createElement("video");
    video.className = "mut-stagevid";
    video.setAttribute("playsinline", "");
    video.setAttribute("preload", "auto");
    video.muted = true;                      // allow programmatic play()
    disp.appendChild(video);
    var canvas = document.createElement("canvas");
    canvas.className = "mut-stage-overlay";
    disp.appendChild(canvas);
    var empty = el("div", "mut-stage-empty",
      "No clip selected &mdash; load a clip and pick a segment on the timeline.");
    disp.appendChild(empty);
    wrap.appendChild(disp);

    // Aspect-preset bar (above the transport).
    var aspbar = el("div", "mut-stage-aspbar");
    aspbar.appendChild(el("span", "mut-stage-asplbl", "Crop"));
    var presets = ["free", "1:1", "4:3", "3:4", "16:9", "9:16"];
    for (var i = 0; i < presets.length; i++) {
      var b = el("button", presets[i] === "free" ? "on" : "");
      b.type = "button";
      b.setAttribute("data-asp", presets[i]);
      b.textContent = presets[i] === "free" ? "Free" : presets[i];
      aspbar.appendChild(b);
    }
    var rb = el("button", "mut-stage-reset");
    rb.type = "button"; rb.setAttribute("data-act", "cropreset");
    rb.title = "Select the whole frame";
    rb.innerHTML = "&#8634; Reset";
    aspbar.appendChild(rb);
    wrap.appendChild(aspbar);

    // Transport bar.
    var bar = el("div", "mut-stage-transport");
    function mkBtn(act, html, title) {
      var x = el("button"); x.type = "button";
      x.setAttribute("data-act", act); x.title = title; x.innerHTML = html;
      return x;
    }
    bar.appendChild(mkBtn("home", "&#9198;", "To clip start"));
    bar.appendChild(mkBtn("stepb", "&#9664;", "Step back"));
    bar.appendChild(mkBtn("play", "&#9654;", "Play / Pause"));
    bar.appendChild(mkBtn("stop", "&#9632;", "Stop"));
    bar.appendChild(mkBtn("stepf", "&#9654;", "Step forward"));
    bar.appendChild(mkBtn("end", "&#9197;", "To clip end"));
    var ro = el("span", "mut-stage-readout", "00:00.000 / 00:00.000");
    bar.appendChild(ro);
    wrap.appendChild(bar);

    root.appendChild(wrap);

    S.root = wrap;
    S.video = video;
    S.canvas = canvas;
    S.ctx = canvas.getContext("2d");
    S.bar = bar;
    S.aspbar = aspbar;
    S.readout = ro;
    S.mounted = true;

    wire();
    render();    // set the initial canvas visibility (hidden until crop mode)
    if (S.url) { loadClip({
      url: S.url, seg_id: S.segId, "in": S.in_, out: S.out, speed: S.speed,
      reverse: S.reverse, src_w: S.srcW, src_h: S.srcH,
      crop: (S.crop && S.crop.w) ? S.crop : null
    }); }
  }

  function wire() {
    // crop drag
    S.canvas.addEventListener("mousedown", down);
    S.canvas.addEventListener("touchstart", down, { passive: false });
    window.addEventListener("mousemove", move);
    window.addEventListener("touchmove", move, { passive: false });
    window.addEventListener("mouseup", up);
    window.addEventListener("touchend", up);

    // aspect presets + reset
    S.aspbar.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      if (b.getAttribute("data-act") === "cropreset") {
        if (!S.hasClip) return;
        S.crop = { x: 0, y: 0, w: S.srcW, h: S.srcH };
        S.aspect = 0;
        render(); pushExport();
        return;
      }
      var a = b.getAttribute("data-asp");
      if (a == null) return;
      setAspect(ASPMAP[a] || 0);
    });

    // transport
    S.bar.addEventListener("click", function (e) {
      var b = e.target.closest("[data-act]");
      if (!b) return;
      switch (b.getAttribute("data-act")) {
        case "home": home(); break;
        case "stepb": stepFrames(-1); break;
        case "play": toggle(); break;
        case "stop": stop(); break;
        case "stepf": stepFrames(1); break;
        case "end": end(); break;
      }
    });

    // video metadata: apply any queued seek + redraw the overlay at real dims.
    S.video.addEventListener("loadedmetadata", function () {
      if (S.video.videoWidth && (!S.srcW || S.srcW !== S.video.videoWidth)) {
        // trust the real decoded dims if the payload lacked them.
        if (!S.srcW) S.srcW = S.video.videoWidth;
        if (!S.srcH) S.srcH = S.video.videoHeight;
        if (S.canvas) { S.canvas.width = S.srcW; S.canvas.height = S.srcH; }
        if (!(S.crop && S.crop.w)) S.crop = { x: 0, y: 0, w: S.srcW, h: S.srcH };
      }
      if (S.pendingSeek != null) {
        var t = S.pendingSeek; S.pendingSeek = null;
        try { S.video.currentTime = Math.max(S.in_, Math.min(S.out, t)); } catch (e) {}
      }
      render();
      updateReadout();
    });
    S.video.addEventListener("seeked", function () {
      if (!S.driving) updateReadout();
    });
    S.video.addEventListener("ended", function () { pause(); });

    window.addEventListener("resize", function () { render(); });
  }

  function tryMount() {
    var root = document.getElementById(STAGE_ROOT);
    if (root && (!root.querySelector(".mut-stage") || !S.mounted)) {
      buildSkeleton(root);
    }
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
        var root = document.getElementById(STAGE_ROOT);
        // Gradio re-renders the tab -> our mount div is replaced; re-mount.
        if (root && !root.querySelector(".mut-stage")) { S.mounted = false; tryMount(); }
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
  }

  // Public surface — the timeline calls seekToTimeline; the plugin calls loadClip.
  // The tool row's crop button drives toggleCropMode/setCropMode/setAspect (JS-only).
  window.MutStage = {
    loadClip: loadClip,
    seekToTimeline: seekToTimeline,
    play: play,
    pause: pause,
    toggle: toggle,
    toggleCropMode: toggleCropMode,
    setCropMode: setCropMode,
    setAspect: setAspectByName,
    remount: tryMount
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
