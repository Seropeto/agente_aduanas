"""
Lógica de negocio de billing: control de cuota, alertas y resets mensuales.
"""
import logging
from datetime import datetime, timezone, date
from dateutil.relativedelta import relativedelta

from backend.auth.database import (
    get_user_by_id,
    increment_query_count,
    mark_alert_sent,
    get_users_needing_reset,
    reset_monthly_queries,
)
from backend.billing.plans import ALERT_THRESHOLD

logger = logging.getLogger(__name__)


def check_quota(user: dict) -> dict:
    """
    Verifica si el usuario puede realizar una consulta.
    Retorna dict con: allowed (bool), reason (str), pct (float).
    Los admins siempre pasan. Los demo sin plan asignado tienen límite propio.
    """
    if user.get("role") == "admin":
        return {"allowed": True, "reason": "", "pct": 0.0}

    billing_status = user.get("billing_status", "activo")
    if billing_status == "suspendido":
        return {
            "allowed": False,
            "reason": "Su cuenta está suspendida temporalmente por haber alcanzado el límite mensual de consultas. Contacte a Toxiro Digital para continuar.",
            "pct": 1.0,
        }

    used = user.get("queries_used") or 0
    limit = user.get("queries_limit") or 0

    if limit == 0:
        return {"allowed": True, "reason": "", "pct": 0.0}

    pct = used / limit

    if pct >= 1.0:
        return {
            "allowed": False,
            "reason": "Ha alcanzado el límite mensual de consultas de su plan. Contacte a Toxiro Digital para agregar un paquete adicional o cambiar de plan.",
            "pct": pct,
        }

    return {"allowed": True, "reason": "", "pct": pct}


async def record_query(user_id: str) -> None:
    """
    Registra una consulta consumida y gestiona alertas/suspensión.
    Se llama de forma asíncrona después de la respuesta al cliente.
    """
    try:
        user = increment_query_count(user_id)
        if not user:
            return

        used = user.get("queries_used") or 0
        limit = user.get("queries_limit") or 0
        if limit == 0:
            return

        pct = used / limit

        # Alerta al 80%
        if pct >= ALERT_THRESHOLD and not user.get("alert_80_sent"):
            from backend.auth.utils import send_quota_alert
            from backend.billing.plans import get_plan
            plan = get_plan(user.get("plan_id", "base"))
            await send_quota_alert(
                to=user["email"],
                name=user["name"],
                pct=pct,
                plan_name=plan["name"],
                used=used,
                limit=limit,
            )
            mark_alert_sent(user_id)

        # Suspender al 100%
        if pct >= 1.0:
            from backend.auth.database import update_user_billing_status
            from backend.auth.utils import send_suspension_notice
            update_user_billing_status(user_id, "suspendido", 0)
            await send_suspension_notice(to=user["email"], name=user["name"])

    except Exception as e:
        logger.error(f"Error registrando consulta para {user_id}: {e}")


def run_monthly_reset() -> int:
    """
    Resetea contadores de usuarios cuya fecha de reset ya pasó.
    Llamado por el scheduler el día 1 de cada mes.
    Retorna cantidad de usuarios reseteados.
    """
    today = date.today().isoformat()
    users = get_users_needing_reset(today)
    count = 0
    for user in users:
        try:
            next_reset = (date.today() + relativedelta(months=1)).replace(day=1).isoformat()
            reset_monthly_queries(user["id"], next_reset)
            count += 1
        except Exception as e:
            logger.error(f"Error reseteando cuota del usuario {user['id']}: {e}")
    if count:
        logger.info(f"Reset mensual completado: {count} usuarios reseteados")
    return count


def calc_reset_date() -> str:
    """Retorna el primer día del mes siguiente."""
    return (date.today() + relativedelta(months=1)).replace(day=1).isoformat()
