"""Theme Coder agent for Stage 2.

Reads a full ``Codebook`` revision and proposes a set of overarching
themes that organise its codes into patterns of meaning. The agent is
stateless: each call takes the codebook + the researcher's framing
(research question, persona, any extra instructions) and returns a
list of transient ``Theme`` rows. Persistence and provenance
(``source``, ``theme_coding_job_id``) are the worker's job.

Following Thematic-LM (Sec. 3.1, App. B "Main Prompts"), the user
message contains the entire codebook serialised to JSON — codes,
descriptions, and supporting quotes with their ids — without
compression. Modern long-context models handle this directly, so we
skip the LLMLingua step the paper used.

A single ``develop_themes_async`` call runs a five-step conversation
against the LLM: an initial proposal (3–10 themes), two "any more
themes?" follow-ups in the same chat, an out-of-band critic pass that
reviews the accumulated themes against the codebook, and a final
consolidation turn where the critic's feedback is injected back into
the original chat and the model produces the definitive list. Only
the last turn's output is parsed and returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openhands.sdk import Message, TextContent
from pydantic import BaseModel, ValidationError

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_json_str
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    Quote,
    Theme,
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

Aim for 3 to 10 themes overall — enough to cover the analytically
important patterns in the codebook, but few enough that each theme is
substantive and distinct.

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
in the user message. Do not invent ids. Do not include codes that
don't belong to the theme just to pad it out. If you cannot construct
a coherent theme set from the codebook, return an empty list of
themes."""


_ADDITIONAL_THEMES_PROMPT = """\
Look at the codebook again. Are there additional themes worth raising
that you did not propose above? Consider patterns you may have
overlooked, alternative interpretive angles, or codes left
unconnected. If so, propose them now, following the same schema as
before. Only propose genuinely new themes — do not restate ones you
already gave. If nothing further is warranted, return an empty list
(`{"themes": []}`)."""


_CRITIC_SYSTEM_PROMPT = """\
You are a critical reviewer of a thematic analysis. Another analyst
has proposed a set of themes over a codebook. Read the codebook and
the proposed themes, then identify weaknesses: themes that are too
narrow or too broad, themes that overlap and should be consolidated,
themes that do not speak to the research focus, patterns in the
codebook that were missed, and groupings of codes that don't hang
together coherently.

Be concrete and constructive. Refer to themes by their titles and to
codes by their `code_id`. Your output is free-form feedback for the
analyst — no JSON, no schema. Keep it focused and actionable; the
analyst will use it to produce a final consolidated list."""


_FINAL_PROMPT_TEMPLATE = """\
A critical reviewer has examined the themes you proposed across the
turns above and given the following feedback:

<<<CRITIC_FEEDBACK>>>
{critique}
<<<END_CRITIC_FEEDBACK>>>

Taking this feedback into account alongside everything you proposed
earlier in this conversation, produce a final consolidated list of
themes. Aim for 3 to 10 themes total. Drop or merge themes that did
not hold up, refine those that needed work, and incorporate any
genuinely new themes the critique raised. Use only `code_id`s and
`quote_id`s from the codebook. This list is your definitive answer."""


# Header used to demarcate the researcher's framing in the user message
# so the model can tell our generic theme-coding instructions apart from
# the study-specific framing the human supplied for this run.
_RESEARCHER_FRAMING_HEADER = (
    "## Researcher's framing for this run\n"
    "The text below was written by the researcher running this job. It "
    "tells you what to look for, what perspective to adopt, and any "
    "study-specific framing. Treat it as your brief, not as a fixed "
    "rule that overrides the JSON output schema or the general theme-"
    "coding guidance in the system prompt.\n\n"
    "<<<RESEARCHER_FRAMING>>>\n"
    "{framing}\n"
    "<<<END_RESEARCHER_FRAMING>>>"
)


_CODEBOOK_HEADER = """\
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

    task: str = "theme_coder"
    max_themes: int = 10
    min_codes_per_theme: int = 2


class ThemeCoderAgent(BaseAgent):
    """Stateless theme-coding agent.

    A single :meth:`develop_themes_async` call runs the LLM over the
    full codebook and returns transient ``Theme`` rows. The agent does
    not own a job, an identity, or a research-context override — the
    caller is responsible for any study-specific framing, which is
    passed in as the ``prompt`` argument and reproduced verbatim inside
    the user message (clearly delimited so the model can tell it apart
    from our generic instructions).
    """

    def __init__(self, config: ThemeCoderConfig | None = None):
        super().__init__(config or ThemeCoderConfig())
        self.theme_config: ThemeCoderConfig = self.config  # type: ignore[assignment]

    def get_system_prompt(self) -> str:
        return THEME_CODER_SYSTEM_PROMPT

    # -- prompt building -----------------------------------------------------

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

    def _build_user_prompt(self, codebook: Codebook, prompt: str) -> str:
        """Assemble the initial user message.

        The researcher's framing (if any) goes first, in a clearly
        delimited block, so the model knows it is a brief from the
        person running the job — separate from the generic theme-coding
        rules in the system prompt and separate from the codebook data.
        The codebook JSON follows.
        """
        sections: list[str] = []
        framing = (prompt or "").strip()
        if framing:
            sections.append(
                _RESEARCHER_FRAMING_HEADER.format(framing=framing)
            )
        sections.append(
            _CODEBOOK_HEADER.format(
                version=codebook.version,
                codebook_json=self._codebook_to_json(codebook),
            )
        )
        return "\n\n".join(sections)

    def _build_critic_user_prompt(
        self,
        codebook: Codebook,
        prompt: str,
        prior_responses: list[str],
    ) -> str:
        """Assemble the critic's user message.

        The critic sees the researcher's framing, the codebook JSON,
        and every prior turn's raw JSON response from the theme coder
        — verbatim, so the critic judges what the analyst actually
        said rather than a summary.
        """
        sections: list[str] = []
        framing = (prompt or "").strip()
        if framing:
            sections.append(
                _RESEARCHER_FRAMING_HEADER.format(framing=framing)
            )
        sections.append(
            _CODEBOOK_HEADER.format(
                version=codebook.version,
                codebook_json=self._codebook_to_json(codebook),
            )
        )
        proposals = "\n\n".join(
            f"### Analyst's turn {i + 1}\n```json\n{r}\n```"
            for i, r in enumerate(prior_responses)
        )
        sections.append(
            "## Themes proposed by the analyst\n\n"
            "Below are the analyst's responses across multiple turns. "
            "Earlier turns proposed an initial set; later turns added "
            "further themes when prompted. Review them as a whole.\n\n"
            f"{proposals}"
        )
        return "\n\n".join(sections)

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

    # -- LLM helpers ---------------------------------------------------------

    async def _chat_async(
        self,
        messages: list[Message],
        response_format: dict | None = None,
    ) -> str:
        """Run one LLM turn over a prebuilt message list and return text.

        Used by the multi-step ``develop_themes_async`` flow so each
        turn can extend the same conversation rather than restarting
        from a single system+user pair.
        """
        kwargs = {"response_format": response_format} if response_format else {}
        response = await self._completion_with_retry_async(messages, kwargs)
        return self._extract_text(response)

    @staticmethod
    def _user(text: str) -> Message:
        return Message(role="user", content=[TextContent(text=text)])

    @staticmethod
    def _assistant(text: str) -> Message:
        return Message(role="assistant", content=[TextContent(text=text)])

    @staticmethod
    def _system(text: str) -> Message:
        return Message(role="system", content=[TextContent(text=text)])

    # -- public API ----------------------------------------------------------

    async def develop_themes_async(
        self, codebook: Codebook, prompt: str
    ) -> list[Theme]:
        """Develop themes via a five-step multi-turn conversation.

        1. Initial proposal of 3–10 themes.
        2. Same chat: any additional themes worth raising?
        3. Same chat: any more themes?
        4. Out-of-band critic reviews the accumulated themes and the
           codebook, returning free-form feedback.
        5. Critic feedback injected into the original chat; model
           returns the final consolidated list.

        Only step 5's response is parsed and returned.
        """
        system_prompt = self.get_system_prompt()
        messages: list[Message] = [
            self._system(system_prompt),
            self._user(self._build_user_prompt(codebook, prompt)),
        ]

        initial = await self._chat_async(
            messages, response_format=THEME_CODER_RESPONSE_SCHEMA
        )
        messages.append(self._assistant(initial))

        messages.append(self._user(_ADDITIONAL_THEMES_PROMPT))
        more1 = await self._chat_async(
            messages, response_format=THEME_CODER_RESPONSE_SCHEMA
        )
        messages.append(self._assistant(more1))

        messages.append(self._user(_ADDITIONAL_THEMES_PROMPT))
        more2 = await self._chat_async(
            messages, response_format=THEME_CODER_RESPONSE_SCHEMA
        )
        messages.append(self._assistant(more2))

        critic_messages: list[Message] = [
            self._system(_CRITIC_SYSTEM_PROMPT),
            self._user(
                self._build_critic_user_prompt(
                    codebook, prompt, [initial, more1, more2]
                )
            ),
        ]
        critique = await self._chat_async(critic_messages)

        messages.append(
            self._user(_FINAL_PROMPT_TEMPLATE.format(critique=critique))
        )
        final = await self._chat_async(
            messages, response_format=THEME_CODER_RESPONSE_SCHEMA
        )
        return self._parse_response(final, codebook)
