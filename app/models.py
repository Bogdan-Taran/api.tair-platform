from datetime import UTC, date, datetime, time
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC)


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)


class User(Identity, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RefreshToken(Identity, Base):
    __tablename__ = "refresh_tokens"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Branch(Identity, Base):
    __tablename__ = "branches"
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(500), default="")
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Tomsk")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Coach(Identity, Base):
    __tablename__ = "coaches"
    name: Mapped[str] = mapped_column(String(200))
    salary_bps: Mapped[int | None] = mapped_column(Integer)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("salary_bps BETWEEN 0 AND 10000"),)


class Group(Identity, Base):
    __tablename__ = "groups"
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    coach_id: Mapped[str] = mapped_column(ForeignKey("coaches.id"))
    name: Mapped[str] = mapped_column(String(200))
    age_min: Mapped[int] = mapped_column(Integer, default=0)
    age_max: Mapped[int] = mapped_column(Integer, default=18)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("age_min >= 0 AND age_max >= age_min"),)


class Athlete(Identity, Base):
    __tablename__ = "athletes"
    name: Mapped[str] = mapped_column(String(200), index=True)
    birth_date: Mapped[date | None] = mapped_column(Date)
    parent_name: Mapped[str] = mapped_column(String(200), default="")
    parent_phone: Mapped[str] = mapped_column(String(50), default="")
    notes: Mapped[str] = mapped_column(String(2000), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Enrollment(Identity, Base):
    __tablename__ = "enrollments"
    athlete_id: Mapped[str] = mapped_column(ForeignKey("athletes.id"), index=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), index=True)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on >= starts_on"),)


class Tariff(Identity, Base):
    __tablename__ = "tariffs"
    name: Mapped[str] = mapped_column(String(200))
    lessons: Mapped[int] = mapped_column(Integer)
    amount_kopecks: Mapped[int] = mapped_column(Integer)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint("lessons IN (1,4,8,12)"),
        CheckConstraint("amount_kopecks > 0"),
        Index(
            "uq_tariffs_active_lessons",
            "lessons",
            unique=True,
            postgresql_where=text("NOT archived"),
            sqlite_where=text("archived = 0"),
        ),
    )


class Payment(Identity, Base):
    __tablename__ = "payments"
    athlete_id: Mapped[str] = mapped_column(ForeignKey("athletes.id"), index=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"))
    tariff_id: Mapped[str] = mapped_column(ForeignKey("tariffs.id"))
    lessons: Mapped[int] = mapped_column(Integer)
    amount_kopecks: Mapped[int] = mapped_column(Integer)
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    method: Mapped[str] = mapped_column(String(20))
    request_key: Mapped[str] = mapped_column(String(100), unique=True)
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (CheckConstraint("lessons > 0 AND amount_kopecks > 0"),)


class Schedule(Identity, Base):
    __tablename__ = "schedules"
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6"),
        CheckConstraint("ends_at > starts_at"),
    )


class Training(Identity, Base):
    __tablename__ = "trainings"
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), index=True)
    coach_id: Mapped[str] = mapped_column(ForeignKey("coaches.id"))
    salary_bps: Mapped[int | None] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date, index=True)
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("group_id", "date", "starts_at"),
        CheckConstraint("ends_at > starts_at"),
    )


class Attendance(Identity, Base):
    __tablename__ = "attendance"
    training_id: Mapped[str] = mapped_column(ForeignKey("trainings.id"), index=True)
    athlete_id: Mapped[str] = mapped_column(ForeignKey("athletes.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"))
    value_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    debt_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    single_price_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("training_id", "athlete_id"),
        CheckConstraint("status IN ('present','absent','unmarked')"),
    )


class Audit(Identity, Base):
    __tablename__ = "audit"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    entity: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    reason: Mapped[str] = mapped_column(String(500))
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
