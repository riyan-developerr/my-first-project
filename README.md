# PrintPilot

PrintPilot is a WhatsApp-first printing shop automation product. Students send PDFs, Word documents, or images to a WhatsApp Business number, choose print options, submit payment proof, and only approved jobs reach the shop printer.

## What Is Included

- FastAPI backend with WhatsApp Cloud API webhooks.
- SQLite database models for customers, files, jobs, proofs, printer profiles, pricing rules, and audit logs.
- Admin dashboard for reviewing jobs, approving payment, changing prices, cancelling jobs, and managing printer/pricing profiles.
- Windows print agent that polls approved jobs and prints through SumatraPDF or the Windows default print action.
- Safeguards that prevent file downloads and printing unless payment is approved.
- Tests for pricing, option parsing, state transitions, and file validation.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`, especially `ADMIN_PASSWORD`, `AGENT_API_KEY`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, and `WHATSAPP_PHONE_NUMBER_ID`.

Start the backend:

```powershell
uvicorn main:app --reload
```

Or run the backend with Docker:

```powershell
docker build -t printpilot .
docker run --env-file .env -p 8000:8000 -v ${PWD}\data:/app/data printpilot
```

Open the admin dashboard:

```text
http://localhost:8000/admin
```

## WhatsApp Cloud API

Configure Meta's WhatsApp Cloud API webhook URL:

```text
https://your-public-domain.com/webhooks/whatsapp
```

Use the same value for `WHATSAPP_VERIFY_TOKEN` in Meta and `.env`.

The backend expects inbound `text`, `document`, and `image` messages. It downloads media with the Graph API when `WHATSAPP_ACCESS_TOKEN` is configured. Without WhatsApp credentials, it creates development placeholders so the flow can still be tested locally. If `WHATSAPP_APP_SECRET` is set, incoming webhook signatures are verified.

## Windows Print Agent

On the shop PC:

```powershell
$env:PRINT_AGENT_API_URL="https://your-public-domain.com"
$env:PRINT_AGENT_API_KEY="change-this-agent-key"
$env:PRINT_AGENT_ID="front-desk-pc"
$env:SUMATRA_PATH="C:\Program Files\SumatraPDF\SumatraPDF.exe"
python .\agent\print_agent.py
```

For Word document conversion, install LibreOffice and optionally set:

```powershell
$env:LIBREOFFICE_PATH="C:\Program Files\LibreOffice\program\soffice.exe"
```

## Tests

```powershell
pytest
```

## Production Notes

- Put the backend behind HTTPS before connecting WhatsApp webhooks.
- Change all default secrets before deployment.
- Give the print agent API key only to trusted shop PCs.
- Configure printer profiles in `/admin` so jobs route to the correct physical printer.
- Keep manual payment approval unless you later integrate a verified payment provider.
