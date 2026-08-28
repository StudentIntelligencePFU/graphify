"""Power Query / M formula language extractor for Microsoft Fabric dataflows and Power BI.

Extracts sections, shared queries, ETL step pipelines, cross-query dependencies,
external data sources (Fabric Warehouse, SQL Database, Dataflows, SharePoint, Excel, Web),
transformations (NestedJoin, Combine, ExpandTableColumn), and DataDestinations.
"""
from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id


def _strip_m_quotes(name: str) -> str:
    """Clean M identifier: #"Query Name" -> Query Name, "Name" -> Name."""
    s = name.strip()
    if s.startswith('#"') and s.endswith('"') and len(s) >= 3:
        return s[2:-1].strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1].strip()
    return s


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


def extract_powerquery(path: Path, content: str | bytes | None = None) -> dict[str, Any]:
    """Extract queries, steps, data sources, destinations and relationships from .pq files."""
    try:
        if content is None:
            text = _read_text_safe(path)
        elif isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = content
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

    def _add_node(nid: str, label: str, line: int, file_type: str = "code", origin_file: str | None = None) -> str:
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

    def _add_edge(src: str, tgt: str, relation: str, line: int, context: str | None = None) -> None:
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
        """Create a sourceless global stub node for external/cross-file query or data source."""
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

    # Find section declaration if any
    sec_match = re.search(r"section\s+([\w\d]+);", text)
    if sec_match:
        sec_name = sec_match.group(1)
        _add_node(_make_id(stem, "section", sec_name), f"Section: {sec_name}", 1)

    # Extract all shared queries / definitions:
    # Pattern: shared QueryName = let ... in ...; or shared #"Query Name" = ...
    shared_pattern = re.compile(
        r"(?:^|\n)\s*shared\s+(#?\"?[^\s=;]+\"?)\s*=\s*(.*?)(?=\n\s*shared\s+|\n\s*\[DataDestinations|\Z)",
        re.DOTALL,
    )

    defined_queries: dict[str, str] = {}  # query_name_clean -> nid

    for m in shared_pattern.finditer(text):
        raw_name = m.group(1).strip()
        query_name = _strip_m_quotes(raw_name)
        query_body = m.group(2).strip()
        line_num = text[: m.start()].count("\n") + 1

        query_nid = _add_node(_make_id(stem, query_name), query_name, line_num)
        defined_queries[query_name.lower()] = query_nid

        # 1. Look for external connectors in query body
        # SQL Database
        for sm in re.finditer(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"', query_body, re.IGNORECASE):
            server, db = sm.group(1), sm.group(2)
            db_stub = _ref_stub(f"{server}/{db}")
            _add_edge(query_nid, db_stub, "reads_from", line_num, context="sql_database")

        # Fabric Warehouse navigation
        if "Fabric.Warehouse" in query_body:
            for wm in re.finditer(r'warehouseId\s*=\s*"([^"]+)"', query_body):
                wh_stub = _ref_stub(f"Warehouse:{wm.group(1)}")
                _add_edge(query_nid, wh_stub, "reads_from", line_num, context="fabric_warehouse")
            for tm in re.finditer(r'Item\s*=\s*"([^"]+)"', query_body):
                item_name = tm.group(1)
                item_stub = _ref_stub(item_name)
                _add_edge(query_nid, item_stub, "reads_from", line_num, context="warehouse_item")

        # PowerPlatform / Dataflows
        if "PowerPlatform.Dataflows" in query_body:
            for em in re.finditer(r'entity\s*=\s*"([^"]+)"', query_body):
                ent = em.group(1)
                ent_stub = _ref_stub(ent)
                _add_edge(query_nid, ent_stub, "reads_from", line_num, context="upstream_dataflow_entity")
            for df_m in re.finditer(r'dataflowId\s*=\s*"([^"]+)"', query_body):
                df_id = df_m.group(1)
                df_stub = _ref_stub(f"Dataflow:{df_id}")
                _add_edge(query_nid, df_stub, "reads_from", line_num, context="upstream_dataflow")

        # SharePoint / Web / Excel / CSV
        for sp_m in re.finditer(r'SharePoint\.\w+\(\s*"([^"]+)"', query_body):
            sp_stub = _ref_stub(sp_m.group(1))
            _add_edge(query_nid, sp_stub, "reads_from", line_num, context="sharepoint_source")
        for web_m in re.finditer(r'Web\.Contents\(\s*"([^"]+)"', query_body):
            web_stub = _ref_stub(web_m.group(1))
            _add_edge(query_nid, web_stub, "reads_from", line_num, context="web_source")

        # 2. Joins with other queries (Table.NestedJoin, Table.Join)
        for jm in re.finditer(r'Table\.(?:NestedJoin|Join)\s*\(\s*[^,]+,\s*\{[^}]+\}\s*,\s*(#?"?[^,"]+"?)\s*,', query_body):
            joined_query = _strip_m_quotes(jm.group(1))
            target_nid = defined_queries.get(joined_query.lower()) or _ref_stub(joined_query)
            _add_edge(query_nid, target_nid, "reads_from", line_num, context="table_join")

        # 3. Table.Combine / Unions
        for cb_m in re.finditer(r'Table\.Combine\(\s*\{([^}]+)\}\s*\)', query_body):
            for ref_item in cb_m.group(1).split(","):
                comb_tbl = _strip_m_quotes(ref_item)
                if comb_tbl:
                    tgt_nid = defined_queries.get(comb_tbl.lower()) or _ref_stub(comb_tbl)
                    _add_edge(query_nid, tgt_nid, "reads_from", line_num, context="table_combine")

    # ─────────────────────────────────────────────────────────────────────────
    # 4. DataDestinations block (Fabric Dataflow outputs)
    # ─────────────────────────────────────────────────────────────────────────
    # Pattern: [DataDestinations = {[Definition = [..., QueryName = "...", ...], Settings = [..., Mappings = ...]]}]
    dest_pattern = re.compile(
        r'QueryName\s*=\s*"([^"]+)"(?:.*?DestinationColumnName\s*=\s*"([^"]+)")?',
        re.DOTALL,
    )
    for dm in re.finditer(r'\[DataDestinations\s*=\s*\{\[Definition\s*=\s*\[(.*?)\]\]\}\]', text, re.DOTALL):
        dest_block = dm.group(1)
        line_num = text[: dm.start()].count("\n") + 1
        qm = re.search(r'QueryName\s*=\s*"([^"]+)"', dest_block)
        if qm:
            dest_query_name = qm.group(1)
            dest_nid = _ref_stub(dest_query_name)
            # Find destination schema / item if present
            item_m = re.search(r'Item\s*=\s*"([^"]+)"', text)
            if item_m:
                dest_table_name = item_m.group(1)
                tbl_nid = _ref_stub(dest_table_name)
                _add_edge(dest_nid, tbl_nid, "writes_to", line_num, context="fabric_destination_table")

    return {"nodes": nodes, "edges": edges}
