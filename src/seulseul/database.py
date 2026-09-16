"""PostgreSQL 엔진과 SQLAlchemy 세션의 공통 기반."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from seulseul.config import DatabaseSettings


class Base(DeclarativeBase):
    """도메인별 영속 모델이 공유하는 SQLAlchemy 선언 기반."""


def create_database_engine(settings: DatabaseSettings) -> Engine:
    """연결이 끊긴 풀 항목을 사용 전에 검사하는 동기 엔진을 만든다."""
    return create_engine(settings.url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """서비스가 트랜잭션 단위로 사용할 세션 팩터리를 만든다."""
    return sessionmaker(bind=engine, expire_on_commit=False)
