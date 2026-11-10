"""Reads of the built-in catalog rows with no registry.

The registry reaches the clients, so a helper that a client imports reads the rows here.
"""
from functools import lru_cache
from typing import Dict, Optional, Tuple

from modules.llm.model_catalog import register_builtin_models
from modules.llm.model_types import ModelConfig


@lru_cache(maxsize=1)
def _rows() -> Dict[str, ModelConfig]:
    rows: Dict[str, ModelConfig] = {}
    register_builtin_models(lambda row: rows.__setitem__(row.name, row))
    return rows


@lru_cache(maxsize=1)
def _wire_ids() -> Dict[Tuple[str, str], str]:
    return {(row.provider.value, alias): row.name for row in _rows().values()
            for alias in row.aliases if alias not in row.served_aliases}


def catalog_row(name: Optional[str]) -> Optional[ModelConfig]:
    """The built-in row named *name*."""
    return _rows().get(name)


def catalog_wire_id(name: Optional[str], provider: Optional[str]) -> Optional[str]:
    """The id *provider*'s API takes for *name*: an alias of that provider's own row reads the
    row name. An alias the API serves as typed, an id with no row, a row of another provider
    and ``provider=None`` (a client that maps no id) all stay as typed."""
    return _wire_ids().get((provider, name), name) if name else name
