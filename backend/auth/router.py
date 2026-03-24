"""
Endpoints de autenticación: login, registro demo, perfil.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr

from .database import (
    admin_exists,
    create_user,
    get_user_by_email,
    get_user_by_id,
    initialize_auth_db,
)
from .utils import (
    create_access_token,
    decode_token,
    generate_password,
    hash_password,
    send_admin_notification,
    send_demo_credentials,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer()


# --- Schemas ---

class LoginRequest(BaseModel):
    email: str
    password: str


class DemoRequest(BaseModel):
    name: str
    company: str
    email: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


# --- Dependency: usuario autenticado ---

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token = credentials.credentials
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    user = get_user_by_id(payload["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o inactivo")

    # Verificar expiración del demo
    if user["expires_at"]:
        expires = datetime.fromisoformat(user["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(
                status_code=403,
                detail="Tu acceso demo ha expirado. Contacta a nuestro equipo para contratar el servicio."
            )

    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Acceso restringido a administradores")
    return user


# --- Endpoints ---

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    user = get_user_by_email(body.email)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Cuenta inactiva")

    if user["expires_at"]:
        expires = datetime.fromisoformat(user["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(
                status_code=403,
                detail="Tu acceso demo ha expirado. Contacta a nuestro equipo para contratar el servicio."
            )

    token = create_access_token(user["id"], user["role"])
    return {
        "access_token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "company": user["company"],
            "role": user["role"],
            "expires_at": user["expires_at"],
        },
    }


@router.post("/demo")
async def request_demo(body: DemoRequest):
    existing = get_user_by_email(body.email)
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una cuenta con ese correo. Inicia sesión."
        )

    password = generate_password()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=3)

    user = create_user(
        user_id=str(uuid.uuid4()),
        email=body.email,
        name=body.name,
        company=body.company,
        password_hash=hash_password(password),
        role="demo",
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )

    # Enviar credenciales al solicitante
    await send_demo_credentials(body.email, body.name, password)

    # Notificar al admin
    await send_admin_notification(body.name, body.company, body.email)

    return {"message": "Acceso demo creado. Revisa tu correo con las credenciales."}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "company": user["company"],
        "role": user["role"],
        "expires_at": user["expires_at"],
    }
