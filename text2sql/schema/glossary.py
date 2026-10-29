"""Business glossary: definitions the prompt should honour, and terms that need clarification.

File format (sources/<name>/glossary.yaml):

    terms:
      spend:
        definition: Sum of transactions.amount where ...
        aliases: [spending]            # optional extra phrases that match this term
      net_worth:
        definition: ...
        ambiguous: true               # materially different answers -> ask, do not guess
        interpretations:
          - label: Investments only
            example_sql: SELECT ...
            cues: [investments only]  # optional: if the question says this, the term is resolved
    conventions:
      - transactions.amount is always positive ...
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Interpretation(BaseModel):
    label: str
    example_sql: str | None = None
    cues: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data):
        return {"label": data} if isinstance(data, str) else data


class GlossaryTerm(BaseModel):
    name: str = ""
    definition: str | None = None
    notes: str | None = None
    aliases: list[str] = Field(default_factory=list)
    ambiguous: bool = False
    interpretations: list[Interpretation] = Field(default_factory=list)

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases_none(cls, v):
        return v or []

    @model_validator(mode="after")
    def _check_ambiguity(self):
        if self.ambiguous:
            if len(self.interpretations) < 2:
                raise ValueError(f"ambiguous term {self.name!r} needs at least 2 interpretations")
            missing = [i.label for i in self.interpretations if not i.example_sql]
            if missing:
                raise ValueError(
                    f"ambiguous term {self.name!r}: interpretations without example_sql: {missing}"
                )
        return self

    @property
    def phrases(self) -> list[str]:
        """Every phrase that should trigger this term, longest first."""
        base = self.name.replace("_", " ").strip()
        return sorted({base, *self.aliases}, key=len, reverse=True)


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in phrase.lower().split()]
    # Allow a plural/possessive tail on the last word ("budgets", "net worth's").
    return re.compile(r"(?<![\w])" + r"[\s_-]+".join(words) + r"(?:s|es|'s)?(?![\w])", re.IGNORECASE)


class Glossary(BaseModel):
    terms: dict[str, GlossaryTerm] = Field(default_factory=dict)
    conventions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _name_terms(self):
        for key, term in self.terms.items():
            term.name = key
        return self

    @classmethod
    def empty(cls) -> Glossary:
        return cls()

    @classmethod
    def load(cls, path: Path, today_sql: str = "CURRENT_DATE") -> Glossary:
        """Load a glossary file. ``{TODAY}`` in any text is replaced by the source's date expression,
        so example SQL stays valid for demo data whose "today" is fixed."""
        raw = path.read_text(encoding="utf-8").replace("{TODAY}", today_sql)
        return cls.model_validate(yaml.safe_load(raw) or {})

    def find(self, text: str) -> list[tuple[GlossaryTerm, str]]:
        """Terms mentioned in ``text`` with the phrase that matched, in order of appearance.

        When one match sits inside a longer match of another term ("savings" inside "savings rate"),
        only the longer, more specific term is reported.
        """
        hits: list[tuple[int, int, GlossaryTerm, str]] = []
        for term in self.terms.values():
            best: re.Match[str] | None = None
            for phrase in term.phrases:
                m = _phrase_regex(phrase).search(text)
                if m and (best is None or len(m.group(0)) > len(best.group(0))):
                    best = m
            if best:
                hits.append((best.start(), best.end(), term, best.group(0)))
        kept = [
            h
            for h in hits
            if not any(
                o is not h and o[0] <= h[0] and h[1] <= o[1] and (o[1] - o[0]) > (h[1] - h[0]) for o in hits
            )
        ]
        kept.sort(key=lambda h: h[0])
        return [(t, p) for _, _, t, p in kept]

    def matches(self, text: str) -> list[GlossaryTerm]:
        return [t for t, _ in self.find(text)]

    @staticmethod
    def _text_of(term: GlossaryTerm) -> str:
        return " ".join(
            filter(None, [term.definition, term.notes, *[i.example_sql for i in term.interpretations]])
        )

    @staticmethod
    def _mentions(text: str, phrase: str) -> bool:
        return bool(re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text, re.IGNORECASE))

    @classmethod
    def tables_in(cls, term: GlossaryTerm, table_names: list[str]) -> list[str]:
        """Tables named directly in the term's definition, notes or example SQL (whole-word match)."""
        blob = cls._text_of(term)
        return [t for t in table_names if cls._mentions(blob, t)]

    def tables_for(self, term: GlossaryTerm, table_names: list[str]) -> list[str]:
        """Like ``tables_in`` but also follows references to other glossary terms, so that
        'savings_rate = net_savings / income' resolves to the tables behind net_savings and income."""
        found: list[str] = []
        seen: set[str] = set()

        def visit(t: GlossaryTerm) -> None:
            if t.name in seen:
                return
            seen.add(t.name)
            for name in self.tables_in(t, table_names):
                if name not in found:
                    found.append(name)
            blob = self._text_of(t)
            for other in self.terms.values():
                if other.name != t.name and (
                    self._mentions(blob, other.name) or self._mentions(blob, other.name.replace("_", " "))
                ):
                    visit(other)

        visit(term)
        return found
