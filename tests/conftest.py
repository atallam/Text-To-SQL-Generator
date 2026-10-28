import pytest

from text2sql.config.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        anthropic_api_key="test-key",
        llm_retry_wait_s=0,
        llm_max_retries=3,
        llm_max_output_attempts=2,
    )
