"""Alembic이 SeulSeul의 SQLAlchemy metadata와 DB 설정을 사용하게 한다."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from seulseul.checklists.model import ChecklistModel
from seulseul.config import load_database_settings
from seulseul.database import Base
from seulseul.notices.model import NoticeModel
from seulseul.users.model import StudentModel

_MAPPED_MODELS = (StudentModel, NoticeModel, ChecklistModel)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    settings = load_database_settings()
    context.configure(
        url=settings.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = load_database_settings().url
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
