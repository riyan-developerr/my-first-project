from __future__ import annotations

import mimetypes
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditEvent,
    Customer,
    PaymentProof,
    PricingRule,
    PrintJob,
    PrinterProfile,
    UploadedFile,
    utcnow,
)


SUPPORTED_MIME_TYPES = {
    "application/pdf": ("pdf", ".pdf"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx", ".docx"),
    "application/msword": ("doc", ".doc"),
    "image/jpeg": ("image", ".jpg"),
    "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"),
}

ACTIVE_JOB_STATUSES = {
    "collecting_options",
    "payment_pending",
    "payment_submitted",
    "approved",
    "printing",
}


class DomainError(ValueError):
    pass


def audit(db: Session, entity_type: str, entity_id: str, action: str, actor: str, details: str | None = None) -> None:
    db.add(AuditEvent(entity_type=entity_type, entity_id=entity_id, action=action, actor=actor, details=details))


def seed_defaults(db: Session) -> None:
    if not db.scalar(select(PricingRule).limit(1)):
        defaults = [
            ("A4", "normal", "bw", "single", 10.0),
            ("A4", "normal", "bw", "double", 9.0),
            ("A4", "normal", "color", "single", 40.0),
            ("A4", "normal", "color", "double", 38.0),
            ("A4", "glossy", "color", "single", 120.0),
            ("A3", "normal", "bw", "single", 25.0),
            ("A3", "normal", "color", "single", 90.0),
            ("A3", "glossy", "color", "single", 250.0),
        ]
        for paper_size, paper_finish, color_mode, sides, price in defaults:
            db.add(
                PricingRule(
                    paper_size=paper_size,
                    paper_finish=paper_finish,
                    color_mode=color_mode,
                    sides=sides,
                    price_per_page=price,
                )
            )

    if not db.scalar(select(PrinterProfile).limit(1)):
        profiles = [
            ("A4 B/W", "Default Printer", "A4", "normal", "bw", "single"),
            ("A4 B/W Duplex", "Default Printer", "A4", "normal", "bw", "double"),
            ("A4 Color", "Default Color Printer", "A4", "normal", "color", "single"),
            ("Glossy Poster", "Default Color Printer", "A4", "glossy", "color", "single"),
        ]
        for name, printer_name, paper_size, paper_finish, color_mode, sides in profiles:
            db.add(
                PrinterProfile(
                    name=name,
                    printer_name=printer_name,
                    paper_size=paper_size,
                    paper_finish=paper_finish,
                    color_mode=color_mode,
                    sides=sides,
                )
            )
    db.commit()


def get_or_create_customer(db: Session, wa_id: str, name: str | None = None) -> Customer:
    customer = db.scalar(select(Customer).where(Customer.wa_id == wa_id))
    if customer:
        if name and customer.name != name:
            customer.name = name
        return customer
    customer = Customer(wa_id=wa_id, phone=wa_id, name=name)
    db.add(customer)
    db.flush()
    audit(db, "customer", str(customer.id), "created", "whatsapp")
    return customer


def validate_media(mime_type: str, size_bytes: int | None = None) -> tuple[str, str]:
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise DomainError("Unsupported file type. Please send PDF, DOC/DOCX, JPG, PNG, or WEBP.")
    if size_bytes is not None and size_bytes > settings.max_upload_mb * 1024 * 1024:
        raise DomainError(f"File is too large. Maximum size is {settings.max_upload_mb} MB.")
    return SUPPORTED_MIME_TYPES[mime_type]


def safe_storage_path(file_id: str, mime_type: str) -> Path:
    _, extension = validate_media(mime_type)
    return settings.storage_dir / f"{file_id}{extension}"


def create_uploaded_file(
    db: Session,
    customer: Customer,
    original_name: str,
    stored_path: Path,
    media_id: str | None,
    mime_type: str,
    size_bytes: int,
    page_count: int | None,
) -> UploadedFile:
    file_kind, _ = validate_media(mime_type, size_bytes)
    uploaded = UploadedFile(
        id=stored_path.stem,
        customer_id=customer.id,
        original_name=original_name[:255],
        stored_name=stored_path.name,
        media_id=media_id,
        mime_type=mime_type,
        file_kind=file_kind,
        size_bytes=size_bytes,
        page_count=page_count,
        expires_at=None,
    )
    db.add(uploaded)
    db.flush()
    audit(db, "file", uploaded.id, "stored", "whatsapp", original_name)
    return uploaded


def create_print_job(db: Session, customer: Customer, uploaded: UploadedFile, extracted_options: dict | None = None) -> PrintJob:
    job = PrintJob(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        uploaded_file_id=uploaded.id,
        paper_size="A4",
        copies=1,
        estimated_pages=uploaded.page_count,
        currency=settings.currency,
    )
    db.add(job)
    db.flush()
    apply_options(job, extracted_options or {})
    update_job_quote(db, job)
    audit(db, "job", job.id, "created", "whatsapp", uploaded.original_name)
    return job


def apply_options(job: PrintJob, options: dict) -> None:
    if job.payment_status == "approved":
        raise DomainError("Approved jobs cannot be changed. Cancel or refund first.")
    allowed = {"paper_size", "paper_finish", "color_mode", "sides", "copies", "page_range"}
    for key, value in options.items():
        if key not in allowed or value in (None, ""):
            continue
        if key == "copies":
            setattr(job, key, max(1, min(int(value), 500)))
        elif key in {"paper_finish", "color_mode", "sides"}:
            setattr(job, key, str(value).lower())
        elif key == "paper_size":
            setattr(job, key, str(value).upper())
        else:
            setattr(job, key, str(value))


def missing_options(job: PrintJob) -> list[str]:
    missing = []
    if not job.paper_finish:
        missing.append("paper finish: normal or glossy/shining")
    if not job.color_mode:
        missing.append("color mode: black-and-white or color")
    if not job.sides:
        missing.append("sides: single-sided or double-sided")
    return missing


def page_count_for_job(job: PrintJob) -> int:
    if job.page_range:
        return count_page_range(job.page_range)
    if job.estimated_pages:
        return max(1, job.estimated_pages)
    if job.uploaded_file and job.uploaded_file.page_count:
        return max(1, job.uploaded_file.page_count)
    return 1


def count_page_range(page_range: str) -> int:
    pages: set[int] = set()
    for part in page_range.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            if not start_raw.isdigit() or not end_raw.isdigit():
                continue
            start, end = int(start_raw), int(end_raw)
            if start > end:
                start, end = end, start
            pages.update(range(max(1, start), min(end, 2000) + 1))
        elif part.isdigit():
            pages.add(max(1, int(part)))
    return max(1, len(pages))


def update_job_quote(db: Session, job: PrintJob) -> None:
    if missing_options(job):
        job.total_amount = None
        return
    rule = find_pricing_rule(db, job)
    pages = page_count_for_job(job)
    job.estimated_pages = pages
    job.total_amount = round(rule.price_per_page * pages * max(1, job.copies), 2)
    job.payment_instructions = settings.payment_instructions
    if job.status == "collecting_options":
        job.status = "payment_pending"


def find_pricing_rule(db: Session, job: PrintJob) -> PricingRule:
    paper_finish = job.paper_finish or "normal"
    color_mode = job.color_mode or "bw"
    sides = job.sides or "single"
    exact = db.scalar(
        select(PricingRule).where(
            PricingRule.paper_size == job.paper_size,
            PricingRule.paper_finish == paper_finish,
            PricingRule.color_mode == color_mode,
            PricingRule.sides == sides,
            PricingRule.active.is_(True),
        )
    )
    if exact:
        return exact
    fallback = db.scalar(select(PricingRule).where(PricingRule.active.is_(True)).order_by(PricingRule.id).limit(1))
    if fallback:
        return fallback
    raise DomainError("No active pricing rule is configured.")


def latest_active_job(db: Session, customer: Customer) -> PrintJob | None:
    return db.scalar(
        select(PrintJob)
        .where(PrintJob.customer_id == customer.id, PrintJob.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(PrintJob.created_at.desc())
        .limit(1)
    )


def submit_payment_proof(
    db: Session,
    job: PrintJob,
    kind: str,
    actor: str,
    transaction_id: str | None = None,
    media_id: str | None = None,
    file_id: str | None = None,
    notes: str | None = None,
) -> PaymentProof:
    if job.status not in {"payment_pending", "payment_submitted"}:
        raise DomainError("Payment proof can only be submitted after the quote is ready.")
    proof = PaymentProof(
        id=str(uuid.uuid4()),
        job_id=job.id,
        kind=kind,
        transaction_id=transaction_id,
        media_id=media_id,
        file_id=file_id,
        notes=notes,
    )
    job.payment_status = "proof_submitted"
    job.status = "payment_submitted"
    db.add(proof)
    audit(db, "job", job.id, "payment_proof_submitted", actor, transaction_id or media_id or notes)
    return proof


def approve_payment(db: Session, job: PrintJob, actor: str) -> None:
    if job.payment_status not in {"proof_submitted", "rejected"}:
        raise DomainError("Only submitted or rejected payment proofs can be approved.")
    job.payment_status = "approved"
    job.status = "approved"
    job.approved_at = utcnow()
    for proof in job.payment_proofs:
        if proof.status == "submitted":
            proof.status = "approved"
            proof.reviewed_by = actor
            proof.reviewed_at = utcnow()
    audit(db, "job", job.id, "payment_approved", actor)


def reject_payment(db: Session, job: PrintJob, actor: str, notes: str | None = None) -> None:
    if job.payment_status not in {"proof_submitted", "approved"}:
        raise DomainError("Only submitted or approved payment can be rejected.")
    job.payment_status = "rejected"
    job.status = "payment_pending"
    for proof in job.payment_proofs:
        if proof.status == "submitted":
            proof.status = "rejected"
            proof.reviewed_by = actor
            proof.reviewed_at = utcnow()
    audit(db, "job", job.id, "payment_rejected", actor, notes)


def cancel_job(db: Session, job: PrintJob, actor: str, reason: str | None = None) -> None:
    job.status = "cancelled"
    schedule_job_files_for_cleanup(db, job)
    audit(db, "job", job.id, "cancelled", actor, reason)


def mark_refunded(db: Session, job: PrintJob, actor: str, reason: str | None = None) -> None:
    job.payment_status = "refunded"
    job.status = "refund_marked"
    schedule_job_files_for_cleanup(db, job)
    audit(db, "job", job.id, "refund_marked", actor, reason)


def reprice_job(db: Session, job: PrintJob, amount: float, actor: str) -> None:
    if job.payment_status == "approved":
        raise DomainError("Approved jobs cannot be repriced. Cancel or refund first.")
    job.total_amount = round(max(0, amount), 2)
    job.status = "payment_pending"
    audit(db, "job", job.id, "repriced", actor, str(job.total_amount))


def find_printer_profile(db: Session, job: PrintJob) -> PrinterProfile | None:
    return db.scalar(
        select(PrinterProfile)
        .where(
            PrinterProfile.active.is_(True),
            PrinterProfile.paper_size == job.paper_size,
            PrinterProfile.paper_finish == (job.paper_finish or "normal"),
            PrinterProfile.color_mode == (job.color_mode or "bw"),
            PrinterProfile.sides == (job.sides or "single"),
        )
        .order_by(PrinterProfile.id)
        .limit(1)
    )


def claim_next_job(db: Session, agent_id: str, minutes: int = 10) -> PrintJob | None:
    now = utcnow()
    job = db.scalar(
        select(PrintJob)
        .where(
            PrintJob.payment_status == "approved",
            or_(
                PrintJob.status == "approved",
                and_(PrintJob.status == "printing", PrintJob.claim_expires_at < now),
            ),
        )
        .order_by(PrintJob.approved_at.asc(), PrintJob.created_at.asc())
        .limit(1)
    )
    if not job:
        return None
    job.status = "printing"
    job.claimed_by = agent_id
    job.claim_expires_at = now + timedelta(minutes=minutes)
    audit(db, "job", job.id, "claimed_for_print", agent_id)
    return job


def complete_print_job(db: Session, job: PrintJob, actor: str) -> None:
    if job.payment_status != "approved":
        raise DomainError("Cannot complete an unpaid job.")
    job.status = "printed"
    job.printed_at = utcnow()
    job.pickup_code = job.pickup_code or generate_pickup_code()
    job.failure_reason = None
    job.claim_expires_at = None
    schedule_job_files_for_cleanup(db, job)
    audit(db, "job", job.id, "printed", actor)


def fail_print_job(db: Session, job: PrintJob, actor: str, reason: str) -> None:
    if job.payment_status != "approved":
        raise DomainError("Cannot mark an unpaid job as failed.")
    job.status = "failed"
    job.failure_reason = reason[:1000]
    job.claim_expires_at = None
    audit(db, "job", job.id, "print_failed", actor, reason)


def schedule_job_files_for_cleanup(db: Session, job: PrintJob) -> None:
    expires_at = utcnow() + timedelta(days=settings.retention_days)
    if job.uploaded_file:
        job.uploaded_file.expires_at = expires_at
    proof_file_ids = [proof.file_id for proof in job.payment_proofs if proof.file_id]
    if proof_file_ids:
        for uploaded in db.scalars(select(UploadedFile).where(UploadedFile.id.in_(proof_file_ids))).all():
            uploaded.expires_at = expires_at


def cleanup_expired_files(db: Session) -> int:
    expired_files = db.scalars(
        select(UploadedFile).where(
            UploadedFile.expires_at.is_not(None),
            UploadedFile.expires_at < utcnow(),
            UploadedFile.status != "deleted",
        )
    ).all()
    deleted = 0
    for uploaded in expired_files:
        path = settings.storage_dir / uploaded.stored_name
        try:
            if path.exists():
                os.remove(path)
            uploaded.status = "deleted"
            deleted += 1
            audit(db, "file", uploaded.id, "deleted_after_retention", "system")
        except OSError as exc:
            audit(db, "file", uploaded.id, "delete_failed", "system", str(exc))
    return deleted


def generate_pickup_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def guess_mime_type(filename: str, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(filename)[0] or fallback


def detect_page_count(path: Path, mime_type: str) -> int | None:
    try:
        if mime_type == "application/pdf":
            from pypdf import PdfReader

            return len(PdfReader(str(path)).pages)
        if mime_type.startswith("image/"):
            from PIL import Image

            with Image.open(path) as image:
                return getattr(image, "n_frames", 1) or 1
    except Exception:
        return None
    return None


def file_url(job: PrintJob) -> str:
    return f"{settings.app_base_url}/agent/jobs/{job.id}/file"
