from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests


API_URL = os.getenv("PRINT_AGENT_API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("PRINT_AGENT_API_KEY", "change-this-agent-key")
AGENT_ID = os.getenv("PRINT_AGENT_ID", "shop-pc")
POLL_SECONDS = int(os.getenv("AGENT_POLL_SECONDS", "5"))
SPOOL_DIR = Path(os.getenv("AGENT_SPOOL_DIR", "./agent_spool"))
SUMATRA_PATH = os.getenv("SUMATRA_PATH", "SumatraPDF.exe")
LIBREOFFICE_PATH = os.getenv("LIBREOFFICE_PATH", "soffice")


def main() -> int:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Print agent {AGENT_ID} polling {API_URL}")
    while True:
        try:
            job = fetch_next_job()
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            process_job(job)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"Agent loop error: {exc}", file=sys.stderr)
            time.sleep(POLL_SECONDS)


def headers() -> dict[str, str]:
    return {"X-Agent-Key": API_KEY}


def fetch_next_job() -> dict | None:
    response = requests.get(f"{API_URL}/agent/jobs/next", params={"agent_id": AGENT_ID}, headers=headers(), timeout=30)
    response.raise_for_status()
    return response.json().get("job")


def process_job(job: dict) -> None:
    job_id = job["id"]
    try:
        local_file = download_file(job)
        printable = normalize_file(local_file, job)
        print_file(printable, job)
        report_status(job_id, "printed", "Printed successfully")
        cleanup_job_files(local_file, printable)
        print(f"Printed {job_id[:8].upper()}")
    except Exception as exc:
        report_status(job_id, "failed", str(exc))
        print(f"Failed {job_id[:8].upper()}: {exc}", file=sys.stderr)


def download_file(job: dict) -> Path:
    filename = safe_filename(job.get("filename") or f"{job['id']}.pdf")
    destination = SPOOL_DIR / f"{job['id']}-{filename}"
    response = requests.get(job["file_url"], headers=headers(), timeout=120)
    response.raise_for_status()
    destination.write_bytes(response.content)
    return destination


def normalize_file(path: Path, job: dict) -> Path:
    mime_type = job.get("mime_type", "")
    if mime_type in {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"}:
        return convert_docx_to_pdf(path)
    return path


def convert_docx_to_pdf(path: Path) -> Path:
    output_dir = path.parent / "converted"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        LIBREOFFICE_PATH,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    converted = output_dir / f"{path.stem}.pdf"
    if not converted.exists():
        raise RuntimeError("LibreOffice did not produce a PDF.")
    return converted


def print_file(path: Path, job: dict) -> None:
    printer_name = job["printer"]["printer_name"]
    settings = build_sumatra_settings(job["options"])
    if shutil.which(SUMATRA_PATH) or Path(SUMATRA_PATH).exists():
        command = [
            SUMATRA_PATH,
            "-silent",
            "-print-to",
            printer_name,
            "-print-settings",
            settings,
            str(path),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
        return

    if os.name == "nt":
        os.startfile(str(path), "print")  # type: ignore[attr-defined]
        return
    raise RuntimeError("No print method available. Install SumatraPDF or run on Windows.")


def build_sumatra_settings(options: dict) -> str:
    parts = [str(max(1, int(options.get("copies") or 1))) + "x"]
    if options.get("sides") == "double":
        parts.append("duplex")
    else:
        parts.append("simplex")
    page_range = options.get("page_range")
    if page_range:
        parts.append(str(page_range))
    return ",".join(parts)


def report_status(job_id: str, status: str, message: str) -> None:
    response = requests.post(
        f"{API_URL}/agent/jobs/{job_id}/status",
        headers={**headers(), "Content-Type": "application/json"},
        json={"status": status, "message": message},
        timeout=30,
    )
    response.raise_for_status()


def cleanup_job_files(*paths: Path) -> None:
    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except OSError:
            pass


def safe_filename(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    return safe[:120] or "print-file"


if __name__ == "__main__":
    raise SystemExit(main())
