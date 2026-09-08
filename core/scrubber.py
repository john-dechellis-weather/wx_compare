"""Client-side forecast scrubber: deck.gl in an HTML component.

WHY NOT pydeck. A pydeck slider is a Streamlit rerun per step: the
server rebuilds every layer, the browser tears the deck down, builds
it again and fetches a new image. That floor is about a second and no
amount of tuning moves it, because the work is the round trip itself.

Instant scrubbing means the browser holds EVERY frame and swaps which
one is drawn, with no server involved. So this page builds its map as
a deck.gl component directly: the frame URLs are baked in, the
browser preloads them all, and the slider is a plain <input type=
"range"> whose handler calls deck.setProps() with a new BitmapLayer.
deck.gl keeps decoded textures, so after the preload a swap is a
single draw call.

Everything else — model, product, long-run toggle — still reruns,
because those change WHICH frames exist. Only the hour is local.

Side benefit: in raw JS the basemap is a proper TileLayer, which is
the one thing pydeck could never express.
"""

from __future__ import annotations

import json


def scrubber_html(frames: list, geo: dict, view: dict,
                  opacity: float, height: int = 720,
                  dark: bool = False) -> str:
    """One HTML document for streamlit.components.v1.html.

    frames: [{"url", "label", "valid"}] oldest first, ALL preloaded.
    geo:    {"fixes", "fix_labels", "classb", "hull", "routes",
             "route_labels", "artcc", "aircraft"} — plain dict lists,
             already in the shapes the Python page builds.
    view:   {"lat", "lon", "zoom"}
    """
    F = json.dumps(frames)
    G = json.dumps(geo)
    V = json.dumps(view)
    style_url = ("https://basemaps.cartocdn.com/gl/dark-matter-nolabels-gl-style/style.json"
                 if dark else
                 "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json")
    return f"""
<!doctype html><html><head><meta charset="utf-8">
<script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/deck.gl@8.9.35/dist.min.js"></script>
<style>
  html,body{{margin:0;padding:0;background:#fff;
    font-family:'Times New Roman',Times,serif;}}
  #wrap{{position:relative;width:100%;height:{height}px;}}
  #map{{position:absolute;inset:0;}}
  #ctl{{position:absolute;left:0;right:0;bottom:0;z-index:5;
    background:rgba(255,255,255,.93);border-top:2px solid #000;
    padding:6px 12px 8px;display:flex;align-items:center;gap:12px;}}
  #ctl input[type=range]{{flex:1;height:22px;}}
  #lbl{{font:bold 16px monospace;min-width:190px;text-align:right;}}
  #st{{font:12px monospace;color:#333;min-width:120px;}}
  #tip{{position:absolute;pointer-events:none;z-index:6;
    background:#fff;border:1px solid #000;padding:3px 6px;
    font:12px monospace;display:none;white-space:nowrap;}}
  .maplibregl-ctrl-bottom-right{{bottom:44px;}}
  #play{{font:bold 13px monospace;border:2px solid #000;
    background:#C0C0C0;padding:3px 10px;cursor:pointer;}}
</style></head><body>
<div id="wrap"><div id="map"></div>
<div id="tip"></div>
<div id="ctl">
  <button id="play">&#9654; loop</button>
  <input type="range" id="sl" min="0" max="{max(0, len(frames) - 1)}"
         value="{max(0, len(frames) - 1)}" step="1">
  <span id="lbl"></span><span id="st"></span>
</div></div>
<script>
const FR={F}, G={G}, V={V}, OP={float(opacity)};
const {{MapboxOverlay,BitmapLayer,PolygonLayer,PathLayer,TextLayer,IconLayer,
       ScatterplotLayer}}=deck;

// PRELOAD EVERY FRAME. This is what makes the scrub instant: after
// this the swap is a texture already in GPU memory. The status text
// counts down so a slow first load reads as loading, not broken.
let loaded=0; const st=document.getElementById('st');
FR.forEach(f=>{{const im=new Image(); im.crossOrigin='anonymous';
  im.onload=im.onerror=()=>{{loaded++;
    st.textContent=loaded<FR.length?`preloading ${{loaded}}/${{FR.length}}`
                                   :`${{FR.length}} frames ready`;}};
  im.src=f.url;}});

// THE SAME BASEMAP AS PAGE 1, by the same route. pydeck does not
// fetch raster tiles; it hands CARTO's Positron GL STYLE to MapLibre,
// and that endpoint is not gated the way basemaps.cartocdn.com raster
// tiles are. deck.gl then sits on the MapLibre map as an overlay.
// This is exactly pydeck's own architecture, done by hand.
const map=new maplibregl.Map({{container:'map',
  style:'{style_url}',
  center:[V.lon,V.lat],zoom:V.zoom,pitch:0,bearing:0,
  attributionControl:true}});

function frameLayer(i){{const f=FR[i]; if(!f) return null;
  return new BitmapLayer({{id:'cam',data:null,image:f.url,
    bounds:f.bounds,opacity:OP,
    // stable id + changing image: deck.gl reuses the layer and only
    // uploads the texture it has not seen, which is none after the
    // preload above.
    updateTriggers:{{image:f.url}}}});}}

function staticLayers(){{const L=[];
  if(G.artcc&&G.artcc.length) L.push(new PolygonLayer({{id:'artcc',
    data:G.artcc,getPolygon:d=>d.polygon,stroked:true,filled:false,
    getLineColor:[110,110,110,170],lineWidthMinPixels:1,
    pickable:true}}));
  if(G.oceanic&&G.oceanic.length) L.push(new PathLayer({{id:'oceanic',
    data:G.oceanic,getPath:d=>d.path,getColor:d=>d.color,
    widthMinPixels:1,getWidth:2,widthUnits:'pixels',pickable:true}}));
  if(G.oceanic_labels&&G.oceanic_labels.length) L.push(new TextLayer({{
    id:'oclbl',data:G.oceanic_labels,getPosition:d=>[d.lon,d.lat],
    getText:d=>d.ident,getSize:10,getColor:d=>d.color,
    getAngle:d=>d.angle,background:true,
    getBackgroundColor:[255,255,255,205],backgroundPadding:[3,1,3,1]}}));
  if(G.routes&&G.routes.length) L.push(new PathLayer({{id:'routes',
    data:G.routes,getPath:d=>d.path,getColor:d=>d.color,
    widthMinPixels:1,getWidth:2,widthUnits:'pixels',pickable:true}}));
  if(G.route_labels&&G.route_labels.length) L.push(new TextLayer({{
    id:'rlbl',data:G.route_labels,getPosition:d=>[d.lon,d.lat],
    getText:d=>d.ident,getSize:10,getColor:d=>d.color,
    getAngle:d=>d.angle,background:true,
    getBackgroundColor:[255,255,255,205],backgroundPadding:[3,1,3,1]}}));
  if(G.classb&&G.classb.length) L.push(new PolygonLayer({{id:'cb',
    data:G.classb,getPolygon:d=>d.polygon,stroked:true,filled:false,
    getLineColor:[0,90,200,190],lineWidthMinPixels:1,getLineWidth:2,
    pickable:true}}));
  if(G.hull&&G.hull.length) L.push(new PolygonLayer({{id:'hull',
    data:G.hull,getPolygon:d=>d.polygon,stroked:true,filled:false,
    getLineColor:[201,122,45,220],lineWidthMinPixels:2,getLineWidth:3,
    pickable:true}}));
  if(G.fixes&&G.fixes.length){{
    // Sized in metres, clamped in pixels: shrinks zoomed out, grows
    // zoomed in, per frame, same numbers as page 1.
    // Chart glyphs (waypoint star, VOR/DME) as tinted mask icons when
    // the row carries one; the old triangle otherwise.
    L.push(new IconLayer({{id:'fixtri',data:G.fixes.filter(d=>d.nav),
      getPosition:d=>[d.lon,d.lat],
      getIcon:d=>({{url:d.nav.url,width:64,height:64,anchorX:32,anchorY:32,mask:true}}),
      getColor:d=>d.tcolor,getSize:d=>d.nsize||5200,sizeUnits:'meters',
      sizeMinPixels:10,sizeMaxPixels:26,pickable:true}}));
    L.push(new TextLayer({{id:'fixtri0',data:G.fixes.filter(d=>!d.nav),
      getPosition:d=>[d.lon,d.lat],getText:()=>'\\u25b2',getSize:2500,
      sizeUnits:'meters',sizeMinPixels:5,sizeMaxPixels:14,
      getColor:d=>d.tcolor,pickable:true}}));
    L.push(new TextLayer({{id:'fixlbl',data:G.fix_labels||G.fixes,
      getPosition:d=>[d.lon,d.lat],getText:d=>d.name,getSize:1800,
      sizeUnits:'meters',sizeMinPixels:7,sizeMaxPixels:11,
      getColor:d=>d.lcolor,getTextAnchor:'start',getPixelOffset:[7,-7],
      background:true,getBackgroundColor:d=>d.chip,
      backgroundPadding:[5,2,5,2]}}));}}
  if(G.aircraft&&G.aircraft.length){{
    // STARS targets: a white square icon tinted per row (mask), a
    // leader baked into the JetBlue/major variant, and a two-line
    // data block at the leader tip. Others are the square alone.
    L.push(new IconLayer({{id:'ac',data:G.aircraft,
      getPosition:d=>[d.lon,d.lat],
      getIcon:d=>({{url:d.atc,width:64,height:64,anchorX:32,anchorY:32,mask:true}}),
      getColor:d=>d.col||[0,90,220,235],getSize:26,sizeUnits:'pixels',
      sizeMinPixels:18,sizeMaxPixels:32,pickable:true}}));
    L.push(new TextLayer({{id:'aclbl',
      data:G.aircraft.filter(d=>d.lbl!==undefined?d.lbl:d.cs),
      getPosition:d=>[d.lon,d.lat],getText:d=>d.lbl||d.cs,getSize:11,
      fontFamily:'monospace',fontWeight:700,lineHeight:1.1,
      getColor:d=>d.col||[0,40,120],getAlignmentBaseline:'bottom',
      getTextAnchor:'start',getPixelOffset:[13,-11],
      background:true,getBackgroundColor:[255,255,255,225],
      backgroundPadding:[3,1,3,1]}}));}}
  return L;}}
const STATIC=staticLayers();

const tip=document.getElementById('tip');
// Forecast field UNDER every vector layer, exactly as on page 1: an
// airway buried under a storm cell is a line nobody sees.
const ov=new MapboxOverlay({{interleaved:false,
  layers:[frameLayer(FR.length-1),...STATIC].filter(Boolean),
  onHover:({{object,x,y}})=>{{if(object&&object.tip){{
    tip.style.display='block';tip.style.left=(x+12)+'px';
    tip.style.top=(y+12)+'px';tip.innerHTML=object.tip;}}
    else tip.style.display='none';}}}});
map.addControl(ov);

const sl=document.getElementById('sl'),lbl=document.getElementById('lbl');
function show(i){{const f=FR[i]; if(!f) return;
  lbl.textContent=f.label;
  ov.setProps({{layers:[frameLayer(i),...STATIC].filter(Boolean)}});}}
sl.addEventListener('input',()=>show(+sl.value));
show(+sl.value);

// Loop: advance every 350 ms, wrap at the end. Same textures, so it
// costs nothing beyond the draw.
let timer=null; const pb=document.getElementById('play');
pb.addEventListener('click',()=>{{
  if(timer){{clearInterval(timer);timer=null;pb.innerHTML='&#9654; loop';return;}}
  pb.innerHTML='&#9632; stop';
  timer=setInterval(()=>{{let i=(+sl.value+1)%FR.length;sl.value=i;show(i);}},350);}});
</script></body></html>"""
