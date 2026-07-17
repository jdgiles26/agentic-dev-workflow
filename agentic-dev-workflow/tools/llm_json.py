"""
llm_json.py — Shared JSON extraction and repair for small-model responses.

Small models (3B) routinely truncate outputs at max_tokens, producing:
  - Unclosed strings / arrays / objects
  - JSON wrapped in ```json … ``` fences
  - Trailing prose after the closing brace

parse_llm_json() handles all of these and returns a Python dict, or raises
ValueError with a clear message if nothing salvageable is found.
"""

import json
import re
from typing import Any


def parse_llm_json(raw: str, context: str = "") -> dict[str, Any]:
    """
    Extract and return the first JSON object from *raw*.

    Tries in order:
      1. Direct parse after stripping markdown fences.
      2. Extract outermost { … } and direct parse.
      3. Extract outermost { … }, repair truncation, then parse.

    Raises ValueError if all three attempts fail.
    *context* is included in the error message for easier debugging.
    """
    cleaned = _strip_fences(raw)

    # 1. Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 2. Extract outermost object
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(
            f"No JSON object found in LLM response{_ctx(context)}.\n"
            f"Raw (first 300 chars): {raw[:300]}"
        )

    end = cleaned.rfind("}")
    if end != -1 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    else:
        candidate = cleaned[start:]

    # 3. Repair truncation and retry
    repaired = _close_json(candidate)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not parse JSON even after repair{_ctx(context)}.\n"
            f"Parse error: {exc}\n"
            f"Raw (first 300 chars): {raw[:300]}"
        ) from exc


def _strip_fences(raw: str) -> str:
    """Remove leading/trailing ```json … ``` markdown fences."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _close_json(fragment: str) -> str:
    """
    Repair a truncated JSON fragment so it can be parsed.

    Strategy:
    1. Walk the fragment tracking string/brace/bracket state.
    2. If we're mid-string when the input ends, strip back to the last
       complete top-level value boundary (comma or opening bracket/brace)
       before closing — this handles truncation inside nested objects.
    3. Append the minimum closing characters needed.
    """
    in_string = False
    escape_next = False
    depth = 0          # total nesting depth (braces + brackets combined)
    open_braces = 0
    open_brackets = 0
    # Track index of last "safe" position: just after a comma or opening
    # bracket/brace at depth == 1 (i.e. the outer array's item boundary)
    last_safe_pos = 0

    for i, ch in enumerate(fragment):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            depth += 1
            if ch == "{":
                open_braces += 1
            else:
                open_brackets += 1
            if depth <= 2:          # top-level or one level in
                last_safe_pos = i + 1
        elif ch in ("}", "]"):
            depth -= 1
            if ch == "}":
                open_braces = max(open_braces - 1, 0)
            else:
                open_brackets = max(open_brackets - 1, 0)
        elif ch == "," and depth == 1:
            last_safe_pos = i + 1   # safe to truncate here

    if in_string and last_safe_pos > 0:
        # Truncated mid-string inside a nested structure: cut back to the
        # last safe boundary so we don't leave a half-written object.
        fragment = fragment[:last_safe_pos].rstrip(", \t\n")
        # Recount open braces/brackets on the trimmed fragment
        open_braces = 0
        open_brackets = 0
        in_string2 = False
        escape2 = False
        for ch in fragment:
            if escape2:
                escape2 = False
                continue
            if ch == "\\" and in_string2:
                escape2 = True
                continue
            if ch == '"':
                in_string2 = not in_string2
                continue
            if in_string2:
                continue
            if ch == "{":
                open_braces += 1
            elif ch == "}":
                open_braces = max(open_braces - 1, 0)
            elif ch == "[":
                open_brackets += 1
            elif ch == "]":
                open_brackets = max(open_brackets - 1, 0)

    suffix = "]" * open_brackets + "}" * open_braces
    return fragment + suffix


def _ctx(context: str) -> str:
    return f" [{context}]" if context else ""
