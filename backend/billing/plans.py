"""
Definición de planes de suscripción y constantes de billing.
"""

PLANS = {
    "base": {
        "name": "Base",
        "queries": 960,
        "price_usd": 50,
    },
    "crecimiento": {
        "name": "Crecimiento",
        "queries": 2880,
        "price_usd": 90,
    },
    "empresarial": {
        "name": "Empresarial",
        "queries": 6400,
        "price_usd": 200,
    },
}

EXTRA_PACK_QUERIES = 200
EXTRA_PACK_PRICE_USD = 10
MAX_EXTRA_PACKS_PER_MONTH = 2
ALERT_THRESHOLD = 0.80   # 80% del límite
DEMO_QUERIES_LIMIT = 30  # Consultas para usuarios demo


def get_plan(plan_id: str) -> dict:
    return PLANS.get(plan_id, PLANS["base"])
