"""
seed_demo_data.py — Sembrado de historial operativo en PostgreSQL (Ticket 004-B)

Inyecta en PostgreSQL (puerto local 5435) un historial controlado de Declaraciones
de Ingreso (DIN) para el user_id de la demostración, de modo que el flujo COMPLEX
del SmartRouter tenga datos relacionales reales que cruzar.

Requiere PostgreSQL del Ticket 001:
  docker-compose up -d postgres
  set POSTGRES_URL=postgresql://agentia:agentia_dev_pass@localhost:5435/agentia_db

Uso:
  python seed_demo_data.py                       # usa el user_id del admin (auth.db)
  python seed_demo_data.py --user-id <uuid>      # user_id explícito
  python seed_demo_data.py --clear               # borra las DIN sembradas antes de re-sembrar
"""
import argparse
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_demo_data")

# DIN sintéticas — periodos 2025 y 2026. Soportan el Caso 2 (auditoría multinivel:
# España, India, EE.UU.; USD 45.000; conflicto de partida 9026 vs 9032).
DEMO_DINS = [
    {
        "doc_id": "din-demo-2025-4471",
        "title": "DIN 2025-4471 — Equipos electrónicos de medición de precisión",
        "content_type": "din",
        "fecha_documento": "2025-11-12",
        "meta": {
            "folio": "2025-DIN-4471",
            "monto_usd": "45000",
            "proveedor": "Instrumentos Ibéricos S.A.",
            "pais_origen": "ES",  # España (Unión Europea)
            "descripcion": "Equipos electrónicos de medición de precisión con sensores integrados.",
            "partida_sugerida": "9026",
        },
    },
    {
        "doc_id": "din-demo-2026-0093",
        "title": "DIN 2026-0093 — Dispositivos ópticos con componentes de audio/video",
        "content_type": "din",
        "fecha_documento": "2026-01-20",
        "meta": {
            "folio": "2026-DIN-0093",
            "monto_usd": "28500",
            "proveedor": "Optical Wearables Inc.",
            "pais_origen": "US",  # Estados Unidos
            "descripcion": "Importación de dispositivos ópticos (gafas inteligentes) con componentes de audio/video.",
            "partida_sugerida": "9004 / 8517 (en disputa)",
        },
    },
    {
        "doc_id": "din-demo-2025-5582",
        "title": "DIN 2025-5582 — Instrumentos de medición y control automático",
        "content_type": "din",
        "fecha_documento": "2025-12-05",
        "meta": {
            "folio": "2025-DIN-5582",
            "monto_usd": "16200",
            "proveedor": "Bharat Metrology Ltd.",
            "pais_origen": "IN",  # India (régimen general / acuerdo de alcance parcial)
            "descripcion": "Instrumentos de medición y control automático de variables eléctricas.",
            "partida_sugerida": "9032",
        },
    },
]


def _resolve_admin_user_id() -> str | None:
    """Obtiene el user_id del admin desde auth.db (SQLite) si no se pasó --user-id."""
    try:
        from backend.auth.database import get_connection
        with get_connection() as conn:
            row = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning(f"No se pudo leer auth.db: {e}")
        return None


async def seed(user_id: str, clear: bool = False) -> dict:
    from backend.database import init_pool, close_pool, is_pg_enabled
    from backend.database import upsert_document, delete_document_pg

    await init_pool()
    if not is_pg_enabled():
        logger.error("PostgreSQL no está habilitado. Configure POSTGRES_URL (puerto 5435).")
        return {"ok": False, "seeded": 0}

    if clear:
        for din in DEMO_DINS:
            await delete_document_pg(din["doc_id"], user_id)
        logger.info("DIN previas eliminadas")

    seeded = 0
    for din in DEMO_DINS:
        await upsert_document(
            doc_id=din["doc_id"],
            user_id=user_id,
            title=din["title"],
            filename=f"{din['meta']['folio']}.pdf",
            content_type=din["content_type"],
            source="Carpeta de despacho (demo)",
            fecha_documento=din["fecha_documento"],
            total_chunks=1,
            tipo_documento="din",
            extra_meta=din["meta"],
        )
        seeded += 1
        logger.info(f"Sembrada {din['meta']['folio']} ({din['meta']['pais_origen']}, "
                    f"USD {din['meta']['monto_usd']}, {din['fecha_documento']})")

    await close_pool()
    return {"ok": True, "seeded": seeded, "user_id": user_id}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Siembra DIN de demo en PostgreSQL (Ticket 004-B)")
    parser.add_argument("--user-id", help="user_id destino (default: admin de auth.db)")
    parser.add_argument("--clear", action="store_true", help="Borra las DIN sembradas antes de re-sembrar")
    args = parser.parse_args(argv)

    user_id = args.user_id or _resolve_admin_user_id()
    if not user_id:
        logger.error("No se pudo determinar user_id. Pase --user-id <uuid>.")
        return 1

    result = asyncio.run(seed(user_id, clear=args.clear))
    if not result["ok"]:
        return 2
    print(f"RESULTADO: {result['seeded']} DIN sembradas para user_id={user_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
