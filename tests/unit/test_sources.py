from text2sql.config.settings import Settings
from text2sql.config.sources import list_sources, load_source


def test_wealthlens_source_loads():
    s = Settings(_env_file=None)
    cfg = load_source("wealthlens", s)
    assert cfg.dialect == "postgresql"
    assert cfg.sample_values.default == "never"
    assert "transactions.txn_type" in cfg.sample_values.allow
    assert (cfg.base_dir / "schema.sql").is_file()
    assert (cfg.base_dir / cfg.glossary).is_file()


def test_list_sources():
    assert "wealthlens" in list_sources(Settings(_env_file=None))


def test_get_env_falls_back_to_dotenv_file(tmp_path, monkeypatch):
    from text2sql.config.settings import get_env

    f = tmp_path / ".env"
    f.write_text("T2S_FROM_DOTENV=hello\n", encoding="utf-8")
    monkeypatch.delenv("T2S_FROM_DOTENV", raising=False)
    assert get_env("T2S_FROM_DOTENV", env_file=f) == "hello"
    monkeypatch.setenv("T2S_FROM_DOTENV", "shell-wins")
    assert get_env("T2S_FROM_DOTENV", env_file=f) == "shell-wins"
    assert get_env("T2S_MISSING", "dflt", env_file=f) == "dflt"
