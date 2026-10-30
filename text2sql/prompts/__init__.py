"""Versioned prompt templates (Jinja2).

Templates live in ``templates/<name>.<version>.<part>.j2``. A change to wording is a NEW version file
(``v2``) so old and new prompts can be compared in evals; snapshot tests pin the rendered output of
every committed version. ``StrictUndefined`` makes a missing variable an error, never a silent blank.
"""

from __future__ import annotations

from functools import lru_cache

from jinja2 import Environment, PackageLoader, StrictUndefined

SQL_GENERATION = "sql_generation"
SQL_REPAIR = "sql_repair"
SQL_DESCRIPTION = "sql_description"  # Phase 3: back-translation (sees the SQL, never the question)
SQL_JUDGE = "sql_judge"  # Phase 3: alignment judge (sees the question + description, never the SQL)
SQL_ALTERNATIVE = "sql_alternative"  # Phase 3: independent second solution for multi-query validation
LATEST_VERSIONS = {
    SQL_GENERATION: "v2",
    SQL_REPAIR: "v1",
    SQL_DESCRIPTION: "v1",
    SQL_JUDGE: "v1",
    SQL_ALTERNATIVE: "v1",
}
PARTS = {  # template files per prompt
    SQL_GENERATION: ("system", "user"),
    SQL_REPAIR: ("user",),
    SQL_DESCRIPTION: ("system", "user"),
    SQL_JUDGE: ("system", "user"),
    SQL_ALTERNATIVE: ("user",),
}


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=PackageLoader("text2sql.prompts", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        autoescape=False,  # prompts are plain text, not HTML
    )


def render(name: str, version: str, part: str, **context) -> str:
    """Render ``templates/<name>.<version>.<part>.j2`` (part is 'system' or 'user')."""
    return _env().get_template(f"{name}.{version}.{part}.j2").render(**context).strip() + "\n"
