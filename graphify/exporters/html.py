"""html — moved verbatim from graphify/export.py."""
from __future__ import annotations

from graphify.exporters.base import COMMUNITY_COLORS  # noqa: E402,F401
from pathlib import Path
import html as _html
from graphify.analyze import _node_community_map
from graphify.paths import write_text_atomic
import json
import networkx as nx
from graphify.security import sanitize_label


MAX_NODES_FOR_VIZ = 5_000
_HTML_STALE_MARKER = ".graph.html.stale"

def _viz_node_limit() -> int:
    """Return the effective viz node limit, honoring GRAPHIFY_VIZ_NODE_LIMIT env var.

    Falls back to MAX_NODES_FOR_VIZ when the env var is unset, empty, or non-integer.
    Set to 0 to disable HTML viz unconditionally (useful for CI runners).
    """
    import os
    raw = os.environ.get("GRAPHIFY_VIZ_NODE_LIMIT")
    if raw is None or not raw.strip():
        return MAX_NODES_FOR_VIZ
    try:
        return int(raw)
    except ValueError:
        return MAX_NODES_FOR_VIZ

def _html_styles() -> str:
    return """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; height: 100vh; overflow: hidden; }
  #graph { flex: 1; }
  #sidebar { width: 280px; background: #1a1a2e; border-left: 1px solid #2a2a4e; display: flex; flex-direction: column; overflow: hidden; }
  #search-wrap { padding: 12px; border-bottom: 1px solid #2a2a4e; }
  #search { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 7px 10px; border-radius: 6px; font-size: 13px; outline: none; }
  #search:focus { border-color: #4E79A7; }
  #search-results { max-height: 140px; overflow-y: auto; padding: 4px 12px; border-bottom: 1px solid #2a2a4e; display: none; }
  .search-item { padding: 4px 6px; cursor: pointer; border-radius: 4px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .search-item:hover { background: #2a2a4e; }
  #info-panel { padding: 14px; border-bottom: 1px solid #2a2a4e; min-height: 140px; }
  #info-panel h3 { font-size: 13px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  #info-content { font-size: 13px; color: #ccc; line-height: 1.6; }
  #info-content .field { margin-bottom: 5px; }
  #info-content .field b { color: #e0e0e0; }
  #info-content .empty { color: #555; font-style: italic; }
  .neighbor-link { display: block; padding: 2px 6px; margin: 2px 0; border-radius: 3px; cursor: pointer; border-left: 3px solid #333; }
  .neighbor-link:hover { background: #2a2a4e; }
  .neighbor-link > div { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #neighbors-list { max-height: 160px; overflow-y: auto; margin-top: 4px; }
  #legend-wrap { flex: 1; overflow-y: auto; padding: 12px; }
  #legend-wrap h3 { font-size: 13px; color: #aaa; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
  .legend-item { display: flex; align-items: center; gap: 8px; padding: 4px 0; cursor: pointer; border-radius: 4px; font-size: 12px; }
  .legend-item:hover { background: #2a2a4e; padding-left: 4px; }
  .legend-item.dimmed { opacity: 0.35; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .legend-count { color: #666; font-size: 11px; }
  #stats { padding: 10px 14px; border-top: 1px solid #2a2a4e; font-size: 11px; color: #555; }
  #legend-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; padding: 4px 0; }
  #legend-controls label { display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 12px; color: #aaa; user-select: none; }
  #legend-controls label:hover { color: #e0e0e0; }
  .legend-cb, #select-all-cb { appearance: none; -webkit-appearance: none; width: 14px; height: 14px; border: 1.5px solid #3a3a5e; border-radius: 3px; background: #0f0f1a; cursor: pointer; position: relative; flex-shrink: 0; }
  .legend-cb:checked, #select-all-cb:checked { background: #4E79A7; border-color: #4E79A7; }
  .legend-cb:checked::after, #select-all-cb:checked::after { content: ''; position: absolute; left: 3.5px; top: 1px; width: 4px; height: 7px; border: solid #fff; border-width: 0 2px 2px 0; transform: rotate(45deg); }
  #select-all-cb:indeterminate { background: #4E79A7; border-color: #4E79A7; }
  #select-all-cb:indeterminate::after { content: ''; position: absolute; left: 2px; top: 5px; width: 8px; height: 2px; background: #fff; border: none; transform: none; }
  #patterns-wrap { padding: 12px; border-bottom: 1px solid #2a2a4e; }
  #patterns-wrap h3 { font-size: 13px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  .pattern-btn { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; background: #0f0f1a; border: 1px solid #3a3a5e; color: #ccc; padding: 7px 10px; border-radius: 6px; font-size: 12.5px; cursor: pointer; margin-bottom: 6px; transition: background .15s, border-color .15s; }
  .pattern-btn:hover { background: #2a2a4e; border-color: #4E79A7; }
  .pattern-btn.active { background: #24314f; border-color: #4E79A7; color: #fff; }
  .pattern-btn .pb-icon { flex-shrink: 0; }
  .pattern-btn .pb-count { margin-left: auto; color: #666; font-size: 11px; }
  #pattern-reset { background: transparent; border-style: dashed; color: #888; }
  #subgroup-select { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 6px 8px; border-radius: 6px; font-size: 12.5px; outline: none; margin-bottom: 10px; }
  #subgroup-select:focus { border-color: #4E79A7; }
  .lineage-trigger { display: block; width: 100%; margin-top: 10px; background: #24314f; border: 1px solid #4E79A7; color: #fff; padding: 7px 10px; border-radius: 6px; font-size: 12.5px; cursor: pointer; }
  .lineage-trigger:hover { background: #2d3d63; }
  #lineage-wrap { display: none; flex: 1; flex-direction: column; overflow-y: auto; padding: 12px; border-top: 1px solid #2a2a4e; }
  #lineage-wrap h3 { font-size: 13px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  #lineage-wrap h3 span { color: #e0e0e0; text-transform: none; letter-spacing: normal; }
  #lineage-hint { font-size: 11px; color: #666; margin-bottom: 8px; line-height: 1.4; }
  #lineage-exit-btn { display: block; width: 100%; margin-bottom: 10px; background: transparent; border: 1px dashed #888; color: #ccc; padding: 6px 10px; border-radius: 6px; font-size: 12px; cursor: pointer; }
  #lineage-exit-btn:hover { border-color: #4E79A7; color: #fff; }
  .lineage-hop { display: flex; align-items: center; gap: 6px; padding: 4px 6px; margin: 2px 0; border-left: 3px solid #333; border-radius: 3px; cursor: pointer; }
  .lineage-hop:hover { background: #2a2a4e; }
  .lineage-hop-marker { flex-shrink: 0; color: #888; font-size: 10px; }
  .lineage-hop-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
  .lineage-hop-dist { flex-shrink: 0; color: #666; font-size: 10px; }
  #lineage-edges-title { margin-top: 10px; color: #aaa; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  .lineage-edge { padding: 4px 6px; margin: 2px 0; font-size: 11.5px; color: #bbb; border-radius: 3px; line-height: 1.5; }
  .lineage-edge.filter { background: rgba(245, 158, 11, 0.12); border-left: 3px solid #f59e0b; color: #ffd699; }
</style>"""

def _hyperedge_script(hyperedges_json: str) -> str:
    return f"""<script>
// Render hyperedges as shaded regions
const hyperedges = {hyperedges_json};
// afterDrawing passes ctx already transformed to network coordinate space.
// Draw node positions raw — no manual pan/zoom/DPR math needed.

// Andrew's monotone chain. Returns the hull in counter-clockwise order, which
// is what the perimeter must be traced in. Collinear and duplicate points
// collapse to the extremes, so degenerate member sets render as a segment
// rather than a zero-area crossed path.
function convexHull(pts) {{
    const p = pts.slice().sort((a, b) => (a.x - b.x) || (a.y - b.y));
    if (p.length < 3) return p;
    const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
    const build = seq => {{
        const out = [];
        for (const q of seq) {{
            while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], q) <= 0) out.pop();
            out.push(q);
        }}
        out.pop();
        return out;
    }};
    const hull = build(p).concat(build(p.slice().reverse()));
    return hull.length >= 3 ? hull : p;
}}
network.on('afterDrawing', function(ctx) {{
    hyperedges.forEach(h => {{
        const positions = h.nodes
            .map(nid => network.getPositions([nid])[nid])
            .filter(p => p !== undefined);
        if (positions.length < 2) return;
        ctx.save();
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = '#6366f1';
        ctx.strokeStyle = '#6366f1';
        ctx.lineWidth = 2;
        ctx.beginPath();
        // Centroid and expanded hull in network coordinates.
        // The perimeter must follow hull order, not h.nodes order: tracing the
        // raw member order self-intersects whenever the layout does not happen
        // to place members in angular order, filling as crossed wedges.
        const cx = positions.reduce((s, p) => s + p.x, 0) / positions.length;
        const cy = positions.reduce((s, p) => s + p.y, 0) / positions.length;
        const hull = convexHull(positions);
        const expanded = hull.map(p => ({{
            x: cx + (p.x - cx) * 1.15,
            y: cy + (p.y - cy) * 1.15
        }}));
        ctx.moveTo(expanded[0].x, expanded[0].y);
        expanded.slice(1).forEach(p => ctx.lineTo(p.x, p.y));
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 0.4;
        ctx.stroke();
        // Label
        ctx.globalAlpha = 0.8;
        ctx.fillStyle = '#4f46e5';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(h.label, cx, cy - 5);
        ctx.restore();
    }});
}});
</script>"""

def _html_script(nodes_json: str, edges_json: str, legend_json: str) -> str:
    return f"""<script>
const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const LEGEND = {legend_json};

// HTML-escape helper — prevents XSS when injecting graph data into innerHTML
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

// Build vis datasets
const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({{
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  _community: n.community, _community_name: n.community_name,
  _source_file: n.source_file, _file_type: n.file_type, _degree: n.degree,
}})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({{
  id: i, from: e.from, to: e.to,
  label: '',
  title: e.title,
  dashes: e.dashes,
  width: e.width,
  color: e.color,
  arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
  _relation: e.label, _context: e.context || '',
}})));

// Shared with the lineage view (_lineage_script), which re-enables physics
// briefly to settle newly-revealed nodes on each expansion (never a fixed
// hierarchical layout — that locks every node to a column, blocking free
// dragging) and must restore EXACTLY this config on exit — a second,
// drifted copy of these numbers would silently diverge from the graph's
// normal resting layout.
const DEFAULT_PHYSICS = {{
  enabled: true,
  solver: 'forceAtlas2Based',
  forceAtlas2Based: {{
    gravitationalConstant: -60,
    centralGravity: 0.005,
    springLength: 120,
    springConstant: 0.08,
    damping: 0.4,
    avoidOverlap: 0.8,
  }},
  stabilization: {{ iterations: 200, fit: true }},
}};

const container = document.getElementById('graph');
const network = new vis.Network(container, {{ nodes: nodesDS, edges: edgesDS }}, {{
  physics: DEFAULT_PHYSICS,
  interaction: {{
    hover: true,
    tooltipDelay: 100,
    hideEdgesOnDrag: true,
    navigationButtons: false,
    keyboard: false,
  }},
  nodes: {{ shape: 'dot', borderWidth: 1.5 }},
  edges: {{ smooth: {{ type: 'continuous', roundness: 0.2 }}, selectionWidth: 3 }},
}});

network.once('stabilizationIterationsDone', () => {{
  network.setOptions({{ physics: {{ enabled: false }} }});
}});

function showInfo(nodeId) {{
  const n = nodesDS.get(nodeId);
  if (!n) return;
  // Walk edges (not getConnectedNodes) so each hop carries WHY it exists —
  // the relation and, when the extractor recorded one, the context (a SQL
  // action name, an m_partition, a DAX expression). That is what turns
  // "51 nodes away from a Visual" into an actually-followable lineage trail
  // (e.g. a PowerAutomate flow's shared_sql read fusing into the same table
  // stub a semantic model partition reads, on into a report visual).
  const edgeIds = network.getConnectedEdges(nodeId);
  const neighborItems = edgeIds.map(eid => {{
    const e = edgesDS.get(eid);
    if (!e) return '';
    const nid = e.from === nodeId ? e.to : e.from;
    const dir = e.from === nodeId ? '->' : '<-';
    const nb = nodesDS.get(nid);
    const color = nb ? nb.color.background : '#555';
    const relLine = esc(dir) + ' ' + esc(e._relation || '') + (e._context ? ` - ${{esc(e._context)}}` : '');
    return `<span class="neighbor-link" style="border-left-color:${{esc(color)}}" data-nid="${{esc(nid)}}">` +
           `<div>${{esc(nb ? nb.label : nid)}}</div>` +
           `<div style="font-size:11px;color:#888">${{relLine}}</div></span>`;
  }}).join('');
  document.getElementById('info-content').innerHTML = `
    <div class="field"><b>${{esc(n.label)}}</b></div>
    <div class="field">Type: ${{esc(n._file_type || 'unknown')}}</div>
    <div class="field">Community: ${{esc(n._community_name)}}</div>
    <div class="field">Source: ${{esc(n._source_file || '-')}}</div>
    <div class="field">Degree: ${{n._degree}}</div>
    ${{edgeIds.length ? `<button class="lineage-trigger" data-nid="${{esc(nodeId)}}">&#128279; Trace lineage</button>` : ''}}
    ${{edgeIds.length ? `<div class="field" style="margin-top:8px;color:#aaa;font-size:11px">Neighbors (${{edgeIds.length}})</div><div id="neighbors-list">${{neighborItems}}</div>` : ''}}
  `;
}}

function focusNode(nodeId) {{
  network.focus(nodeId, {{ scale: 1.4, animation: true }});
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}}

// Neighbor links use a data attribute + one delegated listener rather than an
// inline onclick. A node id/label sourced from a document or a scraped URL
// (graphify add) can contain a double-quote; dropping the stringified id
// unescaped into a quoted onclick both broke every link and allowed a hostile
// source to inject an event handler into the local report (stored XSS, #1838).
// esc() on data-nid keeps the value inside the attribute; the listener reads it
// back verbatim. Bound to document so it survives the innerHTML rebuild that
// recreates #neighbors-list on each showInfo().
document.addEventListener('click', e => {{
  const el = e.target.closest('.neighbor-link');
  if (el && el.dataset.nid !== undefined) focusNode(el.dataset.nid);
}});

// Track hovered node — hover detection is more reliable than click params
let hoveredNodeId = null;
network.on('hoverNode', params => {{
  hoveredNodeId = params.node;
  container.style.cursor = 'pointer';
}});
network.on('blurNode', () => {{
  hoveredNodeId = null;
  container.style.cursor = 'default';
}});
container.addEventListener('click', () => {{
  if (hoveredNodeId !== null) {{
    showInfo(hoveredNodeId);
    network.selectNodes([hoveredNodeId]);
  }}
}});
network.on('click', params => {{
  if (params.nodes.length > 0) {{
    showInfo(params.nodes[0]);
  }} else if (hoveredNodeId === null) {{
    document.getElementById('info-content').innerHTML = '<span class="empty">Click a node to inspect it</span>';
  }}
}});

const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.toLowerCase().trim();
  searchResults.innerHTML = '';
  if (!q) {{ searchResults.style.display = 'none'; return; }}
  const matches = RAW_NODES.filter(n => n.label.toLowerCase().includes(q)).slice(0, 20);
  if (!matches.length) {{ searchResults.style.display = 'none'; return; }}
  searchResults.style.display = 'block';
  matches.forEach(n => {{
    const el = document.createElement('div');
    el.className = 'search-item';
    el.textContent = n.label;
    el.style.borderLeft = `3px solid ${{n.color.background}}`;
    el.style.paddingLeft = '8px';
    el.onclick = () => {{
      network.focus(n.id, {{ scale: 1.5, animation: true }});
      network.selectNodes([n.id]);
      showInfo(n.id);
      searchResults.style.display = 'none';
      searchInput.value = '';
    }};
    searchResults.appendChild(el);
  }});
}});
document.addEventListener('click', e => {{
  if (!searchResults.contains(e.target) && e.target !== searchInput)
    searchResults.style.display = 'none';
}});

const hiddenCommunities = new Set();

const selectAllCb = document.getElementById('select-all-cb');

function updateSelectAllState() {{
  const total = LEGEND.length;
  const hidden = hiddenCommunities.size;
  selectAllCb.checked = hidden === 0;
  selectAllCb.indeterminate = hidden > 0 && hidden < total;
}}

function toggleAllCommunities(hide) {{
  document.querySelectorAll('.legend-item').forEach(item => {{
    hide ? item.classList.add('dimmed') : item.classList.remove('dimmed');
  }});
  document.querySelectorAll('.legend-cb').forEach(cb => {{
    cb.checked = !hide;
  }});
  LEGEND.forEach(c => {{
    if (hide) hiddenCommunities.add(c.cid); else hiddenCommunities.delete(c.cid);
  }});
  const updates = RAW_NODES.map(n => ({{ id: n.id, hidden: hide }}));
  nodesDS.update(updates);
  updateSelectAllState();
}}

const legendEl = document.getElementById('legend');
LEGEND.forEach(c => {{
  const item = document.createElement('div');
  item.className = 'legend-item';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'legend-cb';
  cb.checked = true;
  cb.addEventListener('change', (e) => {{
    e.stopPropagation();
    if (cb.checked) {{
      hiddenCommunities.delete(c.cid);
      item.classList.remove('dimmed');
    }} else {{
      hiddenCommunities.add(c.cid);
      item.classList.add('dimmed');
    }}
    const updates = RAW_NODES
      .filter(n => n.community === c.cid)
      .map(n => ({{ id: n.id, hidden: !cb.checked }}));
    nodesDS.update(updates);
    updateSelectAllState();
  }});
  item.innerHTML = `<div class="legend-dot" style="background:${{c.color}}"></div>
    <span class="legend-label">${{c.label}}</span>
    <span class="legend-count">${{c.count}}</span>`;
  item.prepend(cb);
  item.onclick = (e) => {{
    if (e.target === cb) return;
    cb.checked = !cb.checked;
    cb.dispatchEvent(new Event('change'));
  }};
  legendEl.appendChild(item);
}});
</script>"""


def _patterns_script() -> str:
    """Two <script> blocks: preset node-selection patterns, and a searchable
    dropdown to jump straight to one community ("subgroup") by name.

    Both operate purely on RAW_NODES/RAW_EDGES/LEGEND/nodesDS/network —
    already declared as top-level const/function in the SAME html document by
    _html_script — and reuse RAW_NODES's own field names as-is (community,
    source_file, degree; no leading underscore — that prefix only exists on
    the SEPARATE nodesDS-mapped objects `_html_script` builds for the
    click-to-inspect panel).

    All five patterns are graph-generic (hub/isolated/bridge/stub/
    largest-community): none assume anything about what the code being
    graphed is — this exporter serves any language, any repo.

    Only meaningful for a true per-node render: skipped by the caller when
    ``member_counts`` is set (the aggregated community-meta-graph view, where
    each rendered "node" already IS a whole community — "isolated"/"stub"
    have no sensible per-meta-node meaning there, and every meta-node has an
    empty source_file by construction, which would make "stubs" wrongly
    select all of them — the same class of bug a from-undefined-field
    mismatch caused here during manual testing before this landed).
    """
    return """<script>
(function() {
  const byId = new Map(RAW_NODES.map(n => [n.id, n]));
  const neighborsOf = new Map();
  RAW_EDGES.forEach(e => {
    if (!neighborsOf.has(e.from)) neighborsOf.set(e.from, []);
    if (!neighborsOf.has(e.to)) neighborsOf.set(e.to, []);
    neighborsOf.get(e.from).push(e.to);
    neighborsOf.get(e.to).push(e.from);
  });

  function computeHubs() {
    const N = 40;
    return new Set(RAW_NODES.slice().sort((a, b) => b.degree - a.degree).slice(0, N).map(n => n.id));
  }
  function computeIsolated() {
    return new Set(RAW_NODES.filter(n => n.degree <= 1).map(n => n.id));
  }
  function computeBridges() {
    const out = new Set();
    RAW_NODES.forEach(n => {
      const nbs = neighborsOf.get(n.id) || [];
      const foreignComms = new Set();
      nbs.forEach(nid => {
        const nb = byId.get(nid);
        if (nb && nb.community !== n.community) foreignComms.add(nb.community);
      });
      if (foreignComms.size >= 2) out.add(n.id);
    });
    return out;
  }
  function computeStubs() {
    return new Set(RAW_NODES.filter(n => !n.source_file).map(n => n.id));
  }
  function computeLargestCommunity() {
    if (!LEGEND.length) return new Set();
    const top = LEGEND.reduce((a, b) => (b.count > a.count ? b : a));
    return new Set(RAW_NODES.filter(n => n.community === top.cid).map(n => n.id));
  }

  const PATTERNS = {
    hubs: computeHubs,
    isolated: computeIsolated,
    bridges: computeBridges,
    stubs: computeStubs,
    largest: computeLargestCommunity,
  };

  document.querySelectorAll('.pattern-btn[data-pattern]').forEach(btn => {
    const key = btn.dataset.pattern;
    btn.querySelector('.pb-count').textContent = PATTERNS[key]().size;
  });

  let activeBtn = null;

  function applyPattern(idSet, btn) {
    const updates = RAW_NODES.map(n => ({ id: n.id, hidden: !idSet.has(n.id) }));
    nodesDS.update(updates);
    document.querySelectorAll('.pattern-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    activeBtn = btn || null;
    // The community legend follows the pattern's result: a community shows
    // as visible when at least one of its nodes stayed visible.
    const visibleComms = new Set();
    RAW_NODES.forEach(n => { if (idSet.has(n.id)) visibleComms.add(n.community); });
    // legend-item carries no cid in the DOM: walked in the same order
    // _html_script built the legend from LEGEND.
    LEGEND.forEach((c, i) => {
      const item = legendEl.children[i];
      if (!item) return;
      const cb = item.querySelector('.legend-cb');
      const visible = visibleComms.has(c.cid);
      cb.checked = visible;
      item.classList.toggle('dimmed', !visible);
      if (visible) hiddenCommunities.delete(c.cid); else hiddenCommunities.add(c.cid);
    });
    updateSelectAllState();
    network.fit({ nodes: Array.from(idSet), animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
  }

  document.querySelectorAll('.pattern-btn[data-pattern]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (activeBtn === btn) { toggleAllCommunities(false); network.fit(); activeBtn = null; btn.classList.remove('active'); return; }
      applyPattern(PATTERNS[btn.dataset.pattern](), btn);
    });
  });

  document.getElementById('pattern-reset').addEventListener('click', () => {
    toggleAllCommunities(false);
    document.querySelectorAll('.pattern-btn').forEach(b => b.classList.remove('active'));
    activeBtn = null;
    network.fit();
  });

  // Exposed for the subgroup-select script below: let/const inside this
  // IIFE do NOT cross to another <script> tag (unlike _html_script's
  // top-level const), so window is the only way through.
  window.applyPattern = applyPattern;
  window.__clearActivePattern = function() {
    document.querySelectorAll('.pattern-btn').forEach(b => b.classList.remove('active'));
    activeBtn = null;
  };
})();
</script>
<script>
(function() {
  const sel = document.getElementById('subgroup-select');
  // LEGEND already carries {cid, color, label, count} per community, computed
  // in Python — alphabetical by label so the browser's native jump-to-letter
  // search on a focused <select> works as a name search.
  const options = LEGEND.slice().sort((a, b) => a.label.localeCompare(b.label));
  options.forEach(c => {
    const opt = document.createElement('option');
    opt.value = String(c.cid);
    opt.textContent = `${c.label} (${c.count})`;
    sel.appendChild(opt);
  });

  sel.addEventListener('change', () => {
    window.__clearActivePattern();
    if (!sel.value) { toggleAllCommunities(false); network.fit(); return; }
    const cid = Number(sel.value);
    const idSet = new Set(RAW_NODES.filter(n => n.community === cid).map(n => n.id));
    window.applyPattern(idSet, null);
  });

  document.querySelectorAll('.pattern-btn').forEach(btn => {
    btn.addEventListener('click', () => { sel.value = ''; });
  });
})();
</script>"""


def _lineage_script() -> str:
    """Trace-lineage panel: from a selected node, reveal its DIRECT (one-hop)
    upstream and downstream neighbors, instead of walking the whole
    transitive chain and rendering it all at once.

    An earlier version walked every direction to depth 15 and rendered the
    full transitive closure in one shot. On a real ETL/BI graph that is not a
    clean single chain — a table read by a dozen procedures, a report page
    reached by a dozen visuals — so one click could pull in hundreds of nodes
    across branches the user never asked to see, which is unreadable rather
    than useful (a full multi-hop trace exchanged one large graph for another,
    smaller but still uninterpretable, one). Expansion is now one hop at a
    time and user-driven: clicking "Trace lineage" on a node adds only ITS
    direct neighbors to the canvas; clicking the button again on one of those
    newly-revealed nodes extends the picture from there. The user decides
    which branch is worth following instead of being shown all of them.
    Positioning uses physics (`DEFAULT_PHYSICS`), not a fixed hierarchical
    layout: hierarchical mode locks each node to its computed column, so it
    can only be dragged along one axis — the opposite of letting the user
    freely rearrange a view they are actively building up click by click.

    Two things still make a full-graph render useless for answering "where
    does this data come from / end up, and what filters it along the way":
    (1) it is every OTHER node and edge in the corpus, competing for
    attention and physics-simulation time, and (2) `reads_from`/`writes_to`/
    `contains`/`displays_measure`-style edges already encode direction
    ("arrows follow the sense of the data", per the exporter's own
    edge-direction fix #563) — the chain is already *in* the graph, just
    buried under everything unrelated to it.

    Generic on purpose (no relation name is hardcoded to any one domain): the
    one-hop expansion follows every outgoing/incoming edge regardless of its
    `relation` label, since graphify serves any language/any repo and a
    lineage-shaped corpus (ETL/BI, build pipelines, data contracts) is only
    one case of many. The one exception is display: an edge whose relation
    name contains "filter" (e.g. a Power BI `filters_by_column` slicer/report
    filter) is highlighted amber in both the redrawn graph and the text list,
    since a filter applied partway down a chain silently changes what the
    destination node actually shows — exactly the kind of hop a plain
    neighbor list buries among ordinary data-movement edges.

    Rendering reuses the SAME `nodesDS`/`network` instance the main graph
    uses (hide-everything-but-the-visible-set, matching `applyPattern`'s
    technique) rather than standing up a second vis.Network: vis-network
    already drops edges whose endpoint node is hidden, so no separate
    edge-visibility bookkeeping is needed, and the existing legend/community
    sync in `applyPattern` comes for free when the patterns panel is present
    (skipped, with a manual fallback, in the aggregated community view where
    it isn't).
    """
    return """<script>
(function() {
  const byIdLineage = new Map(RAW_NODES.map(n => [n.id, n]));
  // Adjacency keyed by the EDGE'S OWN endpoint role: outAdj[x] lists edges
  // where x is the source (x's downstream neighbors), inAdj[x] lists edges
  // where x is the target (x's upstream neighbors). RAW_EDGES' from/to
  // already carry the true logical direction (restored via _src/_tgt by the
  // Python side), so no re-derivation here.
  const outAdj = new Map();
  const inAdj = new Map();
  RAW_EDGES.forEach((e, i) => {
    if (!outAdj.has(e.from)) outAdj.set(e.from, []);
    outAdj.get(e.from).push(i);
    if (!inAdj.has(e.to)) inAdj.set(e.to, []);
    inAdj.get(e.to).push(i);
  });

  function isFilterRelation(rel) {
    return /filter/i.test(rel || '');
  }

  function directNeighbors(id) {
    const ids = new Set();
    (outAdj.get(id) || []).forEach(ei => ids.add(RAW_EDGES[ei].to));
    (inAdj.get(id) || []).forEach(ei => ids.add(RAW_EDGES[ei].from));
    return ids;
  }

  let lineageActive = false;
  let lineageRoot = null;
  let visibleIds = new Set();
  let originalEdgeStyles = null;

  function restoreEdgeStyles() {
    if (originalEdgeStyles) {
      edgesDS.update(originalEdgeStyles);
      originalEdgeStyles = null;
    }
  }

  function exitLineage() {
    if (!lineageActive) return;
    lineageActive = false;
    lineageRoot = null;
    visibleIds = new Set();
    restoreEdgeStyles();
    if (window.__clearActivePattern) window.__clearActivePattern();
    toggleAllCommunities(false);
    network.setOptions({ physics: DEFAULT_PHYSICS, layout: { hierarchical: false } });
    network.fit({ animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
    document.getElementById('lineage-wrap').style.display = 'none';
    const patternsWrap = document.getElementById('patterns-wrap');
    if (patternsWrap) patternsWrap.style.display = '';
    document.getElementById('legend-wrap').style.display = '';
  }

  // Hop distance from the root, recomputed over just the currently-visible
  // set on every expansion (cheap: it stays small). Nodes added by expanding
  // from a DIFFERENT node than the root (the user followed a second branch)
  // may be unreachable from the root within the visible set alone — those
  // render with no distance rather than a wrong one.
  function distancesFromRoot() {
    const dist = new Map([[lineageRoot, 0]]);
    let frontier = [lineageRoot];
    while (frontier.length) {
      const next = [];
      frontier.forEach(nid => {
        (outAdj.get(nid) || []).forEach(ei => {
          const other = RAW_EDGES[ei].to;
          if (visibleIds.has(other) && !dist.has(other)) { dist.set(other, dist.get(nid) + 1); next.push(other); }
        });
        (inAdj.get(nid) || []).forEach(ei => {
          const other = RAW_EDGES[ei].from;
          if (visibleIds.has(other) && !dist.has(other)) { dist.set(other, dist.get(nid) - 1); next.push(other); }
        });
      });
      frontier = next;
    }
    return dist;
  }

  function renderLineagePanel() {
    const dist = distancesFromRoot();

    // The induced subgraph over the visible nodes — not just the edges that
    // caused an expansion — so a lateral edge between two visible nodes (two
    // sibling procedures both reading the same staging table) still renders.
    const inducedEdgeIdxs = [];
    RAW_EDGES.forEach((e, i) => {
      if (visibleIds.has(e.from) && visibleIds.has(e.to)) inducedEdgeIdxs.push(i);
    });

    restoreEdgeStyles();
    const styleUpdates = [];
    originalEdgeStyles = [];
    inducedEdgeIdxs.forEach(i => {
      if (!isFilterRelation(RAW_EDGES[i].label)) return;
      const cur = edgesDS.get(i);
      if (!cur) return;
      originalEdgeStyles.push({ id: i, color: cur.color, width: cur.width, dashes: cur.dashes });
      styleUpdates.push({ id: i, color: { color: '#f59e0b', opacity: 0.9 }, width: 3, dashes: [4, 3] });
    });
    if (styleUpdates.length) edgesDS.update(styleUpdates);

    document.getElementById('lineage-title').textContent = byIdLineage.get(lineageRoot).label;
    const ordered = Array.from(visibleIds)
      .map(nid => [nid, dist.has(nid) ? dist.get(nid) : null])
      .sort((a, b) => (a[1] ?? 0) - (b[1] ?? 0));
    document.getElementById('lineage-chain').innerHTML = ordered.map(([nid, d]) => {
      const n = byIdLineage.get(nid);
      if (!n) return '';
      const marker = nid === lineageRoot ? '&#9679;' : (d == null ? '&#8226;' : (d < 0 ? '&#8593;' : '&#8595;'));
      const distLabel = nid === lineageRoot ? 'selected'
        : d == null ? 'connected'
        : d < 0 ? `${-d} upstream` : `${d} downstream`;
      return `<div class="lineage-hop" data-nid="${esc(nid)}" style="border-left-color:${esc(n.color.background)}">` +
        `<span class="lineage-hop-marker">${marker}</span>` +
        `<span class="lineage-hop-label">${esc(n.label)}</span>` +
        `<span class="lineage-hop-dist">${esc(distLabel)}</span></div>`;
    }).join('');

    document.getElementById('lineage-edges').innerHTML = inducedEdgeIdxs.map(i => {
      const e = RAW_EDGES[i];
      const fromN = byIdLineage.get(e.from);
      const toN = byIdLineage.get(e.to);
      if (!fromN || !toN) return '';
      const filter = isFilterRelation(e.label);
      const ctx = e.context ? ` &mdash; ${esc(e.context)}` : '';
      return `<div class="lineage-edge${filter ? ' filter' : ''}">` +
        `${filter ? '&#128269; ' : ''}${esc(fromN.label)} <b>&rarr;${e.label ? ' ' + esc(e.label) : ''} &rarr;</b> ${esc(toN.label)}${ctx}</div>`;
    }).join('');
  }

  // Expand ONE hop from `id`: starts a fresh session (id + its direct
  // neighbors only) if none is active yet, otherwise adds id's direct
  // neighbors to whatever is already visible — the partial, click-driven
  // expansion this panel is built around.
  function traceLineage(id) {
    const node = byIdLineage.get(id);
    if (!node) return;

    if (!lineageActive) {
      lineageActive = true;
      lineageRoot = id;
      visibleIds = new Set([id]);
    }
    directNeighbors(id).forEach(nid => visibleIds.add(nid));

    if (window.applyPattern) {
      // Also syncs the community legend and does an initial fit.
      window.applyPattern(visibleIds, null);
    } else {
      nodesDS.update(RAW_NODES.map(n => ({ id: n.id, hidden: !visibleIds.has(n.id) })));
    }

    // Physics, not a fixed hierarchical layout: vis-network's hierarchical
    // mode locks each node to its computed level (its column, in LR
    // direction) — a node can only be dragged along the OTHER axis, not
    // repositioned freely, and re-running it on every expansion click would
    // re-lay out every visible node from scratch, discarding any manual
    // dragging the user just did to make room. Physics only nudges around
    // newly-revealed connections and settles once, leaving every node
    // completely free to drag afterward — nothing about the view is locked.
    network.setOptions({ physics: DEFAULT_PHYSICS, layout: { hierarchical: false } });
    network.once('stabilizationIterationsDone', () => network.setOptions({ physics: { enabled: false } }));
    setTimeout(() => network.fit({ nodes: Array.from(visibleIds), animation: { duration: 400, easingFunction: 'easeInOutQuad' } }), 60);

    const patternsWrap = document.getElementById('patterns-wrap');
    if (patternsWrap) patternsWrap.style.display = 'none';
    document.getElementById('legend-wrap').style.display = 'none';
    document.getElementById('lineage-wrap').style.display = 'flex';

    renderLineagePanel();
  }

  document.addEventListener('click', e => {
    const trigger = e.target.closest('.lineage-trigger');
    if (trigger && trigger.dataset.nid !== undefined) {
      traceLineage(trigger.dataset.nid);
      return;
    }
    const hop = e.target.closest('.lineage-hop');
    if (hop && hop.dataset.nid !== undefined) {
      focusNode(hop.dataset.nid);
      return;
    }
    if (e.target.closest('#lineage-exit-btn')) exitLineage();
  });
})();
</script>"""


def _html_document_title(output_path: str) -> str:
    """Return a portable label for the graph.html <title>.

    Tracked artifacts must not embed the generator host absolute path
    (regression of #433; reported again as #2598 on Windows). Keep from the
    configured output-dir bare name (``graphify-out`` / ``GRAPHIFY_OUT``
    basename) onward — portable in every case; otherwise fall back to a
    cwd-relative label, and finally the filename only.
    """
    from graphify.paths import GRAPHIFY_OUT_NAME

    raw = str(output_path).replace("\\", "/")
    # Drop Windows drive prefix so Path parts are comparable on any OS.
    if len(raw) >= 3 and raw[1] == ":" and raw[0].isalpha() and raw[2] == "/":
        raw = raw[2:]  # "/Users/..." style after drive strip
    p = Path(raw)

    parts = list(Path(raw).parts)
    # Path("C:/Users/..") on POSIX may keep "C:" as first part — strip it.
    if parts and len(parts[0]) == 2 and parts[0][1] == ":" and parts[0][0].isalpha():
        parts = parts[1:]
    # Prefer keeping from the output-dir marker onward: portable in every
    # case, whereas a cwd-relative path still leaks host/user segments when
    # the graph is built from a directory ABOVE the project (#2598 follow-up).
    marker = GRAPHIFY_OUT_NAME
    for i, part in enumerate(parts):
        if part == marker or part.startswith("graphify-out"):
            return "/".join(parts[i:])

    # No standard out-dir marker (fully custom output path): fall back to a
    # cwd-relative label when the target is under cwd, else the bare filename.
    try:
        resolved = p if p.is_absolute() else (Path.cwd() / p)
        rel = resolved.resolve().relative_to(Path.cwd().resolve())
        label = rel.as_posix()
        if label and label != ".":
            return label
    except (ValueError, OSError, RuntimeError):
        pass

    name = p.name
    return name if name else "graph.html"

def to_html(
    G: nx.Graph,
    communities: dict[int, list[str]],
    output_path: str,
    community_labels: dict[int, str] | None = None,
    member_counts: dict[int, int] | None = None,
    node_limit: int | None = None,
    learning_overlay: dict | None = None,
) -> bool:
    """Generate an interactive vis.js HTML visualization of the graph.

    Features: node size by degree, click-to-inspect panel, search box,
    community filter, physics clustering by community, confidence-styled edges.
    Raises ValueError if graph exceeds MAX_NODES_FOR_VIZ.

    If member_counts is provided (aggregated community view), node sizes are
    based on community member counts rather than graph degree.

    If node_limit is set and the graph exceeds it, automatically builds an
    aggregated community-level meta-graph instead of raising ValueError.

    Returns True when the output was written. Returns False when an aggregated
    view would contain fewer than two communities and is intentionally skipped.
    """
    limit = node_limit if node_limit is not None else _viz_node_limit()
    if G.number_of_nodes() > limit:
        if node_limit is not None:
            # Build aggregated community meta-graph
            from collections import Counter as _Counter
            import networkx as _nx
            print(f"Graph has {G.number_of_nodes()} nodes (above {limit} limit). Building aggregated community view...")
            node_to_community = {nid: cid for cid, members in communities.items() for nid in members}
            meta = _nx.Graph()
            for cid, members in communities.items():
                meta.add_node(str(cid), label=(community_labels or {}).get(cid, f"Community {cid}"))
            edge_counts = _Counter()
            for u, v in G.edges():
                cu, cv = node_to_community.get(u), node_to_community.get(v)
                if cu is not None and cv is not None and cu != cv:
                    edge_counts[(min(cu, cv), max(cu, cv))] += 1
            for (cu, cv), w in edge_counts.items():
                meta.add_edge(str(cu), str(cv), weight=w,
                              relation=f"{w} cross-community edges", confidence="AGGREGATED")
            if meta.number_of_nodes() <= 1:
                print("Single community - aggregated view not useful. Skipping graph.html.")
                return False
            meta_communities = {cid: [str(cid)] for cid in communities}
            mc = {cid: len(members) for cid, members in communities.items()}
            # Remap hyperedges from semantic node IDs to community IDs
            raw_hyperedges = G.graph.get("hyperedges", [])
            if raw_hyperedges:
                remapped = []
                for he in raw_hyperedges:
                    he_members = he.get("nodes", [])
                    comm_ids, seen = [], set()
                    for nid in he_members:
                        c = node_to_community.get(nid)
                        if c is None:
                            continue
                        s = str(c)
                        if s in seen:
                            continue
                        seen.add(s)
                        comm_ids.append(s)
                    if len(comm_ids) < 2:
                        continue
                    remapped.append({
                        "id": he.get("id", ""),
                        "label": he.get("label") or he.get("relation", "").replace("_", " "),
                        "nodes": comm_ids,
                    })
                meta.graph["hyperedges"] = remapped
            written = to_html(meta, meta_communities, output_path,
                              community_labels=community_labels, member_counts=mc)
            if not written:
                return False
            print(f"graph.html written (aggregated: {meta.number_of_nodes()} community nodes, {meta.number_of_edges()} cross-community edges)")
            print("Tip: run with --obsidian for full node-level detail.")
            return True
        raise ValueError(
            f"Graph has {G.number_of_nodes()} nodes - too large for HTML viz "
            f"(limit: {limit}). Use --no-viz, raise GRAPHIFY_VIZ_NODE_LIMIT, "
            f"or reduce input size."
        )

    node_community = _node_community_map(communities)
    degree = dict(G.degree())
    max_deg = max(degree.values(), default=1) or 1
    max_mc = (max(member_counts.values(), default=1) or 1) if member_counts else 1

    # Work-memory overlay (derived sidecar). When not passed explicitly, load it
    # best-effort from the sibling .graphify_learning.json next to the output
    # graph.html (which lives beside graph.json). Empty/missing => no learning
    # fields, so the un-annotated render is byte-identical to pre-feature.
    if learning_overlay is None:
        learning_overlay = {}
        try:
            from graphify.reflect import load_learning_overlay as _llo
            learning_overlay = _llo(Path(output_path))
        except Exception:
            learning_overlay = {}
    # Status -> ring color. preferred=green, contested=amber. Tentative gets no
    # ring (it's not yet trustworthy enough to highlight in the map).
    _RING = {"preferred": "#22c55e", "contested": "#f59e0b"}

    # Build nodes list for vis.js
    vis_nodes = []
    for node_id, data in G.nodes(data=True):
        cid = node_community.get(node_id, 0)
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        label = sanitize_label(data.get("label", node_id))
        deg = degree.get(node_id, 1)
        if member_counts:
            mc = member_counts.get(cid, 1)
            size = 10 + 30 * (mc / max_mc)
            font_size = 12
        else:
            size = 10 + 30 * (deg / max_deg)
            # Only show label for high-degree nodes by default; others show on hover
            font_size = 12 if deg >= max_deg * 0.15 else 0
        node = {
            "id": node_id,
            "label": label,
            "color": {"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
            "size": round(size, 1),
            "font": {"size": font_size, "color": "#ffffff"},
            "title": _html.escape(label),
            "community": cid,
            "community_name": sanitize_label((community_labels or {}).get(cid, f"Community {cid}")),
            "source_file": sanitize_label(str(data.get("source_file") or "")),
            "file_type": data.get("file_type", ""),
            "degree": deg,
        }
        # Conditional learning fields — only present for annotated nodes, so
        # un-annotated output keeps the exact pre-feature node dict shape.
        entry = learning_overlay.get(str(node_id)) if learning_overlay else None
        if entry:
            status = sanitize_label(str(entry.get("status", "")))
            stale = bool(entry.get("stale"))
            node["learning_status"] = status
            node["learning_stale"] = stale
            ring = _RING.get(status)
            if ring:
                # Status-colored ring via the border; stale => desaturated +
                # dashed (vis.js supports per-node `shapeProperties.borderDashes`).
                if stale:
                    ring = "#9ca3af"
                    node["shapeProperties"] = {"borderDashes": [4, 4]}
                node["borderWidth"] = 3
                node["color"] = {
                    "background": color, "border": ring,
                    "highlight": {"background": "#ffffff", "border": ring},
                }
            # Lesson line appended to the hover title.
            if status == "contested":
                lesson = f"Lesson: contested (useful {entry.get('uses', 0)} / dead-end {entry.get('neg', 0)})"
            elif status == "preferred":
                lesson = f"Lesson: preferred source ({entry.get('uses', 0)} useful, score={entry.get('score', 0)})"
            else:
                lesson = f"Lesson: {status} ({entry.get('uses', 0)} useful)"
            if stale:
                lesson += " [code changed — re-verify]"
            node["title"] = _html.escape(label) + "\n" + _html.escape(sanitize_label(lesson))
        vis_nodes.append(node)

    # Build edges list. Restore original edge direction from _src/_tgt
    # (stashed by build.py for exactly this reason): undirected NetworkX
    # canonicalizes endpoint order, which would otherwise flip the arrow
    # for `calls` and `rationale_for` in the rendered graph (#563).
    vis_edges = []
    for u, v, data in G.edges(data=True):
        confidence = data.get("confidence", "EXTRACTED")
        relation = data.get("relation", "")
        true_src = data.get("_src", u)
        true_tgt = data.get("_tgt", v)
        # `context` carries the specific WHY behind an edge (e.g. "shared_sql
        # action=General_Profile_SQL", an m_partition name) — without it, every
        # `reads_from` line looks the same and tracing a lineage path (a
        # PowerAutomate flow's SQL read fusing with a semantic model's table,
        # on into a report visual) means opening each source file by hand.
        context = sanitize_label(str(data.get("context") or ""))
        title = f"{relation} [{confidence}]"
        if context:
            title += f"\n{context}"
        vis_edges.append({
            "from": true_src,
            "to": true_tgt,
            "label": relation,
            "context": context,
            "title": _html.escape(title),
            "dashes": confidence != "EXTRACTED",
            "width": 2 if confidence == "EXTRACTED" else 1,
            "color": {"opacity": 0.7 if confidence == "EXTRACTED" else 0.35},
            "confidence": confidence,
        })

    # Build community legend data
    legend_data = []
    for cid in sorted((community_labels or {}).keys()):
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        lbl = _html.escape(sanitize_label((community_labels or {}).get(cid, f"Community {cid}")))
        n = member_counts.get(cid, len(communities.get(cid, []))) if member_counts else len(communities.get(cid, []))
        legend_data.append({"cid": cid, "color": color, "label": lbl, "count": n})

    # Escape </script> sequences so embedded JSON cannot break out of the script tag
    def _js_safe(obj) -> str:
        return json.dumps(obj).replace("</", "<\\/")

    nodes_json = _js_safe(vis_nodes)
    edges_json = _js_safe(vis_edges)
    legend_json = _js_safe(legend_data)
    hyperedges_json = _js_safe(getattr(G, "graph", {}).get("hyperedges", []))
    title = _html.escape(sanitize_label(_html_document_title(output_path)))
    stats = f"{G.number_of_nodes()} nodes &middot; {G.number_of_edges()} edges &middot; {len(communities)} communities"

    # Patterns panel + subgroup jump-to-community dropdown: only in the true
    # per-node render. In the aggregated community-meta-graph view (member_counts
    # set) each rendered "node" IS a whole community — hub/isolated/bridge/stub
    # do not carry the same meaning there (every meta-node has an empty
    # source_file by construction, which would make "stubs" wrongly select all
    # of them), so the whole panel is skipped rather than shipping a
    # misleading control. See _patterns_script's docstring.
    if member_counts:
        patterns_panel_html = ""
        subgroup_select_html = ""
        patterns_script_html = ""
    else:
        # Icons are HTML numeric character references, not raw emoji bytes:
        # to_html's contract is UTF-8 (write_text_atomic), but a consumer that
        # reads the output without pinning that encoding (e.g. Path.read_text()
        # on Windows, which falls back to the system codepage) chokes on raw
        # multi-byte UTF-8. Numeric refs keep the generated file pure ASCII —
        # the browser still renders the emoji — regardless of what the reader
        # assumes.
        patterns_panel_html = """  <div id="patterns-wrap">
    <h3>Patterns</h3>
    <button class="pattern-btn" data-pattern="hubs"><span class="pb-icon">&#x1F525;</span> Most connected <span class="pb-count"></span></button>
    <button class="pattern-btn" data-pattern="isolated"><span class="pb-icon">&#x1F3DD;&#xFE0F;</span> Isolated (degree &le; 1) <span class="pb-count"></span></button>
    <button class="pattern-btn" data-pattern="bridges"><span class="pb-icon">&#x1F309;</span> Bridges between communities <span class="pb-count"></span></button>
    <button class="pattern-btn" data-pattern="stubs"><span class="pb-icon">&#x2753;</span> Unresolved references <span class="pb-count"></span></button>
    <button class="pattern-btn" data-pattern="largest"><span class="pb-icon">&#x1F310;</span> Largest community <span class="pb-count"></span></button>
    <button class="pattern-btn" id="pattern-reset">&#x21BA; Show all</button>
  </div>
"""
        subgroup_select_html = """    <select id="subgroup-select">
      <option value="">&mdash; Jump to a subgroup &mdash;</option>
    </select>
"""
        patterns_script_html = _patterns_script()

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>graphify - {title}</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"
        integrity="sha384-Ux6phic9PEHJ38YtrijhkzyJ8yQlH8i/+buBR8s3mAZOJrP1gwyvAcIYl3GWtpX1"
        crossorigin="anonymous"></script>
{_html_styles()}
</head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search nodes..." autocomplete="off">
    <div id="search-results"></div>
  </div>
  <div id="info-panel">
    <h3>Node Info</h3>
    <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
  <div id="lineage-wrap">
    <h3>Lineage: <span id="lineage-title"></span></h3>
    <div id="lineage-hint">Direct connections only — click "Trace lineage" on another node below to expand further.</div>
    <button id="lineage-exit-btn">&#8630; Back to full graph</button>
    <div id="lineage-chain"></div>
    <div id="lineage-edges-title">Relations</div>
    <div id="lineage-edges"></div>
  </div>
{patterns_panel_html}  <div id="legend-wrap">
    <h3>Communities</h3>
    <div id="legend-controls">
      <label><input type="checkbox" id="select-all-cb" checked onchange="toggleAllCommunities(!this.checked)">Select All</label>
    </div>
{subgroup_select_html}    <div id="legend"></div>
  </div>
  <div id="stats">{stats}</div>
</div>
{_html_script(nodes_json, edges_json, legend_json)}
{_hyperedge_script(hyperedges_json)}
{patterns_script_html}
{_lineage_script()}
</body>
</html>"""

    write_text_atomic(output_path, html)
    return True
