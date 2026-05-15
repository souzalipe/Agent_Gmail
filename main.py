import imaplib
import threading
import smtplib
import email
import os
import json
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from email.mime.text import MIMEText
from email.header import decode_header

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.groq import Groq

load_dotenv()

app = FastAPI(title="Auto Reply Dashboard")

@app.on_event("startup")
async def startup_event():
    global is_running
    is_running = False
    _pause_event.clear()

# ─── ESTADO GLOBAL (em memória) ───────────────────────────
processed_emails: List[dict] = []
run_log: List[str] = []
is_running: bool = False
_pause_event = threading.Event()  # set() = pausado, clear() = rodando

# ─── AGENTE IA ────────────────────────────────────────────
def build_agent(signature: str = "Felipe Nascimento") -> Agent:
    return Agent(
        model=Groq(id="llama-3.3-70b-versatile"),
        markdown=False,
        instructions=f"""
Você é um assistente corporativo especializado em responder emails profissionais.

Suas respostas devem:
- ser naturais
- parecer escritas por um humano
- ser curtas e objetivas
- ser educadas
- nunca usar placeholders

É PROIBIDO escrever:
- [Nome]
- [Seu Nome]
- [Empresa]
- placeholders similares

Assine sempre:
{signature}
"""
    )


# ─── SCHEMAS ──────────────────────────────────────────────
class RunConfig(BaseModel):
    signature: str = "Felipe Nascimento"
    dry_run: bool = False   # True = gera resposta mas NÃO envia


class EmailRecord(BaseModel):
    id: str
    from_email: str
    subject: str
    body: str
    reply: Optional[str] = None
    sent: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    timestamp: str


# ─── HELPERS ──────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    run_log.append(entry)
    print(entry)


def should_skip(from_email: str, body: str) -> Optional[str]:
    checks = [
        ("no-reply" in from_email.lower(), "Remetente no-reply"),
        ("noreply" in from_email.lower(),  "Remetente noreply"),
        ("google" in from_email.lower(),   "Email do Google"),
        ("unsubscribe" in body.lower(),    "Conteúdo de unsubscribe"),
    ]
    for condition, reason in checks:
        if condition:
            return reason
    return None


# ─── TAREFA EM BACKGROUND ─────────────────────────────────
def process_emails_task(signature: str, dry_run: bool):
    global is_running, processed_emails, run_log

    global is_running, processed_emails, run_log
    is_running = True
    _pause_event.clear()
    processed_emails = []
    run_log = []

    EMAIL_USER = os.getenv("EMAIL_USER")
    EMAIL_PASS = os.getenv("EMAIL_PASS")

    if not EMAIL_USER or not EMAIL_PASS:
        log("❌ Credenciais não encontradas no .env")
        is_running = False
        return

    agent = build_agent(signature)

    try:
        log(f"🔌 Conectando ao Gmail como {EMAIL_USER}...")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        status, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()
        log(f"📬 {len(email_ids)} email(s) não lido(s) encontrado(s)")

        for idx, email_id in enumerate(email_ids):
            if _pause_event.is_set():
                log("⏸️  Agente pausado pelo usuário.")
                break

            eid = email_id.decode()
            status, msg_data = mail.fetch(email_id, "(RFC822)")

            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue

                msg = email.message_from_bytes(response_part[1])

                subject, enc = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(enc if enc else "utf-8")

                from_email = msg.get("From", "")
                log(f"📧 [{idx+1}/{len(email_ids)}] De: {from_email} | Assunto: {subject}")

                # corpo
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")

                record = EmailRecord(
                    id=eid,
                    from_email=from_email,
                    subject=subject,
                    body=body,
                    timestamp=datetime.now().isoformat(),
                )

                # filtros
                skip_reason = should_skip(from_email, body)
                if skip_reason:
                    record.skipped = True
                    record.skip_reason = skip_reason
                    log(f"⏭️  Ignorado: {skip_reason}")
                    processed_emails.append(record.model_dump())
                    continue

                # gerar resposta
                prompt = f"""
Leia o email abaixo e gere uma resposta.

REGRAS:
- Seja profissional
- Seja educado
- Seja objetivo
- NÃO use placeholders
- Assine como {signature}

EMAIL RECEBIDO:
{body}
"""
                log("🤖 Gerando resposta com IA...")
                resposta = agent.run(prompt)
                resposta_texto = resposta.content
                record.reply = resposta_texto

                if dry_run:
                    log("📝 [DRY RUN] Resposta gerada, envio pulado.")
                    record.sent = False
                else:
                    # enviar
                    try:
                        smtp = smtplib.SMTP("smtp.gmail.com", 587)
                        smtp.starttls()
                        smtp.login(EMAIL_USER, EMAIL_PASS)

                        reply_msg = MIMEText(resposta_texto)
                        reply_msg["Subject"] = f"Re: {subject}"
                        reply_msg["From"] = EMAIL_USER
                        reply_msg["To"] = from_email

                        smtp.sendmail(EMAIL_USER, from_email, reply_msg.as_string())
                        smtp.quit()

                        record.sent = True
                        log(f"✅ Resposta enviada para {from_email}")
                    except Exception as e:
                        log(f"❌ Erro ao enviar: {e}")

                processed_emails.append(record.model_dump())

        mail.logout()
        log("✅ Processamento concluído!")

    except Exception as e:
        log(f"❌ Erro geral: {e}")
    finally:
        is_running = False


# ─── ROTAS API ────────────────────────────────────────────
@app.post("/api/run")
async def run(config: RunConfig, bg: BackgroundTasks):
    global is_running
    if is_running:
        raise HTTPException(status_code=409, detail="Já está rodando.")
    bg.add_task(process_emails_task, config.signature, config.dry_run)
    return {"status": "started"}


@app.get("/api/status")
def status():
    return {
        "is_running": is_running,
        "total": len(processed_emails),
        "sent": sum(1 for e in processed_emails if e.get("sent")),
        "skipped": sum(1 for e in processed_emails if e.get("skipped")),
    }


@app.get("/api/emails")
def get_emails():
    return processed_emails


@app.get("/api/log")
def get_log():
    return run_log


@app.post("/api/pause")
def pause_agent():
    _pause_event.set()
    log("⏸️  Agente pausado pelo usuário.")
    return {"status": "paused"}


@app.post("/api/resume")
def resume_agent():
    _pause_event.clear()
    log("▶️  Agente retomado pelo usuário.")
    return {"status": "resumed"}


@app.get("/api/agent-state")
def agent_state():
    return {"is_running": is_running, "is_paused": _pause_event.is_set()}


# ─── DASHBOARD HTML ───────────────────────────────────────
BASE_DIR = Path(__file__).parent

@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_path = BASE_DIR / "templates" / "index.html"
    return html_path.read_text(encoding="utf-8")