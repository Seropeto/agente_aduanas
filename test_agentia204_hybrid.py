"""
Validación local — #AGENTIA-204: Arquitectura RAG Híbrida de 3 capas.
Ejecutar: python test_agentia204_hybrid.py

Tests deterministas (sin red) — son la compuerta:
  1. Capa 3: aritmética tributaria correcta (IVA sobre CIF + Derechos) + extracción CIF.
  2. Clasificador de intención: operativa (DIN/operación) vs genérica.
  3. Caso "Operación Rusia": consulta operativa sin datos → frustración estricta
     (sin llamar al LLM), reusando la plantilla corporativa exacta.

Test best-effort (requiere ANTHROPIC_API_KEY):
  4. Caso "Drones industriales": consulta genérica → Capa 2 fundacional con disclaimer
     obligatorio + Capa 3 (cálculo determinista anexado). Cero NOT_FOUND.
"""
import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # consola Windows (cp1252) no codifica '→'
except Exception:
    pass

from backend.rag import compliance_cl as cc
from backend.rag import frustration as fr
import backend.database as database_mod
from backend.rag.engine import RAGEngine, NOT_FOUND_RESPONSE


class FakeVS:
    """VectorStore falso: simula que NO hay match vectorial (contexto vacío)."""
    def initialize(self): pass
    def close(self): pass
    async def acache_lookup(self, query): return None
    async def acache_store(self, *a, **k): return None
    async def asearch(self, *a, **k): return []
    async def aget_chunks_by_title_fragment(self, *a, **k): return []


def test_compliance_math():
    r = cc.compute_import_taxes(45000)
    # Derecho Ad Valorem 6% sobre CIF; IVA 19% sobre (CIF + Derechos), NO sobre CIF solo.
    assert r["derechos"] == 2700.0, r
    assert r["base_iva"] == 47700.0, r
    assert r["iva"] == 9063.0, r                  # 19% × 47.700 — NO 8.550 (19% × 45.000)
    assert r["total_impuestos"] == 11763.0, r
    # Parametrización TLC: 0% ad valorem → IVA solo sobre CIF.
    r0 = cc.compute_import_taxes(45000, ad_valorem_rate=0.0)
    assert r0["derechos"] == 0.0 and r0["iva"] == round(45000 * 0.19, 2), r0
    # Extracción de CIF en distintos formatos.
    assert cc.extract_cif("importar por US$ 45.000 desde China") == 45000.0
    assert cc.extract_cif("valor CIF de 16.200 dólares") == 16200.0
    assert cc.extract_cif("una consulta sin montos") is None
    md = cc.format_calculation_md(r)
    assert "9,063.00" in md and "Base legal" in md and "TLC" in md
    print("OK Test 1: Capa 3 — IVA sobre (CIF + Derechos) correcto + extracción CIF + TLC")


def test_intent_classifier():
    assert fr.is_operative_query("Audita la operación de importación desde Rusia") is True
    assert fr.is_operative_query("Revisa la operación 2025-DIN-5582") is True
    assert fr.is_operative_query("¿Cómo se clasifica un dron industrial en el arancel?") is False
    assert fr.is_operative_query("Calcula impuestos de importar drones por US$ 45.000 desde China") is False
    assert fr.requested_label("Audita la operación desde Rusia") == "Rusia"
    assert fr.requested_label("Revisa la 2025-DIN-5582") == "2025-DIN-5582"
    print("OK Test 2: clasificador de intención — operativa vs genérica")


async def test_operative_refusal():
    # La operación de Rusia NO está cargada → frustración estricta, SIN LLM.
    async def fake_ops():
        return {"operations": ["2025-DIN-5582"], "countries": ["India"]}
    database_mod.get_available_operations = fake_ops

    engine = RAGEngine(FakeVS())
    res = await engine.query(
        "Audita la operación de importación desde Rusia",
        filter_collection="normativa", user_id=None,
    )
    ans = res["answer"]
    assert "fuera del alcance del entorno actual de Agentia" in ans, ans
    assert "Rusia" in ans and "India" in ans, ans
    assert ans != NOT_FOUND_RESPONSE
    assert "Orientativo" not in ans, "una consulta operativa NO debe caer en Capa 2"
    print("OK Test 3: Operación Rusia → frustración estricta (sin LLM, sin fundacional)")


async def test_drones_foundational():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("WARN Test 4: ANTHROPIC_API_KEY ausente → caso Drones OMITIDO (best-effort)")
        return
    engine = RAGEngine(FakeVS())
    res = await engine.query(
        "¿Cómo se clasifica arancelariamente un dron industrial y cuánto se paga "
        "de impuestos al importarlo por US$ 45.000?",
        filter_collection="normativa", user_id=None,
    )
    ans = res["answer"]
    print("  --- Informe Drones (Capa 2 + 3) ---")
    print("  " + ans.replace("\n", "\n  ")[:700])
    # Cero vacío
    assert ans and ans != NOT_FOUND_RESPONSE, "no debe devolver NOT_FOUND"
    # Capa 2: disclaimer obligatorio al inicio
    assert ans.startswith(cc.DISCLAIMER), "falta el disclaimer de la Capa 2"
    # Capa 3: cálculo determinista anexado con el IVA correcto
    assert "Cálculo de tributos" in ans and "9,063.00" in ans, "falta/erróneo el cálculo Capa 3"
    # Clasificación presente (token flexible)
    low = ans.lower()
    assert any(t in low for t in ("partida", "8806", "aeronave", "dron")), "sin clasificación arancelaria"
    print("OK Test 4: Drones → Capa 2 fundacional + disclaimer + Capa 3 (IVA correcto)")


async def main():
    print("=== #AGENTIA-204 — RAG Híbrida 3 capas (Fallback Cognitivo + Compliance CL) ===\n")
    test_compliance_math()
    test_intent_classifier()
    await test_operative_refusal()
    await test_drones_foundational()
    print("\n=== TESTS DETERMINISTAS PASARON ===")


if __name__ == "__main__":
    asyncio.run(main())
