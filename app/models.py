from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))

    files: Mapped[list["UploadedFile"]] = relationship(back_populates="customer")
    jobs: Mapped[list["PrintJob"]] = relationship(back_populates="customer")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_id: Mapped[str | None] = mapped_column(String(120), index=True)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    file_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="stored", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    customer: Mapped[Customer] = relationship(back_populates="files")
    jobs: Mapped[list["PrintJob"]] = relationship(back_populates="uploaded_file")


class PrintJob(Base, TimestampMixin):
    __tablename__ = "print_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True, nullable=False)
    uploaded_file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="collecting_options", index=True, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(40), default="unpaid", index=True, nullable=False)

    paper_size: Mapped[str] = mapped_column(String(20), default="A4", nullable=False)
    paper_finish: Mapped[str | None] = mapped_column(String(30))
    color_mode: Mapped[str | None] = mapped_column(String(30))
    sides: Mapped[str | None] = mapped_column(String(30))
    copies: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    page_range: Mapped[str | None] = mapped_column(String(80))
    estimated_pages: Mapped[int | None] = mapped_column(Integer)

    total_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="PKR", nullable=False)
    payment_instructions: Mapped[str | None] = mapped_column(Text)
    pickup_code: Mapped[str | None] = mapped_column(String(20), index=True)
    ai_notes: Mapped[str | None] = mapped_column(Text)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(String(120), index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    customer: Mapped[Customer] = relationship(back_populates="jobs")
    uploaded_file: Mapped[UploadedFile] = relationship(back_populates="jobs")
    payment_proofs: Mapped[list["PaymentProof"]] = relationship(back_populates="job")


class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("print_jobs.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    media_id: Mapped[str | None] = mapped_column(String(120))
    file_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("uploaded_files.id"))
    transaction_id: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(30), default="submitted", index=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    job: Mapped[PrintJob] = relationship(back_populates="payment_proofs")


class PrinterProfile(Base, TimestampMixin):
    __tablename__ = "printer_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    printer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    paper_size: Mapped[str] = mapped_column(String(20), default="A4", nullable=False)
    paper_finish: Mapped[str] = mapped_column(String(30), default="normal", nullable=False)
    color_mode: Mapped[str] = mapped_column(String(30), default="bw", nullable=False)
    sides: Mapped[str] = mapped_column(String(30), default="single", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PricingRule(Base, TimestampMixin):
    __tablename__ = "pricing_rules"
    __table_args__ = (
        UniqueConstraint("paper_size", "paper_finish", "color_mode", "sides", name="uq_pricing_combo"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_size: Mapped[str] = mapped_column(String(20), default="A4", nullable=False)
    paper_finish: Mapped[str] = mapped_column(String(30), default="normal", nullable=False)
    color_mode: Mapped[str] = mapped_column(String(30), default="bw", nullable=False)
    sides: Mapped[str] = mapped_column(String(30), default="single", nullable=False)
    price_per_page: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ProcessedWebhook(Base):
    __tablename__ = "processed_webhooks"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
