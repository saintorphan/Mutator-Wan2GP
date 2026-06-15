"""Mutator CSS — main-tab accent + the v0.2 three-zone workspace shell.

Gives the "Mutator" tab button in the app's main tab bar a cyan/teal outline +
glow so it's easy to pick out among the sibling plugins (each uses a distinct
accent: ImageSuite gold, Reel2Reel green, Replicant purple, Mutator cyan/teal).
The class ``.mutator-tabbtn`` is applied at runtime by the small JS tagger in
``plugin.create_ui`` (it matches the tab button whose text is the plugin name).

v0.2 adds container styling for the three vertical zones — WORKSPACE
(``#mutator-workspace``), RESULT (``#mutator-result``) and SEND
(``#mutator-send``) — and the compact tool row that lives inside the workspace.
This module deliberately holds NO timeline- or crop-internal styling: the
draggable single-track timeline is scoped under ``.mut-tl`` in
``assets/static/timeline.css`` (injected via the timeline mount's ``<style>``),
and the crop canvas CSS is inline inside the ``crop.js`` iframe ``srcdoc``. Only
the OUTER zone/tool-row shells live here.

Public surface (unchanged shape): a single module-level ``CSS`` string consumed
by ``plugin.create_ui`` as ``gr.HTML(f"<style>{ui.styles.CSS}</style>")``.
"""

from __future__ import annotations

#: Cyan/teal accent shared with the tab outline + banner (kept in one place).
ACCENT = "#00d9ff"

CSS = """
#mutator-root { position: relative; }
button.mutator-tabbtn {
    border: 2px solid #00d9ff !important;
    border-radius: 8px !important;
    box-shadow: 0 0 7px rgba(0, 217, 255, 0.55) !important;
}

/* Logo banner — same size/position as Image Suite: 4:1 artwork left-aligned at
   the top of the tab, GitHub link far right, both bottom-aligned. */
#mutator-banner {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: 12px; margin: 4px 0 10px 2px;
}
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
/*  v0.2 — three vertical zones: WORKSPACE -> RESULT -> SEND               */
/* ====================================================================== */

/* Shared card shell for every zone. Each zone is a labelled, outlined panel
   so the Workspace / Result / Send split reads as three stacked stages. */
#mutator-workspace,
#mutator-result,
#mutator-send {
    position: relative;
    border: 1px solid #2a2a33;
    border-radius: 12px;
    background: #131319;
    padding: 14px 14px 16px;
    margin: 0 0 14px 0;
}

/* A faint top accent stripe + zone caption via the empty ::before. The caption
   text is supplied per-zone below so the user can see the pipeline at a glance
   without the layout adding extra Markdown headers. */
#mutator-workspace::before,
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
#mutator-workspace::before { content: "Workspace"; }
#mutator-result::before    { content: "Result"; }
#mutator-send::before      { content: "Send"; }

/* The source player + result player should fill their zone width and keep a
   tidy, capped height so the timeline/tools stay on-screen. */
#mutator-workspace video,
#mutator-result video {
    border-radius: 8px;
    background: #000;
    max-height: 460px;
}

/* ---- compact tool row (inside the workspace) ------------------------- */
/* The tool row is a single wrapping flex line: Splice / Rejoin / Crop /
   Flip H / Flip V / Reverse / Speed / Resize sub-group / Colour sliders /
   Undo / Redo. We tighten Gradio's default block gaps so it reads as one
   compact control strip rather than a column stack. NO accordions. */
#mutator-workspace .mutator-toolrow {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 8px 10px;
    padding: 8px 4px;
    margin: 8px 0;
    border-top: 1px solid #23232b;
    border-bottom: 1px solid #23232b;
}
#mutator-workspace .mutator-toolrow > * {
    margin: 0 !important;
    min-width: 0;
}

/* Buttons in the tool row: compact, accent-tinted, equal vertical rhythm. */
#mutator-workspace .mutator-toolrow button {
    min-height: 34px;
    padding: 4px 12px;
    white-space: nowrap;
}

/* The Splice / Rejoin / Crop primary actions get a subtle cyan emphasis so the
   structural edits stand apart from the per-clip adjusters. */
#mutator-workspace .mutator-toolrow .mutator-structural button {
    border-color: rgba(0, 217, 255, 0.55) !important;
    box-shadow: 0 0 5px rgba(0, 217, 255, 0.25) !important;
}

/* Compact sliders / numbers in the tool row — narrow them so several fit on a
   line and labels sit tight above the control. */
#mutator-workspace .mutator-toolrow .mutator-num { width: 88px; flex: 0 0 auto; }
#mutator-workspace .mutator-toolrow .mutator-slider { width: 150px; flex: 0 0 auto; }
#mutator-workspace .mutator-toolrow label { font-size: 12px; }

/* The colour sliders are visually grouped (six narrow sliders + reset). A thin
   divider keeps them distinct from the geometry tools. */
#mutator-workspace .mutator-colourgroup {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 6px 8px;
    padding: 4px 8px;
    border: 1px solid #23232b;
    border-radius: 8px;
    background: #15151b;
}

/* ---- crop canvas mount (toggled open inside the workspace) ----------- */
/* The crop panel is a Gradio Column shown/hidden by the Crop button. When open
   it gets a little breathing room + accent frame; the iframe itself carries its
   own border from the build_crop_widget() inline style, so we only space it. */
#mutator-workspace .mutator-cropwrap {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed rgba(0, 217, 255, 0.4);
}

/* ---- timeline mount spacing ------------------------------------------ */
/* The timeline itself is fully styled under .mut-tl in timeline.css; here we
   only give the mount a top margin so it separates from the tool row. */
#mutator-workspace #mut_tl_root { margin-top: 10px; }

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
