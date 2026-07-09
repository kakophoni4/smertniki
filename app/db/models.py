from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class TicketStatus(StrEnum):
    IN_PROGRESS = "in_progress"  # В работе
    HEALED = "healed"  # Вылечена
    CLOSED = "closed"


class IssueType(StrEnum):
    ADDRESS = "address"  # Недостоверность адреса
    DIRECTOR = "director"  # Недостоверность руководителя / ДЛ
    FOUNDER = "founder"  # Недостоверность учредителя
    LIQUIDATION = "liquidation"  # Ликвидация / исключение
    OTHER = "other"


class AllowedUser(Base):
    __tablename__ = "allowed_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ogrn: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rusprofile_url: Mapped[str] = mapped_column(String(512))

    # Последнее известное состояние флагов
    unreliable_address: Mapped[bool] = mapped_column(Boolean, default=False)
    unreliable_director: Mapped[bool] = mapped_column(Boolean, default=False)
    unreliable_founder: Mapped[bool] = mapped_column(Boolean, default=False)
    is_liquidating: Mapped[bool] = mapped_column(Boolean, default=False)
    is_liquidated: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="company")
    checks: Mapped[list["CheckResult"]] = relationship(back_populates="company")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    issue_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default=TicketStatus.IN_PROGRESS, index=True)
    title: Mapped[str] = mapped_column(String(512))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="tickets")


class CheckResult(Base):
    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    unreliable_address: Mapped[bool] = mapped_column(Boolean, default=False)
    unreliable_director: Mapped[bool] = mapped_column(Boolean, default=False)
    unreliable_founder: Mapped[bool] = mapped_column(Boolean, default=False)
    is_liquidating: Mapped[bool] = mapped_column(Boolean, default=False)
    is_liquidated: Mapped[bool] = mapped_column(Boolean, default=False)
    status_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="checks")
