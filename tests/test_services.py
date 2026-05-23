from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import PrintAssistant
from app.database import Base
from app.models import Customer, UploadedFile
from app.services import (
    DomainError,
    approve_payment,
    apply_options,
    create_print_job,
    find_printer_profile,
    seed_defaults,
    submit_payment_proof,
    update_job_quote,
    validate_media,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as session:
        seed_defaults(session)
        yield session


def make_customer_and_file(db):
    customer = Customer(wa_id="923001234567", name="Student")
    db.add(customer)
    db.flush()
    uploaded = UploadedFile(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        original_name="notes.pdf",
        stored_name="notes.pdf",
        mime_type="application/pdf",
        file_kind="pdf",
        size_bytes=100,
        page_count=5,
    )
    db.add(uploaded)
    db.flush()
    return customer, uploaded


def test_assistant_extracts_print_options_and_transaction_id():
    extraction = PrintAssistant().extract("Print pages 1-3, 2 copies, color, double sided, glossy. TXN AB1234567")
    assert extraction.options["page_range"] == "1-3"
    assert extraction.options["copies"] == 2
    assert extraction.options["color_mode"] == "color"
    assert extraction.options["sides"] == "double"
    assert extraction.options["paper_finish"] == "glossy"
    assert extraction.transaction_id == "AB1234567"


def test_quote_requires_payment_before_approval(db):
    customer, uploaded = make_customer_and_file(db)
    job = create_print_job(
        db,
        customer,
        uploaded,
        {"paper_finish": "normal", "color_mode": "bw", "sides": "single", "copies": 2},
    )
    update_job_quote(db, job)
    assert job.status == "payment_pending"
    assert job.total_amount == 100.0

    with pytest.raises(DomainError):
        approve_payment(db, job, "admin")

    submit_payment_proof(db, job, "text", "student", transaction_id="1234567890")
    approve_payment(db, job, "admin")
    assert job.payment_status == "approved"
    assert job.status == "approved"


def test_file_validation_rejects_unsupported_types():
    with pytest.raises(DomainError):
        validate_media("application/x-msdownload")


def test_exact_printer_profile_is_required(db):
    customer, uploaded = make_customer_and_file(db)
    job = create_print_job(
        db,
        customer,
        uploaded,
        {"paper_finish": "glossy", "color_mode": "bw", "sides": "double"},
    )
    assert find_printer_profile(db, job) is None


def test_approved_job_options_are_locked(db):
    customer, uploaded = make_customer_and_file(db)
    job = create_print_job(
        db,
        customer,
        uploaded,
        {"paper_finish": "normal", "color_mode": "bw", "sides": "single"},
    )
    submit_payment_proof(db, job, "text", "student", transaction_id="1234567890")
    approve_payment(db, job, "admin")

    with pytest.raises(DomainError):
        apply_options(job, {"color_mode": "color"})

    assert job.status == "approved"
    assert job.payment_status == "approved"
