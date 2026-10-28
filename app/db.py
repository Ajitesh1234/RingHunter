import json
from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Float, Text, Integer, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings

_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_args)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # PayPal payer_id
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    device_id: Mapped[str | None] = mapped_column(String, nullable=True)
    card_fp: Mapped[str | None] = mapped_column(String, nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    shipping_address: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO8601


class Txn(Base):
    __tablename__ = "txns"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, index=True)
    ts: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)
    capture_id: Mapped[str | None] = mapped_column(String, nullable=True)
    refunded: Mapped[int] = mapped_column(Integer, default=0)


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True)
    score: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")  # open|investigated|actioned|dismissed
    entry_json: Mapped[str] = mapped_column(Text)
    casefile_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    def as_dict(self):
        return {"id": self.id, "score": self.score, "decision": self.decision, "status": self.status,
                "cluster": json.loads(self.entry_json),
                "casefile": json.loads(self.casefile_json) if self.casefile_json else None}


class ActionLog(Base):  # audit trail + idempotency
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True)
    case_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String)
    result_json: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Blocked(Base):
    __tablename__ = "blocked"
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[int] = mapped_column(Integer)


class Event(Base):  # raw verified PayPal webhooks
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text)


def init_db():
    Base.metadata.create_all(engine)
