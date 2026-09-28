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


def test_reminder_migration_preserves_rows_and_matches_model():
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect

    from seulseul.checklists.model import ChecklistModel

    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_current_head() == "e05243bd795f"
    migration = scripts.get_revision("e05243bd795f").module
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE checklists (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO checklists VALUES (1)")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            actual = {col["name"] for col in inspect(connection).get_columns("checklists")}
            expected = {
                name
                for name in ChecklistModel.__table__.columns.keys()
                if name.startswith("reminder_")
            }
            assert actual - {"id"} == expected
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT id FROM checklists").scalar_one() == 1
    engine.dispose()
