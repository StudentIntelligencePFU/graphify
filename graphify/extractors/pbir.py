"""Power BI Enhanced Report (PBIR) extractor for graphify.

Extracts hierarchical structure from PBIR report definitions:
  Report -> contains -> Page -> contains -> Visual -> displays_column / displays_measure / filters_by_column
  Visual (actionButton) -> navigates_to -> Page
  Report -> filters_by_column -> Table.Column  (from report.json filterConfig)
  Report -> targets_semantic_model -> SemanticModel  (re-emitted from definition.pbir)

Files handled:
  - report.json:  report-level filters + semantic model reference
  - page.json:    page node with displayName
  - pages.json:   page ordering metadata (page_order edges)
  - visual.json:  visual node with data field references and navigation links
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id


def _read_text_safe(path: Path) -> str:
    """Read text handling Windows extended-length long paths (>260 chars)."""
    import os
    p_str = str(path)
    if os.name == "nt" and not p_str.startswith("\\\\?\\"):
        try:
            abs_p = os.path.abspath(p_str)
            p_str = "\\\\?\\UNC\\" + abs_p[2:] if abs_p.startswith("\\\\") else "\\\\?\\" + abs_p
        except Exception:
            pass
    with open(p_str, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_pbir(path: Path, content: str | bytes | None = None) -> dict[str, Any]:
    """Extract metadata and connection edges from Power BI PBIR json files.

    Dispatched by _is_pbir_json() in extract.py for any .json file
    inside a *.Report/definition/ directory tree.
    """
    try:
        if content is None:
            raw_text = _read_text_safe(path)
        elif isinstance(content, bytes):
            raw_text = content.decode("utf-8", errors="replace")
        else:
            raw_text = content
        data = json.loads(raw_text)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    str_path = str(path)
    file_nid = _make_id(str_path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "file_type": "code",
        "source_file": str_path,
        "source_location": None,
    }]
    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = {file_nid}

    def _add_node(nid: str, label: str, line: int = 1, file_type: str = "code") -> str:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": file_type,
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        return nid

    def _add_edge(src: str, tgt: str, relation: str, line: int = 1, context: str | None = None) -> None:
        if not src or not tgt or src == tgt:
            return
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    def _ref_stub(name: str) -> str:
        """Create a sourceless global stub node that will merge across all files."""
        nid = _make_id(name)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "type": "namespace",
            })
        return nid

    # ── Find the report name from the parent directory ending with .Report ──
    report_name = ""
    report_dir: Path | None = None
    for p in path.parents:
        if p.name.endswith(".Report"):
            report_name = p.name.replace(".Report", "")
            report_dir = p
            break

    if not report_name:
        return {"nodes": nodes, "edges": edges}

    # Report node is a SHARED stub across all files in this report
    report_nid = _ref_stub(f"Report: {report_name}")
    filename = path.name.lower()

    # ── 1. report.json: report-level filters + semantic model reference ──
    if filename == "report.json":
        _add_edge(file_nid, report_nid, "contains")
        _extract_column_refs(data.get("filterConfig", {}), report_nid, "filters_by_column", _ref_stub, _add_edge)

        # Re-emit datasetReference from definition.pbir (sibling of definition/)
        if report_dir is not None:
            _emit_semantic_model_ref(report_dir / "definition.pbir", report_nid, _ref_stub, _add_edge)

    # ── 2. page.json: page node ──
    elif filename == "page.json":
        display_name = data.get("displayName", path.parent.name)
        page_nid = _ref_stub(f"Page: {report_name} / {display_name}")
        _add_edge(file_nid, page_nid, "contains")
        _add_edge(report_nid, page_nid, "contains")

        # Emit page metadata as context
        page_type = data.get("type", "")
        visibility = data.get("visibility", "")
        if page_type or visibility:
            ctx_parts = []
            if page_type:
                ctx_parts.append(f"type={page_type}")
            if visibility:
                ctx_parts.append(f"visibility={visibility}")
            edges[-1]["context"] = ";".join(ctx_parts)

    # ── 3. pages.json: page ordering metadata ──
    elif filename == "pages.json":
        page_order = data.get("pageOrder", [])
        pages_dir = path.parent  # .../definition/pages/
        prev_page_nid: str | None = None

        for page_hash in page_order:
            display_name = page_hash
            page_json = pages_dir / page_hash / "page.json"
            if page_json.exists():
                try:
                    pg = json.loads(_read_text_safe(page_json))
                    display_name = pg.get("displayName", page_hash)
                except Exception:
                    pass

            pg_nid = _ref_stub(f"Page: {report_name} / {display_name}")
            _add_edge(report_nid, pg_nid, "contains")

            if prev_page_nid:
                _add_edge(prev_page_nid, pg_nid, "page_order")
            prev_page_nid = pg_nid

        active_page = data.get("activePageName", "")
        if active_page:
            active_display = active_page
            active_json = pages_dir / active_page / "page.json"
            if active_json.exists():
                try:
                    ap = json.loads(_read_text_safe(active_json))
                    active_display = ap.get("displayName", active_page)
                except Exception:
                    pass
            active_nid = _ref_stub(f"Page: {report_name} / {active_display}")
            _add_edge(report_nid, active_nid, "active_page")

    # ── 4. visual.json: visual node + data fields + navigation ──
    elif filename == "visual.json":
        visual_obj = data.get("visual", {})
        visual_type = visual_obj.get("visualType", "unknown")
        visual_id = path.parent.name
        title = _get_visual_title(data) or visual_id

        page_dir = path.parent.parent.parent
        display_name = _resolve_page_display_name(page_dir)

        # Page is a SHARED stub across all visuals on this page
        page_nid = _ref_stub(f"Page: {report_name} / {display_name}")
        
        # Visual is unique to this file
        visual_nid = _add_node(_make_id("Visual:", report_name, display_name, visual_id), f"Visual: {visual_type} - {title}")
        _add_edge(file_nid, visual_nid, "contains")
        _add_edge(page_nid, visual_nid, "contains")

        # Extract column/measure references from queryState
        is_slicer = (visual_type == "slicer")
        col_relation = "filters_by_column" if is_slicer else "displays_column"
        query_state = visual_obj.get("query", {}).get("queryState", {})
        _extract_column_refs(query_state, visual_nid, col_relation, _ref_stub, _add_edge, extract_measures=True)

        # Extract column refs from sort definitions
        sort_def = visual_obj.get("query", {}).get("sortDefinition", {})
        _extract_column_refs(sort_def, visual_nid, "sorts_by", _ref_stub, _add_edge, extract_measures=True)

        # Extract column refs from visual objects (conditional formatting, images, etc.)
        visual_objects = visual_obj.get("objects", {})
        _extract_column_refs(visual_objects, visual_nid, "references_column", _ref_stub, _add_edge, extract_measures=True)

        # Extract page navigation from action buttons
        _extract_navigation(data, visual_nid, report_name, _ref_stub, _add_edge, page_dir.parent)

    return {"nodes": nodes, "edges": edges}


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_page_display_name(page_dir: Path) -> str:
    """Read page.json from a page directory to get displayName, fallback to dir name."""
    display_name = page_dir.name
    page_json_path = page_dir / "page.json"
    if page_json_path.exists():
        try:
            page_data = json.loads(_read_text_safe(page_json_path))
            display_name = page_data.get("displayName", display_name)
        except Exception:
            pass
    return display_name


def _get_visual_title(data: dict[str, Any]) -> str:
    """Extract custom title from visual objects, if present."""
    try:
        title_objs = data.get("visual", {}).get("objects", {}).get("title", [])
        if title_objs:
            title_prop = title_objs[0].get("properties", {}).get("text", {})
            if "expr" in title_prop:
                return title_prop["expr"].get("Literal", {}).get("Value", "").strip("'")
    except Exception:
        pass
    return ""


def _extract_column_refs(
    obj: Any,
    parent_nid: str,
    default_column_relation: str,
    _ref_stub,
    _add_edge,
    *,
    extract_measures: bool = False,
    _seen: set | None = None,
) -> None:
    """Recursively walk a JSON subtree extracting Column and Measure references."""
    if _seen is None:
        _seen = set()

    if isinstance(obj, dict):
        if "Column" in obj:
            col = obj["Column"]
            if isinstance(col, dict):
                expr = col.get("Expression", {})
                source_ref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                entity = source_ref.get("Entity") if isinstance(source_ref, dict) else None
                prop = col.get("Property")
                if entity and prop:
                    ref_key = f"col:{entity}[{prop}]"
                    if ref_key not in _seen:
                        _seen.add(ref_key)
                        stub = _ref_stub(f"{entity}[{prop}]")
                        _add_edge(parent_nid, stub, default_column_relation)

        if extract_measures and "Measure" in obj:
            meas = obj["Measure"]
            if isinstance(meas, dict):
                expr = meas.get("Expression", {})
                source_ref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                entity = source_ref.get("Entity") if isinstance(source_ref, dict) else None
                prop = meas.get("Property")
                if entity and prop:
                    ref_key = f"meas:{entity}[{prop}]"
                    if ref_key not in _seen:
                        _seen.add(ref_key)
                        stub = _ref_stub(f"{entity}[{prop}]")
                        _add_edge(parent_nid, stub, "displays_measure")

        for v in obj.values():
            _extract_column_refs(v, parent_nid, default_column_relation, _ref_stub, _add_edge,
                                 extract_measures=extract_measures, _seen=_seen)

    elif isinstance(obj, list):
        for item in obj:
            _extract_column_refs(item, parent_nid, default_column_relation, _ref_stub, _add_edge,
                                 extract_measures=extract_measures, _seen=_seen)


def _emit_semantic_model_ref(pbir_path: Path, report_nid: str, _ref_stub, _add_edge) -> None:
    """Read definition.pbir and emit Report -> references -> SemanticModel edge."""
    try:
        if not pbir_path.exists():
            return
        pbir_content = _read_text_safe(pbir_path)
        pbir_data = json.loads(pbir_content)
        ds_ref = pbir_data.get("datasetReference", {})

        # byConnection
        by_conn = ds_ref.get("byConnection", {})
        if by_conn:
            conn_str = by_conn.get("connectionString", "")
            catalog_m = re.search(r'initial catalog="([^"]+)"', conn_str, re.IGNORECASE)
            model_id_m = re.search(r"semanticmodelid=([a-f0-9\-]+)", conn_str, re.IGNORECASE)
            if catalog_m:
                model_name = catalog_m.group(1)
                model_stub = _ref_stub(f"SemanticModel: {model_name}")
                _add_edge(report_nid, model_stub, "targets_semantic_model", context="datasetReference")
            elif model_id_m:
                model_stub = _ref_stub(f"SemanticModel:{model_id_m.group(1)}")
                _add_edge(report_nid, model_stub, "targets_semantic_model", context="datasetReference")

        # byPath
        by_path = ds_ref.get("byPath", {})
        if by_path and "path" in by_path:
            model_stub = _ref_stub(f"SemanticModel: {by_path['path']}")
            _add_edge(report_nid, model_stub, "targets_semantic_model", context="datasetReferencePath")
    except Exception:
        pass


def _extract_navigation(
    data: dict[str, Any],
    visual_nid: str,
    report_name: str,
    _ref_stub,
    _add_edge,
    pages_dir: Path,
) -> None:
    """Extract page navigation links from actionButton visuals."""
    try:
        links = data.get("visual", {}).get("visualContainerObjects", {}).get("visualLink", [])
        for link in links:
            props = link.get("properties", {})
            nav_type = props.get("type", {}).get("expr", {}).get("Literal", {}).get("Value", "").strip("'")
            if nav_type != "PageNavigation":
                continue

            nav_expr = props.get("navigationSection", {}).get("expr", {})

            literal_val = nav_expr.get("Literal", {}).get("Value", "").strip("'")
            if literal_val:
                display_name = literal_val
                dest_page_json = pages_dir / literal_val / "page.json"
                if dest_page_json.exists():
                    try:
                        p_data = json.loads(_read_text_safe(dest_page_json))
                        display_name = p_data.get("displayName", literal_val)
                    except Exception:
                        pass
                target_id = _ref_stub(f"Page: {report_name} / {display_name}")
                _add_edge(visual_nid, target_id, "navigates_to")
                continue

            tooltip_val = props.get("tooltip", {}).get("expr", {}).get("Literal", {}).get("Value", "").strip("'")
            if tooltip_val:
                target_id = _ref_stub(f"Page: {report_name} / {tooltip_val}")
                _add_edge(visual_nid, target_id, "navigates_to", context="dynamic_navigation")
    except Exception:
        pass
