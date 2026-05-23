from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import assistant
from app.config import settings
from app.models import Customer, PrintJob, ProcessedWebhook
from app.services import (
    DomainError,
    apply_options,
    audit,
    create_print_job,
    create_uploaded_file,
    detect_page_count,
    get_or_create_customer,
    latest_active_job,
    missing_options,
    safe_storage_path,
    submit_payment_proof,
    update_job_quote,
    validate_media,
)


class WhatsAppClient:
    def __init__(self) -> None:
        self.token = settings.whatsapp_access_token
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.api_version = settings.whatsapp_api_version
        self.base_url = f"https://graph.facebook.com/{self.api_version}"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_number_id)

    def send_text(self, to: str, body: str) -> None:
        if not self.configured:
            print(f"[dev whatsapp] to={to}: {body}")
            return
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body[:4096]},
        }
        with httpx.Client(timeout=20) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            response.raise_for_status()

    def download_media(self, media_id: str, destination: Path) -> tuple[int, str | None]:
        if not self.token:
            destination.write_text("Development placeholder: WhatsApp access token is not configured.\n", encoding="utf-8")
            return destination.stat().st_size, None

        with httpx.Client(timeout=60) as client:
            metadata = client.get(f"{self.base_url}/{media_id}", headers=self._headers())
            metadata.raise_for_status()
            media_url = metadata.json()["url"]
            media_response = client.get(media_url, headers=self._headers())
            media_response.raise_for_status()
            destination.write_bytes(media_response.content)
            return len(media_response.content), media_response.headers.get("content-type")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}


whatsapp_client = WhatsAppClient()


def process_whatsapp_payload(db: Session, payload: dict[str, Any]) -> dict[str, int]:
    processed = 0
    skipped = 0
    for message in iter_messages(payload):
        message_id = message.get("id")
        if not message_id:
            skipped += 1
            continue
        if db.get(ProcessedWebhook, message_id):
            skipped += 1
            continue
        db.add(ProcessedWebhook(event_id=message_id))
        from_wa = message.get("from")
        if not from_wa:
            skipped += 1
            continue
        customer = get_or_create_customer(db, from_wa, message.get("profile", {}).get("name"))
        try:
            handle_message(db, customer, message)
        except DomainError as exc:
            whatsapp_client.send_text(from_wa, str(exc))
            audit(db, "customer", str(customer.id), "message_rejected", "system", str(exc))
        except Exception as exc:
            whatsapp_client.send_text(from_wa, "Sorry, something went wrong while processing your request. Staff has been notified.")
            audit(db, "customer", str(customer.id), "message_error", "system", repr(exc))
        processed += 1
    db.commit()
    return {"processed": processed, "skipped": skipped}


def iter_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {contact.get("wa_id"): contact.get("profile", {}) for contact in value.get("contacts", [])}
            for message in value.get("messages", []):
                message["profile"] = contacts.get(message.get("from"), {})
                messages.append(message)
    return messages


def handle_message(db: Session, customer: Customer, message: dict[str, Any]) -> None:
    msg_type = message.get("type")
    if msg_type == "text":
        handle_text_message(db, customer, message["text"]["body"])
        return
    if msg_type in {"document", "image"}:
        handle_media_message(db, customer, message, msg_type)
        return
    whatsapp_client.send_text(customer.wa_id, "Please send a PDF, DOCX, image, or text instructions for your print job.")


def handle_text_message(db: Session, customer: Customer, text: str) -> None:
    extraction = assistant.extract(text)
    job = latest_active_job(db, customer)
    if not job:
        whatsapp_client.send_text(
            customer.wa_id,
            "Welcome to PrintPilot. Send your PDF, DOCX, or image here, then tell me print options like color/BW, single/double-sided, glossy/normal, copies, and pages.",
        )
        return

    if job.status in {"approved", "printing"}:
        whatsapp_client.send_text(
            customer.wa_id,
            f"Order {short_id(job.id)} is already {job.status}. Staff will update you when it is printed.",
        )
        return

    if extraction.options and job.status in {"collecting_options", "payment_pending"}:
        apply_options(job, extraction.options)
        if extraction.notes:
            job.ai_notes = append_note(job.ai_notes, "; ".join(extraction.notes))
        update_job_quote(db, job)
        audit(db, "job", job.id, "options_updated", "whatsapp", json.dumps(extraction.options))

    if (extraction.transaction_id or extraction.intent == "payment_proof") and job.status in {"payment_pending", "payment_submitted"}:
        submit_payment_proof(
            db,
            job,
            kind="text",
            actor="whatsapp",
            transaction_id=extraction.transaction_id,
            notes=text[:1000],
        )
        whatsapp_client.send_text(customer.wa_id, f"Payment proof received for order {short_id(job.id)}. Staff will verify it before printing.")
        return

    if extraction.transaction_id or extraction.intent == "payment_proof":
        whatsapp_client.send_text(customer.wa_id, f"Order {short_id(job.id)} is not ready for payment proof yet. Please complete the print options first.")
        return

    send_job_guidance(customer, job)


def handle_media_message(db: Session, customer: Customer, message: dict[str, Any], msg_type: str) -> None:
    media = message[msg_type]
    media_id = media.get("id")
    caption = media.get("caption", "")
    mime_type = media.get("mime_type") or ("image/jpeg" if msg_type == "image" else "application/octet-stream")
    original_name = media.get("filename") or f"whatsapp-{msg_type}-{media_id or uuid.uuid4()}"

    job = latest_active_job(db, customer)
    if job and job.status == "payment_pending" and msg_type == "image":
        file_id = store_media(db, customer, original_name, media_id, mime_type, payment_proof=True).id
        extraction = assistant.extract(caption)
        submit_payment_proof(
            db,
            job,
            kind="image",
            actor="whatsapp",
            transaction_id=extraction.transaction_id,
            media_id=media_id,
            file_id=file_id,
            notes=caption[:1000] if caption else None,
        )
        whatsapp_client.send_text(customer.wa_id, f"Payment screenshot received for order {short_id(job.id)}. Staff will verify it before printing.")
        return

    uploaded = store_media(db, customer, original_name, media_id, mime_type, payment_proof=False)
    extraction = assistant.extract(caption)
    new_job = create_print_job(db, customer, uploaded, extraction.options)
    if extraction.notes:
        new_job.ai_notes = "; ".join(extraction.notes)
    update_job_quote(db, new_job)
    send_job_guidance(customer, new_job)


def store_media(
    db: Session,
    customer: Customer,
    original_name: str,
    media_id: str | None,
    mime_type: str,
    payment_proof: bool,
):
    validate_media(mime_type)
    file_id = str(uuid.uuid4())
    path = safe_storage_path(file_id, mime_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    size_bytes, downloaded_content_type = whatsapp_client.download_media(media_id or file_id, path)
    if downloaded_content_type and downloaded_content_type.split(";")[0] in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        mime_type = downloaded_content_type.split(";")[0]
    validate_media(mime_type, size_bytes)
    page_count = None if payment_proof else detect_page_count(path, mime_type)
    return create_uploaded_file(db, customer, original_name, path, media_id, mime_type, size_bytes, page_count)


def send_job_guidance(customer: Customer, job: PrintJob) -> None:
    missing = missing_options(job)
    order = short_id(job.id)
    if missing:
        whatsapp_client.send_text(
            customer.wa_id,
            f"Order {order} received. Please reply with: {', '.join(missing)}. You can also mention copies and page range.",
        )
        return
    whatsapp_client.send_text(
        customer.wa_id,
        (
            f"Order {order} is ready for payment.\n"
            f"Options: {job.paper_size}, {job.paper_finish}, {job.color_mode}, {job.sides}, {job.copies} copies.\n"
            f"Estimated pages: {job.estimated_pages or 1}\n"
            f"Total: {job.currency} {job.total_amount:.2f}\n"
            f"{job.payment_instructions}\n"
            "Reply with payment screenshot or transaction ID. Printing starts only after staff approval."
        ),
    )


def append_note(existing: str | None, note: str) -> str:
    return f"{existing}\n{note}" if existing else note


def short_id(job_id: str) -> str:
    return job_id.split("-")[0].upper()
