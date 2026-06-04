"""
Test de validación local — Ticket 004: Ingesta autónoma de normativa oficial.
Ejecutar: python test_ticket004_ingest.py

No requiere PostgreSQL. Parte 4 carga el modelo de embeddings (all-MiniLM-L6-v2).

Valida:
  1. El endpoint manual POST /api/admin/ingest/normativa fue eliminado (404/405).
  2. parse_articles fragmenta por artículo con cabecera jerárquica.
  3. CLI `python cron_update_laws.py --file ... --dry-run` ejecuta y reporta artículos.
  4. Ingesta real a ChromaDB: documentos divididos por artículo, con cabecera, + cleanup.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import cron_update_laws as cron


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


# Fragmento de prueba del DFL 30 (Ordenanza de Aduanas) con estructura real
DFL30_FRAGMENT = """TÍTULO I
DE LAS DISPOSICIONES GENERALES

Artículo 1°.- La presente Ordenanza regula el ejercicio de la potestad aduanera
y las relaciones entre el Estado y las personas que operan en el comercio exterior.

Artículo 2°.- Para los efectos de esta Ordenanza, los términos que se indican
tendrán el significado que para cada uno se señala en este artículo.

TÍTULO II
DE LA POTESTAD ADUANERA

Artículo 40.- La potestad aduanera es el conjunto de atribuciones que tiene el
Servicio para controlar el ingreso y salida de mercancías del territorio nacional.
"""


def test_endpoint_removed():
    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app)
    r = client.post("/api/admin/ingest/normativa")
    assert r.status_code in (404, 405), f"Esperaba 404/405, obtuvo {r.status_code}"
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/admin/ingest/normativa" not in paths, "La ruta sigue registrada en FastAPI"
    print(f"OK Test 1: endpoint manual eliminado -> HTTP {r.status_code}, ruta ausente del router")


def test_parse_articles():
    chunks = cron.parse_articles(DFL30_FRAGMENT, "DFL 30 (TEST)", source="BCN")

    assert len(chunks) == 3, f"Esperaba 3 articulos, obtuvo {len(chunks)}"

    header_re = re.compile(r"^\[Origen: BCN - DFL 30 \(TEST\), T[ÍI]TULO [IVX]+, Art[íi]culo \d+\] ")
    for c in chunks:
        assert header_re.match(c["text"]), f"Cabecera invalida: {_ascii(c['text'][:80])}"

    assert chunks[0]["metadata"]["articulo"] == "1"
    assert chunks[1]["metadata"]["articulo"] == "2"
    assert chunks[2]["metadata"]["articulo"] == "40"
    assert chunks[0]["metadata"]["jerarquia"].startswith("TÍTULO I")
    assert chunks[2]["metadata"]["jerarquia"].startswith("TÍTULO II")

    # Límite estricto: el chunk del Art. 1 NO debe contener el cuerpo del Art. 2
    assert "significado que para cada uno" not in chunks[0]["text"], "Frontera de articulo mal cortada"
    assert "conjunto de atribuciones" in chunks[2]["text"]

    print("OK Test 2: parse_articles -> 3 articulos con cabecera jerarquica correcta")
    print(f"          ej: {_ascii(chunks[2]['text'][:70])}...")


def test_cli_dry_run():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(DFL30_FRAGMENT)
        tmp = f.name
    try:
        proc = subprocess.run(
            [sys.executable, "cron_update_laws.py", "--file", tmp,
             "--law-name", "DFL 30 (TEST)", "--source", "BCN", "--dry-run"],
            capture_output=True, text=True, timeout=120,
        )
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"CLI retorno {proc.returncode}: {_ascii(out)}"
        assert "3 articulo" in out, f"No reporta 3 articulos: {_ascii(out)}"
        print("OK Test 3: CLI 'cron_update_laws.py --dry-run' ejecuta y reporta 3 articulos")
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_ingest_chromadb():
    from backend.indexer.vectorstore import VectorStore, COLLECTION_NORMATIVA

    vs = VectorStore()
    vs.initialize()

    law_name = "DFL 30 (TEST INGEST)"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(DFL30_FRAGMENT)
        tmp = f.name

    doc_id = None
    try:
        result = cron.ingest_file(tmp, law_name, source="BCN", vector_store=vs, dry_run=False)
        doc_id = result["doc_id"]
        assert result["articulos"] == 3
        assert result["indexed"] == 3, f"Esperaba 3 chunks indexados, obtuvo {result['indexed']}"

        col = vs._get_collection(COLLECTION_NORMATIVA)
        got = col.get(where={"doc_id": doc_id}, include=["documents", "metadatas"])
        assert len(got["ids"]) == 3, f"ChromaDB tiene {len(got['ids'])} chunks, esperaba 3"

        articulos_indexados = sorted(m["articulo"] for m in got["metadatas"])
        assert articulos_indexados == ["1", "2", "40"], articulos_indexados

        for doc in got["documents"]:
            assert doc.startswith("[Origen: BCN - DFL 30 (TEST INGEST),"), \
                f"Chunk sin cabecera jerarquica: {_ascii(doc[:60])}"

        print(f"OK Test 4: ChromaDB indexo 3 articulos {articulos_indexados} con cabecera jerarquica")
    finally:
        Path(tmp).unlink(missing_ok=True)
        if doc_id:
            vs.delete_document(doc_id, COLLECTION_NORMATIVA)
            print(f"OK Test 4: cleanup (doc_id={doc_id} eliminado de normativa)")


def main():
    print("=== Ticket 004 - Ingesta Autonoma de Normativa Oficial ===\n")
    test_endpoint_removed()
    test_parse_articles()
    test_cli_dry_run()
    test_ingest_chromadb()
    print("\n=== TODOS LOS TESTS PASARON ===")


if __name__ == "__main__":
    main()
