"""TMDL (Tabular Model Definition Language) extractor for Power BI and Microsoft Fabric.

Extracts semantic models, tables, columns, DAX measures with dependency analysis,
model relationships, M partitions, shared expressions/parameters, query groups,
security roles, and perspectives.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id
from graphify.ids import normalize_id


# DAX built-in functions / keywords that should not be classified as referenced measures
_DAX_BUILTINS = frozenset({
    "calculate", "calculatetable", "filter", "all", "allexcept", "allselected",
    "values", "distinct", "related", "relatedtable", "sum", "sumx", "average",
    "averagex", "count", "countrows", "counta", "countax", "min", "minx", "max",
    "maxx", "divide", "if", "switch", "blank", "isblank", "not", "and", "or",
    "date", "today", "now", "year", "month", "day", "dateadd", "datesytd",
    "datesmtd", "datesqtd", "totalytd", "totalmtd", "totalqtd", "sameperiodlastyear",
    "parallelperiod", "previousmonth", "previousyear", "nextmonth", "nextyear",
    "earlier", "earliest", "userelationship", "crossfilter", "treatas", "selectedvalue",
    "hasonevalue", "isinscope", "format", "concatenate", "concatenatex", "keepfilters",
    "lookupvalue", "var", "return", "true", "false",
})


def _strip_quotes(name: str) -> str:
    """Strip single or double quotes from TMDL identifier names."""
    n = name.strip()
    if len(n) >= 2 and ((n[0] == "'" and n[-1] == "'") or (n[0] == '"' and n[-1] == '"')):
        return n[1:-1].strip()
    return n


def _norm_tmdl_ident(name: str) -> str:
    """Normalize identifier for lookup (stripping quotes and lowercasing)."""
    return _strip_quotes(name).lower()


def _split_tmdl_column_ref(ref_str: str) -> tuple[str, str]:
    """Split 'Table'.'Column' or Table.Column into (Table, Column)."""
    clean = ref_str.strip()
    if "." in clean:
        parts = clean.rsplit(".", 1)
        return _strip_quotes(parts[0]), _strip_quotes(parts[1])
    return clean, "Unknown"


def _extract_dax_references(expression: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Extract (table_name, column_name) and bare [measure_name] references from a DAX expression.

    Returns:
        (list of (table, col) tuples, list of bare [measure/col] names)
    """
    if not expression:
        return [], []

    # Strip single line comments (-- or //) and multiline comments (/* */)
    clean_expr = re.sub(r"//.*|--.*|/\*.*?\*/", "", expression)

    col_refs: list[tuple[str, str]] = []
    # Pattern: 'Table'[Column] or Table[Column]
    for m in re.finditer(r"(?:'([^']+)'|([a-zA-Z0-9_\s\-\.]+))\[([^\]]+)\]", clean_expr):
        tbl = (m.group(1) or m.group(2) or "").strip()
        col = m.group(3).strip()
        if tbl and col and tbl.lower() not in ("var", "return"):
            col_refs.append((tbl, col))

    # Pattern: bare [MeasureOrCol] (not preceded by table name/quote/close bracket)
    bare_refs: list[str] = []
    for m in re.finditer(r"(?<!['\w\]])\[([^\]]+)\]", clean_expr):
        ref_name = m.group(1).strip()
        if ref_name and ref_name.lower() not in _DAX_BUILTINS:
            bare_refs.append(ref_name)

    return col_refs, bare_refs


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


def extract_tmdl(path: Path, content: str | bytes | None = None) -> dict[str, Any]:
    """Extract semantic entities, columns, measures, relationships and partitions from .tmdl files."""
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
        """Create a sourceless global stub node for external/unresolved table or entity reference."""
        nid = _make_id(name)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "origin_file": str_path,
            })
        return nid

    filename = path.name.lower()
    lines = text.splitlines()

    # ─────────────────────────────────────────────────────────────────────────
    # 1. relationships.tmdl
    # ─────────────────────────────────────────────────────────────────────────
    if filename == "relationships.tmdl" or "relationship " in text:
        # Split relationship blocks
        rel_blocks = re.split(r"(?:^|\n)relationship\s+", text)
        line_offset = 1
        for block in rel_blocks:
            if not block.strip():
                continue
            from_col_m = re.search(r"fromColumn:\s*(.*)", block)
            to_col_m = re.search(r"toColumn:\s*(.*)", block)
            if from_col_m and to_col_m:
                from_tbl, from_col = _split_tmdl_column_ref(from_col_m.group(1).strip())
                to_tbl, to_col = _split_tmdl_column_ref(to_col_m.group(1).strip())

                is_active_m = re.search(r"isActive:\s*(false|true)", block, re.IGNORECASE)
                is_active = is_active_m.group(1).lower() != "false" if is_active_m else True

                cross_filter_m = re.search(r"crossFilteringBehavior:\s*(\w+)", block)
                cross_filter = cross_filter_m.group(1) if cross_filter_m else "oneDirection"

                from_card_m = re.search(r"fromCardinality:\s*(\w+)", block)
                to_card_m = re.search(r"toCardinality:\s*(\w+)", block)
                fc = from_card_m.group(1) if from_card_m else "many"
                tc = to_card_m.group(1) if to_card_m else "one"
                cardinality = f"{fc}-to-{tc}"

                rel_context = f"cardinality={cardinality};active={is_active};filter={cross_filter}"

                # Add nodes for source and target tables / columns
                src_tbl_nid = _ref_stub(from_tbl)
                tgt_tbl_nid = _ref_stub(to_tbl)

                src_col_nid = _ref_stub(f"{from_tbl}[{from_col}]")
                tgt_col_nid = _ref_stub(f"{to_tbl}[{to_col}]")

                # Table to column contains
                _add_edge(src_tbl_nid, src_col_nid, "contains", line_offset)
                _add_edge(tgt_tbl_nid, tgt_col_nid, "contains", line_offset)

                # Column-level and table-level relationship edges
                _add_edge(src_col_nid, tgt_col_nid, "relates_to", line_offset, context=rel_context)
                _add_edge(src_tbl_nid, tgt_tbl_nid, "relates_to", line_offset, context=rel_context)

            line_offset += block.count("\n") + 1
        return {"nodes": nodes, "edges": edges}

    # ─────────────────────────────────────────────────────────────────────────
    # 2. model.tmdl / database.tmdl / expressions.tmdl
    # ─────────────────────────────────────────────────────────────────────────
    if filename in ("model.tmdl", "database.tmdl") or text.lstrip().startswith("model ") or text.lstrip().startswith("database "):
        for idx, line in enumerate(lines, start=1):
            s = line.strip()
            if s.startswith("model ") or s.startswith("database "):
                model_name = _strip_quotes(s.split(None, 1)[1]) if len(s.split(None, 1)) > 1 else "Model"
                _add_node(_make_id(stem, model_name), model_name, idx)
            elif s.startswith("queryGroup "):
                qg_name = _strip_quotes(s.replace("queryGroup", "", 1))
                if qg_name:
                    _add_node(_make_id(stem, "queryGroup", qg_name), f"QueryGroup: {qg_name}", idx)
            elif s.startswith("ref table "):
                tbl_name = _strip_quotes(s.replace("ref table", "", 1))
                if tbl_name:
                    tbl_stub = _ref_stub(tbl_name)
                    _add_edge(file_nid, tbl_stub, "references", idx)
            elif s.startswith("ref cultureInfo "):
                cult_name = _strip_quotes(s.replace("ref cultureInfo", "", 1))
                if cult_name:
                    _add_node(_make_id(stem, "culture", cult_name), f"Culture: {cult_name}", idx)

        return {"nodes": nodes, "edges": edges}

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Table TMDL files (tables/<TableName>.tmdl) and expressions.tmdl
    # ─────────────────────────────────────────────────────────────────────────
    current_table_name: str | None = None
    current_table_nid: str | None = None
    current_measure_name: str | None = None
    current_measure_nid: str | None = None
    current_measure_expr: list[str] = []
    current_measure_line: int = 1

    in_m_partition = False
    m_partition_source: list[str] = []
    m_partition_name: str | None = None
    m_partition_line: int = 1

    def _flush_measure() -> None:
        nonlocal current_measure_name, current_measure_nid, current_measure_expr, current_measure_line
        if current_measure_name and current_measure_nid:
            full_dax = " ".join(current_measure_expr).strip()
            col_refs, bare_refs = _extract_dax_references(full_dax)
            for tbl_ref, col_ref in col_refs:
                target_col_nid = _ref_stub(f"{tbl_ref}[{col_ref}]")
                _add_edge(current_measure_nid, target_col_nid, "reads_from", current_measure_line)
                target_tbl_nid = _ref_stub(tbl_ref)
                _add_edge(current_measure_nid, target_tbl_nid, "reads_from", current_measure_line)
            for bare_ref in bare_refs:
                if bare_ref.lower() != current_measure_name.lower():
                    target_stub = _ref_stub(bare_ref)
                    _add_edge(current_measure_nid, target_stub, "reads_from", current_measure_line)
        current_measure_name = None
        current_measure_nid = None
        current_measure_expr = []

    def _flush_m_partition() -> None:
        nonlocal in_m_partition, m_partition_source, m_partition_name, m_partition_line
        if in_m_partition and m_partition_name:
            m_code = "\n".join(m_partition_source)
            part_nid = _add_node(_make_id(stem, "partition", m_partition_name), f"Partition: {m_partition_name}", m_partition_line)
            if current_table_nid:
                _add_edge(current_table_nid, part_nid, "contains", m_partition_line)

            # Analyze external data sources in M code
            # 1. SQL Database sources
            for m in re.finditer(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"', m_code, re.IGNORECASE):
                server, db = m.group(1), m.group(2)
                src_nid = _ref_stub(f"{server}/{db}")
                _add_edge(part_nid, src_nid, "reads_from", m_partition_line, context="sql_database")

            # 2. Fabric Warehouse sources
            if "Fabric.Warehouse" in m_code:
                for wm in re.finditer(r'warehouseId\s*=\s*"([^"]+)"', m_code):
                    wh_nid = _ref_stub(f"Warehouse:{wm.group(1)}")
                    _add_edge(part_nid, wh_nid, "reads_from", m_partition_line, context="fabric_warehouse")

            # 3. PowerPlatform / Dataflows
            if "PowerPlatform.Dataflows" in m_code:
                for em in re.finditer(r'entity\s*=\s*"([^"]+)"', m_code):
                    df_entity = em.group(1)
                    df_nid = _ref_stub(df_entity)
                    _add_edge(part_nid, df_nid, "reads_from", m_partition_line, context="dataflow_entity")

            # 4. SQL tables mentioned inside M Query string: [Schema].[Table] or FROM Schema.Table
            for sm in re.finditer(r'FROM\s+\[?([\w]+)\]?\.\[?([\w]+)\]?', m_code, re.IGNORECASE):
                sql_tbl = f"{sm.group(1)}.{sm.group(2)}"
                sql_nid = _ref_stub(sql_tbl)
                _add_edge(part_nid, sql_nid, "reads_from", m_partition_line, context="sql_table")

        in_m_partition = False
        m_partition_source = []
        m_partition_name = None

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        # Check for top-level table definition
        if stripped.startswith("table ") and not stripped.startswith("tablePermission"):
            _flush_measure()
            _flush_m_partition()
            current_table_name = _strip_quotes(stripped.replace("table", "", 1))
            current_table_nid = _add_node(_make_id(stem, current_table_name), current_table_name, idx)
            continue

        # Check for shared expression (expressions.tmdl)
        if stripped.startswith("expression "):
            _flush_measure()
            _flush_m_partition()
            parts = stripped.replace("expression", "", 1).split("=", 1)
            expr_name = _strip_quotes(parts[0])
            _add_node(_make_id(stem, "expression", expr_name), f"Expression: {expr_name}", idx)
            continue

        # Check for column definition
        if stripped.startswith("column "):
            _flush_measure()
            _flush_m_partition()
            col_part = stripped.replace("column", "", 1).strip()
            if "=" in col_part:
                col_name_raw, dax_part = col_part.split("=", 1)
                col_name = _strip_quotes(col_name_raw)
                col_nid = _add_node(_make_id(stem, current_table_name or "table", col_name), f"{current_table_name or ''}[{col_name}]", idx)
                if current_table_nid:
                    _add_edge(current_table_nid, col_nid, "contains", idx)
                col_refs, bare_refs = _extract_dax_references(dax_part)
                for tbl_ref, c_ref in col_refs:
                    target_col_nid = _ref_stub(f"{tbl_ref}[{c_ref}]")
                    _add_edge(col_nid, target_col_nid, "reads_from", idx)
            else:
                col_name = _strip_quotes(col_part.split("dataType:")[0])
                col_nid = _add_node(_make_id(stem, current_table_name or "table", col_name), f"{current_table_name or ''}[{col_name}]", idx)
                if current_table_nid:
                    _add_edge(current_table_nid, col_nid, "contains", idx)
            continue

        # Check for measure definition
        if stripped.startswith("measure "):
            _flush_measure()
            _flush_m_partition()
            parts = stripped.replace("measure", "", 1).split("=", 1)
            current_measure_name = _strip_quotes(parts[0])
            current_measure_line = idx
            current_measure_nid = _add_node(
                _make_id(stem, current_table_name or "measure", current_measure_name),
                f"[{current_measure_name}]",
                idx,
            )
            if current_table_nid:
                _add_edge(current_table_nid, current_measure_nid, "contains", idx)
            if len(parts) > 1:
                current_measure_expr = [parts[1].strip()]
            else:
                current_measure_expr = []
            continue

        # Check for partition definition
        if stripped.startswith("partition "):
            _flush_measure()
            _flush_m_partition()
            part_name_raw = stripped.replace("partition", "", 1).split("=", 1)[0]
            m_partition_name = _strip_quotes(part_name_raw)
            m_partition_line = idx
            in_m_partition = True
            m_partition_source = []
            continue

        # Accumulate DAX measure expression or M partition source
        if current_measure_name:
            if any(stripped.startswith(k) for k in ("column ", "measure ", "partition ", "hierarchy ", "annotation ", "lineageTag:", "formatString:")):
                if stripped.startswith(("column ", "measure ", "partition ", "hierarchy ")):
                    _flush_measure()
            else:
                current_measure_expr.append(stripped)

        if in_m_partition:
            if any(stripped.startswith(k) for k in ("column ", "measure ", "partition ", "hierarchy ", "table ")):
                _flush_m_partition()
            else:
                m_partition_source.append(line)

    _flush_measure()
    _flush_m_partition()

    return {"nodes": nodes, "edges": edges}
