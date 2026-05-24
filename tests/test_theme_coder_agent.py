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


def _last_user_text(messages):
    """Pull the text of the last user-role message in a Message list."""
    for m in reversed(messages):
        if m.role == "user":
            return "".join(p.text for p in m.content)
    return ""


def _first_system_text(messages):
    for m in messages:
        if m.role == "system":
            return "".join(p.text for p in m.content)
    return ""


def test_develop_themes_async_runs_five_step_flow_and_returns_final():
    cb = _codebook(
        version=3,
        codes=[
            _code(1, "x", quotes=[_quote(11)]),
            _code(2, "y", quotes=[_quote(12)]),
            _code(3, "z", quotes=[_quote(13)]),
        ],
    )
    agent = ThemeCoderAgent()

    initial_resp = _resp(
        [{"title": "T1", "description": "", "rationale": "",
          "code_ids": [1, 2], "quote_ids": []}]
    )
    more1_resp = _resp(
        [{"title": "T2", "description": "", "rationale": "",
          "code_ids": [2, 3], "quote_ids": []}]
    )
    more2_resp = _resp([])  # nothing new
    critique_text = "Theme T1 overlaps with T2; consolidate."
    final_resp = _resp(
        [{"title": "Final", "description": "d", "rationale": "r",
          "code_ids": [1, 2, 3], "quote_ids": [11, 12, 13]}]
    )

    calls: list[dict] = []
    responses = iter([initial_resp, more1_resp, more2_resp, critique_text, final_resp])

    async def fake_chat(messages, response_format=None):
        calls.append({
            "messages": list(messages),
            "schema": response_format,
            "last_user": _last_user_text(messages),
            "system": _first_system_text(messages),
        })
        return next(responses)

    with patch.object(agent, "_chat_async", side_effect=fake_chat):
        themes = asyncio.run(
            agent.develop_themes_async(cb, prompt="be analytic")
        )

    assert len(calls) == 5
    # Step 1: initial themes — theme coder system prompt + codebook in user.
    assert "theme coder" in calls[0]["system"].lower()
    assert "be analytic" in calls[0]["last_user"]
    assert "version 3" in calls[0]["last_user"]
    assert calls[0]["schema"] is not None

    # Steps 2 and 3: follow-up "more themes?" in the same chat.
    for i in (1, 2):
        assert "additional themes" in calls[i]["last_user"].lower()
        assert calls[i]["schema"] is not None
        # Chat carries the codebook + prior turns: messages list grows.
        assert len(calls[i]["messages"]) > len(calls[i - 1]["messages"])

    # Step 4: critic — fresh session with the critic system prompt and
    # the merged theme list (no codebook header, no turn boundaries).
    # No JSON schema, since the critique is free-form.
    assert "critical reviewer" in calls[3]["system"].lower()
    assert calls[3]["schema"] is None
    critic_user = calls[3]["last_user"]
    assert "version 3" not in critic_user  # codebook header is not included
    # Themes from all three turns appear by title; turn headers do not.
    assert "T1" in critic_user
    assert "T2" in critic_user
    assert "turn 1" not in critic_user.lower()
    assert "turn 2" not in critic_user.lower()
    # Inline codes/quotes give the critic enough context to interpret ids.
    assert '"code": "x"' in critic_user
    assert '"code": "y"' in critic_user

    # Step 5: final consolidation — back in the original chat with the
    # critique injected. Schema is on again; chat carries everything.
    assert critique_text in calls[4]["last_user"]
    assert "final" in calls[4]["last_user"].lower()
    assert calls[4]["schema"] is not None
    assert "theme coder" in calls[4]["system"].lower()

    # Only the final response is parsed and returned.
    assert [t.title for t in themes] == ["Final"]
    assert {q.quote_id for q in themes[0].supporting_quotes} == {11, 12, 13}


def test_merge_theme_responses_inlines_codes_and_quotes_by_id():
    cb = _codebook(
        codes=[
            _code(1, "isolation", quotes=[_quote(11, "alone")]),
            _code(2, "support", quotes=[_quote(12, "they helped"),
                                        _quote(13, "kind")]),
            _code(3, "agency", quotes=[_quote(14, "I chose")]),
        ],
    )
    t1 = _resp([{"title": "A", "description": "", "rationale": "",
                 "code_ids": [1], "quote_ids": [11]}])
    t2 = "not json at all"  # skipped silently
    t3 = _resp([{"title": "B", "description": "", "rationale": "",
                 # 99 is unknown — must be dropped.
                 "code_ids": [2, 99], "quote_ids": [12, 13]},
                {"title": "C", "description": "", "rationale": "",
                 "code_ids": [3], "quote_ids": [14]}])
    merged_str = ThemeCoderAgent._merge_theme_responses([t1, t2, t3], cb)
    merged = json.loads(merged_str)
    assert [t["title"] for t in merged["themes"]] == ["A", "B", "C"]
    # Codes / quotes are inlined as {id, code} / {id, text} so the critic
    # can read what each theme is about without the full codebook.
    b = merged["themes"][1]
    assert b["codes"] == [{"id": 2, "code": "support"}]
    assert b["quotes"] == [
        {"id": 12, "text": "they helped"},
        {"id": 13, "text": "kind"},
    ]


def test_develop_themes_async_records_every_turn_on_last_turns():
    cb = _codebook(
        version=4,
        codes=[
            _code(1, "x", quotes=[_quote(11)]),
            _code(2, "y", quotes=[_quote(12)]),
        ],
    )
    agent = ThemeCoderAgent()

    initial_resp = _resp([])
    more1_resp = _resp([])
    more2_resp = _resp([])
    critique_text = "consolidate everything"
    final_resp = _resp(
        [{"title": "Final", "description": "", "rationale": "",
          "code_ids": [1, 2], "quote_ids": []}]
    )
    responses = iter(
        [initial_resp, more1_resp, more2_resp, critique_text, final_resp]
    )

    async def fake_chat(messages, response_format=None):
        return next(responses)

    with patch.object(agent, "_chat_async", side_effect=fake_chat):
        asyncio.run(agent.develop_themes_async(cb, prompt="framing!"))

    assert [t.label for t in agent.last_turns] == [
        "initial", "additional_1", "additional_2", "critic", "final",
    ]
    assert [t.response for t in agent.last_turns] == [
        initial_resp, more1_resp, more2_resp, critique_text, final_resp,
    ]

    # System prompts: theme coder for proposal/follow-up/final, critic for #4.
    sys_per_turn = [t.system_prompt for t in agent.last_turns]
    for i in (0, 1, 2, 4):
        assert "theme coder" in sys_per_turn[i].lower()
    assert "critical reviewer" in sys_per_turn[3].lower()

    # User prompts: only the new message added that turn, not the whole chat.
    users = [t.user_prompt for t in agent.last_turns]
    assert "framing!" in users[0] and "version 4" in users[0]
    assert users[1] == users[2]  # both follow-ups are the same prompt
    assert "additional themes" in users[1].lower()
    # Critic gets a merged themes list, not raw per-turn JSON; the
    # exact response strings are no longer present, but a `themes`
    # block is.
    assert '"themes"' in users[3]
    assert critique_text in users[4]

    assert all(t.elapsed >= 0 for t in agent.last_turns)

    # Re-running clears the prior trace rather than appending to it.
    responses = iter(
        [initial_resp, more1_resp, more2_resp, critique_text, final_resp]
    )
    with patch.object(agent, "_chat_async", side_effect=fake_chat):
        asyncio.run(agent.develop_themes_async(cb, prompt=""))
    assert len(agent.last_turns) == 5
