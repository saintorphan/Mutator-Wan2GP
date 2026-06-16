"""Mutator CSS — main-tab accent + the v0.2 three-zone workspace shell.

Gives the "Mutator" tab button in the app's main tab bar a cyan/teal outline +
glow so it's easy to pick out among the sibling plugins (each uses a distinct
accent: ImageSuite gold, Reel2Reel green, Replicant purple, Mutator cyan/teal).
The class ``.mutator-tabbtn`` is applied at runtime by the small JS tagger in
``plugin.create_ui`` (it matches the tab button whose text is the plugin name).

v0.4 lays the STAGE (the video-preview player) and the RESULT player SIDE BY
SIDE in a top flex row (``#mutator-top`` → ``#mutator-stage`` | ``#mutator-result``),
with the timeline, load row, inspector and SEND (``#mutator-send``) zone full
width below. This module deliberately holds NO timeline- or stage-internal
styling: the draggable single-track timeline is scoped under ``.mut-tl`` in
``assets/static/timeline.css`` and the stage (video + crop overlay + transport)
under ``.mut-stage`` in ``assets/static/stage.css`` (each injected via its mount's
``<style>`` blob). Only the OUTER zone/row shells live here.

Public surface (unchanged shape): a single module-level ``CSS`` string consumed
by ``plugin.create_ui`` as ``gr.HTML(f"<style>{ui.styles.CSS}</style>")``.
"""

from __future__ import annotations

#: Cyan/teal accent shared with the tab outline + banner (kept in one place).
ACCENT = "#00d9ff"

CSS = """
#mutator-root { position: relative; }
/* The injected <style> blob + the JS tab-tagger ride in hidden gr.HTML blocks
   ABOVE the banner; without this they render as empty boxes and push the logo
   down (that's the phantom padding over the logo). Collapse them entirely. */
.mutator-hidden { display: none !important; }
#mutator-root > .mutator-hidden { display: none !important; height: 0 !important; margin: 0 !important; padding: 0 !important; }
button.mutator-tabbtn {
    border: 2px solid #00d9ff !important;
    border-radius: 8px !important;
    box-shadow: 0 0 7px rgba(0, 217, 255, 0.55) !important;
}

/* Logo banner — same size/position as Image Suite: 4:1 artwork left-aligned at
   the top of the tab, GitHub link far right, both bottom-aligned. */
#mutator-banner {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: 12px; margin: 0 0 10px 2px;
}
/* Kill Gradio's default top padding on the tab body so the logo sits flush. */
#mutator-root { padding-top: 0 !important; margin-top: 0 !important; }
#mutator-banner img {
    height: 104px; width: auto; max-width: 520px;
    object-fit: contain; display: block;
}
#mutator-banner h2 { margin: 0; color: #00d9ff; font-style: italic; }
#mutator-banner #mutator-gh {
    display: inline-flex; align-items: center; gap: 5px;
    color: #00d9ff; text-decoration: none; font-size: 13px;
    padding-bottom: 6px; white-space: nowrap; flex: 0 0 auto;
}
#mutator-banner #mutator-gh:hover { text-decoration: underline; }

/* ====================================================================== */
/*  v0.4 — TOP ROW: STAGE (preview) | RESULT side by side, then SEND       */
/* ====================================================================== */

/* The top row lays the preview stage (left) and the result player (right)
   side by side; they wrap on a narrow viewport. */
#mutator-top {
    display: flex;
    flex-direction: row;
    flex-wrap: wrap;
    align-items: stretch;
    gap: 14px;
    margin: 0 0 14px 0;
}
#mutator-top > #mutator-stage,
#mutator-top > #mutator-result {
    flex: 1 1 0;
    min-width: 320px;
}

/* Shared card shell for the stage / result / send zones — labelled, outlined
   panels so the split reads as distinct stages. */
#mutator-stage,
#mutator-result,
#mutator-send {
    position: relative;
    border: 1px solid #2a2a33;
    border-radius: 12px;
    background: #131319;
    padding: 14px 14px 16px;
}
#mutator-send { margin: 0 0 14px 0; }

/* Pack the stage column's content to the TOP so the tool row follows the
   transport immediately. The row is align-items:stretch (so stage + result
   read the same height), which makes Gradio's flex column stretch the stage
   taller than its content; justify-content:flex-start + gap:0 stops that slack
   from opening a dead band between the transport and the tool row. */
#mutator-stage {
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    gap: 0;
}
#mutator-stage > * { flex: 0 0 auto; }

/* A zone caption via the empty ::before so the pipeline reads at a glance
   without the layout adding extra Markdown headers. */
#mutator-stage::before,
#mutator-result::before,
#mutator-send::before {
    display: block;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #00d9ff;
    opacity: 0.85;
    margin: 0 0 10px 2px;
}
#mutator-stage::before  { content: "Preview"; }
#mutator-result::before { content: "Result"; }
#mutator-send::before   { content: "Send"; }

/* The result player should fill its zone width and keep a tidy, capped height
   so the timeline/tools stay on-screen (the stage caps its own video height in
   stage.css). */
#mutator-result video {
    border-radius: 8px;
    background: #000;
    max-height: 460px;
}

/* ---- LOAD row (full width, below the top row) ------------------------ */
/* The load row (upload / gallery) sits between the timeline and SEND; keep its
   buttons on one compact wrapping line. */
#mutator-loadrow { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }

/* ====================================================================== */
/*  v0.5 — TOOL ROW under the preview (uniform icon buttons) + popups      */
/* ====================================================================== */

/* The preview info line (speed · W×H of the selected clip) sits just under the
   stage video, above the tool row. Keep its vertical margin tight so the tool
   row follows the transport without a dead band. */
#mutator-stage-info, #mutator-stage-info .prose {
    color: #c7c7d1; font-size: 13px; margin: 6px 0 2px 2px;
}
/* Collapse the info line when empty (Gradio wraps the value in a .prose div, so
   :empty on the outer block never matches — target the inner prose and zero the
   whole block) so the tool row hugs the transport with no dead band. */
#mutator-stage-info .prose:empty { display: none; }
#mutator-stage-info:has(.prose:empty) { margin: 0; min-height: 0; padding: 0; }
/* Kill Gradio's per-block vertical padding inside the stage column so the
   mount → info → tool-row stack has no slack between them. */
#mutator-stage > .block,
#mutator-stage > .form,
#mutator-stage > div > .block { padding-top: 0 !important; padding-bottom: 0 !important; }

/* The tool row: a tight wrapping line of uniform square icon buttons matching
   the transport size, sitting directly under the transport/info line. */
#mutator-tools {
    display: flex; flex-wrap: wrap; align-items: center;
    gap: 6px; margin: 6px 0 2px;
}
/* Uniform square-ish icon buttons (same height + weight as the transport).
   line-height:1 keeps the (now monochrome) glyphs vertically centred and the
   same visual size across the whole row. */
.mut-tool button, #mutator-tools button {
    width: 40px; min-width: 40px; height: 36px;
    padding: 0; font-size: 16px; line-height: 1; font-weight: 400;
}

/* RESIZE / SPEED popups: boxed, raised panels rendered just under the tool row
   (absolutely positioned over the stage column so they read as popups). */
#mutator-resize-pop, #mutator-speed-pop {
    position: absolute; z-index: 25; left: 8px;
    background: #15151b; border: 1px solid #2a2a33; border-radius: 10px;
    padding: 10px; min-width: 220px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}
#mutator-resize-pop .mutator-pop-title,
#mutator-speed-pop .mutator-pop-title,
#mutator-color-drawer .mutator-pop-title { color: #00d9ff; }

/* COLOUR + CROP drawers: a right-side panel inside the (position:relative) stage
   column. Only one is open at a time (the toggles are mutually exclusive). */
#mutator-color-drawer,
#mutator-crop-drawer {
    position: absolute; top: 0; right: 0; width: 230px; height: 100%;
    overflow: auto; z-index: 20;
    background: #15151b; border-left: 1px solid #2a2a33;
    padding: 10px;
}
#mutator-crop-drawer .mutator-pop-hint { color: #9aa; font-size: 12px; }

/* ---- timeline mount spacing ------------------------------------------ */
/* The timeline itself is fully styled under .mut-tl in timeline.css; here we
   only give the mount a top margin so it separates from the top row. */
#mutator-timeline #mut_tl_root { margin-top: 4px; }

/* ---- RESULT zone ----------------------------------------------------- */
#mutator-result .mutator-result-info,
#mutator-result .prose { color: #c7c7d1; font-size: 13px; }

/* ---- SEND zone ------------------------------------------------------- */
/* Group the two save buttons + the send-clip action onto compact rows, and
   give the embedded SendTo frame panel a contained, outlined slot so a sibling
   plugin's panel sits cleanly inside the Send zone. */
#mutator-send .mutator-sendrow {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 10px;
    margin: 6px 0;
}
#mutator-send .mutator-sendrow > * { margin: 0 !important; }
#mutator-send button { min-height: 36px; }

#mutator-send .mutator-frameslot {
    border: 1px solid #23232b;
    border-radius: 10px;
    background: #15151b;
    padding: 10px;
    margin: 10px 0;
}
#mutator-send .mutator-frameslot:empty { display: none; }

#mutator-send .mutator-status,
#mutator-send .mutator-status .prose { color: #9aa; font-size: 12px; }
"""
