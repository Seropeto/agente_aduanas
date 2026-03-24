"""
Utilidades de autenticación: JWT, contraseñas, correo.
"""
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from jose import JWTError, jwt
from passlib.context import CryptContext

logger = logging.getLogger(__name__)

# JWT
SECRET_KEY = os.getenv("SECRET_KEY", "cambia-esto-en-produccion-ahora")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

# SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.hostinger.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "contacto@toxirodigital.cloud")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- Contraseñas ---

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def generate_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- JWT ---

def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"sub": user_id, "role": role, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# --- Correo ---

async def send_email(to: str, subject: str, html_body: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP no configurado")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            use_tls=True,
            username=SMTP_USER,
            password=SMTP_PASSWORD,
        )
        logger.info(f"Correo enviado a {to}")
        return True
    except Exception as e:
        logger.error(f"Error enviando correo a {to}: {e}")
        return False


async def send_demo_credentials(to: str, name: str, password: str) -> bool:
    subject = "Tu acceso demo a AgentIA Aduanas"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #1e40af;">AgentIA Aduanas — Acceso Demo</h2>
      <p>Hola <strong>{name}</strong>,</p>
      <p>Tu acceso demo está listo. Tienes <strong>3 días</strong> para explorar el sistema.</p>
      <div style="background: #f1f5f9; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <p style="margin: 0;"><strong>URL:</strong> <a href="https://aduanas.toxirodigital.cloud">aduanas.toxirodigital.cloud</a></p>
        <p style="margin: 8px 0 0;"><strong>Usuario:</strong> {to}</p>
        <p style="margin: 8px 0 0;"><strong>Contraseña:</strong> <code style="background:#e2e8f0;padding:2px 6px;border-radius:4px;">{password}</code></p>
      </div>
      <p>Si tienes preguntas, responde este correo.</p>
      <p style="color: #64748b; font-size: 13px;">Este acceso expira en 3 días.</p>
    </div>
    """
    return await send_email(to, subject, body)


async def send_admin_notification(name: str, company: str, email: str) -> bool:
    subject = f"Nuevo acceso demo solicitado — {company}"
    body = f"""
    <div style="font-family: Arial, sans-serif;">
      <h3>Nuevo acceso demo solicitado</h3>
      <p><strong>Nombre:</strong> {name}</p>
      <p><strong>Empresa:</strong> {company}</p>
      <p><strong>Correo:</strong> {email}</p>
    </div>
    """
    return await send_email(ADMIN_EMAIL, subject, body)
