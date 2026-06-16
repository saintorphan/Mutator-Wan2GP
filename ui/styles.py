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

/* ---- LOAD / STRUCTURE row + INSPECTOR (full width, below the top row) - */
/* The load row (upload / gallery / splice / rejoin) sits between the timeline
   and the inspector; keep its buttons on one compact wrapping line. */
#mutator-loadrow { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }

/* The inspector group holds the SELECTED clip's edits (speed / reverse / flip /
   resize / colour / undo / redo). Give it the same card shell as the zones. */
#mutator-inspector {
    border: 1px solid #2a2a33;
    border-radius: 12px;
    background: #131319;
    padding: 12px 14px 14px;
    margin: 0 0 14px 0;
}
#mutator-inspector .mutator-inspector-title { color: #00d9ff; }

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
