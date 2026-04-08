"""
Endpoints de billing: panel admin y cuota del cliente.
"""
import uuid
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth.database import (
    get_all_users,
    get_user_by_id,
    update_user_plan,
    update_user_billing_status,
    update_user_info,
    add_extra_pack,
    create_user,
)
from backend.auth.router import get_current_user, require_admin
from backend.auth.utils import hash_password, generate_password, send_client_credentials
from backend.billing.plans import PLANS, get_plan, EXTRA_PACK_QUERIES, EXTRA_PACK_PRICE_USD
from backend.billing.service import calc_reset_date

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])


# ------------------------------------------------------------------ #
# Modelos                                                              #
# ------------------------------------------------------------------ #

class CreateClientBody(BaseModel):
    email: str
    name: str
    company: str
    plan_id: str
    tipo_vps: str = "agentia"


class UpdatePlanBody(BaseModel):
    plan_id: str


class UpdateStatusBody(BaseModel):
    billing_status: str  # "activo" | "suspendido"


class UpdateInfoBody(BaseModel):
    name: str
    company: str
    tipo_vps: str


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _user_with_quota(user: dict) -> dict:
    """Agrega campos calculados de cuota al dict del usuario."""
    used = user.get("queries_used") or 0
    limit = user.get("queries_limit") or 0
    pct = round(used / limit, 4) if limit > 0 else 0.0
    plan = get_plan(user.get("plan_id", "base"))
    return {
        **user,
        "password_hash": "***",
        "pct": pct,
        "plan_name": plan["name"],
        "plan_price_usd": plan["price_usd"],
    }


# ------------------------------------------------------------------ #
# Endpoints — Panel Admin                                              #
# ------------------------------------------------------------------ #

@router.get("/api/admin/clients")
async def list_clients(admin: dict = Depends(require_admin)):
    """Lista todos los clientes con su estado de cuota."""
    users = get_all_users()
    return [_user_with_quota(u) for u in users]


@router.post("/api/admin/clients")
async def create_client(body: CreateClientBody, admin: dict = Depends(require_admin)):
    """Crea un nuevo cliente, asigna plan y envía credenciales por email."""
    plan = PLANS.get(body.plan_id)
    if not plan:
        raise HTTPException(status_code=400, detail=f"Plan inválido: {body.plan_id}")

    password = generate_password()
    user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    reset_date = calc_reset_date()

    try:
        user = create_user(
            user_id=user_id,
            email=body.email.lower(),
            name=body.name,
            company=body.company,
            password_hash=hash_password(password),
            role="cliente",
            created_at=now,
            expires_at=None,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error creando usuario: {str(e)}")

    update_user_plan(
        user_id=user_id,
        plan_id=body.plan_id,
        queries_limit=plan["queries"],
        subscription_start=now,
        reset_date=reset_date,
    )

    update_user_info(user_id, body.name, body.company, body.tipo_vps)

    try:
        await send_client_credentials(
            to=body.email,
            name=body.name,
            company=body.company,
            password=password,
            plan_name=plan["name"],
        )
    except Exception as e:
        logger.warning(f"No se pudo enviar email de credenciales a {body.email}: {e}")

    return _user_with_quota(get_user_by_id(user_id))


@router.get("/api/admin/clients/{user_id}")
async def get_client(user_id: str, admin: dict = Depends(require_admin)):
    """Detalle de un cliente."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return _user_with_quota(user)


@router.put("/api/admin/clients/{user_id}/plan")
async def change_plan(user_id: str, body: UpdatePlanBody, admin: dict = Depends(require_admin)):
    """Cambia el plan de un cliente."""
    plan = PLANS.get(body.plan_id)
    if not plan:
        raise HTTPException(status_code=400, detail=f"Plan inválido: {body.plan_id}")
    now = datetime.now(timezone.utc).isoformat()
    reset_date = calc_reset_date()
    user = update_user_plan(
        user_id=user_id,
        plan_id=body.plan_id,
        queries_limit=plan["queries"],
        subscription_start=now,
        reset_date=reset_date,
    )
    if not user:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return _user_with_quota(user)


@router.put("/api/admin/clients/{user_id}/status")
async def change_status(user_id: str, body: UpdateStatusBody, admin: dict = Depends(require_admin)):
    """Activa o suspende un cliente."""
    if body.billing_status not in ("activo", "suspendido"):
        raise HTTPException(status_code=400, detail="Estado inválido")
    is_active = 1 if body.billing_status == "activo" else 0
    user = update_user_billing_status(user_id, body.billing_status, is_active)
    if not user:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return _user_with_quota(user)


@router.put("/api/admin/clients/{user_id}/info")
async def update_client_info(user_id: str, body: UpdateInfoBody, admin: dict = Depends(require_admin)):
    """Actualiza nombre, empresa y tipo de VPS del cliente."""
    user = update_user_info(user_id, body.name, body.company, body.tipo_vps)
    if not user:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return _user_with_quota(user)


@router.post("/api/admin/clients/{user_id}/extra-pack")
async def add_pack(user_id: str, admin: dict = Depends(require_admin)):
    """Agrega un paquete adicional de 200 consultas (máx 2/mes)."""
    result = add_extra_pack(user_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Error"))
    return _user_with_quota(result["user"])


# ------------------------------------------------------------------ #
# Endpoints — Panel del Cliente                                        #
# ------------------------------------------------------------------ #

@router.get("/api/me/quota")
async def my_quota(current_user: dict = Depends(get_current_user)):
    """Cuota del usuario autenticado."""
    used = current_user.get("queries_used") or 0
    limit = current_user.get("queries_limit") or 0
    pct = round(used / limit, 4) if limit > 0 else 0.0
    plan = get_plan(current_user.get("plan_id", "base"))
    return {
        "plan_id": current_user.get("plan_id", "base"),
        "plan_name": plan["name"],
        "queries_used": used,
        "queries_limit": limit,
        "pct": pct,
        "billing_status": current_user.get("billing_status", "activo"),
        "extra_packs": current_user.get("extra_packs") or 0,
        "reset_date": current_user.get("reset_date"),
    }


# ------------------------------------------------------------------ #
# Endpoint público — lista de planes                                   #
# ------------------------------------------------------------------ #

@router.get("/api/plans")
async def list_plans():
    """Lista de planes disponibles (público)."""
    return [{"id": k, **v} for k, v in PLANS.items()]
