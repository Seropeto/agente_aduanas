"""
Validación local — AGENT-003: System Prompt externalizado + Protocolo de Frustración Elegante.
Ejecutar: python test_agent003_frustration.py

Tests deterministas (sin red) — son la compuerta:
  1. El System Prompt se lee desde archivo (NO hardcoded): cambiar la ruta cambia el prompt.
  2. build_frustration_response() devuelve la plantilla EXACTA exigida.
  3. detect_scope(): Rusia (no cargada) -> fuera de alcance; India/DIN cargada -> en alcance.
  4. analyze_with_protocol() para Rusia -> plantilla EXACTA (sin LLM).

Test best-effort (requiere ANTHROPIC_API_KEY):
  5. India en alcance -> el LLM clasifica (9026 vs 9032), NO dispara frustración.
"""
import asyncio
import os
import tempfile

from backend.rag import frustration as fr

# Plantilla exacta esperada para Rusia con India como única operación cargada.
EXPECTED_RUSIA = (
    "La consulta solicitada se encuentra fuera del alcance del entorno actual de "
    "Agentia. El análisis de operaciones para Rusia requiere la inyección previa de "
    "la Carpeta de Despacho y documentación de respaldo en el sistema. Actualmente, "
    "solo se encuentran validadas las operaciones asociadas a India."
)


def test_prompt_externalizado():
    # Carga desde el archivo canónico
    prompt = fr.load_system_prompt()
    assert prompt and "PROTOCOLO DE FRUSTRACIÓN ELEGANTE" in prompt
    assert "fuera del alcance del entorno actual de Agentia" in prompt
    assert "90.26" in prompt and "90.32" in prompt

    # Prueba de externalización: si apunto la ruta a otro archivo, el prompt cambia
    # -> demuestra que NO está hardcoded en el código.
    original = fr._PROMPT_PATH
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write("PROMPT-ALTERNO-DE-PRUEBA")
            tmp_path = tmp.name
        fr._PROMPT_PATH = tmp_path
        assert fr.load_system_prompt() == "PROMPT-ALTERNO-DE-PRUEBA"
    finally:
        fr._PROMPT_PATH = original
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    print("OK Test 1: System Prompt leído desde archivo (externalizado, NO hardcoded)")


def test_plantilla_exacta():
    out = fr.build_frustration_response("Rusia", ["India"])
    assert out == EXPECTED_RUSIA, f"\nESPERADO:\n{EXPECTED_RUSIA}\nOBTENIDO:\n{out}"
    # Caso sin operaciones cargadas
    vacio = fr.build_frustration_response("Rusia", [])
    assert "ninguna operación cargada actualmente" in vacio
    print("OK Test 2: build_frustration_response() devuelve la plantilla EXACTA")


def test_detect_scope():
    # Rusia no está entre los países cargados -> fuera de alcance
    in_scope, label = fr.detect_scope(
        "Audita la operación de importación desde Rusia",
        available_countries=["India"], available_operations=["2025-DIN-5582"],
    )
    assert in_scope is False and label == "Rusia", (in_scope, label)

    # Consulta sobre una DIN cargada -> en alcance
    in_scope2, _ = fr.detect_scope(
        "Clasifica la operación 2025-DIN-5582 de India",
        available_countries=["India"], available_operations=["2025-DIN-5582"],
    )
    assert in_scope2 is True

    # País cargado -> en alcance
    in_scope3, _ = fr.detect_scope(
        "Revisa la operación de India",
        available_countries=["India"], available_operations=["2025-DIN-5582"],
    )
    assert in_scope3 is True
    print("OK Test 3: detect_scope() — Rusia fuera de alcance, India/DIN en alcance")


async def test_protocolo_rusia():
    # Fuera de alcance -> plantilla exacta, SIN llamar al LLM.
    out = await fr.analyze_with_protocol(
        query="Audita la operación de Rusia",
        context_text="",  # no hay carpeta de despacho de Rusia
        available_labels=["India"],
        requested_label="Rusia",
    )
    assert out == EXPECTED_RUSIA, f"\nESPERADO:\n{EXPECTED_RUSIA}\nOBTENIDO:\n{out}"
    print("OK Test 4: analyze_with_protocol(Rusia) -> Protocolo de Frustración EXACTO (sin LLM)")


async def test_clasificacion_india():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("WARN Test 5: ANTHROPIC_API_KEY ausente -> clasificación India OMITIDA (best-effort)")
        return
    contexto = (
        "[Operación: 2025-DIN-5582 | Origen: India]\n"
        "Mercancía: transmisor de presión industrial modelo PT-100. Mide la presión "
        "de línea (0-100 bar) y la despliega en un visor digital. NO posee lazo de "
        "control ni actúa sobre válvulas; únicamente mide y muestra la variable.\n\n"
        "Normativa: Partida 90.26 — instrumentos para la medida de presión. "
        "Partida 90.32 — instrumentos de regulación o control automáticos (con lazo "
        "de retroalimentación). RGI 3 b) carácter esencial para artículos compuestos."
    )
    out = await fr.analyze_with_protocol(
        query="¿En qué partida clasifica esta mercancía, 90.26 o 90.32? Fundamenta.",
        context_text=contexto,
        available_labels=["India"],
        requested_label="India",
    )
    print("  --- Dictamen India (LLM) ---")
    print("  " + out.replace("\n", "\n  ")[:600])
    assert "fuera del alcance del entorno actual de Agentia" not in out, "no debió frustrarse en alcance"
    assert ("9026" in out or "90.26" in out or "9032" in out or "90.32" in out), "debió clasificar"
    print("OK Test 5: India en alcance -> el LLM clasifica (no frustración)")


async def main():
    print("=== AGENT-003 — Prompt externalizado + Protocolo de Frustración Elegante ===\n")
    test_prompt_externalizado()
    test_plantilla_exacta()
    test_detect_scope()
    await test_protocolo_rusia()
    await test_clasificacion_india()
    print("\n=== TESTS DETERMINISTAS PASARON ===")


if __name__ == "__main__":
    asyncio.run(main())
