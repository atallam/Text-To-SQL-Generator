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
LATEST_VERSIONS = {SQL_GENERATION: "v2", SQL_REPAIR: "v1"}
PARTS = {SQL_GENERATION: ("system", "user"), SQL_REPAIR: ("user",)}  # template files per prompt


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
