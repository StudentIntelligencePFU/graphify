"""Fabric configuration and metadata extractor for Microsoft Fabric & Power BI artifacts.

Extracts structure and cross-item edges from:
- `.platform`: Fabric item metadata (Dataflow, SemanticModel, Report, Warehouse, Notebook).
- `.pbir`: Power BI Report definition pointers linking reports to semantic models (`datasetReference`).
- `.pbism`: Power BI Semantic Model definitions.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id

# Definition files that carry an item's actual logic, keyed by the `type` in
# .platform metadata. The .platform file only holds metadata, so without these
# edges the item node is a degree-1 orphan and its logic sits in a disconnected
# island — `neighbors("Dataflow: X")` returned nothing even though the sibling
# mashup.pq was fully extracted (#dataflow-island).
#
# Only files an extractor actually mints a node for belong here, or the edge
# dangles: a Dataflow's queryMetadata.json is deliberately absent because
# extract_json returns no nodes for it (it is column metadata, not logic).
_ITEM_DEFINITION_FILES: dict[str, tuple[str, ...]] = {
    "Dataflow": ("mashup.pq",),
    "Notebook": ("notebook-content.py",),
}


def _read_text_safe(path: Path) -> str:
    """Read text handling Windows extended-length long paths (>260 chars)."""
    p_str = str(path)
    if os.name == "nt" and not p_str.startswith("\\\\?\\"):
        try:
            abs_p = os.path.abspath(p_str)
            p_str = "\\\\?\\UNC\\" + abs_p[2:] if abs_p.startswith("\\\\") else "\\\\?\\" + abs_p
        except Exception:
            pass
    with open(p_str, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_fabric_config(path: Path, content: str | bytes | None = None) -> dict[str, Any]:
    """Extract metadata and connection edges from Fabric .platform, .pbir, and .pbism files."""
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

    stem = _file_stem(path)
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

    def _add_node(nid: str, label: str, line: int = 1, file_type: str = "code", origin_file: str | None = None) -> str:
        if nid not in seen_ids:
            seen_ids.add(nid)
            node_dict: dict[str, Any] = {
                "id": nid,
                "label": label,
                "file_type": file_type,
                "source_file": str_path if origin_file is None else "",
                "source_location": f"L{line}" if origin_file is None else "",
            }
            if origin_file:
                node_dict["origin_file"] = origin_file
            nodes.append(node_dict)
            if origin_file is None:
                edges.append({
                    "source": file_nid,
                    "target": nid,
                    "relation": "contains",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                    "weight": 1.0,
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

    filename = path.name.lower()
    suffix = path.suffix.lower()

    # 1. .platform file
    if suffix == ".platform" or filename == ".platform":
        meta = data.get("metadata", {})
        item_type = meta.get("type", "FabricItem")
        display_name = meta.get("displayName") or path.parent.name
        description = meta.get("description", "")
        item_label = f"{item_type}: {display_name}"

        item_nid = _add_node(_make_id(stem, display_name), item_label, 1)
        if description:
            # Also attach description context to edge
            edges[0]["context"] = f"description={description}"

        # Link the item to the sibling files holding its definition. The target id
        # is minted as _make_id(str(<path>)) — byte-for-byte what the owning
        # extractor (powerquery for mashup.pq) mints for its own file node — so
        # both endpoints go through the same key in extract()'s file-id remap and
        # land on the canonical repo-relative id together. Deriving it any other
        # way (e.g. via _file_stem) yields a different key and the edge dangles.
        for def_name in _ITEM_DEFINITION_FILES.get(item_type, ()):
            def_path = path.parent / def_name
            try:
                if not def_path.exists():
                    continue
            except OSError:
                continue
            _add_edge(item_nid, _make_id(str(def_path)), "contains", 1,
                      context="item_definition")

        return {"nodes": nodes, "edges": edges}

    # 2. .pbir file (Power BI Report definition)
    if suffix == ".pbir" or filename.endswith(".pbir"):
        report_name = path.parent.name.replace(".Report", "")
        report_nid = _ref_stub(f"Report: {report_name}")
        _add_edge(file_nid, report_nid, "contains", 1)

        ds_ref = data.get("datasetReference", {})
        # byConnection
        by_conn = ds_ref.get("byConnection", {})
        if by_conn:
            conn_str = by_conn.get("connectionString", "")
            catalog_m = re.search(r'initial catalog="([^"]+)"', conn_str, re.IGNORECASE)
            model_id_m = re.search(r"semanticmodelid=([a-f0-9\-]+)", conn_str, re.IGNORECASE)
            if catalog_m:
                model_name = catalog_m.group(1)
                model_stub = _ref_stub(f"SemanticModel: {model_name}")
                _add_edge(report_nid, model_stub, "targets_semantic_model", 1, context="datasetReference")
            elif model_id_m:
                model_stub = _ref_stub(f"SemanticModel:{model_id_m.group(1)}")
                _add_edge(report_nid, model_stub, "targets_semantic_model", 1, context="datasetReference")

        # byPath
        by_path = ds_ref.get("byPath", {})
        if by_path and "path" in by_path:
            target_path = by_path["path"]
            model_stub = _ref_stub(target_path)
            _add_edge(report_nid, model_stub, "targets_semantic_model", 1, context="datasetReferencePath")

        return {"nodes": nodes, "edges": edges}

    # 3. .pbism file (Power BI Semantic Model definition)
    if suffix == ".pbism" or filename.endswith(".pbism"):
        model_name = path.parent.name.replace(".SemanticModel", "")
        model_nid = _ref_stub(f"SemanticModel: {model_name}")
        _add_edge(file_nid, model_nid, "contains", 1)
        return {"nodes": nodes, "edges": edges}

    return {"nodes": nodes, "edges": edges}
