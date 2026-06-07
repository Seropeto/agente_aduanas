# backend/database — PostgreSQL store (metadata Ticket 001 + pgvector Sprint AGENT)
from .pg import init_pool, close_pool, is_pg_enabled
from .pg import upsert_document, delete_document_pg, query_documents, get_document, count_documents
from .pg import insert_knowledge_chunks, search_chunks_hybrid, get_available_operations
from .pg import (
    delete_chunks_by_meta, chunks_meta_exists, count_chunks_by_domain,
    clear_chunks_by_domain, search_chunks_by_title, list_internal_documents,
    cache_lookup_pg, cache_store_pg, clear_semantic_cache_pg,
)

__all__ = [
    "init_pool", "close_pool", "is_pg_enabled",
    "upsert_document", "delete_document_pg", "query_documents", "get_document", "count_documents",
    "insert_knowledge_chunks", "search_chunks_hybrid", "get_available_operations",
    "delete_chunks_by_meta", "chunks_meta_exists", "count_chunks_by_domain",
    "clear_chunks_by_domain", "search_chunks_by_title", "list_internal_documents",
    "cache_lookup_pg", "cache_store_pg", "clear_semantic_cache_pg",
]
