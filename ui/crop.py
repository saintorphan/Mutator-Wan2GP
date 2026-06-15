"""The crop canvas widget: a self-contained ``<iframe srcdoc>`` editor plus the
two hidden Gradio pipes that bridge the browser crop rectangle to Python.

Unlike the timeline, the crop JS is NOT shipped via ``add_custom_js`` — it lives
INSIDE the iframe ``srcdoc`` (a fully self-contained HTML+CSS+JS document read
from ``assets/static/crop.js``), exactly the way ImageSuite's ``modify_canvas``
embeds its editor. The iframe is sandboxed enough that ``<script>`` inside the
``srcdoc`` DOES run (the gr.HTML-innerHTML caveat only bites top-level mounts).

The bridge is two-directional and follows the v0.2 spec §4.4 contract verbatim:

  * JS -> Python: on a (debounced) crop change the iframe writes
    ``{"seg_id","x","y","w","h"}`` — integers in SOURCE pixels — into the hidden
    Gradio Textbox ``#mut_crop_to_py`` via ``parent.document`` + the native value
    setter + bubbling input/change events (the only reliable JS->Gradio path).

  * Python -> JS: to open / refresh the canvas the plugin sets the hidden
    ``gr.HTML`` ``#mut_crop_from_py`` to the value of :func:`crop_bridge_html` — a
    one-shot ``<iframe srcdoc>`` whose ``<script>`` calls the parked handle
    ``parent.window.__mut_crop_setframe({frame, seg_id, src_w, src_h})``. A
    ``nonce`` comment forces the value to differ on every call so Gradio
    re-renders the HTML and the injector script re-executes.

The crop rectangle is expressed in SOURCE pixels: the canvas bitmap is the
UN-cropped source frame at its native ``naturalWidth x naturalHeight``, so the
rect needs no transform — Python only rounds (even w/h for libx264) before
applying it to the selected segment. This module never imports the host or
ffmpeg directly; the frame data-URI is produced by ``core.render.extract_frame``
(reached via :func:`frame_data_uri`), keeping the single ffmpeg surface in core.
"""
from __future__ import annotations

import html as _html
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import gradio as gr

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cost
    from core.model import Segment

logger = logging.getLogger("mutator.crop")

_STATIC = Path(__file__).resolve().parent.parent / "assets" / "static"

# --- elem_ids (binding — timeline.js / crop.js / plugin.py all agree on these) ---
CROP_FRAME_ID = "mut_crop_frame"      # the crop <iframe> (the editor srcdoc)
CROP_TO_PY_ID = "mut_crop_to_py"      # hidden Textbox: JS -> Py {seg_id,x,y,w,h}
CROP_FROM_PY_ID = "mut_crop_from_py"  # hidden HTML: Py -> JS one-shot setframe injector

# The parked window handles the iframe installs (binding contract strings).
SETFRAME_HANDLE = "__mut_crop_setframe"    # background receiver of a new frame
EXPORTNOW_HANDLE = "__mut_crop_exportnow"  # synchronous flush of the current crop

# Accent colour shared across the plugin (NOT the ImageSuite pink).
ACCENT = "#00d9ff"

# Fallback editor doc, used when assets/static/crop.js is not present yet. It is
# a minimal but functional self-contained crop canvas honouring the §4.4/§5.3
# contract (parks __mut_crop_setframe / __mut_crop_exportnow, writes the
# {seg_id,x,y,w,h} JSON into #mut_crop_to_py via the native setter). The shipped
# assets/static/crop.js supersedes this whenever it exists.
_FALLBACK_CROP_DOC = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box;font-family:system-ui,sans-serif}
html,body{height:100%}
body{background:#15151b;color:#ddd;overflow:hidden;user-select:none}
#root{display:flex;flex-direction:column;height:100%;width:100%}
#bar{display:flex;flex-wrap:wrap;gap:4px;padding:6px;background:#1d1d25;border-bottom:1px solid #333}
#bar button{background:#2a2a35;border:1px solid #3a3a48;color:#cfcfe0;border-radius:6px;
  padding:5px 9px;font-size:11px;cursor:pointer;line-height:1.1}
#bar button.on{background:@@ACCENT@@;border-color:@@ACCENT@@;color:#06121a;font-weight:700}
#bar .sp{flex:1}
#bar #info{align-self:center;font-size:10px;color:#8a8a9a;padding-right:6px}
#wrap{flex:1;position:relative;overflow:auto;
  background:#101015 repeating-conic-gradient(#1a1a22 0 25%,#141419 0 50%) 0/24px 24px}
#stage{position:relative;margin:10px auto}
#stage canvas{position:absolute;top:0;left:0;display:block}
#disp{position:relative;cursor:crosshair}
#empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:#555;font-size:13px;text-align:center;pointer-events:none;line-height:1.6}
</style></head><body>
<div id="root">
  <div id="bar">
    <button data-asp="free" class="on">Free</button>
    <button data-asp="1:1">1:1</button>
    <button data-asp="4:3">4:3</button>
    <button data-asp="3:4">3:4</button>
    <button data-asp="16:9">16:9</button>
    <button data-asp="9:16">9:16</button>
    <button id="reset">&#8634; Reset</button>
    <span class="sp"></span>
    <span id="info"></span>
  </div>
  <div id="wrap"><div id="stage">
    <canvas id="bg"></canvas>
    <canvas id="disp"></canvas>
    <div id="empty">No frame yet &#8212; open <b>Crop</b> on a selected clip.</div>
  </div></div>
</div>
<script>
(function(){
var W=0,H=0,hasBg=false,baseImg=null,SEG_ID=null;
var wrap=document.getElementById('wrap'),stage=document.getElementById('stage');
var bg=document.getElementById('bg'),disp=document.getElementById('disp');
var bgx=bg.getContext('2d'),dx=disp.getContext('2d');
var baseScale=1;
var crop={x:0,y:0,w:0,h:0};
var aspect=0;                       // 0 = free, else w/h ratio
var dragging=null,dragStart=null;
var ACCENT="@@ACCENT@@";
var ASPMAP={'free':0,'1:1':1,'4:3':4/3,'3:4':3/4,'16:9':16/9,'9:16':9/16};

function setSize(w,h){ W=w; H=h; bg.width=w; bg.height=h; disp.width=w; disp.height=h; fitView(); }
function fitView(){ var availW=stage.parentNode.clientWidth-20;
  baseScale=Math.min(1,availW/W||1); applyView(); }
function applyView(){ stage.style.width=(W*baseScale)+'px'; stage.style.height=(H*baseScale)+'px';
  bg.style.width='100%'; bg.style.height='100%'; disp.style.width='100%'; disp.style.height='100%'; }

function paintBg(){ bgx.clearRect(0,0,W,H); if(baseImg) bgx.drawImage(baseImg,0,0,W,H); }
function handlePts(){ var x0=crop.x,y0=crop.y,x1=crop.x+crop.w,y1=crop.y+crop.h,
    mx=(x0+x1)/2,my=(y0+y1)/2;
  return [{x:x0,y:y0,h:'nw'},{x:mx,y:y0,h:'n'},{x:x1,y:y0,h:'ne'},
          {x:x1,y:my,h:'e'},{x:x1,y:y1,h:'se'},{x:mx,y:y1,h:'s'},
          {x:x0,y:y1,h:'sw'},{x:x0,y:my,h:'w'}]; }
function drawCropOverlay(){ dx.clearRect(0,0,W,H); if(!hasBg) return;
  dx.save(); dx.fillStyle='rgba(0,0,0,.5)';
  dx.fillRect(0,0,W,crop.y);
  dx.fillRect(0,crop.y+crop.h,W,H-(crop.y+crop.h));
  dx.fillRect(0,crop.y,crop.x,crop.h);
  dx.fillRect(crop.x+crop.w,crop.y,W-(crop.x+crop.w),crop.h);
  dx.restore();
  var ds=baseScale||1, lw=Math.max(1,1.5/ds), hs=Math.max(3,6/ds);
  dx.save(); dx.strokeStyle=ACCENT; dx.lineWidth=lw;
  dx.strokeRect(crop.x,crop.y,crop.w,crop.h);
  dx.strokeStyle='rgba(255,255,255,.35)'; dx.lineWidth=Math.max(0.5,0.75/ds);
  for(var i=1;i<3;i++){ dx.beginPath(); dx.moveTo(crop.x+crop.w*i/3,crop.y);
    dx.lineTo(crop.x+crop.w*i/3,crop.y+crop.h); dx.stroke();
    dx.beginPath(); dx.moveTo(crop.x,crop.y+crop.h*i/3);
    dx.lineTo(crop.x+crop.w,crop.y+crop.h*i/3); dx.stroke(); }
  dx.fillStyle=ACCENT;
  handlePts().forEach(function(p){ dx.beginPath(); dx.rect(p.x-hs,p.y-hs,hs*2,hs*2); dx.fill(); });
  dx.restore(); }
function render(){ paintBg(); drawCropOverlay(); rsInfo(); }

function pos(e){ var r=disp.getBoundingClientRect(),t=e.touches?e.touches[0]:e;
  var x=(t.clientX-r.left)/r.width*W, y=(t.clientY-r.top)/r.height*H;
  return {x:Math.max(0,Math.min(W,x)), y:Math.max(0,Math.min(H,y))}; }
function hitHandle(p){ var ds=baseScale||1, r=11/ds, pts=handlePts();
  for(var i=0;i<pts.length;i++){ if(Math.abs(p.x-pts[i].x)<r && Math.abs(p.y-pts[i].y)<r) return pts[i].h; }
  if(p.x>=crop.x && p.x<=crop.x+crop.w && p.y>=crop.y && p.y<=crop.y+crop.h) return 'move';
  return null; }
function clampCrop(){ if(crop.w<8) crop.w=8; if(crop.h<8) crop.h=8;
  if(crop.w>W) crop.w=W; if(crop.h>H) crop.h=H;
  if(crop.x<0) crop.x=0; if(crop.y<0) crop.y=0;
  if(crop.x+crop.w>W) crop.x=W-crop.w; if(crop.y+crop.h>H) crop.y=H-crop.h; }
function applyAspectResize(h){ if(!aspect) return;
  var cx=crop.x+crop.w/2, cy=crop.y+crop.h/2;
  if(h==='e'||h==='w'){ var nh=crop.w/aspect; crop.y=cy-nh/2; crop.h=nh; }
  else if(h==='n'||h==='s'){ var nw=crop.h*aspect; crop.x=cx-nw/2; crop.w=nw; }
  else { var nh2=crop.w/aspect; if(h==='nw'||h==='ne'){ crop.y=(crop.y+crop.h)-nh2; } crop.h=nh2; }
  clampCrop(); }
function setAspect(a){ aspect=a;
  if(aspect){ var cx=crop.x+crop.w/2, cy=crop.y+crop.h/2;
    var nw=crop.w, nh=nw/aspect; if(nh>H){ nh=H; nw=nh*aspect; }
    if(nw>W){ nw=W; nh=nw/aspect; }
    crop.w=nw; crop.h=nh; crop.x=cx-nw/2; crop.y=cy-nh/2; clampCrop(); render(); pushExport(); } }

function down(e){ if(!hasBg) return; if(e.button!==undefined && e.button!==0) return;
  var p=pos(e); var h=hitHandle(p); if(!h){ h='se'; crop.x=p.x; crop.y=p.y; crop.w=1; crop.h=1; }
  e.preventDefault(); dragging=h;
  dragStart={p:p, crop:{x:crop.x,y:crop.y,w:crop.w,h:crop.h}}; }
function move(e){ if(!dragging) return; e.preventDefault(); var p=pos(e);
  var s=dragStart.crop, ddx=p.x-dragStart.p.x, ddy=p.y-dragStart.p.y;
  if(dragging==='move'){ crop.x=s.x+ddx; crop.y=s.y+ddy; clampCrop(); }
  else { var x0=s.x,y0=s.y,x1=s.x+s.w,y1=s.y+s.h;
    if(dragging.indexOf('w')>=0) x0=Math.min(p.x,x1-8);
    if(dragging.indexOf('e')>=0) x1=Math.max(p.x,x0+8);
    if(dragging.indexOf('n')>=0) y0=Math.min(p.y,y1-8);
    if(dragging.indexOf('s')>=0) y1=Math.max(p.y,y0+8);
    crop.x=x0; crop.y=y0; crop.w=x1-x0; crop.h=y1-y0;
    if(aspect) applyAspectResize(dragging); clampCrop(); }
  render(); }
function up(){ if(!dragging) return; dragging=null; pushExport(); }
disp.addEventListener('mousedown',down); window.addEventListener('mousemove',move);
window.addEventListener('mouseup',up);
disp.addEventListener('touchstart',down,{passive:false});
disp.addEventListener('touchmove',move,{passive:false}); window.addEventListener('touchend',up);

document.querySelectorAll('#bar button[data-asp]').forEach(function(b){ b.addEventListener('click',function(){
  document.querySelectorAll('#bar button[data-asp]').forEach(function(o){ o.classList.toggle('on',o===b); });
  setAspect(ASPMAP[b.dataset.asp]||0); }); });
document.getElementById('reset').addEventListener('click',function(){ if(!hasBg) return;
  crop={x:0,y:0,w:W,h:H}; aspect=0;
  document.querySelectorAll('#bar button[data-asp]').forEach(function(o){ o.classList.toggle('on',o.dataset.asp==='free'); });
  render(); pushExport(); });

function rsInfo(){ var i=document.getElementById('info'); if(!i) return;
  i.textContent=hasBg?('crop '+Math.round(crop.w)+'×'+Math.round(crop.h)
    +' @ '+Math.round(crop.x)+','+Math.round(crop.y)):''; }

// -- JS -> Python: native value-setter DOM write into the hidden Gradio Textbox --
function setHidden(id,val){ try{ var pd=parent.document;
  var e=pd.querySelector('#'+id+' textarea')||pd.querySelector('#'+id+' input'); if(!e) return;
  var proto=(e.tagName==='TEXTAREA')?parent.HTMLTextAreaElement.prototype:parent.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto,'value').set.call(e,val);
  e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); }catch(err){} }
function buildExport(){ return JSON.stringify({ seg_id: SEG_ID,
  x: Math.round(crop.x), y: Math.round(crop.y),
  w: Math.round(crop.w), h: Math.round(crop.h) }); }
function exportNow(){ if(!hasBg) return; setHidden('@@TO_PY@@', buildExport()); }
var exportTimer=null;
function pushExport(){ if(!hasBg) return; rsInfo(); clearTimeout(exportTimer); exportTimer=setTimeout(exportNow,120); }
try{ parent.window['@@EXPORTNOW@@']=exportNow; }catch(e){}

// -- Python -> JS: receive a new source frame + seg id, init the crop to full --
function setBg(payload){ payload=payload||{};
  var im=new Image(); im.onload=function(){ baseImg=im; SEG_ID=payload.seg_id;
    setSize(im.naturalWidth,im.naturalHeight);
    crop={x:0,y:0,w:W,h:H}; aspect=0;
    document.querySelectorAll('#bar button[data-asp]').forEach(function(o){ o.classList.toggle('on',o.dataset.asp==='free'); });
    hasBg=true; document.getElementById('empty').style.display='none';
    fitView(); render(); pushExport(); }; im.src=payload.frame; }
try{ parent.window['@@SETFRAME@@']=setBg; }catch(e){}

window.addEventListener('resize',function(){ if(!hasBg) return;
  var availW=stage.parentNode.clientWidth-20; baseScale=Math.min(1,availW/W||1); applyView(); render(); });
})();
</script></body></html>"""
# Substitute the four sentinels via str.replace (NOT %/str.format) so the literal
# ``%``, ``{`` and ``}`` throughout the CSS/JS (e.g. ``height:100%``) are never
# treated as format specifiers.
_FALLBACK_CROP_DOC = (
    _FALLBACK_CROP_DOC
    .replace("@@ACCENT@@", ACCENT)
    .replace("@@TO_PY@@", CROP_TO_PY_ID)
    .replace("@@SETFRAME@@", SETFRAME_HANDLE)
    .replace("@@EXPORTNOW@@", EXPORTNOW_HANDLE)
)


def crop_js() -> str:
    """The crop editor document (a self-contained HTML+CSS+JS ``srcdoc`` body).

    Read from ``assets/static/crop.js`` when present (the canonical source the
    sibling implementer ships); otherwise fall back to the built-in minimal
    crop canvas so the widget is still usable. Either way the returned document
    parks ``__mut_crop_setframe`` / ``__mut_crop_exportnow`` on ``parent.window``
    and writes ``{seg_id,x,y,w,h}`` JSON into ``#mut_crop_to_py``.
    """
    path = _STATIC / "crop.js"
    try:
        text = path.read_text(encoding="utf-8")
        if text.strip():
            return text
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("Could not read crop.js, using built-in fallback", exc_info=True)
    return _FALLBACK_CROP_DOC


def build_crop_widget() -> dict:
    """Build the crop-canvas mount + its two hidden bridge components.

    Returns ``{crop_mount, crop_to_py, crop_from_py}``; ``plugin.py`` owns all
    wiring. ``crop_mount`` is the editor ``<iframe srcdoc>``; ``crop_to_py`` is
    the hidden Textbox the iframe writes the source-px crop rect into (JS->Py);
    ``crop_from_py`` is the hidden ``gr.HTML`` that carries the one-shot
    ``setframe`` injector iframe (Py->JS) built by :func:`crop_bridge_html`.
    """
    doc = crop_js()
    iframe = (
        f'<iframe id="{CROP_FRAME_ID}" srcdoc="'
        + _html.escape(doc, quote=True)
        + '" style="width:100%;height:560px;border:1px solid #333;'
        + 'border-radius:10px;background:#15151b;"></iframe>'
    )
    c: dict = {}
    c["crop_mount"] = gr.HTML(iframe)
    # Hidden JS->Py pipe; interactive so the iframe can write into it.
    c["crop_to_py"] = gr.Textbox(
        elem_id=CROP_TO_PY_ID, visible=False, interactive=True, value="", lines=1
    )
    # Hidden Py->JS injector carrier; its value is set to crop_bridge_html(...).
    c["crop_from_py"] = gr.HTML(visible=False, elem_id=CROP_FROM_PY_ID)
    return c


def _js_string(s: str) -> str:
    """JSON-encode a string for embedding in inline JS, neutralising the chars
    that could prematurely close the embedded ``<script>`` / iframe srcdoc."""
    return (
        json.dumps(s)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def crop_bridge_html(
    frame_data_uri: str,
    seg_id: str,
    src_w: int,
    src_h: int,
    nonce: str = "",
) -> str:
    """Build the hidden one-shot ``<iframe srcdoc>`` that injects a source frame
    into the crop canvas (Python -> JS).

    The returned HTML is assigned to the ``crop_from_py`` ``gr.HTML``. Its inline
    ``<script>`` calls the parked handle::

        parent.window.__mut_crop_setframe({frame, seg_id, src_w, src_h})

    ``frame_data_uri`` is a ``data:image/png;base64,…`` of the UN-cropped source
    frame (so its natural pixel size equals ``src_w x src_h``). The ``nonce``
    comment forces the gr.HTML value to differ on every call so Gradio re-renders
    and the injector re-executes even when the same frame is re-sent. All payload
    strings are JSON-escaped so they cannot break out of the script/srcdoc.
    """
    payload = (
        "{frame:"
        + _js_string(frame_data_uri or "")
        + ",seg_id:"
        + _js_string(str(seg_id or ""))
        + ",src_w:"
        + str(int(src_w or 0))
        + ",src_h:"
        + str(int(src_h or 0))
        + "}"
    )
    inner = (
        "<script>/*"
        + str(nonce)
        + "*/try{parent.window['"
        + SETFRAME_HANDLE
        + "']("
        + payload
        + ");}catch(e){}</script>"
    )
    return (
        '<iframe srcdoc="'
        + _html.escape(inner, quote=True)
        + '" style="display:none;width:0;height:0;border:none"></iframe>'
    )


def frame_data_uri(seg: "Segment", at_src_sec: Optional[float] = None) -> str:
    """A ``data:image/png;base64,…`` of the segment's UN-cropped source frame.

    Thin convenience wrapper that delegates to ``core.render.extract_frame`` so
    the single ffmpeg surface stays in ``core``. Imported lazily to keep this UI
    module free of any ffmpeg/core hard dependency at import time. The frame's
    natural width/height equals ``seg.src_w x seg.src_h`` — the SOURCE-pixel space
    the crop rect is emitted in. Returns ``""`` on any failure (the caller
    surfaces a Warning).
    """
    try:
        from core import render
    except Exception:
        logger.warning("core.render unavailable for frame extraction", exc_info=True)
        return ""
    try:
        return render.extract_frame(seg, at_src_sec)
    except Exception:
        logger.warning("extract_frame failed for segment", exc_info=True)
        return ""
