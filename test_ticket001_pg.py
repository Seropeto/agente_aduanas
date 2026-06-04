"""
Test de validación local — Ticket 001: PostgreSQL Hybrid Persistence
Ejecutar: python test_ticket001_pg.py

Requiere PostgreSQL corriendo para los tests de integración:
  docker-compose up -d postgres
  # luego setear: set POSTGRES_URL=postgresql://agentia:agentia_dev_pass@localhost:5432/agentia_db

Si POSTGRES_URL no está configurado, los tests 1 y 2 (modo graceful) igual pasan.
"""
import asyncio
import os


def test_imports():
    from backend.database import (
        init_pool, close_pool, is_pg_enabled,
        upsert_document, delete_document_pg, query_documents, get_document, count_documents,
    )
    assert not is_pg_enabled(), "Pool no debe estar activo antes de init_pool()"
    print("OK Test 1: importaciones OK, is_pg_enabled()=False sin POSTGRES_URL")


async def test_noop_without_pg():
    from backend.database import (
        upsert_document, delete_document_pg, query_documents, get_document, count_documents,
    )
    await upsert_document("doc1", "user1", "Test Doc")
    assert await delete_document_pg("doc1", "user1") is False
    assert await query_documents("user1") == []
    assert await get_document("doc1", "user1") is None
    assert await count_documents("user1") == 0
    print("OK Test 2: no-ops seguros sin POSTGRES_URL")


async def test_with_real_pg():
    pg_url = os.getenv("POSTGRES_URL", "")
    if not pg_url:
        print("SKIP Test 3: POSTGRES_URL no configurado (requiere docker-compose up -d postgres)")
        return

    from backend.database import init_pool, close_pool, is_pg_enabled
    from backend.database import upsert_document, delete_document_pg, query_documents, count_documents

    await init_pool()
    assert is_pg_enabled()
    print(f"OK PostgreSQL conectado")

    # Insertar documento de prueba
    await upsert_document(
        doc_id="test-doc-001",
        user_id="test-user-001",
        title="Factura SAP-2024-001",
        filename="factura_001.pdf",
        content_type="factura",
        source="Documento interno",
        fecha_documento="2024-10-15",
        total_chunks=5,
    )
    print("OK upsert_document")

    # Acceptance criteria: listar facturas entre Oct y Mar
    docs = await query_documents(
        user_id="test-user-001",
        content_type="factura",
        fecha_desde="2024-10-01",
        fecha_hasta="2025-03-31",
    )
    assert len(docs) >= 1
    assert docs[0]["doc_id"] == "test-doc-001"
    print(f"OK query_documents filtrada: {len(docs)} resultado(s) en rango Oct-Mar")

    # Multi-tenant: otro usuario no ve documentos ajenos
    docs_other = await query_documents(user_id="otro-user-999")
    assert all(d["user_id"] != "test-user-001" for d in docs_other)
    print("OK aislamiento multi-tenant")

    # Upsert idempotente
    await upsert_document(
        doc_id="test-doc-001",
        user_id="test-user-001",
        title="Factura actualizada",
        content_type="factura",
        fecha_documento="2024-10-15",
        total_chunks=7,
    )
    docs_upd = await query_documents(user_id="test-user-001", search_title="actualizada")
    assert docs_upd[0]["total_chunks"] == 7
    print("OK upsert idempotente")

    # Delete con aislamiento (doc_id + user_id)
    deleted = await delete_document_pg("test-doc-001", "test-user-001")
    assert deleted
    docs_after = await query_documents(user_id="test-user-001")
    assert all(d["doc_id"] != "test-doc-001" for d in docs_after)
    print("OK delete_document_pg")

    await close_pool()
    print("\nOK Test 3: todos los tests de integración PostgreSQL PASARON")


async def main():
    print("=== Ticket 001 — PostgreSQL Hybrid Persistence ===\n")
    test_imports()
    await test_noop_without_pg()
    await test_with_real_pg()
    print("\n=== TODOS LOS TESTS PASARON ===")


if __name__ == "__main__":
    asyncio.run(main())
