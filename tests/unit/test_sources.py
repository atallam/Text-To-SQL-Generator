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
