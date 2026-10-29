import pytest

from tests.helpers import FIXTURES
from text2sql.config.settings import Settings
from text2sql.config.sources import load_source
from text2sql.schema.embeddings import LexicalEmbedder
from text2sql.schema.glossary import Glossary
from text2sql.schema.model import SchemaSnapshot


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        anthropic_api_key="test-key",
        llm_retry_wait_s=0,
        llm_max_retries=3,
        llm_max_output_attempts=2,
    )


@pytest.fixture(scope="session")
def wealthlens_source():
    return load_source("wealthlens", Settings(_env_file=None))


@pytest.fixture(scope="session")
def wealthlens_snapshot() -> SchemaSnapshot:
    """Offline copy of the introspected WealthLens schema (regenerate: python -m scripts.dump_schema)."""
    return SchemaSnapshot.model_validate_json(
        (FIXTURES / "wealthlens_schema.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def wealthlens_glossary(wealthlens_source) -> Glossary:
    src = wealthlens_source
    return Glossary.load(src.base_dir / src.glossary, src.today_sql)


@pytest.fixture(scope="session")
def lexical() -> LexicalEmbedder:
    return LexicalEmbedder()
