"""Per-repo configuration for graphify extractors.

Discovered via a small dotfile (`.graphifyconfig.json`) at or above the file
being extracted — same discovery idea as `.graphifyignore` in
`graphify.detect`, but for scalar settings instead of ignore globs.

Currently supports one key:

    default_catalog: a SQL catalog/database name that *some* T-SQL in the repo
    qualifies a table with (``[MyWarehouse].[schema].[table]``), while other
    extractors never see that name at all — Power Query's navigation-record
    parser (`powerquery._nav_tables`) reads a Fabric ``{[Schema=...,
    Item=...]}`` record that structurally has no catalog field, so it always
    mints a 2-part ``schema.table`` label. If some T-SQL in the same repo
    happens to write the 3-part form, the SQL extractor mints a DIFFERENT
    node id for the same physical table, and the dataflow-vs-warehouse
    lineage silently splits into two disconnected stubs instead of one
    connected node (confirmed for `student-intelligence-repo`: a Dataflow's
    writes_to and a stored procedure's reads_from landed on two different
    nodes for `stg.exe_UCMA_Renovaciones_2610`, one of them degree-1).

    Setting `default_catalog` tells the SQL extractor: when a cleaned
    identifier has 3+ dot-parts and the first one matches this name
    (case-insensitively), drop it — canonicalizing to the same `schema.table`
    form the rest of the graph already uses for that object.

Opt-in and per-repo: a repo with no `.graphifyconfig.json` gets `None` back
from every lookup, so every call site that consults this module is a no-op
for it — this changes nothing for any repo that hasn't added the file.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CONFIG_FILENAME = ".graphifyconfig.json"
_WALK_LIMIT = 64  # bounded: never loop forever on a malformed/cyclic filesystem


@lru_cache(maxsize=None)
def _load_config(repo_root: Path) -> dict:
    config_path = repo_root / _CONFIG_FILENAME
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=None)
def _find_repo_root(start: Path) -> Path | None:
    """Walk upward from `start` looking for `.graphifyconfig.json` or a `.git` dir.

    Prefers a directory that actually HAS the config file; falls back to the
    nearest `.git` (so a repo without the file resolves to *some* stable root
    and gets cached as "no config" rather than re-walking on every call).
    """
    current = start if start.is_dir() else start.parent
    git_root: Path | None = None
    for _ in range(_WALK_LIMIT):
        if (current / _CONFIG_FILENAME).is_file():
            return current
        if git_root is None and (current / ".git").exists():
            git_root = current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return git_root


def default_catalog_for(path: Path) -> str | None:
    """The configured `default_catalog` for the repo containing `path`, or None.

    Cached per repo root, so this costs one filesystem walk + one file read
    per repo per process — not per call.
    """
    root = _find_repo_root(path.resolve())
    if root is None:
        return None
    value = _load_config(root).get("default_catalog")
    return value if isinstance(value, str) and value else None
