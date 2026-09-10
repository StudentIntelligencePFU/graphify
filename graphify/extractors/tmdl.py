"""TMDL (Tabular Model Definition Language) extractor for Power BI and Microsoft Fabric.

Extracts semantic models, tables, columns, DAX measures with dependency analysis,
model relationships and M partition source lineage.

Every model entity — SemanticModel, Table, ``Table[Column]``, ``Table[Measure]`` —
is a sourceless ``type: namespace`` stub keyed on the bare ``_make_id(label)``,
the same shape the PBIR and Power Query extractors use. That is what lets the
node this extractor defines from ``*.SemanticModel/definition/`` land on the SAME
id that:

  * the PBIR extractor mints from a visual's ``Entity[prop]`` reference and from a
    report's ``targets_semantic_model`` edge, and
  * the SQL / Power Query extractors mint for a ``schema.table`` warehouse object.

Without that, the semantic-model layer is an island: ``neighbors(SemanticModel)``
returns nothing and the chain warehouse -> model -> report -> page -> visual is
broken at the model boundary.

Why sourceless rather than source-backed: a bare id is not the ``<path>_<entity>``
form graphify's dedup treats as a "definer", so a source-backed node here loses
the collision to its own reference stub and is dropped with a warning (every
accented filename, every ``model.tmdl``). Provenance instead rides the ``contains``
edge (file + line) and the file node one hop away — the shape the warehouse layer
already uses (a namespace stub for lineage plus a separate SQL-owned node).

The cost is that two same-named tables in different models collapse to one node
(rare: three ``Table1`` across throwaway profiling models); reconciliation is
worth it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id
from graphify.extractors.powerquery import _nav_tables, _native_sql_tables


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
    "lookupvalue", "var", "return", "true", "false", "coalesce", "coalescex",
})


def _strip_quotes(name: str) -> str:
    """Strip a single pair of surrounding single/double quotes from a TMDL identifier."""
    n = name.strip()
    if len(n) >= 2 and ((n[0] == "'" and n[-1] == "'") or (n[0] == '"' and n[-1] == '"')):
        return n[1:-1].strip()
    return n


def _split_tmdl_column_ref(ref_str: str) -> tuple[str, str]:
    """Split ``'Table'.'Column'`` or ``Table.Column`` into ``(Table, Column)``."""
    clean = ref_str.strip()
    if "." in clean:
        parts = clean.rsplit(".", 1)
        return _strip_quotes(parts[0]), _strip_quotes(parts[1])
    return _strip_quotes(clean), "Unknown"


def _semantic_model_name(path: Path) -> str | None:
    """Model name from the enclosing ``<name>.SemanticModel`` directory, else None.

    ``model.tmdl`` always declares ``model Model`` — the real name is the folder,
    which is also what PBIR reads from a report's ``initial catalog="<name>"``.
    """
    for parent in path.parents:
        if parent.name.endswith(".SemanticModel"):
            return parent.name[: -len(".SemanticModel")]
    return None


def _extract_dax_references(expression: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Extract ``(table, column)`` and bare ``[measure]`` references from a DAX string."""
    if not expression:
        return [], []

    clean_expr = re.sub(r"//.*|--.*|/\*.*?\*/", "", expression)

    col_refs: list[tuple[str, str]] = []
    for m in re.finditer(r"(?:'([^']+)'|([a-zA-Z0-9_\s\-\.]+))\[([^\]]+)\]", clean_expr):
        tbl = (m.group(1) or m.group(2) or "").strip()
        col = m.group(3).strip()
        if tbl and col and tbl.lower() not in ("var", "return"):
            col_refs.append((tbl, col))

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


def _m_source_targets(m_code: str) -> list[tuple[str, str]]:
    """``(schema, table)`` warehouse objects a partition/expression M body reads.

    Reuses the Power Query resolver so a semantic-model partition and a dataflow
    query that hit the same warehouse table land on the same node:

      * navigation form ``…{[Schema="dm", Item="X"]}[Data]``  -> _nav_tables
      * native SQL form ``Sql.Database(s, d, [Query="SELECT … FROM [dm].[X]"])`` -> _native_sql_tables
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for schema, table in (*_nav_tables(m_code), *_native_sql_tables(m_code)):
        pair = (schema, table)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def extract_tmdl(path: Path, content: str | bytes | None = None) -> dict[str, Any]:
    """Extract model, tables, columns, measures, relationships and M lineage from .tmdl."""
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
    model_name = _semantic_model_name(path)

    nodes: list[dict[str, Any]] = [{
        "id": file_nid,
        "label": path.name,
        "file_type": "code",
        "source_file": str_path,
        "source_location": None,
    }]
    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = {file_nid}

    _edge_seen: set[tuple[str, str, str]] = set()

    def _add_edge(src: str, tgt: str, relation: str, line: int, context: str | None = None) -> None:
        if not src or not tgt or src == tgt or (src, tgt, relation) in _edge_seen:
            return
        _edge_seen.add((src, tgt, relation))
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
        """Sourceless ``type: namespace`` node that merges by label across every file.

        Same shape as the PBIR and Power Query extractors' stubs, so a
        SemanticModel / Table / ``Table[Column]`` / ``Table[Measure]`` / warehouse
        object this file names lands on the exact node they mint for that name.

        Model entities are NOT minted source-backed on purpose: a bare
        ``_make_id(label)`` id is not the ``<path>_<entity>`` form graphify's
        dedup expects of a definer, so an owned node here loses the collision to
        the reference stub and gets dropped with a warning (accented filenames,
        every ``model.tmdl``). Provenance instead rides the ``contains`` edge
        (this file + line) and the file node one hop away — the same shape the
        warehouse layer already uses (a namespace stub for lineage + a separate
        SQL-owned node).
        """
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

    def _entity(name: str, line: int, container: str | None) -> str:
        """A model entity stub plus a ``contains`` edge from its container."""
        nid = _ref_stub(name)
        if container:
            _add_edge(container, nid, "contains", line)
        return nid

    def _local_node(nid: str, label: str, line: int) -> str:
        """File-local owned node (Expression / QueryGroup / Culture) — never
        referenced from another file, so a path-derived id is safe here."""
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid, "label": label, "file_type": "code",
                "source_file": str_path, "source_location": f"L{line}",
            })
            _add_edge(file_nid, nid, "contains", line)
        return nid

    # SemanticModel node — bare id so it reconciles with PBIR's targets_semantic_model.
    model_nid = _ref_stub(f"SemanticModel: {model_name}") if model_name else None

    filename = path.name.lower()
    lines = text.splitlines()

    # ─────────────────────────────────────────────────────────────────────────
    # 1. relationships.tmdl  ->  Column relates_to Column  (+ Table relates_to Table)
    # ─────────────────────────────────────────────────────────────────────────
    if filename == "relationships.tmdl" or re.search(r"(?:^|\n)relationship\s", text):
        rel_blocks = re.split(r"(?:^|\n)relationship\s+", text)
        line_offset = 1
        for block in rel_blocks:
            if not block.strip():
                line_offset += block.count("\n") + 1
                continue
            from_col_m = re.search(r"fromColumn:\s*(.*)", block)
            to_col_m = re.search(r"toColumn:\s*(.*)", block)
            if from_col_m and to_col_m:
                from_tbl, from_col = _split_tmdl_column_ref(from_col_m.group(1).strip())
                to_tbl, to_col = _split_tmdl_column_ref(to_col_m.group(1).strip())

                is_active_m = re.search(r"isActive:\s*(false|true)", block, re.IGNORECASE)
                is_active = is_active_m.group(1).lower() != "false" if is_active_m else True
                cross_filter_m = re.search(r"crossFilteringBehavior:\s*(\w+)", block)
                cross_filter = cross_filter_m.group(1) if cross_filter_m else "singleDirection"
                from_card_m = re.search(r"fromCardinality:\s*(\w+)", block)
                to_card_m = re.search(r"toCardinality:\s*(\w+)", block)
                fc = from_card_m.group(1) if from_card_m else "many"
                tc = to_card_m.group(1) if to_card_m else "one"
                rel_context = f"cardinality={fc}-to-{tc};active={is_active};filter={cross_filter}"

                src_tbl_nid = _ref_stub(from_tbl)
                tgt_tbl_nid = _ref_stub(to_tbl)
                src_col_nid = _ref_stub(f"{from_tbl}[{from_col}]")
                tgt_col_nid = _ref_stub(f"{to_tbl}[{to_col}]")

                _add_edge(src_tbl_nid, src_col_nid, "contains", line_offset)
                _add_edge(tgt_tbl_nid, tgt_col_nid, "contains", line_offset)
                _add_edge(src_col_nid, tgt_col_nid, "relates_to", line_offset, context=rel_context)
                _add_edge(src_tbl_nid, tgt_tbl_nid, "relates_to", line_offset, context=rel_context)

            line_offset += block.count("\n") + 1
        return {"nodes": nodes, "edges": edges}

    # ─────────────────────────────────────────────────────────────────────────
    # 2. model.tmdl / database.tmdl  ->  SemanticModel + contains Table
    # ─────────────────────────────────────────────────────────────────────────
    if filename in ("model.tmdl", "database.tmdl") or text.lstrip().startswith(("model ", "database ")):
        container = model_nid or file_nid
        for idx, line in enumerate(lines, start=1):
            s = line.strip()
            if s.startswith("queryGroup "):
                qg = _strip_quotes(s.replace("queryGroup", "", 1))
                if qg:
                    _local_node(_make_id(stem, "queryGroup", qg), f"QueryGroup: {qg}", idx)
            elif s.startswith("ref table "):
                tbl = _strip_quotes(s.replace("ref table", "", 1))
                if tbl and not tbl.startswith(("LocalDateTable_", "DateTableTemplate_")):
                    _add_edge(container, _ref_stub(tbl), "contains", idx)
            elif s.startswith("ref cultureInfo "):
                cult = _strip_quotes(s.replace("ref cultureInfo", "", 1))
                if cult:
                    _local_node(_make_id(stem, "culture", cult), f"Culture: {cult}", idx)
        return {"nodes": nodes, "edges": edges}

    # Auto-generated date tables carry no business meaning — skip entirely.
    if filename.startswith(("localdatetable_", "datetabletemplate_")):
        return {"nodes": nodes, "edges": edges}

    # ─────────────────────────────────────────────────────────────────────────
    # 3. tables/<Table>.tmdl  and  expressions.tmdl
    # ─────────────────────────────────────────────────────────────────────────
    current_table_name: str | None = None
    current_table_nid: str | None = None
    current_measure_name: str | None = None
    current_measure_nid: str | None = None
    current_measure_expr: list[str] = []
    current_measure_line: int = 1

    in_m_source = False
    m_source_lines: list[str] = []
    m_owner_nid: str | None = None      # table or expression node the source feeds
    m_source_line: int = 1

    # A measure's DAX expression ends where the next object or a property line
    # begins. Properties (formatString / displayFolder / description / …) sit
    # between the expression and the next object and must not be swept into the
    # DAX (a description string can hold ``[...]`` that reads as a column ref).
    _STOP_PREFIXES = ("column ", "measure ", "partition ", "hierarchy ", "table ",
                      "expression ", "annotation ", "lineageTag:", "changedProperty ",
                      "extendedProperty ", "formatString:", "displayFolder:",
                      "description:", "isHidden", "dataType:", "formatStringDefinition")

    def _flush_measure() -> None:
        nonlocal current_measure_name, current_measure_nid, current_measure_expr
        if current_measure_name and current_measure_nid:
            full_dax = " ".join(current_measure_expr).strip()
            col_refs, bare_refs = _extract_dax_references(full_dax)
            for tbl_ref, col_ref in col_refs:
                _add_edge(current_measure_nid, _ref_stub(f"{tbl_ref}[{col_ref}]"), "reads_from", current_measure_line)
                _add_edge(current_measure_nid, _ref_stub(tbl_ref), "reads_from", current_measure_line)
            for bare_ref in bare_refs:
                if bare_ref.lower() != current_measure_name.lower():
                    tgt = f"{current_table_name}[{bare_ref}]" if current_table_name else bare_ref
                    _add_edge(current_measure_nid, _ref_stub(tgt), "reads_from", current_measure_line)
        current_measure_name = None
        current_measure_nid = None
        current_measure_expr = []

    def _flush_m_source() -> None:
        nonlocal in_m_source, m_source_lines, m_owner_nid
        if in_m_source and m_owner_nid:
            m_code = "\n".join(m_source_lines)
            for schema, table in _m_source_targets(m_code):
                _add_edge(m_owner_nid, _ref_stub(f"{schema}.{table}"), "reads_from", m_source_line, context="m_partition")
            if "PowerPlatform.Dataflows" in m_code or "PowerBI.Dataflows" in m_code:
                for em in re.finditer(r'\[\s*(?:entity|Id)\s*=\s*"([^"]+)"', m_code):
                    _add_edge(m_owner_nid, _ref_stub(em.group(1)), "reads_from", m_source_line, context="dataflow_entity")
        in_m_source = False
        m_source_lines = []
        m_owner_nid = None

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            if in_m_source:
                m_source_lines.append(line)
            continue

        if stripped.startswith("table ") and not stripped.startswith("tablePermission"):
            _flush_measure()
            _flush_m_source()
            current_table_name = _strip_quotes(stripped.replace("table", "", 1))
            current_table_nid = _entity(current_table_name, idx, model_nid)
            continue

        if stripped.startswith("expression "):
            _flush_measure()
            _flush_m_source()
            expr_name = _strip_quotes(stripped.replace("expression", "", 1).split("=", 1)[0])
            expr_nid = _local_node(_make_id(stem, "expression", expr_name), f"Expression: {expr_name}", idx)
            in_m_source = True
            m_source_lines = []
            m_owner_nid = expr_nid
            m_source_line = idx
            continue

        if stripped.startswith("column "):
            _flush_measure()
            _flush_m_source()
            col_part = stripped.replace("column", "", 1).strip()
            tbl = current_table_name or ""
            if "=" in col_part and "dataType:" not in col_part.split("=", 1)[0]:
                col_name_raw, dax_part = col_part.split("=", 1)
                col_name = _strip_quotes(col_name_raw)
                col_nid = _entity(f"{tbl}[{col_name}]", idx, current_table_nid)
                col_refs, _ = _extract_dax_references(dax_part)
                for t_ref, c_ref in col_refs:
                    _add_edge(col_nid, _ref_stub(f"{t_ref}[{c_ref}]"), "reads_from", idx)
            else:
                col_name = _strip_quotes(col_part.split("dataType:")[0].split("=")[0])
                _entity(f"{tbl}[{col_name}]", idx, current_table_nid)
            continue

        if stripped.startswith("measure "):
            _flush_measure()
            _flush_m_source()
            parts = stripped.replace("measure", "", 1).split("=", 1)
            current_measure_name = _strip_quotes(parts[0])
            current_measure_line = idx
            tbl = current_table_name or ""
            current_measure_nid = _entity(f"{tbl}[{current_measure_name}]", idx, current_table_nid)
            current_measure_expr = [parts[1].strip()] if len(parts) > 1 else []
            continue

        if stripped.startswith("partition "):
            _flush_measure()
            _flush_m_source()
            body = stripped.replace("partition", "", 1)
            mode = body.split("=", 1)[1].strip() if "=" in body else ""
            if mode.startswith("m"):
                in_m_source = True
                m_source_lines = []
                m_owner_nid = current_table_nid
                m_source_line = idx
            continue

        if current_measure_name:
            if stripped.startswith(_STOP_PREFIXES):
                _flush_measure()
            else:
                current_measure_expr.append(stripped)
                continue

        if in_m_source:
            if stripped.startswith(("column ", "measure ", "partition ", "hierarchy ", "table ", "expression ")):
                _flush_m_source()
            elif not m_source_lines:
                # partition/expression preamble, then `source =` (M body may start
                # on that same line or the next).
                src_m = re.match(r"source\s*=\s*(.*)$", stripped)
                if src_m is not None:
                    rest = src_m.group(1).strip().strip("`").strip()
                    if rest:
                        m_source_lines.append(rest)
                elif not re.match(r"(mode|queryGroup|dataView|annotation|isActive|lineageTag)\s*[:=]", stripped):
                    m_source_lines.append(line)
            else:
                m_source_lines.append(line)

    _flush_measure()
    _flush_m_source()

    return {"nodes": nodes, "edges": edges}
