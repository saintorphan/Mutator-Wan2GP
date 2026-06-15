"""Mutator logo banner.

Rendered at the top of the Mutator tab (same size/position as Image Suite's
banner) so the editor content lines up beneath it. The PNG is base64-embedded
into the HTML so it survives Gradio's static-file routing.

Drop the banner artwork at ``assets/mutator_logo.png`` — if it's missing we fall
back to a styled text banner so the plugin still renders.
"""
from __future__ import annotations

import base64
import functools
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_LOGO = _ASSETS / "mutator_logo.png"


@functools.lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    # Read + base64-encode once (the banner is ~310KB); create_ui can be called
    # repeatedly, so memoise rather than re-encode the PNG on every render.
    try:
        b64 = base64.b64encode(_LOGO.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


_REPO_URL = "https://github.com/saintorphan/Mutator-Wan2GP"
# Inline GitHub mark so the link needs no external asset.
_GH_SVG = ('<svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor" '
           'aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 '
           '5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49'
           '-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 '
           '1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2'
           '-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 '
           '.67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 '
           '2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 '
           '3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38'
           'A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>')


def _gh_link() -> str:
    return (f'<a id="mutator-gh" href="{_REPO_URL}" target="_blank" '
            f'rel="noopener noreferrer" title="Mutator-Wan2GP on GitHub">'
            f'{_GH_SVG}<span>GitHub</span></a>')


def banner_html() -> str:
    uri = _logo_data_uri()
    left = (f'<img src="{uri}" alt="Mutator"/>' if uri
            else '<h2>Mutator</h2>')
    return f'<div id="mutator-banner">{left}{_gh_link()}</div>'
