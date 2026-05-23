from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal, get_db, init_db
from app.models import PaymentProof, PricingRule, PrintJob, PrinterProfile, UploadedFile
from app.security import require_admin, require_agent
from app.services import (
    DomainError,
    approve_payment,
    audit,
    cancel_job,
    claim_next_job,
    cleanup_expired_files,
    complete_print_job,
    fail_print_job,
    file_url,
    find_printer_profile,
    mark_refunded,
    reject_payment,
    reprice_job,
    seed_defaults,
)
from app.whatsapp import process_whatsapp_payload, whatsapp_client


templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed_defaults(db)
        cleanup_expired_files(db)
        db.commit()
    retention_task = asyncio.create_task(retention_loop())
    try:
        yield
    finally:
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    def verify_whatsapp(
        hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
        hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
        hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    ) -> str:
        if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token and hub_challenge:
            return hub_challenge
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @app.post("/webhooks/whatsapp")
    async def receive_whatsapp(
        request: Request,
        x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
        db: Session = Depends(get_db),
    ) -> dict[str, int]:
        body = await request.body()
        verify_meta_signature(body, x_hub_signature_256)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        return process_whatsapp_payload(db, payload)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(
        request: Request,
        _: str = Depends(require_admin),
        db: Session = Depends(get_db),
        status: str | None = None,
    ) -> HTMLResponse:
        stmt = (
            select(PrintJob)
            .options(selectinload(PrintJob.customer), selectinload(PrintJob.uploaded_file), selectinload(PrintJob.payment_proofs))
            .order_by(PrintJob.created_at.desc())
            .limit(100)
        )
        if status:
            stmt = stmt.where(PrintJob.status == status)
        jobs = db.scalars(stmt).all()
        pricing_rules = db.scalars(select(PricingRule).order_by(PricingRule.id)).all()
        printers = db.scalars(select(PrinterProfile).order_by(PrinterProfile.id)).all()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "settings": settings,
                "jobs": jobs,
                "pricing_rules": pricing_rules,
                "printers": printers,
                "status": status,
            },
        )

    @app.get("/admin/jobs/{job_id}", response_class=HTMLResponse)
    def admin_job_detail(
        request: Request,
        job_id: str,
        _: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ) -> HTMLResponse:
        job = get_job_or_404(db, job_id)
        proofs = db.scalars(select(PaymentProof).where(PaymentProof.job_id == job_id).order_by(PaymentProof.submitted_at.desc())).all()
        return templates.TemplateResponse(request, "job.html", {"job": job, "proofs": proofs})

    @app.get("/admin/files/{file_id}")
    def admin_download_file(file_id: str, _: str = Depends(require_admin), db: Session = Depends(get_db)):
        uploaded = db.get(UploadedFile, file_id)
        if not uploaded:
            raise HTTPException(status_code=404, detail="File not found")
        stored = Path(settings.storage_dir) / uploaded.stored_name
        if not stored.exists():
            raise HTTPException(status_code=404, detail="Stored file not found")
        return FileResponse(path=stored, filename=uploaded.original_name, media_type=uploaded.mime_type)

    @app.post("/admin/jobs/{job_id}/approve-payment")
    def admin_approve_payment(job_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
        job = get_job_or_404(db, job_id)
        try:
            approve_payment(db, job, admin)
            db.commit()
            whatsapp_client.send_text(job.customer.wa_id, f"Payment approved for order {job.id[:8].upper()}. Your job is now in the print queue.")
        except DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return back_to_job(job_id)

    @app.post("/admin/jobs/{job_id}/reject-payment")
    def admin_reject_payment(
        job_id: str,
        notes: str = Form(default=""),
        admin: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        job = get_job_or_404(db, job_id)
        try:
            reject_payment(db, job, admin, notes)
            db.commit()
            whatsapp_client.send_text(job.customer.wa_id, f"Payment proof for order {job.id[:8].upper()} was not accepted. Please send a valid proof or contact the counter.")
        except DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return back_to_job(job_id)

    @app.post("/admin/jobs/{job_id}/cancel")
    def admin_cancel_job(
        job_id: str,
        reason: str = Form(default=""),
        admin: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        job = get_job_or_404(db, job_id)
        cancel_job(db, job, admin, reason)
        db.commit()
        whatsapp_client.send_text(job.customer.wa_id, f"Order {job.id[:8].upper()} has been cancelled. Please contact the counter if this is unexpected.")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/jobs/{job_id}/refund")
    def admin_refund_job(
        job_id: str,
        reason: str = Form(default=""),
        admin: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        job = get_job_or_404(db, job_id)
        mark_refunded(db, job, admin, reason)
        db.commit()
        return back_to_job(job_id)

    @app.post("/admin/jobs/{job_id}/reprice")
    def admin_reprice_job(
        job_id: str,
        amount: float = Form(...),
        admin: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        job = get_job_or_404(db, job_id)
        try:
            reprice_job(db, job, amount, admin)
            db.commit()
            whatsapp_client.send_text(job.customer.wa_id, f"Order {job.id[:8].upper()} total updated to {job.currency} {job.total_amount:.2f}. Please send payment proof after paying.")
        except DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return back_to_job(job_id)

    @app.post("/admin/jobs/{job_id}/requeue")
    def admin_requeue_job(job_id: str, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
        job = get_job_or_404(db, job_id)
        if job.payment_status != "approved":
            raise HTTPException(status_code=400, detail="Only paid jobs can be requeued.")
        job.status = "approved"
        job.failure_reason = None
        job.claimed_by = None
        job.claim_expires_at = None
        audit(db, "job", job.id, "requeued", admin)
        db.commit()
        return back_to_job(job_id)

    @app.post("/admin/pricing/{rule_id}")
    def admin_update_pricing(
        rule_id: int,
        price_per_page: float = Form(...),
        active: str | None = Form(default=None),
        _: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        rule = db.get(PricingRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Pricing rule not found")
        rule.price_per_page = round(max(0, price_per_page), 2)
        rule.active = active == "on"
        db.commit()
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/pricing")
    def admin_create_pricing(
        paper_size: str = Form(...),
        paper_finish: str = Form(...),
        color_mode: str = Form(...),
        sides: str = Form(...),
        price_per_page: float = Form(...),
        _: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        paper_size = paper_size.upper()[:20]
        paper_finish = paper_finish.lower()[:30]
        color_mode = color_mode.lower()[:30]
        sides = sides.lower()[:30]
        rule = db.scalar(
            select(PricingRule).where(
                PricingRule.paper_size == paper_size,
                PricingRule.paper_finish == paper_finish,
                PricingRule.color_mode == color_mode,
                PricingRule.sides == sides,
            )
        )
        if not rule:
            rule = PricingRule(paper_size=paper_size, paper_finish=paper_finish, color_mode=color_mode, sides=sides)
            db.add(rule)
        rule.price_per_page = round(max(0, price_per_page), 2)
        rule.active = True
        db.commit()
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/printers/{profile_id}")
    def admin_update_printer(
        profile_id: int,
        printer_name: str = Form(...),
        active: str | None = Form(default=None),
        _: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        profile = db.get(PrinterProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Printer profile not found")
        profile.printer_name = printer_name[:255]
        profile.active = active == "on"
        db.commit()
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/printers")
    def admin_create_printer(
        name: str = Form(...),
        printer_name: str = Form(...),
        paper_size: str = Form(...),
        paper_finish: str = Form(...),
        color_mode: str = Form(...),
        sides: str = Form(...),
        _: str = Depends(require_admin),
        db: Session = Depends(get_db),
    ):
        db.add(
            PrinterProfile(
                name=name[:120],
                printer_name=printer_name[:255],
                paper_size=paper_size.upper()[:20],
                paper_finish=paper_finish.lower()[:30],
                color_mode=color_mode.lower()[:30],
                sides=sides.lower()[:30],
                active=True,
            )
        )
        db.commit()
        return RedirectResponse("/admin", status_code=303)

    @app.get("/agent/jobs/next")
    def agent_next_job(
        agent_id: str = Query(default="shop-pc"),
        _: str = Depends(require_agent),
        db: Session = Depends(get_db),
    ):
        job = claim_next_job(db, agent_id)
        if not job:
            db.commit()
            return {"job": None}
        profile = find_printer_profile(db, job)
        if not profile:
            fail_print_job(db, job, agent_id, "No active printer profile is configured.")
            db.commit()
            return {"job": None, "error": "No active printer profile is configured."}
        db.commit()
        return {"job": agent_job_payload(job, profile)}

    @app.get("/agent/jobs/{job_id}/file")
    def agent_download_file(job_id: str, _: str = Depends(require_agent), db: Session = Depends(get_db)):
        job = get_job_or_404(db, job_id)
        if job.payment_status != "approved":
            raise HTTPException(status_code=403, detail="Payment must be approved before downloading a printable file.")
        stored = Path(settings.storage_dir) / job.uploaded_file.stored_name
        if not stored.exists():
            raise HTTPException(status_code=404, detail="Stored file not found")
        return FileResponse(path=stored, filename=job.uploaded_file.original_name, media_type=job.uploaded_file.mime_type)

    @app.post("/agent/jobs/{job_id}/status")
    async def agent_report_status(
        job_id: str,
        request: Request,
        actor: str = Depends(require_agent),
        db: Session = Depends(get_db),
    ):
        body = await request.json()
        job = get_job_or_404(db, job_id)
        status = body.get("status")
        message = body.get("message", "")
        try:
            if status == "printed":
                complete_print_job(db, job, actor)
                db.commit()
                whatsapp_client.send_text(
                    job.customer.wa_id,
                    f"Order {job.id[:8].upper()} has been printed. Pickup code: {job.pickup_code}. Please show this at the counter.",
                )
            elif status == "failed":
                fail_print_job(db, job, actor, message or "Print agent reported failure.")
                db.commit()
            else:
                raise HTTPException(status_code=400, detail="Status must be printed or failed.")
        except DomainError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return app


async def retention_loop() -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        with SessionLocal() as db:
            cleanup_expired_files(db)
            db.commit()


def verify_meta_signature(body: bytes, signature: str | None) -> None:
    if not settings.whatsapp_app_secret:
        return
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing Meta webhook signature")
    expected = "sha256=" + hmac.new(settings.whatsapp_app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not secrets.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid Meta webhook signature")


def get_job_or_404(db: Session, job_id: str) -> PrintJob:
    job = db.scalar(
        select(PrintJob)
        .where(PrintJob.id == job_id)
        .options(selectinload(PrintJob.customer), selectinload(PrintJob.uploaded_file), selectinload(PrintJob.payment_proofs))
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def agent_job_payload(job: PrintJob, profile: PrinterProfile) -> dict:
    return {
        "id": job.id,
        "file_url": file_url(job),
        "filename": job.uploaded_file.original_name,
        "mime_type": job.uploaded_file.mime_type,
        "printer": {
            "profile": profile.name,
            "printer_name": profile.printer_name,
        },
        "options": {
            "paper_size": job.paper_size,
            "paper_finish": job.paper_finish,
            "color_mode": job.color_mode,
            "sides": job.sides,
            "copies": job.copies,
            "page_range": job.page_range,
        },
    }


def back_to_job(job_id: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=303)


app = create_app()
