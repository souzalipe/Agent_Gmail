import imaplib
import email
from email.header import decode_header
import os
from dotenv import load_dotenv

load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

# Conecta ao Gmail
mail = imaplib.IMAP4_SSL("imap.gmail.com")

# Login
mail.login(EMAIL_USER, EMAIL_PASS)

# Caixa de entrada
mail.select("inbox")

# Procura emails não lidos
status, messages = mail.search(None, "UNSEEN")

email_ids = messages[0].split()

print(f"Emails encontrados: {len(email_ids)}")

for email_id in email_ids:
    status, msg_data = mail.fetch(email_id, "(RFC822)")

    for response_part in msg_data:
        if isinstance(response_part, tuple):

            msg = email.message_from_bytes(response_part[1])

            subject, encoding = decode_header(msg["Subject"])[0]

            if isinstance(subject, bytes):
                subject = subject.decode(encoding if encoding else "utf-8")

            from_email = msg.get("From")

            print("\n--- EMAIL ---")
            print("De:", from_email)
            print("Assunto:", subject)

            # Corpo do email
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()

                    if content_type == "text/plain":
                        body = part.get_payload(decode=True).decode()

                        print("Mensagem:")
                        print(body)

            else:
                body = msg.get_payload(decode=True).decode()

                print(body)

mail.logout()