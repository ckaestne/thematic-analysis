"""Theme Coder agent for Stage 2.

Reads a full ``Codebook`` revision and proposes a set of overarching
themes that organise its codes into patterns of meaning. The agent is
stateless: each call takes the codebook + the customisable part of the
system prompt (research question, persona, extra instructions) and
returns a list of transient ``Theme`` rows. Persistence and provenance
(``source``, ``theme_coding_job_id``) are the worker's job.

Following Thematic-LM (Sec. 3.1, App. B "Main Prompts"), the user
message contains the entire codebook serialised to JSON — codes,
descriptions, and supporting quotes with their ids — without
compression. Modern long-context models handle this directly, so we
skip the LLMLingua step the paper used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_json_str
from thematic_analysis.prompts import join_system_prompt_sections
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    Quote,
    Theme,
)
from thematic_analysis_inc.db.research_context import (
    to_domain as _research_context_to_domain,
)


THEME_CODER_SYSTEM_PROMPT = """\
You are a theme coder in a reflexive thematic analysis. You read the
full codebook produced by an earlier coding pass — every code, its
description, and the quotes that support it — and propose a set of
overarching *themes*: patterns of meaning that organise the codes into
a coherent answer to the research question.

A theme is not a topic heading or a bucket of related codes. It is a
pattern of meaning organised around a central concept that says
something analytically interesting about the data with respect to the
research focus. Each theme should be supported by multiple codes; a
theme grounded in a single code is usually too narrow.

For each theme, provide:
- a short, evocative title;
- a one- to three-sentence description of what the theme captures;
- a brief rationale stating why the chosen codes belong together and
  how the theme speaks to the research focus;
- the list of `code_id`s (from the codebook) that make up the theme;
- a small set of `quote_id`s (from the supporting quotes of those
  codes) that best illustrate the theme — pick the most representative,
  not all of them.

Use only `code_id`s and `quote_id`s that appear in the codebook JSON
below. Do not invent ids. Do not include codes that don't belong to the
theme just to pad it out. If you cannot construct a coherent theme set
from the codebook, return an empty list of themes."""


THEME_CODER_USER_PROMPT = """\
## Codebook (version {version})

The codebook below contains every code in the current revision, each
with its description and the quotes that support it. Use the `code_id`
and `quote_id` values when referring back to them in your response.

```json
{codebook_json}
```

Output themes following the required schema."""


class _ThemeItem(BaseModel):
    """Pydantic shape mirroring the JSON-schema item the LLM returns."""

    title: str
    description: str
    rationale: str
    code_ids: list[int]
    quote_ids: list[int]

    model_config = {"extra": "ignore"}


class _ThemeCoderResponse(BaseModel):
    themes: list[_ThemeItem]

    model_config = {"extra": "ignore"}


THEME_CODER_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "theme_development",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "themes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "rationale": {"type": "string"},
                            "code_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                            "quote_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                            },
                        },
                        "required": [
                            "title", "description", "rationale",
                            "code_ids", "quote_ids",
                        ],
                    },
                },
            },
            "required": ["themes"],
        },
    },
}


@dataclass
class ThemeCoderConfig(AgentConfig):
    """Configuration for the Theme Coder agent."""

    max_themes: int = 10
    min_codes_per_theme: int = 2


class ThemeCoderAgent(BaseAgent):
    """Stateless theme-coding agent.

    A single :meth:`develop_themes` call runs the LLM over the full
    codebook and returns transient ``Theme`` rows. The agent does not
    own a job, an identity, or a research-context override — those
    live on (respectively) the worker, the caller's ``prompt`` argument,
    and the ``codebook.research_context`` row the worker passes in.
    """

    def __init__(self, config: ThemeCoderConfig | None = None):
        super().__init__(config or ThemeCoderConfig())
        self.theme_config: ThemeCoderConfig = self.config  # type: ignore[assignment]

    def get_system_prompt(self) -> str:
        # Static system prompt; per-call customisation comes through the
        # ``prompt`` argument to ``develop_themes`` and the codebook's
        # research context.
        return THEME_CODER_SYSTEM_PROMPT

    # -- prompt building -----------------------------------------------------

    def _build_system_prompt(self, codebook: Codebook, prompt: str) -> str:
        research_section = ""
        rc_row = codebook.research_context
        if rc_row is not None:
            rc = _research_context_to_domain(rc_row)
            if not rc.is_empty():
                research_section = (
                    "## Research context\n"
                    + rc.to_prompt_section(role="coder")
                )

        job_section = ""
        cleaned = (prompt or "").strip()
        if cleaned:
            job_section = "## Job instructions\n" + cleaned

        return join_system_prompt_sections(
            THEME_CODER_SYSTEM_PROMPT,
            research_context_instructions=research_section,
            identity_instructions=job_section,
        )

    def _codebook_to_json(self, codebook: Codebook) -> str:
        """Serialise the codebook for the user prompt.

        Includes every code with its description plus the full list of
        supporting quotes (id + text). No truncation, no compression.
        """
        codes_payload: list[dict] = []
        for c in codebook.codes or []:
            codes_payload.append(
                {
                    "code_id": c.code_id,
                    "code": c.code,
                    "description": c.description or "",
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text}
                        for q in (c.supporting_quotes or [])
                    ],
                }
            )
        return json.dumps({"codes": codes_payload}, indent=2)

    def _build_user_prompt(self, codebook: Codebook) -> str:
        return THEME_CODER_USER_PROMPT.format(
            version=codebook.version,
            codebook_json=self._codebook_to_json(codebook),
        )

    # -- response parsing ----------------------------------------------------

    def _parse_response(
        self, response: str, codebook: Codebook
    ) -> list[Theme]:
        json_str = extract_json_str(response)
        if json_str is None:
            return []
        try:
            parsed = _ThemeCoderResponse.model_validate_json(json_str)
        except (json.JSONDecodeError, ValidationError):
            return []

        # Build lookups so we can drop ids the LLM hallucinated and
        # restrict quote ids to those that actually support each theme's
        # codes (per the paper, each theme's quotes come from its codes).
        code_by_id: dict[int, Code] = {
            c.code_id: c for c in (codebook.codes or []) if c.code_id is not None
        }
        quotes_for_code: dict[int, dict[int, Quote]] = {
            c.code_id: {
                q.quote_id: q
                for q in (c.supporting_quotes or [])
                if q.quote_id is not None
            }
            for c in (codebook.codes or [])
            if c.code_id is not None
        }

        out: list[Theme] = []
        for item in parsed.themes[: self.theme_config.max_themes]:
            title = item.title.strip()
            if not title:
                continue

            codes: list[Code] = []
            seen_codes: set[int] = set()
            for cid in item.code_ids:
                code = code_by_id.get(cid)
                if code is None or cid in seen_codes:
                    continue
                seen_codes.add(cid)
                codes.append(code)
            if len(codes) < self.theme_config.min_codes_per_theme:
                continue

            allowed_quotes: dict[int, Quote] = {}
            for cid in seen_codes:
                allowed_quotes.update(quotes_for_code.get(cid, {}))
            quotes: list[Quote] = []
            seen_quotes: set[int] = set()
            for qid in item.quote_ids:
                q = allowed_quotes.get(qid)
                if q is None or qid in seen_quotes:
                    continue
                seen_quotes.add(qid)
                quotes.append(q)

            out.append(
                Theme(
                    codebook_used_id=codebook.version,
                    # source / theme_coding_job_id left for the worker.
                    source="",
                    title=title,
                    description=item.description.strip(),
                    rationale=item.rationale.strip(),
                    codes=codes,
                    supporting_quotes=quotes,
                )
            )
        return out

    # -- public API ----------------------------------------------------------

    def develop_themes(
        self, codebook: Codebook, prompt: str
    ) -> list[Theme]:
        response = self._call_llm(
            self._build_system_prompt(codebook, prompt),
            self._build_user_prompt(codebook),
            response_format=THEME_CODER_RESPONSE_SCHEMA,
        )
        return self._parse_response(response, codebook)

    async def develop_themes_async(
        self, codebook: Codebook, prompt: str
    ) -> list[Theme]:
        response = await self._call_llm_async(
            self._build_system_prompt(codebook, prompt),
            self._build_user_prompt(codebook),
            response_format=THEME_CODER_RESPONSE_SCHEMA,
        )
        return self._parse_response(response, codebook)
