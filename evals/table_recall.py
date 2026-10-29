"""Phase 1 exit-gate metric: does the relevance filter keep the tables a question needs?

    python -m evals.table_recall                                  # offline: committed schema fixture
    python -m evals.table_recall --embedder sentence-transformers # real model (needs the extra)
    python -m evals.table_recall --sweep                          # recall vs context size trade-off
    python -m evals.table_recall --live                           # introspect the real database instead

Question recall = share of questions whose required tables are ALL inside the filtered context
(target >= 95%). Also reports average context size, because including every table scores 100%
recall and proves nothing. The ambiguity section of the seed file is checked too.
Exit code 1 when recall misses the target or an ambiguity expectation fails.
"""

from __future__ import annotations

import argparse
import itertools
from dataclasses import dataclass
from pathlib import Path

import yaml

from text2sql.config.settings import Settings
from text2sql.config.sources import RelevanceConfig, SourceConfig, load_source
from text2sql.generation.ambiguity import detect_ambiguity
from text2sql.schema.embeddings import Embedder, LexicalEmbedder, SentenceTransformerEmbedder
from text2sql.schema.glossary import Glossary
from text2sql.schema.model import SchemaSnapshot
from text2sql.schema.relevance import RelevanceFilter

DEFAULT_SEED = Path("evals/golden/wealthlens_seed.yaml")
DEFAULT_SNAPSHOT = Path("tests/fixtures/wealthlens_schema.json")
TARGET = 0.95


@dataclass
class RecallResult:
    questions: int
    full_hits: int
    table_recall: float
    avg_tables: float
    total_tables: int
    misses: list[dict]

    @property
    def question_recall(self) -> float:
        return self.full_hits / self.questions if self.questions else 0.0


def load_seed(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def measure(
    questions: list[dict],
    snapshot: SchemaSnapshot,
    embedder: Embedder,
    cfg: RelevanceConfig,
    glossary: Glossary,
) -> RecallResult:
    flt = RelevanceFilter(embedder, cfg, glossary)
    full, per_table, sizes, misses = 0, [], [], []
    for q in questions:
        sel = flt.select(q["question"], snapshot)
        need, got = set(q["required_tables"]), set(sel.names)
        sizes.append(len(got))
        per_table.append(len(need & got) / len(need))
        if need <= got:
            full += 1
        else:
            misses.append(
                {
                    "id": q["id"],
                    "question": q["question"],
                    "missing": sorted(need - got),
                    "scores": {
                        k: round(v, 3) for k, v in sorted(sel.all_scores.items(), key=lambda kv: -kv[1])
                    },
                }
            )
    return RecallResult(
        questions=len(questions),
        full_hits=full,
        table_recall=sum(per_table) / len(per_table),
        avg_tables=sum(sizes) / len(sizes),
        total_tables=len(snapshot.tables),
        misses=misses,
    )


def check_ambiguity(cases: list[dict], glossary: Glossary) -> list[str]:
    failures = []
    for case in cases:
        request, _ = detect_ambiguity(case["question"], glossary)
        got = sorted(t.term for t in request.terms) if request else []
        if case["expect"] == "clarification":
            if sorted(case["terms"]) != got:
                failures.append(
                    f"{case['question']!r}: expected clarification {case['terms']}, got {got or 'none'}"
                )
        elif request is not None:
            failures.append(f"{case['question']!r}: expected no clarification, got {got}")
    return failures


def _make_embedder(kind: str, model: str) -> Embedder:
    return LexicalEmbedder() if kind == "lexical" else SentenceTransformerEmbedder(model)


def _load_snapshot(args, source: SourceConfig) -> SchemaSnapshot:
    if args.live:
        from text2sql.connectors.registry import get_connector
        from text2sql.schema.introspect import introspect_connector

        connector = get_connector(source)
        try:
            return introspect_connector(connector, with_samples=False)
        finally:
            connector.dispose()
    return SchemaSnapshot.model_validate_json(args.snapshot.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    defaults = Settings(_env_file=None)
    p = argparse.ArgumentParser(description="Measure the relevance filter's table recall.")
    p.add_argument("--source", default="wealthlens")
    p.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    p.add_argument(
        "--snapshot", type=Path, default=DEFAULT_SNAPSHOT, help="Schema JSON from scripts.dump_schema"
    )
    p.add_argument("--live", action="store_true", help="Introspect the real database instead of --snapshot")
    p.add_argument(
        "--embedder", choices=["lexical", "sentence-transformers"], default=defaults.embedding_provider
    )
    p.add_argument("--model", default=defaults.embedding_model)
    p.add_argument("--target", type=float, default=TARGET)
    p.add_argument(
        "--sweep", action="store_true", help="Grid over threshold ratio, neighbors and glossary boost"
    )
    p.add_argument("--no-glossary-boost", action="store_true")
    p.add_argument("--neighbors", choices=["none", "outgoing", "both"], default=None)
    p.add_argument("--relative", type=float, default=None, help="Override relative_to_top")
    args = p.parse_args(argv)

    source = load_source(args.source)
    seed = load_seed(args.seed)
    snapshot = _load_snapshot(args, source)
    glossary = (
        Glossary.load(source.base_dir / source.glossary, source.today_sql)
        if source.glossary
        else Glossary.empty()
    )
    embedder = _make_embedder(args.embedder, args.model)
    print(
        f"Embedder: {embedder.name} | schema: {len(snapshot.tables)} tables | "
        f"{len(seed['questions'])} questions\n"
    )

    if args.sweep:
        rows = []
        for rel, nb, boost in itertools.product(
            [0.6, 0.7, 0.8, 0.85, 0.9, 0.95], ["none", "outgoing", "both"], [True, False]
        ):
            cfg = source.relevance.model_copy(
                update={"relative_to_top": rel, "neighbors": nb, "glossary_boost": boost}
            )
            r = measure(seed["questions"], snapshot, embedder, cfg, glossary)
            rows.append((r.question_recall, -r.avg_tables, rel, nb, boost, r))
        print("relative  neighbors  glossary  question-recall  table-recall  avg-tables")
        for _, _, rel, nb, boost, r in sorted(rows, key=lambda x: (x[0], x[1]), reverse=True)[:18]:
            print(
                f"{rel:<9} {nb:<10} {str(boost):<9} {r.question_recall:>14.0%}  {r.table_recall:>11.0%}  "
                f"{r.avg_tables:>6.1f}/{r.total_tables}"
            )
        return 0

    updates: dict = {}
    if args.no_glossary_boost:
        updates["glossary_boost"] = False
    if args.neighbors:
        updates["neighbors"] = args.neighbors
    if args.relative is not None:
        updates["relative_to_top"] = args.relative
    cfg = source.relevance.model_copy(update=updates)

    r = measure(seed["questions"], snapshot, embedder, cfg, glossary)
    print(
        f"Question recall : {r.full_hits}/{r.questions} = {r.question_recall:.0%}  (target {args.target:.0%})"
    )
    print(f"Table recall    : {r.table_recall:.0%}")
    print(f"Context size    : {r.avg_tables:.1f} of {r.total_tables} tables on average")
    print(f"Settings        : {cfg.model_dump()}")
    for m in r.misses:
        print(
            f"\n  MISS {m['id']}: {m['question']}\n       missing {m['missing']}\n"
            f"       scores  {m['scores']}"
        )

    amb = check_ambiguity(seed.get("ambiguity", []), glossary)
    total_amb = len(seed.get("ambiguity", []))
    print(f"\nAmbiguity checks: {total_amb - len(amb)}/{total_amb} as expected")
    for f in amb:
        print(f"  FAIL {f}")

    ok = r.question_recall >= args.target and not amb
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
