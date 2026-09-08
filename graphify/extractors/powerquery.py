"""Power Query / M formula language extractor for Microsoft Fabric dataflows and Power BI.

Extracts sections, shared queries, ETL step pipelines, cross-query dependencies,
external data sources (Fabric Warehouse, SQL Database, Dataflows, SharePoint, Excel, Web),
transformations (NestedJoin, Combine, ExpandTableColumn), and DataDestinations.

Warehouse and SQL tables are emitted SCHEMA-QUALIFIED (``ods.Alumnos_Base``, not
``Alumnos_Base``) so they land on the same node the SQL extractor mints for the
same table. Without the schema the dataflow layer and the warehouse layer stayed
two disconnected islands joined only by accidental bare-name collisions.
"""
from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id

# An M identifier: a quoted #"..." form (which MAY contain spaces), a plain
# "..." form, or a bare token. The bare-token-only pattern this replaced silently
# dropped every `shared #"PFU v_lead_pfuonline"` — 120 of 766 declarations in a
# real Fabric repo — and, because the query-body lookahead keyed on the same
# pattern, folded each dropped declaration into the PRECEDING query's body.
_M_NAME = r'#"(?:[^"]|"")*"|"(?:[^"]|"")*"|[^\s=;"]+'

# One M navigation record: `{[Schema = "ods", Item = "Base"]}` or `{[Name = "X"]}`.
_NAV_RE = re.compile(r"\{\[(.*?)\]\}", re.DOTALL)
_KV_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# Native SQL embedded in a connector: `Sql.Database(server, db, [Query = "..."])`
# and `Value.NativeQuery(source, "...")`. This is where most of a Fabric
# dataflow's READ lineage actually lives — the navigation-record form covers the
# destination side and little else — so without parsing it the dataflow layer
# looks like it consumes nothing.
_M_STRING = r'"((?:[^"]|"")*)"'
_NATIVE_SQL_RE = re.compile(
    r"\[\s*Query\s*=\s*" + _M_STRING + r"|Value\.NativeQuery\s*\(\s*[^,]+,\s*" + _M_STRING,
    re.IGNORECASE,
)
# schema-qualified table in T-SQL, bracketed or not. The schema qualifier is
# required: it is what keeps CTE and alias names (always unqualified) out.
_SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+\[?(\w+)\]?\s*\.\s*\[?(\w+)\]?",
    re.IGNORECASE,
)
# M escape sequences that appear inside embedded SQL.
_M_ESCAPES = (("#(lf)", "\n"), ("#(cr)", "\r"), ("#(tab)", "\t"), ('""', '"'))


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


def _item_name(path: Path) -> str:
    """Fabric item this .pq belongs to: `.../SI_ODS_Base_1.Dataflow/mashup.pq` -> SI_ODS_Base_1.

    Used to qualify query labels. Query IDs were already unique (``_file_stem``
    encodes the whole path) but the LABELS were bare M identifiers, so with 68
    dataflows in one repo a lookup for `Matriculas_DL` resolved by fuzzy match to
    whichever of them the resolver happened to reach first.
    """
    parent = path.parent.name
    for suffix in (".Dataflow", ".SemanticModel", ".Report"):
        if parent.endswith(suffix):
            return parent[: -len(suffix)]
    return parent or path.stem


def _nav_tables(body: str) -> list[tuple[str, str]]:
    """Yield (schema, table) pairs from the M navigation chain in a query body.

    Handles both shapes Fabric emits:

    * flat        ``Nav{[Schema = "ods", Item = "Base"]}[Data]``
    * hierarchical ``Nav{[Schema = "ods"]}[Data], Nav2{[Name = "Base"]}[Data]``

    A bare ``Item``/``Name`` with no schema in scope is skipped — that is how
    Excel/CSV sheet navigation (``{[Item="Sheet1", Kind="Sheet"]}``) is kept from
    being mistaken for a warehouse table. The pending schema is consumed once so
    a later unrelated navigation cannot inherit a stale one.
    """
    pairs: list[tuple[str, str]] = []
    pending_schema: str | None = None
    for nav in _NAV_RE.finditer(body):
        kv = dict(_KV_RE.findall(nav.group(1)))
        table = kv.get("Item") or kv.get("Name")
        schema = kv.get("Schema")
        if table:
            effective = schema or pending_schema
            if effective:
                pairs.append((effective, table))
            pending_schema = None
        elif schema:
            pending_schema = schema
    return pairs


def _native_sql_tables(body: str) -> list[tuple[str, str]]:
    """Yield (schema, table) pairs referenced by SQL embedded in a query body.

    Deduplicated per query — an ETL query naming the same table in six CTEs is
    one dependency, not six.
    """
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for m in _NATIVE_SQL_RE.finditer(body):
        sql = m.group(1) if m.group(1) is not None else m.group(2)
        if not sql:
            continue
        for old, new in _M_ESCAPES:
            sql = sql.replace(old, new)
        for tm in _SQL_TABLE_RE.finditer(sql):
            pair = (tm.group(1), tm.group(2))
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


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
    item_name = _item_name(path)

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
        _add_node(_make_id(stem, "section", sec_name), f"Section: {item_name}.{sec_name}", 1)

    # ─────────────────────────────────────────────────────────────────────────
    # Pass 0. Resolve the destination bindings.
    #
    # In M the `[DataDestinations = ...]` attribute PRECEDES the `shared`
    # declaration it applies to, and names a pointer query whose body is a bare
    # navigation to the target table. So the binding is
    # (pointer query, source query) and the lineage edge is
    # source --writes_to--> <table the pointer navigates to>.
    #
    # A name only counts as a pointer when it is bound to a DIFFERENT source
    # query. A block that names the very query it precedes (or names a query
    # that is never declared) leaves that query an ordinary reader, so its
    # `reads_from` edges must survive.
    # ─────────────────────────────────────────────────────────────────────────
    dest_bind_re = re.compile(
        r'\[DataDestinations\s*=.*?QueryName\s*=\s*"([^"]+)".*?\]\}\]\s*\n\s*shared\s+('
        + _M_NAME + r')\s*=',
        re.DOTALL,
    )
    bindings: list[tuple[str, str, int]] = []  # (pointer_name, source_name, line)
    for dm in dest_bind_re.finditer(text):
        bindings.append((
            _strip_m_quotes(dm.group(1)),
            _strip_m_quotes(dm.group(2)),
            text[: dm.start()].count("\n") + 1,
        ))
    dest_pointer_names: set[str] = {
        p.lower() for p, s, _ in bindings if p.lower() != s.lower()
    }

    # ─────────────────────────────────────────────────────────────────────────
    # Pass 1. Collect every shared query: name, body, line, node id.
    # ─────────────────────────────────────────────────────────────────────────
    shared_pattern = re.compile(
        r"(?:^|\n)\s*shared\s+(" + _M_NAME + r")\s*=\s*(.*?)"
        r"(?=\n\s*shared\s+|\n\s*\[DataDestinations|\Z)",
        re.DOTALL,
    )

    defined_queries: dict[str, str] = {}      # query_name_lower -> nid
    query_bodies: dict[str, str] = {}         # query_name_lower -> body
    query_lines: dict[str, int] = {}          # query_name_lower -> line
    pointer_targets: dict[str, str] = {}      # pointer_name_lower -> "schema.table"

    for m in shared_pattern.finditer(text):
        query_name = _strip_m_quotes(m.group(1).strip())
        query_body = m.group(2).strip()
        line_num = text[: m.start()].count("\n") + 1
        key = query_name.lower()

        query_nid = _add_node(_make_id(stem, query_name), f"{item_name}[{query_name}]", line_num)
        defined_queries[key] = query_nid
        query_bodies[key] = query_body
        query_lines[key] = line_num

        if key in dest_pointer_names:
            tables = _nav_tables(query_body)
            if tables:
                schema, table = tables[-1]
                pointer_targets[key] = f"{schema}.{table}"

    # ─────────────────────────────────────────────────────────────────────────
    # Pass 2. Per-query connectors, sources and cross-query dependencies.
    # ─────────────────────────────────────────────────────────────────────────
    for key, query_nid in defined_queries.items():
        query_body = query_bodies[key]
        line_num = query_lines[key]
        is_pointer = key in dest_pointer_names

        # 1. External connectors in the query body.
        # SQL Database — the server/database endpoint itself.
        for sm in re.finditer(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"', query_body, re.IGNORECASE):
            server, db = sm.group(1), sm.group(2)
            db_stub = _ref_stub(f"{server}/{db}")
            _add_edge(query_nid, db_stub, "reads_from", line_num, context="sql_database")

        # Fabric Warehouse — the warehouse itself, by GUID and/or display name.
        if "Fabric.Warehouse" in query_body:
            for wm in re.finditer(r'warehouseId\s*=\s*"([^"]+)"', query_body):
                _add_edge(query_nid, _ref_stub(f"Warehouse:{wm.group(1)}"), "reads_from",
                          line_num, context="fabric_warehouse")
            for dm_ in re.finditer(r'displayName\s*=\s*"([^"]+)"', query_body):
                _add_edge(query_nid, _ref_stub(f"Warehouse: {dm_.group(1)}"), "reads_from",
                          line_num, context="fabric_warehouse_name")

        # Schema-qualified tables reached through the navigation chain. Applies to
        # Fabric.Warehouse AND Sql.Database, which share the record-navigation
        # syntax. A destination pointer is skipped here — pass 3 emits its edge
        # as writes_to, from the query that actually feeds it.
        if not is_pointer:
            for schema, table in _nav_tables(query_body):
                _add_edge(query_nid, _ref_stub(f"{schema}.{table}"), "reads_from",
                          line_num, context="warehouse_table")

            # Tables named by SQL passed through to the source ([Query = "..."],
            # Value.NativeQuery). Most read lineage in this repo lives here.
            for schema, table in _native_sql_tables(query_body):
                _add_edge(query_nid, _ref_stub(f"{schema}.{table}"), "reads_from",
                          line_num, context="native_sql_table")

        # PowerPlatform / Dataflows
        if "PowerPlatform.Dataflows" in query_body:
            for em in re.finditer(r'entity\s*=\s*"([^"]+)"', query_body):
                _add_edge(query_nid, _ref_stub(em.group(1)), "reads_from",
                          line_num, context="upstream_dataflow_entity")
            for df_m in re.finditer(r'dataflowId\s*=\s*"([^"]+)"', query_body):
                _add_edge(query_nid, _ref_stub(f"Dataflow:{df_m.group(1)}"), "reads_from",
                          line_num, context="upstream_dataflow")

        # SharePoint / Web / Excel / CSV
        for sp_m in re.finditer(r'SharePoint\.\w+\(\s*"([^"]+)"', query_body):
            _add_edge(query_nid, _ref_stub(sp_m.group(1)), "reads_from",
                      line_num, context="sharepoint_source")
        for web_m in re.finditer(r'Web\.Contents\(\s*"([^"]+)"', query_body):
            _add_edge(query_nid, _ref_stub(web_m.group(1)), "reads_from",
                      line_num, context="web_source")

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
    # Pass 3. DataDestinations — where the dataflow WRITES, from the bindings
    # resolved in pass 0.
    #
    # The previous implementation instead ran `re.search(r'Item = "..."', text)`
    # over the WHOLE FILE and took the first hit, so every destination in a file
    # resolved to the same table — in SI_ODS_Base_1 all three DataDestinations
    # pointed at Matriculas_Consolidado_Base, making two of the three edges
    # outright false.
    # ─────────────────────────────────────────────────────────────────────────
    for pointer_name, source_name, line_num in bindings:
        source_nid = defined_queries.get(source_name.lower())
        if not source_nid:
            continue

        target = pointer_targets.get(pointer_name.lower())
        if target:
            _add_edge(source_nid, _ref_stub(target), "writes_to",
                      line_num, context="fabric_destination_table")
        else:
            # Pointer query not resolvable to a table (external reference, or a
            # navigation shape not covered): keep the structural edge rather
            # than dropping the destination entirely.
            pointer_nid = defined_queries.get(pointer_name.lower()) or _ref_stub(pointer_name)
            _add_edge(source_nid, pointer_nid, "writes_to",
                      line_num, context="fabric_destination_unresolved")

    return {"nodes": nodes, "edges": edges}
