"""Unit and integration tests for TMDL, Power Query, and Fabric metadata extractors."""
from __future__ import annotations

import tempfile
from pathlib import Path

import json

import graphify.extract as facade
from graphify.extract import extract
from graphify.extractors import LANGUAGE_EXTRACTORS
from graphify.extractors.fabric_config import extract_fabric_config
from graphify.extractors.json_config import extract_json
from graphify.extractors.powerautomate import extract_powerautomate
from graphify.extractors.powerquery import extract_powerquery
from graphify.extractors.tmdl import extract_tmdl


def test_fabric_extractors_registry_and_facade():
    """Verify all new extractors are registered in LANGUAGE_EXTRACTORS and re-exported from facade."""
    assert facade.extract_tmdl is extract_tmdl
    assert LANGUAGE_EXTRACTORS["tmdl"] is extract_tmdl

    assert facade.extract_powerquery is extract_powerquery
    assert LANGUAGE_EXTRACTORS["powerquery"] is extract_powerquery

    assert facade.extract_fabric_config is extract_fabric_config
    assert LANGUAGE_EXTRACTORS["fabric_config"] is extract_fabric_config


def test_tmdl_table_columns_measures_extraction(tmp_path: Path):
    """Verify TMDL table, columns, calculated columns, DAX measures and M partitions are extracted."""
    model_dir = tmp_path / "SalesModel.SemanticModel" / "definition" / "tables"
    model_dir.mkdir(parents=True)
    tmdl_content = """table 'Ventas'
\tlineageTag: a1b2c3d4-0000

\tcolumn ID_Venta
\t\tdataType: int64
\t\tsummarizeBy: count

\tcolumn Monto
\t\tdataType: double
\t\tsummarizeBy: sum

\tcolumn MontoConIVA = 'Ventas'[Monto] * 1.21

\tmeasure 'Total Ventas' = SUM('Ventas'[Monto])
\t\tformatString: #,##0.00

\tmeasure 'Total Con Descuento' = [Total Ventas] - [Descuento Global]
\t\tdescription: Total neto

\tpartition 'Ventas' = m
\t\tmode: import
\t\tsource =
\t\t\tlet
\t\t\t    Source = Sql.Database("sqlserver.db.windows.net", "SalesDB", [Query="SELECT * FROM [dbo].[Ventas Fact]"])
\t\t\tin
\t\t\t    Source
"""
    tmdl_file = model_dir / "Ventas.tmdl"
    tmdl_file.write_text(tmdl_content, encoding="utf-8")

    res = extract_tmdl(tmdl_file)
    labels = {n["label"] for n in res["nodes"]}

    # Table and columns — entity nodes carry NO file-stem prefix so they reconcile
    # with the same names as referenced from a PBIR visual.
    assert "Ventas" in labels
    assert "Ventas[ID_Venta]" in labels
    assert "Ventas[Monto]" in labels
    assert "Ventas[MontoConIVA]" in labels
    # Measures are table-qualified (a visual references them as 'Ventas'[Total Ventas]).
    assert "Ventas[Total Ventas]" in labels
    assert "Ventas[Total Con Descuento]" in labels

    edges = res["edges"]
    lbl = {n["id"]: n["label"] for n in res["nodes"]}
    reads_from = {(lbl.get(e["source"], e["source"]), lbl.get(e["target"], e["target"]))
                  for e in edges if e["relation"] == "reads_from"}

    # measure -> measure dependency
    assert ("Ventas[Total Con Descuento]", "Ventas[Total Ventas]") in reads_from
    # the SemanticModel node is minted and owns the table
    assert "SemanticModel: SalesModel" in labels
    assert any(lbl.get(e["source"]) == "SemanticModel: SalesModel"
               and lbl.get(e["target"]) == "Ventas"
               and e["relation"] == "contains" for e in edges)
    # partition M lineage -> warehouse table, bracketed name with a space survives
    assert ("Ventas", "dbo.Ventas Fact") in reads_from


def test_tmdl_partition_navigation_source(tmp_path: Path):
    """A record-navigation M partition (no [Query=]) still yields warehouse lineage."""
    tdir = tmp_path / "M.SemanticModel" / "definition" / "tables"
    tdir.mkdir(parents=True)
    (tdir / "Dim.tmdl").write_text(
        "table Dim\n"
        "\tcolumn A\n\t\tdataType: string\n"
        "\tpartition Dim = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t    Origen = Sql.Database("srv", "WH"),\n'
        '\t\t\t    d = Origen{[Schema="dm", Item="(Dim)_Cosa"]}[Data]\n'
        "\t\t\tin\n\t\t\t    d\n",
        encoding="utf-8",
    )
    res = extract_tmdl(tdir / "Dim.tmdl")
    lbl = {n["id"]: n["label"] for n in res["nodes"]}
    reads_from = {(lbl.get(e["source"]), lbl.get(e["target"]))
                  for e in res["edges"] if e["relation"] == "reads_from"}
    assert ("Dim", "dm.(Dim)_Cosa") in reads_from


def test_tmdl_model_file_lists_tables(tmp_path: Path):
    """model.tmdl `ref table` lines become SemanticModel -> contains -> Table."""
    mdir = tmp_path / "Foo.SemanticModel" / "definition"
    mdir.mkdir(parents=True)
    (mdir / "model.tmdl").write_text(
        "model Model\n\tculture: es-ES\n\n"
        "ref table Ventas\n"
        "ref table Clientes\n"
        "ref table LocalDateTable_abc\n",
        encoding="utf-8",
    )
    res = extract_tmdl(mdir / "model.tmdl")
    lbl = {n["id"]: n["label"] for n in res["nodes"]}
    contains = {(lbl.get(e["source"]), lbl.get(e["target"]))
               for e in res["edges"] if e["relation"] == "contains"}
    assert ("SemanticModel: Foo", "Ventas") in contains
    assert ("SemanticModel: Foo", "Clientes") in contains
    # auto date tables are noise, never linked
    assert not any(t == "LocalDateTable_abc" for _, t in contains)
    # model / table / measure nodes are sourceless `namespace` stubs so they
    # reconcile with PBIR by label; provenance rides the contains edge + file node.
    model_node = next(n for n in res["nodes"] if n["label"] == "SemanticModel: Foo")
    assert model_node.get("type") == "namespace"
    assert not model_node["source_file"]
    contains_edge = next(e for e in res["edges"]
                         if e["relation"] == "contains" and lbl.get(e["target"]) == "Ventas")
    assert contains_edge["source_file"].endswith("model.tmdl")


def test_tmdl_relationships_extraction(tmp_path: Path):
    """Verify TMDL relationships file extracts model relations and cardinality."""
    rel_content = """relationship rel_ventas_clientes
\tcrossFilteringBehavior: bothDirections
\tfromCardinality: many
\ttoCardinality: one
\tfromColumn: 'Ventas'.ID_Cliente
\ttoColumn: Clientes.ID_Cliente
"""
    rel_file = tmp_path / "relationships.tmdl"
    rel_file.write_text(rel_content, encoding="utf-8")

    res = extract_tmdl(rel_file)
    labels = {n["label"] for n in res["nodes"]}
    assert "Ventas" in labels
    assert "Clientes" in labels
    assert "Ventas[ID_Cliente]" in labels
    assert "Clientes[ID_Cliente]" in labels

    rel_edges = [e for e in res["edges"] if e["relation"] == "relates_to"]
    assert len(rel_edges) >= 2  # Table-level and column-level
    assert any("bothDirections" in (e.get("context") or "") for e in rel_edges)


def test_powerquery_extraction(tmp_path: Path):
    """Verify Power Query M extraction with steps, joins and data sources."""
    pq_content = """section Section1;

shared Clientes = let
    Source = Sql.Database("server.database.windows.net", "MainDB"),
    Tabla = Source{[Schema="dbo", Item="Dim_Clientes"]}[Data]
in
    Tabla;

shared VentasConsolidadas = let
    Source = Fabric.Warehouse([CreateNavigationProperties = false]),
    Nav = Source{[warehouseId = "wh-1234"]}[Data],
    Data = Nav{[Schema = "stg", Item = "VentasRaw"]}[Data],
    Joined = Table.NestedJoin(Data, {"ID_Cliente"}, Clientes, {"ID_Cliente"}, "ClienteInfo")
in
    Joined;

[DataDestinations = {[Definition = [QueryName = "VentasConsolidadas"]]} ]
"""
    df_dir = tmp_path / "DF_Ventas.Dataflow"
    df_dir.mkdir()
    pq_file = df_dir / "mashup.pq"
    pq_file.write_text(pq_content, encoding="utf-8")

    res = extract_powerquery(pq_file)
    labels = {n["label"] for n in res["nodes"]}

    # Query labels are qualified by the owning Fabric item: the bare M identifier
    # is not unique across the dozens of dataflows a single repo holds.
    assert "DF_Ventas[Clientes]" in labels
    assert "DF_Ventas[VentasConsolidadas]" in labels

    # Tables are schema-qualified so they land on the same node the SQL
    # extractor mints for the same table, instead of a bare-name ghost.
    assert "dbo.Dim_Clientes" in labels
    assert "stg.VentasRaw" in labels

    edges = res["edges"]
    # Check SQL & Warehouse source edges
    assert any("sql_database" in (e.get("context") or "") for e in edges)
    assert any("fabric_warehouse" in (e.get("context") or "") for e in edges)
    assert any("table_join" in (e.get("context") or "") for e in edges)


def test_fabric_config_extraction(tmp_path: Path):
    """Verify .platform, .pbir and .pbism file extraction."""
    # 1. .platform
    platform_file = tmp_path / ".platform"
    platform_file.write_text('{"metadata": {"type": "Dataflow", "displayName": "DF_Ventas", "description": "ETL Ventas"}}', encoding="utf-8")
    res_plat = extract_fabric_config(platform_file)
    assert any("Dataflow: DF_Ventas" in n["label"] for n in res_plat["nodes"])

    # 2. .pbir
    pbir_file = tmp_path / "definition.pbir"
    pbir_file.write_text('{"datasetReference": {"byConnection": {"connectionString": "Data Source=\\"pbiazure\\";initial catalog=\\"SalesModel\\""}}}', encoding="utf-8")
    res_pbir = extract_fabric_config(pbir_file)
    assert any("SalesModel" in n["label"] for n in res_pbir["nodes"])
    # Same relation name pbir.py's own datasetReference re-emit uses (see its
    # module docstring: "Report -> targets_semantic_model -> SemanticModel") —
    # not "references", which this test asserted before ever being run.
    assert any(e["relation"] == "targets_semantic_model" for e in res_pbir["edges"])


def test_powerquery_quoted_query_names_with_spaces(tmp_path: Path):
    """`shared #"Name With Spaces"` must be extracted, not silently dropped.

    The bare-token name pattern this replaced dropped ~16% of the declarations in
    a real Fabric repo and, because the query-body lookahead keyed on the same
    pattern, folded each dropped body into the preceding query's.
    """
    pq_content = (
        'section Section1;\n\n'
        'shared #"PFU v_lead_pfuonline" = let\n'
        '    Source = Sql.Database("srv", "DB", [Query = "SELECT * FROM [stg].[atn_lead_Raw]"])\n'
        'in\n    Source;\n\n'
        'shared Otra = let\n'
        '    Source = Sql.Database("srv", "DB", [Query = "SELECT * FROM [ods].[Otra_Base]"])\n'
        'in\n    Source;\n'
    )
    df_dir = tmp_path / "SI_ATENEA_2.Dataflow"
    df_dir.mkdir()
    (df_dir / "mashup.pq").write_text(pq_content, encoding="utf-8")

    res = extract_powerquery(df_dir / "mashup.pq")
    by_id = {n["id"]: n["label"] for n in res["nodes"]}
    labels = set(by_id.values())
    assert "SI_ATENEA_2[PFU v_lead_pfuonline]" in labels
    assert "SI_ATENEA_2[Otra]" in labels

    # Each query keeps its own body: the spaced-name query must not have been
    # absorbed into a neighbour, which is how the old pattern failed.
    reads = {
        (by_id.get(e["source"]), by_id.get(e["target"]))
        for e in res["edges"]
        if e["relation"] == "reads_from"
    }
    assert ("SI_ATENEA_2[PFU v_lead_pfuonline]", "stg.atn_lead_Raw") in reads
    assert ("SI_ATENEA_2[Otra]", "ods.Otra_Base") in reads


def test_powerquery_datadestination_binds_to_its_own_table(tmp_path: Path):
    """Each [DataDestinations] block resolves to ITS OWN destination table.

    The previous implementation searched the whole file for the first
    `Item = "..."` and reused it for every destination, so a file with three
    destinations emitted the same target three times — two of them false.
    """
    pq_content = (
        'section Section1;\n\n'
        '[DataDestinations = {[Definition = [Kind = "Reference", QueryName = "A_DataDestination"], '
        'Settings = [Kind = "Manual"]]}]\n'
        'shared A = let Source = Sql.Database("srv", "DB", '
        '[Query = "SELECT 1 FROM [stg].[Src_A]"]) in Source;\n\n'
        '[DataDestinations = {[Definition = [Kind = "Reference", QueryName = "B_DataDestination"], '
        'Settings = [Kind = "Manual"]]}]\n'
        'shared B = let Source = Sql.Database("srv", "DB", '
        '[Query = "SELECT 1 FROM [stg].[Src_B]"]) in Source;\n\n'
        'shared A_DataDestination = let\n'
        '    Pattern = Fabric.Warehouse([HierarchicalNavigation = null]),\n'
        '    Nav = Pattern{[warehouseId = "wh-1"]}[Data],\n'
        '    T = Nav{[Schema = "ods", Item = "Tabla_A"]}[Data]\n'
        'in\n    T;\n\n'
        'shared B_DataDestination = let\n'
        '    Pattern = Fabric.Warehouse([HierarchicalNavigation = true]),\n'
        '    Nav = Pattern{[displayName = "SI_SQLandia"]}[Data],\n'
        '    Sch = Nav{[Schema = "ods"]}[Data],\n'
        '    T = Sch{[Name = "Tabla_B_Distinta"]}[Data]\n'
        'in\n    T;\n'
    )
    df_dir = tmp_path / "SI_ODS_X.Dataflow"
    df_dir.mkdir()
    (df_dir / "mashup.pq").write_text(pq_content, encoding="utf-8")

    res = extract_powerquery(df_dir / "mashup.pq")
    by_id = {n["id"]: n["label"] for n in res["nodes"]}
    writes = {
        (by_id.get(e["source"]), by_id.get(e["target"]))
        for e in res["edges"]
        if e["relation"] == "writes_to"
    }
    # Distinct targets, and the hierarchical (Schema then Name) form resolves to
    # a table whose name differs from the query that feeds it.
    assert ("SI_ODS_X[A]", "ods.Tabla_A") in writes
    assert ("SI_ODS_X[B]", "ods.Tabla_B_Distinta") in writes
    assert len(writes) == 2


def test_powerquery_native_sql_tables(tmp_path: Path):
    """Tables named only inside `[Query = "..."]` native SQL are still lineage."""
    pq_content = (
        'section Section1;\n\n'
        'shared Q = let\n'
        '    Source = Sql.Database("srv", "DB", [Query = "#(lf)WITH ctc AS (#(lf)'
        'SELECT * FROM [stg].[atn_contact_Raw]#(lf))#(lf)'
        'SELECT * FROM ctc JOIN ods.Calendario c ON 1=1"])\n'
        'in\n    Source;\n'
    )
    df_dir = tmp_path / "DF_N.Dataflow"
    df_dir.mkdir()
    (df_dir / "mashup.pq").write_text(pq_content, encoding="utf-8")

    res = extract_powerquery(df_dir / "mashup.pq")
    labels = {n["label"] for n in res["nodes"]}
    assert "stg.atn_contact_Raw" in labels
    assert "ods.Calendario" in labels
    # `ctc` is a CTE, not a table: unqualified names must not become nodes.
    assert "ctc" not in labels


def test_fabric_config_links_dataflow_to_its_mashup(tmp_path: Path):
    """A .platform item must link to the sibling file holding its definition.

    Without this edge the item node is a degree-1 orphan and the Mashup lineage
    extracted from the sibling mashup.pq sits in a disconnected island —
    `neighbors("Dataflow: X")` returned nothing at all.
    """
    df_dir = tmp_path / "DF_Ventas.Dataflow"
    df_dir.mkdir()
    (df_dir / "mashup.pq").write_text("section Section1;\n", encoding="utf-8")
    platform_file = df_dir / ".platform"
    platform_file.write_text(
        '{"metadata": {"type": "Dataflow", "displayName": "DF_Ventas"}}', encoding="utf-8"
    )

    res = extract_fabric_config(platform_file)
    targets = {e["target"] for e in res["edges"] if e.get("context") == "item_definition"}
    assert targets, "no item_definition edge emitted"

    mashup_nid = next(
        n["id"] for n in extract_powerquery(df_dir / "mashup.pq")["nodes"]
        if n["label"] == "mashup.pq"
    )
    # The id the .platform extractor points at must be exactly the one the
    # powerquery extractor mints for the same file, or the edge dangles: both
    # must be the same key going into extract()'s file-id remap.
    assert mashup_nid in targets


def test_fabric_config_no_definition_edge_when_file_absent(tmp_path: Path):
    """No sibling mashup.pq on disk -> no dangling edge to a node nobody mints."""
    df_dir = tmp_path / "DF_Vacio.Dataflow"
    df_dir.mkdir()
    platform_file = df_dir / ".platform"
    platform_file.write_text(
        '{"metadata": {"type": "Dataflow", "displayName": "DF_Vacio"}}', encoding="utf-8"
    )
    res = extract_fabric_config(platform_file)
    assert not [e for e in res["edges"] if e.get("context") == "item_definition"]


# ---------------------------------------------------------------------------
# Power Automate extractor (graphify.extractors.powerautomate)
# ---------------------------------------------------------------------------

def _make_flow(tmp_path: Path, flow_name: str, *, environment: str = "grupo-planeta",
                metadata: dict | None = None, definition: dict | None = None,
                connections: dict | None = None) -> Path:
    """Build a `power-automate/<environment>/<flow_name>/` dir and return its metadata.json path."""
    flow_dir = tmp_path / "power-automate" / environment / flow_name
    flow_dir.mkdir(parents=True)
    meta = {
        "displayName": flow_name,
        "flowId": "ab1afc01-93ec-2f4b-3f08-25db43b94f19",
        "state": "Started",
        "triggerTypes": ["Request"],
        "solutions": [],
        "managedSolutions": [],
    }
    if metadata:
        meta.update(metadata)
    metadata_path = flow_dir / "metadata.json"
    metadata_path.write_text(json.dumps(meta), encoding="utf-8")
    if definition is not None:
        (flow_dir / "definition.json").write_text(json.dumps(definition), encoding="utf-8")
    if connections is not None:
        (flow_dir / "connections.json").write_text(json.dumps(connections), encoding="utf-8")
    return metadata_path


def _sql_action(query: str) -> dict:
    """A WDL action shaped like the real `ExecutePassThroughNativeQuery_V2` actions."""
    return {
        "inputs": {
            "host": {
                "apiId": "/providers/Microsoft.PowerApps/apis/shared_sql",
                "connectionName": "shared_sql",
                "operationId": "ExecutePassThroughNativeQuery_V2",
            },
            "parameters": {
                "database": "@outputs('Config')?['sqlDatabase']",
                "query/query": query,
            },
        },
    }


def test_powerautomate_registered_in_dispatch_and_facade(tmp_path: Path):
    """Verify the extractor is registered in LANGUAGE_EXTRACTORS/facade AND wired
    into the dispatcher for every .json under power-automate/, not just
    metadata.json -- the trap the brief calls out: definition.json is 250KB+ of
    WDL that would otherwise fall through to the generic JSON extractor and
    explode into thousands of disconnected islands, exactly like the PBIR bug
    this same dispatcher already guards against."""
    assert facade.extract_powerautomate is extract_powerautomate
    assert LANGUAGE_EXTRACTORS["powerautomate"] is extract_powerautomate

    base = tmp_path / "power-automate" / "grupo-planeta" / "Some Flow"
    assert facade._get_extractor(base / "metadata.json") is extract_powerautomate
    assert facade._get_extractor(base / "definition.json") is extract_powerautomate
    assert facade._get_extractor(base / "connections.json") is extract_powerautomate
    assert facade._get_extractor(tmp_path / "power-automate" / "grupo-planeta" / "_connections.json") is extract_powerautomate
    assert facade._get_extractor(tmp_path / "power-automate" / "_env_map.json") is extract_powerautomate

    # A .json OUTSIDE power-automate/ must keep going to the generic extractor.
    assert facade._get_extractor(tmp_path / "config" / "settings.json") is extract_json


def test_powerautomate_flow_node_attributes(tmp_path: Path):
    """Flow node carries trigger_types (list, plural key), state, flow_id, and an
    environment derived from the grandparent directory name -- NOT a field read
    out of metadata.json, which doesn't have one (confirmed against real data)."""
    metadata_path = _make_flow(
        tmp_path, "Matriculacion_OBS",
        metadata={"triggerTypes": ["Request", "Recurrence"], "state": "Started",
                  "flowId": "ab1afc01-93ec-2f4b-3f08-25db43b94f19"},
    )
    res = extract_powerautomate(metadata_path)
    flow_nodes = [n for n in res["nodes"] if n["label"] == "Flow: Matriculacion_OBS"]
    assert len(flow_nodes) == 1
    flow = flow_nodes[0]
    assert flow["trigger_types"] == ["Request", "Recurrence"]
    assert flow["state"] == "Started"
    assert flow["flow_id"] == "ab1afc01-93ec-2f4b-3f08-25db43b94f19"
    assert flow["environment"] == "grupo-planeta"

    # A Flow with zero outgoing edges is the exact symptom the Dataflow-island
    # bug had (memoria graphify-indexado-dataflows) -- guard against regressing
    # into it now that this flow has no SQL/connections/solutions.
    outgoing = [e for e in res["edges"] if e["source"] == flow["id"]]
    assert outgoing == []


def test_powerautomate_sql_reads_and_writes(tmp_path: Path):
    """SQL action nested inside a Scope (recursing through `actions`) yields
    reads_from for FROM/JOIN and writes_to for INSERT INTO, with labels matching
    the `esquema.tabla` convention SQL/PowerQuery extractors already mint --
    this is what lets neighbors('dm.(Hec)_VOC_HLO_Respuestas') find the flow."""
    query = (
        "INSERT INTO [dbo].[Log] (Msg) "
        "SELECT R.IdAlumno FROM [dm].[(Hec)_VOC_HLO_Respuestas] R "
        "INNER JOIN [dm].[(Dim)_VOC_HLO_Proyecto] P ON P.IdProyecto = R.IdProyecto"
    )
    definition = {
        "triggers": {"manual": {"type": "Request", "kind": "Http"}},
        "actions": {
            "Scope_1": {
                "type": "Scope",
                "actions": {"Run_SQL": _sql_action(query)},
            },
        },
    }
    metadata_path = _make_flow(tmp_path, "Matriculacion_OBS", definition=definition)
    res = extract_powerautomate(metadata_path)

    by_id = {n["id"]: n["label"] for n in res["nodes"]}
    flow_id = next(n["id"] for n in res["nodes"] if n["label"] == "Flow: Matriculacion_OBS")

    reads = {by_id[e["target"]] for e in res["edges"]
              if e["source"] == flow_id and e["relation"] == "reads_from"}
    writes = {by_id[e["target"]] for e in res["edges"]
              if e["source"] == flow_id and e["relation"] == "writes_to"}

    assert reads == {"dm.(Hec)_VOC_HLO_Respuestas", "dm.(Dim)_VOC_HLO_Proyecto"}
    assert writes == {"dbo.Log"}


def test_powerautomate_connections_excludes_shared_logicflows(tmp_path: Path):
    """Connection nodes are minted for real credentials, never for
    shared_logicflows -- real data showed 54/91 connections are that internal
    'Run a Child Flow' connector (53 of which sit in status Error), and
    including them would make god_nodes() over Connection: report plumbing
    instead of the most-shared real credential, and manufacture a false
    governance alarm out of the Error status."""
    connections = {
        "shared_sql": {"connectionName": "sql-conn-guid", "id": "/providers/Microsoft.PowerApps/apis/shared_sql"},
        "shared_logicflows": {"connectionName": "logicflow-guid", "id": "/providers/Microsoft.PowerApps/apis/shared_logicflows"},
    }
    metadata_path = _make_flow(tmp_path, "Matriculacion_OBS", connections=connections)
    env_connections = {
        "sql-conn-guid": {
            "apiId": "shared_sql", "displayName": "SI_SQLandia (SQL server)",
            "createdBy": "studentintelligence@planetadeagostini.es",
            "status": "Connected", "expirationTime": "2026-09-14T12:23:43Z",
        },
        "logicflow-guid": {
            "apiId": "shared_logicflows", "displayName": "UCMA - TFB - Email 1",
            "createdBy": "5aa70b5a-18ab-4181-94d2-81b773814cd2",
            "status": "Error", "expirationTime": None,
        },
    }
    (tmp_path / "power-automate" / "grupo-planeta" / "_connections.json").write_text(
        json.dumps(env_connections), encoding="utf-8"
    )

    res = extract_powerautomate(metadata_path)
    conn_nodes = [n for n in res["nodes"] if n["label"].startswith("Connection:")]
    assert len(conn_nodes) == 1
    # Label is qualified by apiId (bug A fix) -- bare displayName alone would
    # collide with every other connector authenticated as the same account.
    assert conn_nodes[0]["label"] == "Connection: shared_sql / SI_SQLandia (SQL server)"
    assert conn_nodes[0]["connector_type"] == "shared_sql"
    assert conn_nodes[0]["status"] == "Connected"
    assert conn_nodes[0]["connection_name"] == "sql-conn-guid"

    flow_id = next(n["id"] for n in res["nodes"] if n["label"] == "Flow: Matriculacion_OBS")
    uses_edges = [e for e in res["edges"] if e["source"] == flow_id and e["relation"] == "uses"]
    assert len(uses_edges) == 1
    assert uses_edges[0]["target"] == conn_nodes[0]["id"]


def test_powerautomate_connections_disambiguates_same_account_same_connector(tmp_path: Path):
    """Bug A: two DISTINCT connection GUIDs, same connector, same account
    (real data: up to 4 separate shared_powerbi connections for one mailbox)
    must mint TWO nodes, not collapse into one -- otherwise
    neighbors('Connection: X') answers for the wrong credential and revoking
    one doesn't tell you the other flow is still fine."""
    # Realistic connectionName shapes (from real data): distinct GUIDs, so
    # the first 8 chars used for disambiguation genuinely differ.
    guid_a = "0300f615-a02b-44a7-829d-0fbe71c2794e"
    guid_b = "8ce76975-4fb1-4f40-a1f2-8948fc9d05c1"
    metadata_path_a = _make_flow(tmp_path, "Flow_A", connections={"shared_powerbi": {"connectionName": guid_a}})
    metadata_path_b = _make_flow(tmp_path, "Flow_B", connections={"shared_powerbi": {"connectionName": guid_b}})
    env_connections = {
        guid_a: {"apiId": "shared_powerbi", "displayName": "UGZD68@psoplaneta.com",
                 "createdBy": "u1", "status": "Connected", "expirationTime": None},
        guid_b: {"apiId": "shared_powerbi", "displayName": "UGZD68@psoplaneta.com",
                 "createdBy": "u1", "status": "Connected", "expirationTime": None},
    }
    (tmp_path / "power-automate" / "grupo-planeta" / "_connections.json").write_text(
        json.dumps(env_connections), encoding="utf-8"
    )

    res_a = extract_powerautomate(metadata_path_a)
    res_b = extract_powerautomate(metadata_path_b)
    label_a = next(n["label"] for n in res_a["nodes"] if n["label"].startswith("Connection:"))
    label_b = next(n["label"] for n in res_b["nodes"] if n["label"].startswith("Connection:"))

    assert label_a != label_b
    # Both carry the common base, disambiguated by a suffix off their own GUID.
    assert label_a.startswith("Connection: shared_powerbi / UGZD68@psoplaneta.com-")
    assert label_b.startswith("Connection: shared_powerbi / UGZD68@psoplaneta.com-")

    # Re-extracting flow A alone (as if flow B were processed in a different
    # run/order) must yield the EXACT SAME label -- the disambiguation comes
    # from the full environment catalogue, not from processing order.
    res_a_again = extract_powerautomate(metadata_path_a)
    label_a_again = next(n["label"] for n in res_a_again["nodes"] if n["label"].startswith("Connection:"))
    assert label_a_again == label_a


def test_powerautomate_unresolved_connection_reference(tmp_path: Path):
    """Bug B: a solution-aware flow's connection reference with no literal
    connectionName (only connectionReferenceLogicalName) must still produce a
    Connection node and a `uses` edge, marked unresolved -- silently dropping
    it made the graph claim a flow doesn't use Office 365 when it does (real
    data: PRISMA_ESD_EmailReceived and 17 other refs across the pull)."""
    connections = {
        "shared_office365-1": {
            "apiName": "office365",
            "connectionName": None,
            "connectionReferenceLogicalName": "new_sharedoffice365_4262b",
            "displayName": "Office 365 Outlook",
        },
    }
    metadata_path = _make_flow(tmp_path, "PRISMA_ESD_EmailReceived", connections=connections)
    # No _connections.json needed -- nothing here can resolve to a GUID anyway.
    (tmp_path / "power-automate" / "grupo-planeta" / "_connections.json").write_text("{}", encoding="utf-8")

    res = extract_powerautomate(metadata_path)
    conn_nodes = [n for n in res["nodes"] if n["label"].startswith("Connection:")]
    assert len(conn_nodes) == 1
    conn = conn_nodes[0]
    assert conn["label"] == "Connection: office365 (ref: new_sharedoffice365_4262b)"
    assert conn["unresolved"] is True
    assert conn["connection_reference"] == "new_sharedoffice365_4262b"
    assert conn["connector_type"] == "office365"
    assert "owner" not in conn
    assert "status" not in conn

    flow_id = next(n["id"] for n in res["nodes"] if n["label"].startswith("Flow:"))
    uses_edges = [e for e in res["edges"] if e["source"] == flow_id and e["relation"] == "uses"]
    assert len(uses_edges) == 1
    assert uses_edges[0]["target"] == conn["id"]


def test_powerautomate_connection_ref_with_neither_key_is_skipped(tmp_path: Path):
    """No connectionName AND no connectionReferenceLogicalName -> nothing to
    key a node on, so it is skipped rather than stubbed with a guess."""
    connections = {"shared_mystery": {"apiName": "mystery"}}
    metadata_path = _make_flow(tmp_path, "Flow_Mystery", connections=connections)
    (tmp_path / "power-automate" / "grupo-planeta" / "_connections.json").write_text("{}", encoding="utf-8")

    res = extract_powerautomate(metadata_path)
    assert not [n for n in res["nodes"] if n["label"].startswith("Connection:")]


def test_powerautomate_sql_ignores_comment_text_and_ctes(tmp_path: Path):
    """Bug C-adjacent, found while re-verifying: a `/* ... */` comment can
    contain a stray 'JOIN word.\\n\\nWORD' shape (real query had a Spanish
    comment explaining a JOIN that was deliberately NOT done, mentioning
    'intencionado.' followed by a blank line then 'LOOKUPVALUE'), which must
    NOT become a fake table node. CTEs (single-part names after FROM/JOIN)
    must not become nodes either -- only real schema.table refs, bracketed or
    not, embedded in a realistic WDL query with @{...} noise and DECLARE."""
    query = (
        "DECLARE @BrandKey VARCHAR(50) = '@{outputs('Config')?['brandKey']}';\n"
        "-- pull the raw responses first\n"
        "WITH ProjectScope AS (\n"
        "    SELECT DISTINCT P.IdProyecto\n"
        "    FROM [dm].[(Dim)_VOC_HLO_Proyecto] P\n"
        "    INNER JOIN [dm].[(Dim)_VOC_HLO_Marca] M ON M.Clave_Marca = P.Clave_Marca\n"
        "    WHERE P.Clave_Marca = @BrandKey\n"
        "),\n"
        "ScopeRows AS (\n"
        "    SELECT R.IdAlumno FROM [dm].[(Hec)_VOC_HLO_Respuestas] R\n"
        "    INNER JOIN ProjectScope S ON S.IdProyecto = R.IdProyecto\n"
        "    /*\n"
        "        LEFT JOIN intencionado.\n\n"
        "        LOOKUPVALUE de DAX puede devolver BLANK. No lo hacemos.\n"
        "    */\n"
        ")\n"
        "SELECT * FROM ScopeRows JOIN ops.bb_info_audit A ON 1 = 1"
    )
    definition = {"actions": {"Run_SQL": _sql_action(query)}}
    metadata_path = _make_flow(tmp_path, "Comment_Test_Flow", definition=definition)
    res = extract_powerautomate(metadata_path)

    labels = {n["label"] for n in res["nodes"]
              if not n["label"].startswith(("Flow:", "Connection:", "Solution:")) and n["label"] != "metadata.json"}
    assert labels == {
        "dm.(Dim)_VOC_HLO_Proyecto",
        "dm.(Dim)_VOC_HLO_Marca",
        "dm.(Hec)_VOC_HLO_Respuestas",
        "ops.bb_info_audit",
    }
    # No brackets ever survive into a label, and no CTE (ProjectScope,
    # ScopeRows) or comment fragment (intencionado, LOOKUPVALUE) leaked in.
    assert not any("[" in l or "]" in l for l in labels)
    assert "ProjectScope" not in labels and "ScopeRows" not in labels
    assert not any("intencionado" in l or "LOOKUPVALUE" in l for l in labels)


def test_powerautomate_solutions_is_managed(tmp_path: Path):
    """Each entry in `solutions` gets a belongs_to edge; `is_managed` is True only
    for the subset also present in `managedSolutions` -- real data (pull
    2026-09-15) has exactly one flow, 'SI - HLO + UCMA _ Generación PDF
    Docentes (No Demo)', where that subset is non-empty."""
    metadata_path = _make_flow(
        tmp_path, "Matriculacion_OBS",
        metadata={"solutions": ["Active", "VOC_Matriculacion"], "managedSolutions": ["VOC_Matriculacion"]},
    )
    res = extract_powerautomate(metadata_path)
    sol_nodes = {n["label"]: n for n in res["nodes"] if n["label"].startswith("Solution:")}
    assert set(sol_nodes) == {"Solution: Active", "Solution: VOC_Matriculacion"}
    assert sol_nodes["Solution: Active"]["is_managed"] is False
    assert sol_nodes["Solution: VOC_Matriculacion"]["is_managed"] is True

    flow_id = next(n["id"] for n in res["nodes"] if n["label"] == "Flow: Matriculacion_OBS")
    belongs_to_targets = {e["target"] for e in res["edges"]
                           if e["source"] == flow_id and e["relation"] == "belongs_to"}
    assert belongs_to_targets == {sol_nodes["Solution: Active"]["id"], sol_nodes["Solution: VOC_Matriculacion"]["id"]}


def test_powerautomate_no_solution_edges_when_list_empty(tmp_path: Path):
    """Empty `solutions` -> zero belongs_to edges, which is a valid governance
    signal (flow outside any solution), not an extraction gap."""
    metadata_path = _make_flow(tmp_path, "Sin_Solucion", metadata={"solutions": [], "managedSolutions": []})
    res = extract_powerautomate(metadata_path)
    assert not [e for e in res["edges"] if e["relation"] == "belongs_to"]


def test_powerautomate_sibling_json_files_emit_nothing_directly(tmp_path: Path):
    """definition.json/connections.json/_connections.json/_env_map.json are
    consumed as siblings from metadata.json's own extraction and must return no
    nodes/edges when the dispatcher calls this extractor on them directly --
    otherwise the Flow node (or the environment's Connection catalogue) gets
    double-minted."""
    flow_dir = tmp_path / "power-automate" / "grupo-planeta" / "Some Flow"
    flow_dir.mkdir(parents=True)
    definition_path = flow_dir / "definition.json"
    definition_path.write_text(json.dumps({"actions": {}, "triggers": {}}), encoding="utf-8")
    connections_path = flow_dir / "connections.json"
    connections_path.write_text(json.dumps({"shared_sql": {"connectionName": "x"}}), encoding="utf-8")

    for p in (definition_path, connections_path):
        res = extract_powerautomate(p)
        assert res == {"nodes": [], "edges": []}


def test_powerautomate_duplicate_display_names_get_distinct_labels(tmp_path: Path):
    """Two flows sharing a display name must NOT share a label.

    Real case: two flows both named "Docentes OBS - Evaluacion docente"
    (arquitectura_pull_github.md §4.5). Their node ids differ (derived from the
    path) so they never merge, which is worse than merging: two distinct nodes
    with the SAME label mean `neighbors(label=...)` resolves to whichever is
    found first and silently hides the other -- and the two real ones differ by
    8x in outgoing edges. The pull already marked the collision by suffixing the
    folder with the first 8 chars of the flowId; the label mirrors that.
    """
    name = "Docentes OBS - Evaluacion docente"
    first = _make_flow(
        tmp_path, f"{name}-224124db",
        metadata={"displayName": name, "flowId": "224124db-daf5-05dd-b931-c00ddc2d8b2e"},
    )
    second = _make_flow(
        tmp_path, f"{name}-5a14ee7b",
        metadata={"displayName": name, "flowId": "5a14ee7b-ae8b-7540-b2f3-e84a19fa9eb1"},
    )

    labels = []
    for p in (first, second):
        res = extract_powerautomate(p)
        flow = [n for n in res["nodes"] if n["label"].startswith("Flow: ")][0]
        labels.append(flow["label"])
        # The raw display name stays recoverable even with the suffix in place.
        assert flow["display_name"] == name

    assert labels == [f"Flow: {name} (224124db)", f"Flow: {name} (5a14ee7b)"]
    assert len(set(labels)) == 2


def test_powerautomate_uncollided_flow_label_has_no_suffix(tmp_path: Path):
    """The 49-of-51 case: a flow whose folder carries no `-<8hex>` collision
    marker keeps a clean label. Guards against leaking the suffix into every
    node while fixing the duplicate case."""
    metadata_path = _make_flow(
        tmp_path, "Matriculacion_OBS",
        metadata={"flowId": "ab1afc01-93ec-2f4b-3f08-25db43b94f19"},
    )
    res = extract_powerautomate(metadata_path)
    labels = [n["label"] for n in res["nodes"] if n["label"].startswith("Flow: ")]
    assert labels == ["Flow: Matriculacion_OBS"]
