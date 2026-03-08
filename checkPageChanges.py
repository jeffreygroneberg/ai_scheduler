"""
Page Change Checker

AI agent that monitors a configured URL for cancellation-related
changes and sends email notifications when differences are detected.

Uses the GitHub Copilot SDK with custom tools:
  - CompareContentOfPage: compares current page content against a stored snapshot
  - SendMailTo: sends email notifications via SMTP (with optional screenshot attachment)
  - ReportResult: structured result reporting

Uses the Playwright MCP server AUSSCHLIESSLICH for screenshots (NOT for content fetching):
  - browser_navigate: opens a URL in a headless browser
  - browser_take_screenshot: captures a full-page screenshot for email attachment
  Content fetching uses web_fetch (built-in Copilot tool).
"""

import asyncio
import difflib
import hashlib
import logging
import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Optional

from copilot import CopilotClient, PermissionHandler
from copilot.tools import define_tool
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

URL = os.getenv("PAGE_URL", "https://www.schauinsland-reisen.de/service/wichtige-informationen?lang=de-de")
SNAPSHOT_FILE = Path("previous_snapshot.txt")
SCREENSHOT_DIR = Path("screenshots")
MODEL = "gpt-5.2"
REASONING_EFFORT = "high"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    """Structured result returned by the monitoring agent."""
    changes_detected: bool = Field(description="Ob inhaltliche Änderungen erkannt wurden")
    removed: list[str] = Field(default_factory=list, description="Entfernte Inhalte")
    added: list[str] = Field(default_factory=list, description="Neue oder geänderte Inhalte")
    summary: str = Field(description="Kurze Zusammenfassung auf Deutsch")
    email_sent: bool = Field(default=False, description="Ob eine E-Mail versendet wurde")
    email_recipients: Optional[list[str]] = Field(default=None, description="Empfänger, falls Mail gesendet")


@define_tool(description="Meldet das strukturierte Endergebnis des Monitoring-Durchlaufs. MUSS als letzter Schritt aufgerufen werden.")
async def ReportResult(params: AgentResult) -> str:  # noqa: N802
    """Receive and log the structured agent result."""
    log.info("=== Agent-Ergebnis ===")
    log.info("Änderungen: %s", params.changes_detected)
    if params.removed:
        log.info("Entfernt:   %s", params.removed)
    if params.added:
        log.info("Neu:        %s", params.added)
    log.info("Zusammenfassung: %s", params.summary)
    log.info("Mail gesendet: %s", params.email_sent)
    if params.email_recipients:
        log.info("Empfänger: %d", len(params.email_recipients))
    return "Ergebnis erfolgreich gemeldet."


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class CompareParams(BaseModel):
    current_content: str = Field(description="Der aktuelle Textinhalt der Seite")


@define_tool(
    description=(
        f"Vergleicht den Seiteninhalt mit dem gespeicherten Snapshot "
        f"({SNAPSHOT_FILE}) und zeigt Unterschiede."
    )
)
async def CompareContentOfPage(params: CompareParams) -> str:  # noqa: N802
    """Compare the current page content against the stored snapshot."""
    log.info("Empfangener Content: %d Zeichen", len(params.current_content))

    def _normalize(text: str) -> str:
        """Strip whitespace per line and collapse blank lines for stable hashing."""
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for i, line in enumerate(lines)
                         if line or (i > 0 and lines[i - 1]))

    if not SNAPSHOT_FILE.exists():
        log.warning("Kein Snapshot gefunden (%s)", SNAPSHOT_FILE)
        SNAPSHOT_FILE.write_text(params.current_content, encoding="utf-8")
        log.info("Initialen Snapshot gespeichert (%d Zeichen)", len(params.current_content))
        return (
            "Kein vorheriger Snapshot vorhanden. Dies ist der erste Lauf. "
            "Snapshot wurde gespeichert."
        )

    previous_text = SNAPSHOT_FILE.read_text(encoding="utf-8")
    log.info("Snapshot geladen: %d Zeichen", len(previous_text))

    current_hash = hashlib.sha256(_normalize(params.current_content).encode()).hexdigest()
    previous_hash = hashlib.sha256(_normalize(previous_text).encode()).hexdigest()
    log.info("Hash aktuell:  %s…", current_hash[:16])
    log.info("Hash snapshot: %s…", previous_hash[:16])

    if current_hash == previous_hash:
        log.info("Keine Änderungen.")
        return "Keine Änderungen festgestellt. Der Inhalt ist identisch zum letzten Snapshot."

    # Snapshot sofort aktualisieren
    SNAPSHOT_FILE.write_text(params.current_content, encoding="utf-8")
    log.info("Änderungen erkannt! Snapshot aktualisiert.")

    # Menschenlesbaren Diff erzeugen
    diff_lines = list(difflib.unified_diff(
        _normalize(previous_text).splitlines(),
        _normalize(params.current_content).splitlines(),
        fromfile="Vorher",
        tofile="Aktuell",
        lineterm="",
    ))
    removed = [l[1:].strip() for l in diff_lines if l.startswith("-") and not l.startswith("---") and l[1:].strip()]
    added = [l[1:].strip() for l in diff_lines if l.startswith("+") and not l.startswith("+++") and l[1:].strip()]

    diff_summary = "Änderungen erkannt!\n\n"
    if removed:
        diff_summary += "ENTFERNT:\n" + "\n".join(f"  - {l}" for l in removed) + "\n\n"
    if added:
        diff_summary += "NEU/GEÄNDERT:\n" + "\n".join(f"  + {l}" for l in added) + "\n"

    return diff_summary


class SendMailParams(BaseModel):
    to: str = Field(description="Empfänger-E-Mail-Adresse(n), kommagetrennt bei mehreren")
    subject: str = Field(description="Betreff der E-Mail")
    body: str = Field(description="Inhalt der E-Mail (Klartext)")
    screenshot_filename: Optional[str] = Field(
        default=None,
        description="Dateiname des Screenshots (im screenshots/ Ordner), der als Anhang mitgesendet wird",
    )


@define_tool(description="Sendet eine E-Mail-Benachrichtigung über SMTP. Optional mit Screenshot als Anhang.")
async def SendMailTo(params: SendMailParams) -> str:  # noqa: N802
    """Send an email notification via SMTP, optionally with a screenshot attachment."""
    server_addr = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    port = int(os.environ.get("MAIL_PORT", "587"))
    username = os.environ.get("MAIL_USERNAME", "")
    password = os.environ.get("MAIL_PASSWORD", "")
    mail_from = os.environ.get("MAIL_FROM", username)

    # Always use NOTIFY_EMAIL env var for recipients (security: avoid leaking email via prompt/logs)
    notify_email = os.environ.get("NOTIFY_EMAIL", "")
    raw = notify_email if notify_email else params.to
    recipients = [addr.strip() for addr in raw.split(",") if addr.strip()]
    log.info("Sende Mail an %d Empfänger", len(recipients))
    log.info("Betreff: %s", params.subject)

    # Check for screenshot attachment (Playwright saves to CWD or output-dir)
    screenshot_path = None
    if params.screenshot_filename:
        candidates = [
            SCREENSHOT_DIR / params.screenshot_filename,
            Path(params.screenshot_filename),
        ]
        for candidate in candidates:
            if candidate.exists():
                screenshot_path = candidate
                break
        if screenshot_path is None:
            log.warning("Screenshot nicht gefunden in: %s", [str(c) for c in candidates])

    if screenshot_path:
        # Multipart email with text + image attachment
        msg = MIMEMultipart()
        msg.attach(MIMEText(params.body, "plain", "utf-8"))
        with open(screenshot_path, "rb") as f:
            img_part = MIMEBase("image", "png")
            img_part.set_payload(f.read())
            encoders.encode_base64(img_part)
            img_part.add_header(
                "Content-Disposition",
                f"attachment; filename={screenshot_path.name}",
            )
            msg.attach(img_part)
        log.info("Screenshot angehängt: %s", screenshot_path.name)
    else:
        msg = MIMEText(params.body, "plain", "utf-8")

    msg["Subject"] = params.subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(server_addr, port) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)

    log.info("Mail erfolgreich an %d Empfänger versendet!", len(recipients))
    return f"E-Mail erfolgreich an {len(recipients)} Empfänger gesendet."


# ---------------------------------------------------------------------------
# Agent prompt
# ---------------------------------------------------------------------------

def _build_prompt() -> str:
    """Build the agent instruction prompt, injecting runtime config."""
    notify_email = os.environ.get("NOTIFY_EMAIL", "")
    if not notify_email:
        log.warning("NOTIFY_EMAIL ist nicht gesetzt – der Agent kann keine Mails versenden.")

    return (
        # --- ROLLE ---
        "<role>\n"
        "Du bist ein deterministischer Monitoring-Agent. Du führst exakt die unten beschriebenen "
        "Schritte aus, ohne Abweichung, ohne Improvisation.\n"
        "</role>\n\n"

        # --- ERLAUBTE TOOLS ---
        "<allowed_tools>\n"
        "Du darfst AUSSCHLIESSLICH folgende Tools verwenden:\n"
        "  1. web_fetch          → Seiteninhalt als Text abrufen\n"
        "  2. CompareContentOfPage → Textvergleich mit Snapshot\n"
        "  3. browser_navigate    → URL im Browser öffnen (nur für Screenshots)\n"
        "  4. browser_take_screenshot → Screenshot erstellen (nur nach browser_navigate)\n"
        "  5. SendMailTo          → E-Mail versenden\n"
        "  6. ReportResult        → Strukturiertes Ergebnis melden\n\n"
        "VERBOTEN: bash, view, create, browser_snapshot und alle anderen Tools.\n"
        "</allowed_tools>\n\n"

        # --- TOOL-ZUORDNUNG ---
        "<tool_rules>\n"
        "- Seiteninhalt abrufen    → IMMER web_fetch\n"
        "- Screenshot erstellen    → IMMER browser_navigate + browser_take_screenshot\n"
        "- Snapshot lesen/schreiben → wird automatisch von CompareContentOfPage erledigt\n"
        "</tool_rules>\n\n"

        # --- WORKFLOW ---
        "<workflow>\n"
        "Führe diese Schritte der Reihe nach aus:\n\n"

        f"SCHRITT 1: Seiteninhalt abrufen\n"
        f"  Rufe web_fetch auf mit der URL: {URL}\n"
        "  Speichere den zurückgegebenen Text für Schritt 2.\n\n"

        "SCHRITT 2: Inhalt vergleichen\n"
        "  Rufe CompareContentOfPage auf mit dem Text aus Schritt 1 als current_content.\n"
        "  Merke dir das Ergebnis.\n\n"

        "SCHRITT 3: Entscheidung\n"
        "  Prüfe das Ergebnis von CompareContentOfPage:\n\n"

        '  WENN das Ergebnis "Keine Änderungen" enthält ODER es zwar Änderungen gibt,\n'
        "  aber KEINE Zeilen mit ENTFERNT/NEU/GEÄNDERT vorhanden sind:\n"
        "    → Gehe direkt zu SCHRITT 6 (keine Mail, kein Screenshot).\n\n"

        "  WENN das Ergebnis ENTFERNT- oder NEU/GEÄNDERT-Einträge enthält:\n"
        "    → Fahre mit SCHRITT 4 fort.\n\n"

        f"SCHRITT 4: Screenshot erstellen\n"
        f"  4a) Rufe browser_navigate auf mit url: \"{URL}\"\n"
        "  4b) Warte bis die Navigation abgeschlossen ist.\n"
        "  4c) Rufe browser_take_screenshot auf mit genau diesen Parametern:\n"
        '       {{ "fullPage": true, "filename": "pageToCheck.png" }}\n'
        "  Erst nach erfolgreichem Screenshot weiter zu Schritt 5.\n\n"

        "SCHRITT 5: E-Mail senden\n"
        "  Fasse die Änderungen kurz auf Deutsch zusammen.\n"
        "  Fokus: Stornierungen, Reiseabsagen, Umbuchungsregelungen.\n"
        "  Rufe SendMailTo auf mit genau diesen Parametern:\n"
        '    to: "<den konfigurierten Empfänger>"\n'
        '    subject: "🔔 Änderungen bei Stornierungen erkannt"\n'
        f"    body: Zusammenfassung + konkrete Änderungen (ENTFERNT/NEU) + Link {URL}\n"
        '    screenshot_filename: "pageToCheck.png"\n\n'

        "SCHRITT 6: Ergebnis melden (IMMER ausführen)\n"
        "  Rufe ReportResult auf mit:\n"
        "    changes_detected: true/false\n"
        "    removed: Liste entfernter Inhalte (oder leere Liste)\n"
        "    added: Liste neuer Inhalte (oder leere Liste)\n"
        "    summary: Kurze Zusammenfassung auf Deutsch\n"
        "    email_sent: true/false\n"

        "</workflow>\n\n"

        # --- WICHTIGE REGELN ---
        "<rules>\n"
        "- Reine Leerzeichen- oder Formatierungsänderungen sind KEINE inhaltlichen Änderungen.\n"
        "- Überspringe KEINEN Schritt. Führe jeden Schritt einzeln und sequentiell aus.\n"
        "- ReportResult wird IMMER aufgerufen, auch wenn keine Änderungen vorliegen.\n"
        "</rules>"
    )


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

def _make_event_handler(done: asyncio.Event):
    """Return a callback that logs streaming events and signals completion."""

    def _handler(event):
        etype = event.type.value if hasattr(event.type, "value") else str(event.type)

        if etype == "tool.execution_start":
            log.info("Tool gestartet: %s", event.data.tool_name)
        elif etype == "tool.execution_complete":
            log.info("Tool fertig:    %s", event.data.tool_name)
        elif etype == "assistant.reasoning_delta":
            log.debug("Reasoning: %s", event.data.delta_content or "")
        elif etype == "session.idle":
            done.set()

    return _handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    """Start the Copilot agent, send the monitoring prompt, and wait."""
    client_opts = {}
    cli_path = os.environ.get("COPILOT_CLI_PATH")
    if cli_path:
        client_opts["cli_path"] = cli_path
        log.info("Using external CLI: %s", cli_path)

    # Use COPILOT_TOKEN (local .env) or GITHUB_TOKEN (Actions) for auth
    token = os.environ.get("COPILOT_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        client_opts["github_token"] = token

    SCREENSHOT_DIR.mkdir(exist_ok=True)

    client = CopilotClient(client_opts)
    await client.start()

    try:
        session = await client.create_session({
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "streaming": True,
            "on_permission_request": PermissionHandler.approve_all,
            "tools": [CompareContentOfPage, SendMailTo, ReportResult],
            "mcp_servers": {
                "playwright": {
                    "type": "local",
                    "command": "npx",
                    "args": [
                        "-y", "@playwright/mcp@latest",
                        "--headless",
                        "--output-dir", str(SCREENSHOT_DIR.resolve()),
                    ],
                    "tools": ["browser_navigate", "browser_take_screenshot"],
                },
            },
        })

        done = asyncio.Event()
        session.on(_make_event_handler(done))
        await session.send({"prompt": _build_prompt()})
        await done.wait()
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
