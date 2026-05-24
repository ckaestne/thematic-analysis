"""Tests for ThemeCoderAgent.

These cover prompt building and response parsing in isolation (the LLM
call is patched). End-to-end persistence is exercised by
``test_inc_themes.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from thematic_analysis.agents import ThemeCoderAgent, ThemeCoderConfig


def _quote(qid: int, text: str = "q"):
    return SimpleNamespace(quote_id=qid, text=text)


def _code(cid: int, label: str, description: str = "", quotes=None):
    return SimpleNamespace(
        code_id=cid,
        code=label,
        description=description,
        supporting_quotes=quotes or [],
    )


def _codebook(version: int = 7, codes=None):
    # No research_context attr — the agent no longer reads one.
    return SimpleNamespace(version=version, codes=codes or [])


def _resp(themes: list[dict]) -> str:
    return json.dumps({"themes": themes})


# --- config / construction --------------------------------------------------


class TestThemeCoderConfig:
    def test_defaults(self):
        cfg = ThemeCoderConfig()
        assert cfg.max_themes == 10
        assert cfg.min_codes_per_theme == 2


class TestPromptBuilding:
    def test_system_prompt_is_fixed(self):
        agent = ThemeCoderAgent()
        sys = agent.get_system_prompt()
        assert "theme coder" in sys.lower()
        # Per-run framing lives in the user message, not the system one.
        assert "Researcher's framing" not in sys

    def test_user_prompt_serialises_full_codebook(self):
        cb = _codebook(
            version=9,
            codes=[
                _code(
                    1, "isolation", description="being alone",
                    quotes=[_quote(11, "I felt alone")],
                ),
                _code(
                    2, "support", description="receiving help",
                    quotes=[_quote(12, "they helped"), _quote(13, "kind")],
                ),
            ],
        )
        user = ThemeCoderAgent()._build_user_prompt(cb, prompt="")
        assert "version 9" in user
        payload = json.loads(
            user.split("```json", 1)[1].split("```", 1)[0]
        )
        assert {c["code_id"] for c in payload["codes"]} == {1, 2}
        sup = next(c for c in payload["codes"] if c["code_id"] == 2)
        assert {q["quote_id"] for q in sup["quotes"]} == {12, 13}

    def test_user_prompt_includes_researcher_framing_delimited(self):
        agent = ThemeCoderAgent()
        cb = _codebook()
        framing = "Act as a critical discourse analyst focused on power."
        user = agent._build_user_prompt(cb, prompt=framing)
        # Framing appears, clearly demarcated, BEFORE the codebook block.
        assert "Researcher's framing" in user
        assert "<<<RESEARCHER_FRAMING>>>" in user
        assert "<<<END_RESEARCHER_FRAMING>>>" in user
        assert framing in user
        assert user.index(framing) < user.index("## Codebook")

    def test_user_prompt_omits_framing_block_when_empty(self):
        user = ThemeCoderAgent()._build_user_prompt(_codebook(), prompt="   ")
        assert "Researcher's framing" not in user
        assert "RESEARCHER_FRAMING" not in user


# --- response parsing -------------------------------------------------------


def _agent_with(codebook):
    return ThemeCoderAgent(), codebook


def test_parse_returns_themes_with_codes_and_quotes():
    cb = _codebook(
        codes=[
            _code(1, "isolation", quotes=[_quote(11)]),
            _code(2, "support", quotes=[_quote(12), _quote(13)]),
            _code(3, "agency", quotes=[_quote(14)]),
        ],
    )
    agent, _ = _agent_with(cb)
    resp = _resp(
        [
            {
                "title": "Relational support",
                "description": "Connection helps.",
                "rationale": "Both codes describe interpersonal help.",
                "code_ids": [1, 2],
                "quote_ids": [11, 12, 13],
            },
        ]
    )
    themes = agent._parse_response(resp, cb)
    assert len(themes) == 1
    t = themes[0]
    assert t.title == "Relational support"
    assert t.codebook_used_id == cb.version
    assert [c.code_id for c in t.codes] == [1, 2]
    assert sorted(q.quote_id for q in t.supporting_quotes) == [11, 12, 13]


def test_parse_drops_unknown_code_ids_and_short_themes():
    cb = _codebook(
        codes=[
            _code(1, "x", quotes=[_quote(11)]),
            _code(2, "y", quotes=[_quote(12)]),
        ],
    )
    agent, _ = _agent_with(cb)
    resp = _resp(
        [
            # Only one valid code → below min_codes_per_theme (=2), dropped.
            {
                "title": "Too thin",
                "description": "",
                "rationale": "",
                "code_ids": [1, 99],
                "quote_ids": [11],
            },
            # Both valid → kept.
            {
                "title": "Coherent",
                "description": "",
                "rationale": "",
                "code_ids": [1, 2],
                "quote_ids": [],
            },
        ]
    )
    themes = agent._parse_response(resp, cb)
    assert [t.title for t in themes] == ["Coherent"]


def test_parse_drops_quote_ids_not_supporting_the_picked_codes():
    cb = _codebook(
        codes=[
            _code(1, "x", quotes=[_quote(11)]),
            _code(2, "y", quotes=[_quote(12)]),
            _code(3, "z", quotes=[_quote(99)]),
        ],
    )
    agent, _ = _agent_with(cb)
    resp = _resp(
        [
            {
                "title": "T",
                "description": "",
                "rationale": "",
                "code_ids": [1, 2],
                "quote_ids": [11, 12, 99],  # 99 belongs to code 3, drop
            }
        ]
    )
    themes = agent._parse_response(resp, cb)
    assert sorted(q.quote_id for q in themes[0].supporting_quotes) == [11, 12]


def test_parse_respects_max_themes():
    cb = _codebook(
        codes=[_code(i, f"c{i}", quotes=[_quote(100 + i)]) for i in range(1, 5)]
    )
    agent = ThemeCoderAgent(config=ThemeCoderConfig(max_themes=2))
    resp = _resp(
        [
            {
                "title": f"T{i}", "description": "", "rationale": "",
                "code_ids": [1, 2], "quote_ids": [],
            }
            for i in range(4)
        ]
    )
    themes = agent._parse_response(resp, cb)
    assert len(themes) == 2


def test_parse_empty_or_invalid_response_returns_empty():
    cb = _codebook(codes=[_code(1, "x"), _code(2, "y")])
    agent = ThemeCoderAgent()
    assert agent._parse_response("nope, not json at all", cb) == []
    assert agent._parse_response('{"themes": "wrong-shape"}', cb) == []


# --- public API integration via patched LLM ---------------------------------


def test_develop_themes_async_calls_llm_and_returns_parsed():
    cb = _codebook(
        version=3,
        codes=[
            _code(1, "x", quotes=[_quote(11)]),
            _code(2, "y", quotes=[_quote(12)]),
        ],
    )
    agent = ThemeCoderAgent()
    captured: dict = {}

    async def fake_call(system_prompt, user_prompt, response_format=None):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        captured["schema"] = response_format
        return _resp(
            [
                {
                    "title": "T", "description": "d", "rationale": "r",
                    "code_ids": [1, 2], "quote_ids": [11, 12],
                }
            ]
        )

    with patch.object(agent, "_call_llm_async", side_effect=fake_call):
        themes = asyncio.run(
            agent.develop_themes_async(cb, prompt="be analytic")
        )

    assert len(themes) == 1
    # Per-run framing is in the user message, not the system prompt.
    assert "be analytic" not in captured["system"]
    assert "be analytic" in captured["user"]
    assert "version 3" in captured["user"]
    assert captured["schema"] is not None
