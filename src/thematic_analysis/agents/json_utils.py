"""Helpers for extracting JSON from raw LLM responses."""

import json
import re


def extract_json_str(response: str) -> str | None:
    """Return the JSON substring from an LLM response, or None.

    Prefers a fenced ```json ... ``` block; falls back to the first
    ``{...}`` span.
    """
    fenced = re.search(r"```(?:json)?\s*(.*?)```", response, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    braces = re.search(r"\{.*\}", response, re.DOTALL)
    if braces:
        return braces.group(0)
    return None


def extract_response_json(response: str) -> dict | None:
    """Extract and json-decode the JSON object from an LLM response."""
    json_str = extract_json_str(response)
    if json_str is None:
        return None
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None
