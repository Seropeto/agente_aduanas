# backend/database — PostgreSQL metadata store (Ticket 001)
from .pg import init_pool, close_pool, is_pg_enabled
from .pg import upsert_document, delete_document_pg, query_documents, get_document, count_documents

__all__ = [
    "init_pool", "close_pool", "is_pg_enabled",
    "upsert_document", "delete_document_pg", "query_documents", "get_document", "count_documents",
]
