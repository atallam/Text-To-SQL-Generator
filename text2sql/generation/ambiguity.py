"""Ambiguity handling: ask, do not guess.

When a question uses a glossary term flagged ``ambiguous`` (e.g. "revenue" gross vs net, "late"
promised vs requested date, "net worth" with or without bank balances), the pipeline stops and returns
a structured clarification request listing every interpretation with example SQL. The caller (API/UI)
shows it, the user picks one, and the question is re-submitted with that choice, which is then
injected into the prompt.

This is deterministic and glossary-driven on purpose: it is cheap, auditable and unit-testable. Terms
that are not in the glossary are not detected; add them to the source's glossary.yaml.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from text2sql.schema.glossary import Glossary, GlossaryTerm, Interpretation


class ClarificationOption(BaseModel):
    index: int
    label: str
    example_sql: str


class AmbiguousTerm(BaseModel):
    term: str
    matched_text: str
    definition: str | None = None
    options: list[ClarificationOption]


class ClarificationRequest(BaseModel):
    """The question is ambiguous. Choose one interpretation per term and ask again."""

    question: str
    terms: list[AmbiguousTerm]
    message: str = Field(default="")

    def render_message(self) -> str:
        lines = ["Your question could mean different things. Which did you intend?"]
        for t in self.terms:
            lines.append(f'\n"{t.matched_text}" can mean:')
            for o in t.options:
                lines.append(f"  {o.index + 1}. {o.label}")
        return "\n".join(lines)


def _cued(term: GlossaryTerm, question: str) -> Interpretation | None:
    """An interpretation whose cue phrase appears in the question resolves the ambiguity by itself.
    If cues of different interpretations both appear, the question is still ambiguous."""
    q = question.lower()
    hits = [i for i in term.interpretations if any(c.lower() in q for c in i.cues)]
    return hits[0] if len(hits) == 1 else None


def resolve_choice(term: GlossaryTerm, choice: int | str) -> Interpretation:
    """``choice`` is a 0-based option index or the option's label (case-insensitive)."""
    if isinstance(choice, int):
        if not 0 <= choice < len(term.interpretations):
            raise ValueError(
                f"{term.name}: option {choice} out of range (0..{len(term.interpretations) - 1})"
            )
        return term.interpretations[choice]
    for interp in term.interpretations:
        if interp.label.lower() == str(choice).lower():
            return interp
    raise ValueError(f"{term.name}: no interpretation labelled {choice!r}")


def detect_ambiguity(
    question: str,
    glossary: Glossary,
    clarifications: dict[str, int | str] | None = None,
) -> tuple[ClarificationRequest | None, dict[str, Interpretation]]:
    """Return (clarification or None, resolved interpretations by term name).

    A term is resolved when the caller already supplied a choice for it, or when the question
    itself contains a cue phrase for exactly one interpretation.
    """
    clarifications = clarifications or {}
    unresolved: list[AmbiguousTerm] = []
    resolved: dict[str, Interpretation] = {}
    for term, matched in glossary.find(question):
        if not term.ambiguous:
            continue
        if term.name in clarifications:
            resolved[term.name] = resolve_choice(term, clarifications[term.name])
        elif (cue := _cued(term, question)) is not None:
            resolved[term.name] = cue
        else:
            unresolved.append(
                AmbiguousTerm(
                    term=term.name,
                    matched_text=matched,
                    definition=term.definition,
                    options=[
                        ClarificationOption(index=i, label=interp.label, example_sql=interp.example_sql or "")
                        for i, interp in enumerate(term.interpretations)
                    ],
                )
            )
    if not unresolved:
        return None, resolved
    request = ClarificationRequest(question=question, terms=unresolved)
    request.message = request.render_message()
    return request, resolved
