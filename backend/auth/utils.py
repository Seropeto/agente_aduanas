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


async def send_quota_alert(to: str, name: str, pct: float, plan_name: str,
                           used: int, limit: int) -> bool:
    subject = "AgentIA — Ha consumido el 80% de su cuota mensual"
    pct_display = int(pct * 100)
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
      <h2 style="color: #d97706;">⚠️ Alerta de consumo — AgentIA Aduanas</h2>
      <p>Estimado/a <strong>{name}</strong>,</p>
      <p>Ha utilizado el <strong>{pct_display}% de su cuota mensual</strong> de consultas en el plan <strong>{plan_name}</strong>.</p>
      <div style="background: #fef3c7; border: 1px solid #f59e0b; padding: 16px; border-radius: 8px; margin: 20px 0;">
        <p style="margin: 0;"><strong>Consultas utilizadas:</strong> {used} de {limit}</p>
        <p style="margin: 8px 0 0;"><strong>Consultas restantes:</strong> {limit - used}</p>
      </div>
      <p>Si necesita más consultas antes de que se renueve su plan, puede:</p>
      <ul>
        <li>Adquirir un paquete adicional de 200 consultas ($10 USD)</li>
        <li>Subir al plan superior para continuar sin interrupciones</li>
      </ul>
      <p>Contáctenos respondiendo este correo o escribiendo a <a href="mailto:{ADMIN_EMAIL}">{ADMIN_EMAIL}</a>.</p>
    </div>
    """
    return await send_email(to, subject, body)


async def send_suspension_notice(to: str, name: str) -> bool:
    subject = "AgentIA — Su acceso ha sido suspendido temporalmente"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
      <h2 style="color: #dc2626;">🔒 Acceso suspendido — AgentIA Aduanas</h2>
      <p>Estimado/a <strong>{name}</strong>,</p>
      <p>Ha alcanzado el <strong>límite mensual de consultas</strong> de su plan.</p>
      <p>Su acceso ha sido suspendido temporalmente hasta que:</p>
      <ul>
        <li>Se renueve su cuota mensual (día 1 del próximo mes), o</li>
        <li>Adquiera un paquete adicional de 200 consultas ($10 USD), o</li>
        <li>Suba al plan superior</li>
      </ul>
      <p>Para reactivar su acceso de inmediato, contáctenos a <a href="mailto:{ADMIN_EMAIL}">{ADMIN_EMAIL}</a>.</p>
    </div>
    """
    return await send_email(to, subject, body)


async def send_client_credentials(to: str, name: str, company: str,
                                   password: str, plan_name: str) -> bool:
    subject = "AgentIA Aduanas — Credenciales de acceso"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
      <h2 style="color: #0a3b8c;">Bienvenido/a a AgentIA Aduanas</h2>
      <p>Estimado/a <strong>{name}</strong> de <strong>{company}</strong>,</p>
      <p>Su cuenta ha sido activada con el plan <strong>{plan_name}</strong>. A continuación sus credenciales de acceso:</p>
      <div style="background: #f1f5f9; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <p style="margin: 0;"><strong>URL:</strong> <a href="https://aduanas.toxirodigital.cloud">aduanas.toxirodigital.cloud</a></p>
        <p style="margin: 8px 0 0;"><strong>Usuario:</strong> {to}</p>
        <p style="margin: 8px 0 0;"><strong>Contraseña:</strong> <code style="background:#e2e8f0;padding:2px 6px;border-radius:4px;">{password}</code></p>
      </div>
      <p>Le recomendamos cambiar su contraseña en el primer inicio de sesión.</p>
      <p>Si tiene preguntas, responda este correo.</p>
    </div>
    """
    return await send_email(to, subject, body)
