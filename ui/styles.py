"""Mutator CSS — main-tab accent styling.

Gives the "Mutator" tab button in the app's main tab bar a cyan/teal outline +
glow so it's easy to pick out among the sibling plugins (each uses a distinct
accent: ImageSuite gold, Reel2Reel green, Replicant purple, Mutator cyan/teal).
The class ``.mutator-tabbtn`` is applied at runtime by the small JS tagger in
``plugin.create_ui`` (it matches the tab button whose text is the plugin name).
"""

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
"""
