"""The pinned sibling schemas as a registry keyed by ``$id`` (tests only; never imported by src/).

Each pinned file's ``$id`` is ``<sibling>/<path>.json`` without the ``schemas/`` segment; the
schemas use JSON Schema 2020-12 with relative ``$ref``s across files (``docs/pinned-schemas.md``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

PINNED = Path(__file__).resolve().parent.parent / "pinned_schemas"


def _load() -> tuple[dict[str, Any], Registry[Any]]:
    docs: dict[str, Any] = {}
    registry: Registry[Any] = Registry()
    for path in sorted(PINNED.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        docs[doc["$id"]] = doc
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    return docs, registry


DOCS, REGISTRY = _load()


def validator(schema_id: str) -> Draft202012Validator:
    if schema_id not in DOCS:
        raise KeyError(f"no pinned schema {schema_id!r}")
    return Draft202012Validator(DOCS[schema_id], registry=REGISTRY)


def validate(schema_id: str, instance: object) -> None:
    validator(schema_id).validate(instance)
