import imaplib
import smtplib
import email
import os

from email.mime.text import MIMEText
from email.header import decode_header

from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.groq import Groq

load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

# ======================
# AGENTE
# ======================

agent = Agent(
    model=Groq(id="llama-3.3-70b-versatile"),
    markdown=False,
    instructions="""
    Você é um assistente corporativo.
    Responda emails de forma educada,
    profissional e objetiva.
    """
)

# ======================
# LER EMAILS
# ======================

mail = imaplib.IMAP4_SSL("imap.gmail.com")

mail.login(EMAIL_USER, EMAIL_PASS)

mail.select("inbox")

status, messages = mail.search(None, "UNSEEN")

email_ids = messages[0].split()

print(f"Emails encontrados: {len(email_ids)}")

# ======================
# PROCESSAR EMAILS
# ======================

for email_id in email_ids:

    status, msg_data = mail.fetch(email_id, "(RFC822)")

    for response_part in msg_data:

        if isinstance(response_part, tuple):

            msg = email.message_from_bytes(response_part[1])

            subject, encoding = decode_header(msg["Subject"])[0]

            if isinstance(subject, bytes):
                subject = subject.decode(
                    encoding if encoding else "utf-8"
                )

            from_email = msg.get("From")

            print("\n--- EMAIL ---")
            print("De:", from_email)
            print("Assunto:", subject)

            # ======================
            # PEGAR CORPO
            # ======================

            body = ""

            if msg.is_multipart():

                for part in msg.walk():

                    content_type = part.get_content_type()

                    if content_type == "text/plain":

                        body = part.get_payload(
                            decode=True
                        ).decode()

                        break

            else:

                body = msg.get_payload(
                    decode=True
                ).decode()

            print("Mensagem:")
            print(body)

            # ======================
            # IA GERA RESPOSTA
            # ======================

            prompt = f"""
            Responda este email: Olá!
            Obrigado pelo contato.
            Recebemos sua mensagem e retornaremos em breve.

            Atenciosamente

            Equipe de Suporte
            """

            resposta = agent.run(prompt)

            resposta_texto = resposta.content

            print("\nResposta IA:")
            print(resposta_texto)

            # ======================
            # ENVIAR EMAIL
            # ======================

            smtp = smtplib.SMTP(
                "smtp.gmail.com",
                587
            )

            smtp.starttls()

            smtp.login(
                EMAIL_USER,
                EMAIL_PASS
            )

            reply = MIMEText(resposta_texto)

            reply["Subject"] = f"Re: {subject}"

            reply["From"] = EMAIL_USER

            reply["To"] = from_email

            smtp.sendmail(
                EMAIL_USER,
                from_email,
                reply.as_string()
            )

            smtp.quit()

            print("Resposta enviada!")

mail.logout()