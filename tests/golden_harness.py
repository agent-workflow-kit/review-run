"""Golden-file harness shared by every command's tests.

``check_golden`` first validates the document against its schema under ``schemas/`` and only then
compares it with ``tests/golden/<name>``, so a ``--json`` output that no longer matches its schema
fails even when the stored golden was regenerated from it. ``UPDATE_GOLDEN=1`` rewrites the file
(the schema check still runs first).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.conftest import validate_against

GOLDEN = Path(__file__).resolve().parent / "golden"


def render(document: object) -> str:
    return json.dumps(document, indent=2, sort_keys=False, ensure_ascii=True) + "\n"


def check_golden(name: str, schema_name: str, document: object) -> None:
    validate_against(schema_name, document)
    text = render(document)
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text
