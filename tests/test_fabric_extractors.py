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
\t\tsource = ```
\t\t\tlet
\t\t\t    Source = Sql.Database("sqlserver.db.windows.net", "SalesDB", [Query="SELECT * FROM [dbo].[Ventas]"])
\t\t\tin
\t\t\t    Source
\t\t\t```
"""
    tmdl_file = tmp_path / "Ventas.tmdl"
    tmdl_file.write_text(tmdl_content, encoding="utf-8")

    res = extract_tmdl(tmdl_file)
    nodes = {n["id"]: n for n in res["nodes"]}
    labels = {n["label"] for n in res["nodes"]}

    # Table and columns
    assert "Ventas" in labels
    assert "Ventas[ID_Venta]" in labels
    assert "Ventas[Monto]" in labels
    assert "Ventas[MontoConIVA]" in labels
    assert "[Total Ventas]" in labels
    assert "[Total Con Descuento]" in labels

    # Measure dependencies
    edges = res["edges"]
    # [Total Con Descuento] -> [Total Ventas]
    reads_from = [(e["source"], e["target"]) for e in edges if e["relation"] == "reads_from"]
    assert any("Total Ventas" in str(tgt) or "total_ventas" in str(tgt) for src, tgt in reads_from)


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
    pq_file = tmp_path / "mashup.pq"
    pq_file.write_text(pq_content, encoding="utf-8")

    res = extract_powerquery(pq_file)
    labels = {n["label"] for n in res["nodes"]}

    assert "Clientes" in labels
    assert "VentasConsolidadas" in labels

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
