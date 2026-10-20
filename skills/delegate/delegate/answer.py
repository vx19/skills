"""The worker's final answer: the `--schema` prompt contract and its check.

`--schema` is provider-neutral on purpose. The schema rides in the prompt and
core checks the captured answer afterwards. Native structured-output flags
differ per runner (grok's switches the stream format; codex's needs OpenAI
strict schemas), so none is used. The check is deliberately small (stdlib
only, no jsonschema): the answer must hold one JSON object carrying every
top-level `required` key. Surrounding prose or a code fence is tolerated, and
the object itself is saved as `answer.json` so a caller never re-parses prose.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


class SchemaError(ValueError):
    pass


def read_schema(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SchemaError(f"--schema file not found: {path}")
    try:
        schema = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SchemaError(f"--schema is not valid JSON: {e}") from None
    if not isinstance(schema, dict):
        raise SchemaError("--schema must be a JSON object (a JSON Schema)")
    return schema


def instruction(schema: dict) -> str:
    return ("Answer format: your final message must be exactly one JSON object "
            "matching this JSON Schema, with no prose around it:\n"
            + json.dumps(schema, indent=2))


_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.DOTALL)


def _loads(candidate: str):
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def parse(text: str) -> dict:
    """The answer's JSON object.

    Models narrate. So accept, in order: the whole answer, the last fenced
    block, or the span from the first `{` to the last `}`, whichever first
    parses as one JSON object.
    """
    body = text.strip()
    whole = _loads(body)
    if isinstance(whole, dict):
        return whole
    candidates = [block.strip() for block in _FENCE.findall(body)[-1:]]
    start, end = body.find("{"), body.rfind("}")
    if 0 <= start < end:
        candidates.append(body[start:end + 1])
    for candidate in candidates:
        obj = _loads(candidate)
        if isinstance(obj, dict):
            return obj
    if whole is not None:
        raise SchemaError("answer is not a JSON object")
    raise SchemaError("answer holds no JSON object")


def check(text: str | None, schema: dict) -> tuple[dict | None, str | None]:
    """(the answer's object, None) when it satisfies the contract, else (None, reason)."""
    if not text or not text.strip():
        return None, "no final answer was captured"
    try:
        obj = parse(text)
    except SchemaError as e:
        return None, str(e)
    required = schema.get("required")
    if isinstance(required, list):
        missing = [k for k in required if isinstance(k, str) and k not in obj]
        if missing:
            return None, f"answer is missing required key(s): {', '.join(missing)}"
    return obj, None
