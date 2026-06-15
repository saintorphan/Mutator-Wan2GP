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
"""
