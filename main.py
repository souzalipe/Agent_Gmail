import imaplib
import threading
import smtplib
import email
import os
import json
import re
import uuid
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from email.mime.text import MIMEText
from email.header import decode_header

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, String, Boolean, Text, Integer
from sqlalchemy.orm import declarative_base, Session as DBSession

from agno.agent import Agent
from agno.models.groq import Groq

# latin-1 aceita qualquer byte — evita crash em .env salvo como Windows-1252
load_dotenv(encoding='latin-1')

app = FastAPI(title="Auto Reply Dashboard")

# ─── BANCO DE DADOS ───────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///emails.db")
Base = declarative_base()
db_engine = None

class EmailModel(Base):
    __tablename__ = "emails"
    uid          = Column(String, primary_key=True)
    email_id     = Column(String)
    from_email   = Column(Text)
    subject      = Column(Text)
    body         = Column(Text)
    reply        = Column(Text, nullable=True)
    sent         = Column(Boolean, default=False)
    skipped      = Column(Boolean, default=False)
    skip_reason  = Column(Text, nullable=True)
    important    = Column(Boolean, default=False)
    importance_reason = Column(Text, nullable=True)
    timestamp    = Column(String)

def init_db():
    global db_engine
    try:
        db_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
        Base.metadata.create_all(db_engine)
        print("✅ Banco de dados conectado!")
    except Exception as e:
        print(f"⚠️ Banco de dados indisponível: {e}")

def save_to_db(record_dict: dict):
    if not db_engine:
        return
    try:
        with DBSession(db_engine) as session:
            obj = EmailModel(
                uid=str(uuid.uuid4()),
                email_id=record_dict["id"],
                from_email=record_dict["from_email"],
                subject=record_dict["subject"],
                body=record_dict["body"],
                reply=record_dict.get("reply"),
                sent=record_dict.get("sent", False),
                skipped=record_dict.get("skipped", False),
                skip_reason=record_dict.get("skip_reason"),
                important=record_dict.get("important", False),
                importance_reason=record_dict.get("importance_reason"),
                timestamp=record_dict["timestamp"],
            )
            session.add(obj)
            session.commit()
    except Exception as e:
        print(f"⚠️ Erro ao salvar no banco: {e}")

def db_total_count() -> int:
    if not db_engine:
        return 0
    try:
        with DBSession(db_engine) as session:
            return session.query(EmailModel).count()
    except Exception:
        return 0

def db_fetch_history(limit: int = 200, offset: int = 0) -> list:
    if not db_engine:
        return []
    try:
        with DBSession(db_engine) as session:
            rows = (
                session.query(EmailModel)
                .order_by(EmailModel.timestamp.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.email_id, "from_email": r.from_email,
                    "subject": r.subject, "body": r.body, "reply": r.reply,
                    "sent": r.sent, "skipped": r.skipped, "skip_reason": r.skip_reason,
                    "important": r.important, "importance_reason": r.importance_reason,
                    "timestamp": r.timestamp,
                }
                for r in rows
            ]
    except Exception:
        return []


@app.on_event("startup")
async def startup_event():
    global is_running
    is_running = False
    _pause_event.clear()
    init_db()


# ─── ESTADO GLOBAL (em memória) ───────────────────────────
processed_emails: List[dict] = []
important_emails: List[dict] = []
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
    dry_run: bool = False


class EmailRecord(BaseModel):
    id: str
    from_email: str
    subject: str
    body: str
    reply: Optional[str] = None
    sent: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    important: bool = False
    importance_reason: Optional[str] = None
    timestamp: str


# ─── HELPERS ──────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    run_log.append(entry)
    print(entry)


def should_skip(from_email: str, subject: str, body: str) -> Optional[str]:
    f = from_email.lower()
    s = subject.lower()
    b = body.lower()

    # ── No-reply / automatizados
    if any(x in f for x in ("no-reply", "noreply", "do-not-reply", "donotreply")):
        return "Remetente no-reply/automatizado"

    # ── Google
    if "google" in f:
        return "Email do Google"

    # ── Facebook
    if "facebookmail.com" in f or "facebook.com" in f:
        return "Notificação do Facebook"
    if any(x in s or x in b for x in (
        "pedido de amizade", "quer ser seu amigo", "adicionou você",
        "friend request", "wants to be your friend", "added you as a friend",
    )):
        return "Pedido de amizade (Facebook)"

    # ── Spam
    spam_kw = [
        "você ganhou", "parabéns, você", "você foi selecionado", "você foi escolhido",
        "resgate seu prêmio", "clique aqui para resgatar", "resgate agora",
        "ganhou um prêmio", "ganhou um iphone", "ganhou um brinde",
        "lottery winner", "you have won", "congratulations you", "claim your prize",
        "free gift", "free money", "make money fast", "work from home",
    ]
    if any(x in s or x in b for x in spam_kw):
        return "Spam detectado"

    # ── Marketing / Promoções
    promo_from = [
        "newsletter", "marketing@", "promo@", "offers@", "deals@",
        "promotions@", "campaign@", "news@", "mailchimp", "sendgrid",
        "klaviyo", "brevo", "constantcontact", "hubspot",
    ]
    if any(x in f for x in promo_from):
        return "Email de marketing/newsletter"

    promo_subject = [
        "% off", "% de desconto", "desconto exclusivo", "desconto especial",
        "oferta especial", "oferta imperdível", "oferta por tempo limitado",
        "black friday", "cyber monday", "liquidação", "frete grátis", "frete gratis",
        "compre agora", "últimas unidades", "últimas horas", "só hoje",
        "promoção", "promoção relâmpago", "super oferta", "super desconto",
        "sale", "big sale", "flash sale", "deal of the day", "limited offer",
        "buy now", "shop now", "order now", "act now",
        "ganhe", "aproveite", "não perca", "não perca essa oferta",
        "cupom", "coupon", "voucher", "cashback",
    ]
    if any(x in s for x in promo_subject):
        return "Promoção/Anúncio"

    # ── Unsubscribe no corpo (indicador de marketing)
    if any(x in b for x in ("unsubscribe", "cancelar inscrição", "opt-out", "opt out", "descadastrar")):
        return "Email de marketing (opt-out)"

    return None


# ─── IA: CLASSIFICAÇÃO DE IMPORTÂNCIA ─────────────────────
def classify_importance(agent: Agent, body: str, subject: str) -> tuple[bool, Optional[str]]:
    prompt = f"""Analise o email abaixo e responda APENAS em JSON válido, sem texto extra.

Formato exato: {{"importante": true, "motivo": "razão em português"}} ou {{"importante": false, "motivo": null}}

Critérios para importante=true (basta um):
- Reunião, videoconferência ou encontro agendado
- Evento com data e hora
- Prazo ou deadline
- Solicitação urgente
- Tarefa atribuída à pessoa
- Contrato, proposta ou acordo
- Entrevista ou processo seletivo
- Pagamento ou cobrança com vencimento

ASSUNTO: {subject}
EMAIL:
{body[:1500]}
"""
    try:
        result = agent.run(prompt)
        text = result.content.strip()
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            if data.get("importante"):
                return True, data.get("motivo") or "Email importante"
    except Exception:
        pass
    return False, None


# ─── TAREFA EM BACKGROUND ─────────────────────────────────
def process_emails_task(signature: str, dry_run: bool):
    global is_running, processed_emails, important_emails, run_log

    is_running = True
    _pause_event.clear()
    processed_emails = []
    important_emails = []
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

                # filtros expandidos
                skip_reason = should_skip(from_email, subject, body)
                if skip_reason:
                    record.skipped = True
                    record.skip_reason = skip_reason
                    log(f"⏭️  Ignorado: {skip_reason}")
                    record_dict = record.model_dump()
                    processed_emails.append(record_dict)
                    save_to_db(record_dict)
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
                record.reply = resposta.content

                log("🔍 Classificando importância...")
                is_important, importance_reason = classify_importance(agent, body, subject)
                if is_important:
                    record.important = True
                    record.importance_reason = importance_reason
                    log(f"⭐ Email importante: {importance_reason}")

                if dry_run:
                    log("📝 [DRY RUN] Resposta gerada, envio pulado.")
                else:
                    try:
                        smtp = smtplib.SMTP("smtp.gmail.com", 587)
                        smtp.starttls()
                        smtp.login(EMAIL_USER, EMAIL_PASS)

                        reply_msg = MIMEText(record.reply)
                        reply_msg["Subject"] = f"Re: {subject}"
                        reply_msg["From"] = EMAIL_USER
                        reply_msg["To"] = from_email

                        smtp.sendmail(EMAIL_USER, from_email, reply_msg.as_string())
                        smtp.quit()

                        record.sent = True
                        log(f"✅ Resposta enviada para {from_email}")
                    except Exception as e:
                        log(f"❌ Erro ao enviar: {e}")

                record_dict = record.model_dump()
                processed_emails.append(record_dict)
                save_to_db(record_dict)
                if record.important:
                    important_emails.append(record_dict)

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
        "important": len(important_emails),
        "db_total": db_total_count(),
        "db_connected": db_engine is not None,
    }


@app.get("/api/emails")
def get_emails():
    return processed_emails


@app.get("/api/important-emails")
def get_important_emails():
    return important_emails


@app.get("/api/history")
def get_history(limit: int = Query(default=200, le=500), offset: int = Query(default=0)):
    return db_fetch_history(limit=limit, offset=offset)


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
