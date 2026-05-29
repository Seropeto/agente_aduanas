"""
Script de diagnóstico para validar los tres fixes del ticket RAG/Alucinación.

Uso:
    python test_dictamen.py <ruta_al_pdf>

Ejemplo:
    python test_dictamen.py dictamen_72_2002.pdf

Qué verifica:
    [A] OCR por página: extrae texto completo incluyendo "SE DECLARA"
    [B] Chunking: genera suficientes chunks cubiertos por TOP_K_INTERNOS
    [C] Guardrail: las constantes de seguridad están activas en engine.py
"""
import sys
from pathlib import Path

OK   = "OK"
FAIL = "FALLO"

def check(label: str, passed: bool, detail: str = "") -> bool:
    icon = f"[{OK}]  " if passed else f"[{FAIL}]"
    print(f"  {icon} {label}")
    if detail:
        print(f"         {detail}")
    return passed


# ── Fix A: Extracción PDF ──────────────────────────────────────────────────────

def test_extraccion(pdf_path: Path) -> tuple[bool, str]:
    print("\n[A] EXTRACCION OCR POR PAGINA")
    from backend.indexer.document_processor import DocumentProcessor
    proc = DocumentProcessor()
    text = proc._extract_pdf(pdf_path)

    check("Texto extraido (>100 chars)", len(text) > 100,
          f"{len(text)} chars extraidos")

    ok_declara = check("Contiene 'SE DECLARA'",   "SE DECLARA" in text.upper())
    ok_partida = check("Contiene '8529.9090'",    "8529.9090"  in text,
                       "Partida arancelaria final del dictamen")
    check("Contiene referencia al Cap. 44",
          "44" in text and ("cap" in text.lower()))
    check("Contiene referencia al Cap. 94",
          "94" in text and ("cap" in text.lower()))

    all_ok = ok_declara and ok_partida
    if not all_ok:
        print(f"\n  Primeros 500 chars extraidos:\n  {text[:500]!r}")
        print(f"\n  Ultimos 500 chars extraidos:\n  {text[-500:]!r}")
    return all_ok, text


# ── Fix B: Chunking y Top_K ────────────────────────────────────────────────────

def test_chunking(text: str) -> bool:
    print("\n[B] CHUNKING Y COBERTURA")
    from backend.indexer.document_processor import DocumentProcessor, CHUNK_SIZE, CHUNK_OVERLAP
    from backend.rag.engine import TOP_K_INTERNOS
    proc = DocumentProcessor()
    chunks = proc._chunk_text(text)

    check(f"Genera chunks (esperado >=3 para dictamen de 4 pag.)", len(chunks) >= 3,
          f"{len(chunks)} chunks de ~{CHUNK_SIZE} palabras con {CHUNK_OVERLAP} overlap")

    ok_last    = check("'SE DECLARA' esta en algun chunk",
                       any("SE DECLARA" in c.upper() for c in chunks))
    ok_partida = check("'8529.9090' esta en algun chunk",
                       any("8529.9090" in c for c in chunks))
    check(f"TOP_K_INTERNOS ({TOP_K_INTERNOS}) >= numero de chunks ({len(chunks)})",
          TOP_K_INTERNOS >= len(chunks),
          "Si falla: aumentar TOP_K_INTERNOS en engine.py")

    return ok_last and ok_partida


# ── Fix C: Guardrail del SYSTEM_PROMPT ────────────────────────────────────────

def test_guardrail() -> bool:
    print("\n[C] GUARDRAIL ANTI-ALUCINACION")
    from backend.rag.engine import SYSTEM_PROMPT, INTERNAL_DOC_SIGNALS
    from backend.indexer.document_processor import OCR_PAGE_THRESHOLD

    ok1 = check("GUARDRAIL presente en SYSTEM_PROMPT",
                 "GUARDRAIL ANTI-ALUCINACI" in SYSTEM_PROMPT)
    ok2 = check("'PROHIBIDO' presente en SYSTEM_PROMPT",
                 "PROHIBIDO" in SYSTEM_PROMPT)
    ok3 = check("'el dictamen' en INTERNAL_DOC_SIGNALS",
                 "el dictamen" in INTERNAL_DOC_SIGNALS)
    ok4 = check("'normativo cargado' en INTERNAL_DOC_SIGNALS",
                 "normativo cargado" in INTERNAL_DOC_SIGNALS)
    ok5 = check(f"OCR_PAGE_THRESHOLD == 200 (actual: {OCR_PAGE_THRESHOLD})",
                 OCR_PAGE_THRESHOLD == 200)

    return all([ok1, ok2, ok3, ok4, ok5])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Uso: python test_dictamen.py <ruta_al_pdf>")
        print("Ejemplo: python test_dictamen.py dictamen_72_2002.pdf")
        print("\nSolo guardrail (sin PDF):")
        print("  python test_dictamen.py --solo-guardrail")
        sys.exit(0)

    results = []

    if sys.argv[1] == "--solo-guardrail":
        results.append(test_guardrail())
    else:
        pdf_path = Path(sys.argv[1])
        if not pdf_path.exists():
            print(f"[{FAIL}] PDF no encontrado: {pdf_path}")
            sys.exit(1)

        ok_a, text = test_extraccion(pdf_path)
        results.append(ok_a)

        if text:
            results.append(test_chunking(text))
        else:
            print(f"\n  [{FAIL}] Sin texto extraido -- omitiendo test de chunking")
            results.append(False)

        results.append(test_guardrail())

    print("\n" + "-" * 50)
    total_ok = sum(results)
    total    = len(results)
    if all(results):
        print(f"[{OK}] Todos los tests pasaron ({total}/{total})")
        print("Siguiente paso: re-cargar el dictamen en la UI para re-indexarlo con el nuevo OCR")
    else:
        print(f"[{FAIL}] {total - total_ok}/{total} tests fallaron -- revisar los FALLO arriba")
        sys.exit(1)


if __name__ == "__main__":
    main()
