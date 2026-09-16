"""DB 설정과 SQLAlchemy 공통 기반을 실제 PostgreSQL 연결 없이 검증한다."""

import pytest

from seulseul.config import ConfigError, DatabaseSettings, load_database_settings
from seulseul.database import create_database_engine, create_session_factory

DATABASE_URL = "postgresql+psycopg://seulseul:secret@127.0.0.1:5432/seulseul_dev"


def test_load_database_settings_accepts_psycopg_url() -> None:
    assert load_database_settings({"DATABASE_URL": f"  {DATABASE_URL}  "}) == DatabaseSettings(
        url=DATABASE_URL
    )


@pytest.mark.parametrize("url", ["", "postgresql://localhost/seulseul", "sqlite:///local.db"])
def test_load_database_settings_rejects_missing_or_wrong_driver(url: str) -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_database_settings({"DATABASE_URL": url})


def test_create_database_engine_and_session_factory_do_not_connect_eagerly() -> None:
    engine = create_database_engine(DatabaseSettings(url=DATABASE_URL))
    session_factory = create_session_factory(engine)

    assert engine.url.drivername == "postgresql+psycopg"
    assert session_factory.kw["expire_on_commit"] is False

    engine.dispose()
