"""Power Automate flow extractor.

Extracts lineage from Power Automate flows pulled into the repo as JSON under
`power-automate/<environment>/<Display Name>/{definition.json,connections.json,
metadata.json}`, plus the environment-level `power-automate/<environment>/
_connections.json` and repo-level `power-automate/_env_map.json`.

v1 scope (see PowerAutomate/Documentacion/arquitectura_graphify_powerautomate.md
for the full design — this covers only the sections resolved against real data
at write time):

- Node `Flow: <displayName>` (§3).
- §4.1 SQL: `Flow --reads_from/writes_to--> esquema.tabla`, from native
  `shared_sql` `ExecutePassThroughNativeQuery_V2` actions. This is Workflow
  Definition Language, not the M `Sql.Database(...)` Dataflows use — see
  `graphify.extractors.powerquery` for that side of the same technique.
- §4.5 Excel: `Flow --reads_from/writes_to--> ExcelTable: <file>/<table>` from
  `shared_excelonlinebusiness` GetItems/GetItem (reads) and AddRowV2 (writes).
  Also: `ExcelFile: <file> --contains--> ExcelTable: <file>/<table>`.
  When the environment's `_drive_items.json` (written by `pa_pull.ipynb`'s Graph
  resolution step, sibling of `_connections.json`) has a resolved entry for a
  flow's `(drive, file)` pair, the `ExcelFile` node is minted using the
  resolved canonical URL as its label instead of the bare drive-item id — the
  SAME label `graphify.extractors.powerquery` mints from a Dataflow's
  `Web.Contents("https://...")` reading that same Excel file, so the two sides
  merge into one node instead of staying disconnected islands. Falls back to
  `ExcelFile: <file>` when unresolved (e.g. `_drive_items.json` absent, or the
  pull's identity couldn't reach that particular OneDrive/SharePoint item).
- §4.5 Forms: `Form: <form_id> --triggers--> Flow` from `shared_microsoftforms`
  CreateFormWebhook triggers, and `Flow --reads_from--> Form: <form_id>` from
  GetFormResponseById actions.
- §4.5 OneDrive: `Flow --writes_to--> OneDriveFolder: <folderPath>` from
  `shared_onedriveforbusiness` CreateFile with literal folderPath.
- §4.5 SharePoint: `Flow --writes_to--> SharePointSite: <dataset>` from
  `shared_sharepointonline` CreateFile with literal dataset.
- §4.7 Connections: `Flow --uses--> Connection: <displayName>`, excluding
  `shared_logicflows` (the internal "Run a Child Flow" plumbing, not a real
  credential — see the module-level note on `_SHARED_LOGICFLOWS_API_ID`).
- §4.8 Solutions: `Flow --belongs_to--> Solution: <uniquename>`.

Deliberately NOT built here (left for v2 — needs indirection-resolution
`_env_map.json` doesn't carry data for yet): Power BI dataset/report reads
(§4.2), Dataverse/AI Builder (§4.3), child-flow calls (§4.4), RunScriptProd
Excel operations (§4.5 omitted: file parameter is always a WDL expression),
CreateFile.name on OneDrive/SharePoint (omitted: always expression), Fabric
pipeline/notebook calls (§4.6).

Only `metadata.json` mints nodes — it is the file that "owns" the Flow entity,
and it reads its siblings (`definition.json`, `connections.json`) and its
environment's `_connections.json` for edges. Every other `.json` under
`power-automate/` (definition.json, connections.json, _connections.json,
_env_map.json) returns no nodes/edges when extracted directly: extracting them
again would either double-mint the Flow node or, for the ones with no node of
their own, turn a >250KB definition.json into thousands of disconnected
islands under the generic JSON extractor — precisely the failure mode
`_ITEM_DEFINITION_FILES` in `fabric_config.py` exists to avoid for
Dataflows/Notebooks. The dispatcher in `graphify/extract.py` must route every
`.json` under a `power-automate/` directory to this extractor for that to
hold; see `_is_power_automate_json` there.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id

# The connector Power Automate uses internally for the "Run a Child Flow"
# action. Real data (pull 2026-09-15, `student-intelligence-repo`, 91
# connections across 51 flows): 54/91 (59%) are this connector, and 53 of
# those 54 sit in status "Error" — it is inter-flow plumbing, not an external
# credential, and none of its "connections" ever resolve to a person. Modeling
# it as a `Connection:` node would make `god_nodes()` over connections report
# this plumbing instead of the most-shared real credential, and would turn
# "58% of connections are in Error" into a false governance alarm — both
# documented in §4.7 of the architecture doc. Filtered by apiId, never by
# connector display name, same principle as the AI Builder / Dataverse
# disambiguation the doc calls out in §4.3.
_SHARED_LOGICFLOWS_API_ID = "shared_logicflows"

# `host.apiId` for a native SQL action ends in this (it is the full API path,
# e.g. "/providers/Microsoft.PowerApps/apis/shared_sql"); `host.operationId`
# is compared exactly.
_SQL_API_ID_SUFFIX = "/shared_sql"
_SQL_OPERATION_ID = "ExecutePassThroughNativeQuery_V2"

# Excel Online (Business): §4.5 operations on ExcelTable. Measure: 8 AddRowV2 (writes),
# 25 GetItems (reads), 4 GetItem (reads) across 51 flows. RunScriptProd is deliberately
# left out because its `file` parameter is always a WDL expression, never a literal.
_EXCEL_API_ID_SUFFIX = "/shared_excelonlinebusiness"
_EXCEL_ADDROW_OPERATION_ID = "AddRowV2"
_EXCEL_GETITEMS_OPERATION_ID = "GetItems"
_EXCEL_GETITEM_OPERATION_ID = "GetItem"

# Forms: §4.5 triggers and §4.5bis read operations. Real data: 4 CreateFormWebhook
# triggers (form_id literal), 4 GetFormResponseById actions (form_id literal).
_FORMS_API_ID_SUFFIX = "/shared_microsoftforms"
_FORMS_WEBHOOK_OPERATION_ID = "CreateFormWebhook"
_FORMS_GET_RESPONSE_OPERATION_ID = "GetFormResponseById"

# OneDrive for Business: §4.5 CreateFile writes with folderPath literal.
# Real data: 23 literal folderPath (written to), 16 expression folderPath (omitted).
_ONEDRIVE_API_ID_SUFFIX = "/shared_onedriveforbusiness"
_ONEDRIVE_CREATEFILE_OPERATION_ID = "CreateFile"

# SharePoint Online: §4.5 CreateFile writes with dataset literal.
# Real data: 1 literal dataset (written to), 1 expression dataset (omitted).
_SHAREPOINT_API_ID_SUFFIX = "/shared_sharepointonline"
_SHAREPOINT_CREATEFILE_OPERATION_ID = "CreateFile"

# Schema-qualified table reference immediately after a DML/query keyword, e.g.
# `FROM [dm].[(Hec)_VOC_HLO_Respuestas]` or `INSERT INTO dbo.Log`. Mirrors
# `graphify.extractors.powerquery._SQL_TABLE_RE` (same rationale: a bracketed
# identifier may hold anything but `]` — Fabric DM tables are named
# `[dm].[(Dim)_VOC_UCMA_Asignaturas]` — and the schema qualifier is required
# so CTE/alias names, always unqualified, never turn into ghost table nodes).
# Multi-word verbs are listed before the single-word alternatives they start
# with ("DELETE FROM" before a bare "FROM" would ever get a chance to consume
# just the "FROM" half at that position) so `DELETE FROM x.y` is not also
# double-counted as a read of `x.y` via the plain "FROM" branch. The dot may
# only have same-line horizontal whitespace around it (`[ \t]*`, not `\s*`) --
# a real `[schema].[table]` reference is always written tight; a `\s*` here
# once reached across a blank line inside a `/* ... */` comment (see
# `_strip_sql_comments`) and matched a stray word as a fake "table".
_SQL_TABLE_ACTION_RE = re.compile(
    r"\b(?P<verb>DELETE\s+FROM|INSERT\s+INTO|INSERT|UPDATE|EXECUTE|EXEC|FROM|JOIN)\s+"
    r"(?:\[(?P<s1>[^\]]+)\]|(?P<s2>[A-Za-z0-9_#$]+))"
    r"[ \t]*\.[ \t]*"
    r"(?:\[(?P<t1>[^\]]+)\]|(?P<t2>[A-Za-z0-9_#$]+))",
    re.IGNORECASE,
)
# First token of `verb` (after collapsing whitespace) that means "this action
# writes to the table that follows", per the architecture doc §4.1: "Dirección
# por verbo: SELECT -> reads_from; INSERT/UPDATE/DELETE/EXEC -> writes_to".
_SQL_WRITE_VERBS = frozenset({"DELETE", "INSERT", "UPDATE", "EXEC", "EXECUTE"})


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


def _iter_actions(container: dict[str, Any]):
    """Yield every `(name, action_dict)` in a WDL action tree, recursing into
    every nesting shape Power Automate uses for branching and looping:

    - `actions` — Scope, If-true, For-each, Until.
    - `elseActions` — If-false.
    - `cases.<key>.actions` and `default.actions` — Switch.

    A trigger container has this same `{name: {...}}` shape at the root (a
    flow can only have one trigger today, but WDL still stores it as a dict),
    so callers can pass `definition.get("triggers", {})` here too.
    """
    if not isinstance(container, dict):
        return
    for name, action in container.items():
        if not isinstance(action, dict):
            continue
        yield name, action
        nested = action.get("actions")
        if isinstance(nested, dict):
            yield from _iter_actions(nested)
        else_actions = action.get("elseActions")
        if isinstance(else_actions, dict):
            yield from _iter_actions(else_actions)
        cases = action.get("cases")
        if isinstance(cases, dict):
            for case_obj in cases.values():
                if isinstance(case_obj, dict):
                    case_actions = case_obj.get("actions")
                    if isinstance(case_actions, dict):
                        yield from _iter_actions(case_actions)
        default = action.get("default")
        if isinstance(default, dict):
            default_actions = default.get("actions")
            if isinstance(default_actions, dict):
                yield from _iter_actions(default_actions)


def _strip_sql_comments(text: str) -> str:
    """Strip `--` line comments and `/* ... */` block comments before regex
    matching. Same recipe as `graphify.extractors.sql` (`clean_text`). Real
    data forced this: a query had a `/* LEFT JOIN intencionado.\n\n
    LOOKUPVALUE de DAX puede devolver BLANK ... */` comment explaining a JOIN
    that was deliberately NOT done, and the un-stripped text let the
    schema/table regex's dot-adjacency span the blank line and mint a fake
    `intencionado.LOOKUPVALUE` node.
    """
    text = re.sub(r"--.*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _tables_from_sql(sql_text: str) -> list[tuple[str, str, str]]:
    """Yield `(schema, table, relation)` triples referenced by native T-SQL text.

    Regex-only, deliberately not a SQL parser: the query string embedded in a
    Power Automate SQL action is Workflow Definition Language, not plain
    SQL — it is sprinkled with `@{...}` expressions and `DECLARE @Var ...`
    statements (see `Matriculacion_OBS`'s real queries). This only anchors on
    a DML/query keyword immediately followed by a schema.table pair, so it
    survives that noise structurally: it cannot raise on it, and it cannot
    invent a table out of a WDL expression that never contains a bare
    `KEYWORD schema.table` shape.

    Deduplicated per query (schema, table, relation) — the same table named
    across several CTEs of one query is one dependency, not several.
    """
    if not sql_text:
        return []
    sql_text = _strip_sql_comments(sql_text)
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for m in _SQL_TABLE_ACTION_RE.finditer(sql_text):
        verb = re.sub(r"\s+", " ", m.group("verb").strip().upper()).split(" ")[0]
        schema = m.group("s1") or m.group("s2")
        table = m.group("t1") or m.group("t2")
        if not schema or not table:
            continue
        relation = "writes_to" if verb in _SQL_WRITE_VERBS else "reads_from"
        key = (schema, table, relation)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# Strips the `shared-<connector>-` prefix Dataverse puts on some connection
# names (e.g. `shared-office365-f5462450-503c-4f16-a90e-34996bf51190`) so the
# GUID underneath is what gets shortened, not the constant connector slug —
# see `_connection_labels`.
_SHARED_NAME_PREFIX_RE = re.compile(r"^shared-[a-z0-9]+-", re.IGNORECASE)


def _short_suffix(connection_name: str) -> str:
    """First 8 chars of the GUID identifying a connection, stripping the
    optional `shared-<connector>-` prefix. Same recipe the repo's own Fase-1
    pull already uses to disambiguate two flows sharing a display name (e.g.
    `Docentes OBS - Evaluacion docente-224124db` / `-5a14ee7b` are exactly the
    first 8 chars of each flow's own connectionName-shaped id).
    """
    stripped = _SHARED_NAME_PREFIX_RE.sub("", connection_name)
    return (stripped[:8] or connection_name[:8]) or connection_name


def _connection_labels(env_connections: dict[str, Any]) -> dict[str, str]:
    """Assign every connection GUID in the environment catalogue a `Connection:` label.

    Base label is `apiId / displayName`, NOT bare `displayName` (bug found by
    the coordinator against real data): `displayName` in this catalogue names
    the ACCOUNT/credential a connection authenticates as (an email, or a
    literal `"{database} {server}"`), not the connector — real data has 9
    completely unrelated connectors (Forms, Dataflows, OneDrive, Office365
    x2, Excel, Power BI, Dataverse) all authenticated as the same mailbox, and
    a bare-displayName label collapsed all 9 into one `Connection:` node. That
    breaks §4.7's whole purpose: `neighbors("Connection: X")` must answer
    "what breaks if THIS credential is revoked", not "what breaks if this
    person leaves".

    Qualifying by apiId still leaves real collisions: the same connector
    re-registered several times under the same account (real data: up to 4
    separate `shared_powerbi` connections for one mailbox). Those get a short
    suffix off their own connectionName (`_short_suffix`), assigned from the
    FULL catalogue up front — not lazily the first time a label collision is
    noticed — so the same GUID gets the same label no matter which flow's
    metadata.json is extracted first, and two flows referencing two different
    colliding GUIDs never get merged onto one node.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for key, conn in env_connections.items():
        if not isinstance(conn, dict):
            continue
        base = (str(conn.get("apiId") or ""), str(conn.get("displayName") or key))
        groups.setdefault(base, []).append(key)

    labels: dict[str, str] = {}
    for (api_id, display), keys in groups.items():
        base_label = f"Connection: {api_id} / {display}"
        if len(keys) == 1:
            labels[keys[0]] = base_label
        else:
            for key in keys:
                labels[key] = f"{base_label}-{_short_suffix(key)}"
    return labels


def extract_powerautomate(path: Path, content: str | bytes | None = None) -> dict[str, Any]:
    """Extract a Flow node and its SQL/Connection/Solution edges from `metadata.json`.

    Every other `.json` file under `power-automate/` (definition.json,
    connections.json, the environment's `_connections.json`, the repo's
    `_env_map.json`) is read as a SIBLING from here, and returns no nodes or
    edges when this function is called on it directly — see the module
    docstring for why.
    """
    if path.name.lower() != "metadata.json":
        return {"nodes": [], "edges": []}

    try:
        if content is None:
            raw_text = _read_text_safe(path)
        elif isinstance(content, bytes):
            raw_text = content.decode("utf-8", errors="replace")
        else:
            raw_text = content
        metadata = json.loads(raw_text)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    if not isinstance(metadata, dict):
        return {"nodes": [], "edges": [], "error": "metadata.json is not a JSON object"}

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

    def _add_node(nid: str, label: str, extra: dict[str, Any] | None = None) -> str:
        if nid not in seen_ids:
            seen_ids.add(nid)
            node_dict: dict[str, Any] = {
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": "L1",
            }
            if extra:
                node_dict.update(extra)
            nodes.append(node_dict)
            edges.append({
                "source": file_nid,
                "target": nid,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": "L1",
                "weight": 1.0,
            })
        return nid

    def _add_edge(src: str, tgt: str, relation: str, context: str | None = None) -> None:
        if not src or not tgt or src == tgt:
            return
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": "L1",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    def _ref_stub(name: str, extra: dict[str, Any] | None = None) -> str:
        nid = _make_id(name)
        if nid not in seen_ids:
            seen_ids.add(nid)
            node_dict: dict[str, Any] = {
                "id": nid,
                "label": name,
                "file_type": "code",
                "source_file": "",
                "source_location": "",
                "type": "namespace",
            }
            if extra:
                node_dict.update(extra)
            nodes.append(node_dict)
        return nid

    flow_dir = path.parent
    display_name = metadata.get("displayName") or flow_dir.name
    # The environment is NOT a field in metadata.json — it is the name of the
    # grandparent directory the Fase-1 pull already chose:
    # power-automate/<environment>/<Display Name>/metadata.json.
    environment = flow_dir.parent.name

    # Display names are NOT unique in the environment: there are two flows both
    # called "Docentes OBS - Evaluacion docente" (see arquitectura_pull_github.md
    # §4.5). A bare `Flow: <displayName>` label gives them byte-identical labels.
    # They do not merge into one node — ids derive from the path — which is worse:
    # two distinct nodes share a label, so `neighbors(label="Flow: ...")` resolves
    # to whichever comes first and silently hides the other. The two real ones
    # differ by 8x in outgoing edges (39 vs 5), so the hidden answer is very
    # different from the shown one.
    #
    # No cross-file bookkeeping is needed to detect this: the pull already
    # resolved the collision by suffixing the FOLDER with the first 8 chars of the
    # flowId, so the folder name itself is the signal. Non-colliding flows keep a
    # clean label (49 of 51 cases).
    flow_id = str(metadata.get("flowId") or "")
    id_suffix = flow_id[:8]
    disambiguated = bool(id_suffix) and flow_dir.name.endswith(f"-{id_suffix}")
    flow_label = f"Flow: {display_name} ({id_suffix})" if disambiguated else f"Flow: {display_name}"

    flow_nid = _add_node(
        _make_id(stem, display_name),
        flow_label,
        extra={
            "trigger_types": metadata.get("triggerTypes") or [],
            "state": metadata.get("state"),
            "flow_id": metadata.get("flowId"),
            "environment": environment,
            # Raw display name kept so it is recoverable even when the label
            # carries the disambiguating suffix.
            "display_name": display_name,
        },
    )

    # ---- §4.5 Excel: resolve ExcelFile identity against _drive_items.json ----
    # Environment-level file written by pa_pull.ipynb's Graph resolution step
    # (sibling of `_connections.json`). Read as a SIBLING, same pattern as the
    # connections catalogue below: a missing/malformed file just means no
    # (drive, file) pair resolves, never an error for this extraction.
    drive_items: dict[str, Any] = {}
    drive_items_path = flow_dir.parent / "_drive_items.json"
    try:
        if drive_items_path.exists():
            loaded_drive_items = json.loads(_read_text_safe(drive_items_path))
            if isinstance(loaded_drive_items, dict):
                drive_items = loaded_drive_items
    except (OSError, json.JSONDecodeError):
        pass

    def _excel_file_identity(drive: str, file_id: str) -> tuple[str, dict[str, Any]]:
        """Label/extra to mint the `ExcelFile: <file_id>` node with.

        A resolved `_drive_items.json` entry for this (drive, file) pair means
        the label becomes the canonical URL instead — the same string
        `powerquery.extract_powerquery` mints from `Web.Contents(...)` for the
        Dataflow reading this same Excel file, so `_make_id()` collides and the
        two sides merge into one node at build time.
        """
        entry = drive_items.get(f"{drive}/{file_id}")
        if (isinstance(entry, dict) and entry.get("resolved")
                and isinstance(entry.get("canonical_url"), str) and entry["canonical_url"]):
            return entry["canonical_url"], {"drive_item_id": file_id, "drive": drive}
        return f"ExcelFile: {file_id}", {}

    # ---- §4.1 SQL: Flow --reads_from/writes_to--> esquema.tabla ----
    # §4.5 Excel: Flow --reads_from/writes_to--> ExcelTable: <file>/<table>
    # §4.5 Forms: Form: <form_id> --triggers--> Flow (§4.5 trigger)
    #            Flow --reads_from--> Form: <form_id> (§4.5bis read)
    # §4.5 OneDrive: Flow --writes_to--> OneDriveFolder: <folderPath>
    # §4.5 SharePoint: Flow --writes_to--> SharePointSite: <dataset>
    # §4.5 Excel-file containment: ExcelFile: <file> --contains--> ExcelTable: <file>/<table>
    definition_path = flow_dir / "definition.json"
    try:
        if definition_path.exists():
            definition = json.loads(_read_text_safe(definition_path))
            if isinstance(definition, dict):
                # Track deduplication for extended lineage (Excel, Forms, OneDrive, SharePoint).
                # SQL is NOT deduplicated — each table reference emits its own edge, maintaining
                # the original behavior where multiple SQL actions or multiple tables in one query
                # result in separate edges (e.g. two CTEs reading from the same table = 2 edges).
                dedup_edges: dict[tuple[str, str, str], str] = {}

                # Process triggers (distinct from actions for Form webhooks)
                trigger_root = definition.get("triggers")
                if isinstance(trigger_root, dict):
                    for action_name, action in _iter_actions(trigger_root):
                        inputs = action.get("inputs")
                        if not isinstance(inputs, dict):
                            continue
                        host = inputs.get("host")
                        if not isinstance(host, dict):
                            continue
                        api_id = str(host.get("apiId") or "")
                        operation_id = str(host.get("operationId") or "")
                        parameters = inputs.get("parameters")
                        if not isinstance(parameters, dict):
                            parameters = {}

                        # SQL action (native query, triggers rarely use this but possible)
                        if api_id.endswith(_SQL_API_ID_SUFFIX):
                            if operation_id == _SQL_OPERATION_ID:
                                query = parameters.get("query/query")
                                if isinstance(query, str):
                                    for schema, table, relation in _tables_from_sql(query):
                                        table_nid = _ref_stub(f"{schema}.{table}")
                                        _add_edge(flow_nid, table_nid, relation,
                                                  context=f"shared_sql action={action_name}")

                        # Forms webhook trigger: Form --triggers--> Flow (note: reversed direction)
                        elif api_id.endswith(_FORMS_API_ID_SUFFIX):
                            if operation_id == _FORMS_WEBHOOK_OPERATION_ID:
                                form_id = parameters.get("form_id")
                                if isinstance(form_id, str) and not form_id.startswith("@"):
                                    form_nid = _ref_stub(f"Form: {form_id}")
                                    dedup_key = (form_nid, "triggers", flow_nid)
                                    if dedup_key not in dedup_edges:
                                        dedup_edges[dedup_key] = f"forms trigger={action_name}"

                # Process actions
                actions_root = definition.get("actions")
                if isinstance(actions_root, dict):
                    for action_name, action in _iter_actions(actions_root):
                        inputs = action.get("inputs")
                        if not isinstance(inputs, dict):
                            continue
                        host = inputs.get("host")
                        if not isinstance(host, dict):
                            continue
                        api_id = str(host.get("apiId") or "")
                        operation_id = str(host.get("operationId") or "")
                        parameters = inputs.get("parameters")
                        if not isinstance(parameters, dict):
                            parameters = {}

                        # SQL action
                        if api_id.endswith(_SQL_API_ID_SUFFIX):
                            if operation_id == _SQL_OPERATION_ID:
                                query = parameters.get("query/query")
                                if isinstance(query, str):
                                    for schema, table, relation in _tables_from_sql(query):
                                        table_nid = _ref_stub(f"{schema}.{table}")
                                        _add_edge(flow_nid, table_nid, relation,
                                                  context=f"shared_sql action={action_name}")

                        # Excel operations
                        elif api_id.endswith(_EXCEL_API_ID_SUFFIX):
                            # AddRowV2 writes to ExcelTable
                            if operation_id == _EXCEL_ADDROW_OPERATION_ID:
                                drive = parameters.get("drive")
                                file_id = parameters.get("file")
                                table = parameters.get("table")
                                source = parameters.get("source")
                                if (isinstance(drive, str) and not drive.startswith("@") and
                                    isinstance(file_id, str) and not file_id.startswith("@") and
                                    isinstance(table, str) and not table.startswith("@")):
                                    extra_attrs = {"drive": drive}
                                    if isinstance(source, str) and not source.startswith("@"):
                                        extra_attrs["source"] = source
                                    table_nid = _ref_stub(f"ExcelTable: {file_id}/{table}",
                                                        extra=extra_attrs)
                                    excel_file_label, excel_file_extra = _excel_file_identity(drive, file_id)
                                    file_nid = _ref_stub(excel_file_label, extra=excel_file_extra)
                                    dedup_key = (flow_nid, "writes_to", table_nid)
                                    if dedup_key not in dedup_edges:
                                        dedup_edges[dedup_key] = f"excel action={action_name}"
                                    dedup_file_key = (file_nid, "contains", table_nid)
                                    if dedup_file_key not in dedup_edges:
                                        dedup_edges[dedup_file_key] = "excel_table_of_file"

                            # GetItems and GetItem read from ExcelTable
                            elif operation_id in (_EXCEL_GETITEMS_OPERATION_ID, _EXCEL_GETITEM_OPERATION_ID):
                                drive = parameters.get("drive")
                                file_id = parameters.get("file")
                                table = parameters.get("table")
                                source = parameters.get("source")
                                if (isinstance(drive, str) and not drive.startswith("@") and
                                    isinstance(file_id, str) and not file_id.startswith("@") and
                                    isinstance(table, str) and not table.startswith("@")):
                                    extra_attrs = {"drive": drive}
                                    if isinstance(source, str) and not source.startswith("@"):
                                        extra_attrs["source"] = source
                                    table_nid = _ref_stub(f"ExcelTable: {file_id}/{table}",
                                                        extra=extra_attrs)
                                    excel_file_label, excel_file_extra = _excel_file_identity(drive, file_id)
                                    file_nid = _ref_stub(excel_file_label, extra=excel_file_extra)
                                    dedup_key = (flow_nid, "reads_from", table_nid)
                                    if dedup_key not in dedup_edges:
                                        dedup_edges[dedup_key] = f"excel action={action_name}"
                                    dedup_file_key = (file_nid, "contains", table_nid)
                                    if dedup_file_key not in dedup_edges:
                                        dedup_edges[dedup_file_key] = "excel_table_of_file"

                        # Forms read operation
                        elif api_id.endswith(_FORMS_API_ID_SUFFIX):
                            if operation_id == _FORMS_GET_RESPONSE_OPERATION_ID:
                                form_id = parameters.get("form_id")
                                if isinstance(form_id, str) and not form_id.startswith("@"):
                                    form_nid = _ref_stub(f"Form: {form_id}")
                                    # The graph is undirected. If a flow reads from a form that also
                                    # triggers it (same form_id), two edges between the same pair of
                                    # nodes would collapse to one during build, with the last-written
                                    # relation winning. To preserve the more informative "triggers"
                                    # relation (which directly answers "what fires this form?"), we
                                    # omit the redundant "reads_from" edge when a trigger exists for
                                    # the same (form, flow) pair. A flow reading from a form that does
                                    # NOT trigger it is legitimate and is emitted normally.
                                    trigger_key = (form_nid, "triggers", flow_nid)
                                    if trigger_key in dedup_edges:
                                        # This form triggered the flow; the reads_from edge is redundant.
                                        continue
                                    dedup_key = (flow_nid, "reads_from", form_nid)
                                    if dedup_key not in dedup_edges:
                                        dedup_edges[dedup_key] = f"forms action={action_name}"

                        # OneDrive CreateFile write
                        elif api_id.endswith(_ONEDRIVE_API_ID_SUFFIX):
                            if operation_id == _ONEDRIVE_CREATEFILE_OPERATION_ID:
                                folder_path = parameters.get("folderPath")
                                if isinstance(folder_path, str) and not folder_path.startswith("@"):
                                    folder_nid = _ref_stub(f"OneDriveFolder: {folder_path}")
                                    dedup_key = (flow_nid, "writes_to", folder_nid)
                                    if dedup_key not in dedup_edges:
                                        dedup_edges[dedup_key] = f"onedrive action={action_name}"

                        # SharePoint CreateFile write
                        elif api_id.endswith(_SHAREPOINT_API_ID_SUFFIX):
                            if operation_id == _SHAREPOINT_CREATEFILE_OPERATION_ID:
                                dataset = parameters.get("dataset")
                                if isinstance(dataset, str) and not dataset.startswith("@"):
                                    site_nid = _ref_stub(f"SharePointSite: {dataset}")
                                    dedup_key = (flow_nid, "writes_to", site_nid)
                                    if dedup_key not in dedup_edges:
                                        dedup_edges[dedup_key] = f"sharepoint action={action_name}"

                # Emit deduplicated edges for extended lineage (Excel, Forms, OneDrive, SharePoint)
                for (src, relation, tgt), context in dedup_edges.items():
                    _add_edge(src, tgt, relation, context=context)
    except (OSError, json.JSONDecodeError):
        # A missing/malformed sibling definition.json must not cost the Flow
        # node its other edges (Connections, Solutions) — worst case this flow
        # loses SQL and extended lineage, which is exactly what an empty/broken file means.
        pass

    # ---- §4.7 Connections: Flow --uses--> Connection: <label> ----
    # Cross-reference key is `connectionName` in the flow's own connections.json,
    # NOT `id` (verified against real data: `id` there is always the literal
    # API path, e.g. "/providers/Microsoft.PowerApps/apis/shared_sql" — the
    # same value for every flow using that connector, never a GUID; the value
    # that actually keys into the environment's `_connections.json` is
    # `connectionName`). See this extractor's final report for the discrepancy
    # with the architecture doc, which described the `id` field as the lookup
    # key.
    connections_path = flow_dir / "connections.json"
    env_connections_path = flow_dir.parent / "_connections.json"
    try:
        if connections_path.exists() and env_connections_path.exists():
            flow_connections = json.loads(_read_text_safe(connections_path))
            env_connections = json.loads(_read_text_safe(env_connections_path))
            if isinstance(flow_connections, dict) and isinstance(env_connections, dict):
                conn_labels = _connection_labels(env_connections)
                for ref_name, ref in flow_connections.items():
                    if not isinstance(ref, dict):
                        continue
                    conn_key = ref.get("connectionName")
                    if conn_key:
                        # Resolved: the ref points at a real entry in the
                        # environment's connection catalogue.
                        conn = env_connections.get(conn_key)
                        if not isinstance(conn, dict):
                            continue
                        if conn.get("apiId") == _SHARED_LOGICFLOWS_API_ID:
                            continue
                        conn_nid = _ref_stub(
                            conn_labels[conn_key],
                            extra={
                                "connector_type": conn.get("apiId"),
                                "owner": conn.get("createdBy"),
                                "status": conn.get("status"),
                                "expires_at": conn.get("expirationTime"),
                                "connection_name": conn_key,
                            },
                        )
                        _add_edge(flow_nid, conn_nid, "uses", context=f"ref={ref_name}")
                        continue

                    logical_name = ref.get("connectionReferenceLogicalName")
                    if not logical_name:
                        # Neither a resolvable GUID nor a solution connection
                        # reference — nothing left to key a Connection node
                        # on. Deliberately skipped, not stubbed with a guess.
                        continue
                    # Unresolved: a solution-aware flow's connection reference,
                    # never registered with a literal connection GUID in this
                    # pull. `apiName` here is the SHORT connector name (e.g.
                    # "office365") as it appears in the FLOW's own
                    # connections.json — do not confuse it with `displayName`
                    # in the ENVIRONMENT catalogue above, which names the
                    # account/credential, not the connector; the two live in
                    # differently-shaped dicts read from different files.
                    api_name = ref.get("apiName") or "unknown"
                    conn_nid = _ref_stub(
                        f"Connection: {api_name} (ref: {logical_name})",
                        extra={
                            "connector_type": api_name,
                            "unresolved": True,
                            "connection_reference": logical_name,
                        },
                    )
                    _add_edge(flow_nid, conn_nid, "uses", context=f"ref={ref_name}")
    except (OSError, json.JSONDecodeError):
        pass

    # ---- §4.8 Solutions: Flow --belongs_to--> Solution: <uniquename> ----
    managed = set(metadata.get("managedSolutions") or [])
    for solution_name in metadata.get("solutions") or []:
        if not isinstance(solution_name, str):
            continue
        sol_nid = _ref_stub(
            f"Solution: {solution_name}",
            extra={"is_managed": solution_name in managed},
        )
        _add_edge(flow_nid, sol_nid, "belongs_to")

    return {"nodes": nodes, "edges": edges}
