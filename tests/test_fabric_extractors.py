"""Unit and integration tests for TMDL, Power Query, and Fabric metadata extractors."""
from __future__ import annotations

import tempfile
from pathlib import Path

import graphify.extract as facade
from graphify.extract import extract
from graphify.extractors import LANGUAGE_EXTRACTORS
from graphify.extractors.fabric_config import extract_fabric_config
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
    # the model node is source-backed (owned), not a bare stub
    model_node = next(n for n in res["nodes"] if n["label"] == "SemanticModel: Foo")
    assert model_node["source_file"]


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
    assert any(e["relation"] == "references" for e in res_pbir["edges"])


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
