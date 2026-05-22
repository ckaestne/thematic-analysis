"""Generate per-agent tailored prompt sections from a freeform research context.

The user writes a freeform description of the research context and research
question(s). Each downstream agent (coder, theme coder, reviewer, theme
aggregator) needs that information framed for its specific job — a coder
needs guidance on what counts as a relevant code, a theme coder on the
topic-vs-theme distinction, and so on. This module asks an LLM to produce
one tailored prompt section per role.
"""

from __future__ import annotations

import asyncio

from openhands.sdk import LLM, Message, TextContent

from thematic_analysis.research_context import AGENT_ROLES


_ROLE_BRIEFS: dict[str, str] = {
    "coder": (
        "The downstream agent is a *coder*. It reads one short text segment "
        "at a time and assigns 0–3 short codes that capture features of the "
        "segment relevant to the research question(s). Codes should name "
        "analytic features that could recur across segments, not paraphrase "
        "the segment. A segment with nothing relevant to the research "
        "question should get zero codes. Emphasise: how to judge relevance "
        "to the research question(s); whether to read for semantic surface "
        "or latent meaning; what kinds of features to attend to; what to "
        "ignore."
    ),
    "coding_critic": (
        "The downstream agent is a *coding critic*. It runs immediately "
        "after the coder, in a fresh chat session with no access to the "
        "codebook or the coder's identity. It sees one text segment and "
        "the 0–3 codes the coder just produced, and writes a short prose "
        "critique pushing back on those codes. Its single most important "
        "job is to judge whether each code is genuinely responsive to the "
        "research question(s), or merely paraphrases the segment / "
        "describes content that is off-topic for this study. Emphasise "
        "what counts as on-topic vs. off-topic for this specific research "
        "focus, with concrete examples of features that would be relevant "
        "and features that would not. Make explicit that recommending the "
        "coder drop all codes — leaving the segment uncoded — is the "
        "correct outcome whenever the segment does not speak to the "
        "research question(s); the critic must not invent relevance to "
        "justify keeping codes."
    ),
    "reviewer": (
        "The downstream agent is a *reviewer*. It judges, code-by-code, "
        "whether an aggregated candidate code should be added to the "
        "codebook, merged with an existing code, or skipped as off-topic. "
        "Emphasise how to decide that a code is on-topic for the research "
        "question(s) vs. off-topic; how to spot codes that are merely "
        "descriptive of the segment rather than analytically relevant."
    ),
}


_META_SYSTEM_PROMPT = """\
You translate a researcher's freeform research context into a short
prompt section that will be inserted into another LLM agent's system
prompt. The target agent is performing one specific step of a reflexive
thematic analysis pipeline (Braun & Clarke).

Your output is the prompt section itself — no preamble, no commentary,
no markdown code fences. It will be inserted verbatim under a heading
like "## Research Context" inside the target agent's system prompt.

Write in second person, addressing the target agent. Keep it tight
(roughly 50–200 words). Start by stating the research context and the
research question(s) the researcher wrote, in your own slightly
condensed phrasing if helpful, then give the target agent specific
guidance for its job — what to attend to, what counts as relevant, what
to avoid. Stay grounded in what the researcher actually wrote; do not
invent research questions or framings they did not state.

Assume the base system prompt already defines the agent's general role,
workflow, and output format. Do not restate that generic role guidance.
In particular, do not repeat boilerplate such as "you are a coder",
"you are a critic", "your job is to...", summaries of what inputs the
agent sees, or generic instructions about coding/reviewing/themeing.
Write only the research-context-specific delta: what this study is about,
what counts as on-topic vs. off-topic for this study, what kinds of
features matter here, and any study-specific framing the agent should use.

Do not add boilerplate methodology advice (6 Rs, definitions of
"theme", etc.) — that is supplied separately. Focus on what the role
needs to know about the research context and how it needs to apply the
research context.
"""


def _meta_user_prompt(description: str, role: str) -> str:
    brief = _ROLE_BRIEFS.get(role, "")
    return (
        "## Researcher's freeform research context\n"
        f'"""\n{description.strip()}\n"""\n\n'
        "## Target agent\n"
        f"{brief}\n\n"
        "Now write only the research-context-specific prompt fragment for this "
        "agent. Do not repeat the agent's generic role or workflow."
    )


def _load_llm() -> LLM:
    return LLM.load_from_env()


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # Drop opening fence (``` or ```markdown etc.)
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _completion_text(llm: LLM, system: str, user: str) -> str:
    response = llm.completion(
        messages=[
            Message(role="system", content=[TextContent(text=system)]),
            Message(role="user", content=[TextContent(text=user)]),
        ]
    )
    parts: list[str] = []
    for part in response.message.content:
        if isinstance(part, TextContent):
            parts.append(part.text)
    return _strip_fences("".join(parts))


def generate_tailored_prompt(
    description: str, role: str, llm: LLM | None = None
) -> str:
    """Generate a single tailored prompt section for one agent role.

    Args:
        description: The freeform research context.
        role: One of ``AGENT_ROLES``.
        llm: Optional LLM instance (mostly for tests / dependency injection).

    Returns:
        The tailored prompt section (plain markdown, no code fences).
    """
    if role not in AGENT_ROLES:
        raise ValueError(f"unknown agent role: {role!r}")
    if not description.strip():
        return ""
    llm = llm or _load_llm()
    return _completion_text(
        llm, _META_SYSTEM_PROMPT, _meta_user_prompt(description, role)
    )


def generate_all_tailored_prompts(
    description: str, llm: LLM | None = None
) -> dict[str, str]:
    """Generate tailored prompts for every role in ``AGENT_ROLES``.

    Runs the role generations concurrently via ``asyncio.to_thread`` so
    the four LLM calls overlap.
    """
    if not description.strip():
        return {}
    llm = llm or _load_llm()

    async def _run() -> dict[str, str]:
        tasks = [
            asyncio.to_thread(generate_tailored_prompt, description, role, llm)
            for role in AGENT_ROLES
        ]
        results = await asyncio.gather(*tasks)
        return dict(zip(AGENT_ROLES, results))

    try:
        return asyncio.run(_run())
    except RuntimeError:
        # Fall back to sequential calls if an event loop is already running.
        return {
            role: generate_tailored_prompt(description, role, llm)
            for role in AGENT_ROLES
        }
