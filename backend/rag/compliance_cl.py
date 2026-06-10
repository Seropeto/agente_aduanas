"""
Capa 3 — Filtro de cumplimiento tributario chileno (DETERMINISTA).

La aritmética de tributos NO se delega al LLM: se calcula aquí, con la fórmula
correcta de la base imponible del IVA de importación:

    Derecho Ad Valorem = tasa × CIF            (tasa general 6%, 0% con TLC vigente)
    Base imponible IVA = CIF + Derecho Ad Valorem        (NO solo el CIF)
    IVA                = 19% × (CIF + Derechos)

Fundamento: DL 825, Art. 16 letra a) — la base imponible del IVA en las
importaciones es el valor aduanero (CIF) más los gravámenes aduaneros que se
causen en la misma importación. La cita legal viaja como Data Provenance.

Las tasas (6% / 19%) son las vigentes según la normativa indexada (Capa 1); la
tasa Ad Valorem es PARAMETRIZABLE para contemplar preferencias arancelarias (TLC).
"""
import re

AD_VALOREM_GENERAL = 0.06   # tasa general del Arancel Aduanero de Chile
IVA_RATE = 0.19             # DL 825

# Etiqueta obligatoria para todo output de conocimiento paramétrico (Capa 2).
DISCLAIMER = "[Orientativo — Conocimiento paramétrico, sujeto a verificación oficial]"

# Cita legal de la base imponible (Data Provenance de la Capa 3).
BASE_LEGAL_IVA = ("DL 825, Art. 16 letra a): la base imponible del IVA de importación es "
                  "el valor aduanero (CIF) más los gravámenes aduaneros de la misma importación.")


def compute_import_taxes(cif: float, ad_valorem_rate: float = AD_VALOREM_GENERAL) -> dict:
    """Calcula los tributos de internación de forma determinista.

    Returns dict con cif, ad_valorem_rate, derechos, base_iva, iva,
    total_impuestos y total_internacion (todos redondeados a 2 decimales)."""
    cif = float(cif)
    derechos = round(cif * ad_valorem_rate, 2)
    base_iva = round(cif + derechos, 2)
    iva = round(base_iva * IVA_RATE, 2)
    total_impuestos = round(derechos + iva, 2)
    return {
        "cif": round(cif, 2),
        "ad_valorem_rate": ad_valorem_rate,
        "derechos": derechos,
        "base_iva": base_iva,
        "iva": iva,
        "total_impuestos": total_impuestos,
        "total_internacion": round(cif + total_impuestos, 2),
    }


# ── Extracción de un valor CIF desde la consulta (si lo hay) ───────────────────

_NUM = r"(\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_CIF_PATTERNS = [
    re.compile(rf"(?:US\$|USD|CIF|valor(?:\s+CIF)?)\s*\$?\s*{_NUM}", re.I),
    re.compile(rf"{_NUM}\s*(?:USD|US\$|d[oó]lares)", re.I),
]


def _to_float(s: str) -> float:
    """Normaliza un número con separadores chilenos o anglosajones a float."""
    s = s.strip().replace(" ", "")
    if "," in s and "." in s:
        # El último separador es el decimal.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.split(",")[-1]
        s = s.replace(",", "") if len(tail) == 3 else s.replace(",", ".")
    elif s.count(".") >= 1:
        tail = s.rpartition(".")[2]
        if len(tail) == 3:  # punto como separador de miles
            s = s.replace(".", "")
    return float(s)


def extract_cif(query: str) -> float | None:
    """Devuelve el valor CIF/monto en USD mencionado en la consulta, o None."""
    for pat in _CIF_PATTERNS:
        m = pat.search(query or "")
        if m:
            try:
                val = _to_float(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                continue
    return None


def format_calculation_md(res: dict, partida: str | None = None) -> str:
    """Bloque Markdown del cálculo determinista, con base legal y advertencia TLC."""
    pct = f"{res['ad_valorem_rate'] * 100:.0f}%"
    lines = [
        "### Cálculo de tributos de internación (estimado)",
        "",
        "| Concepto | Base | Tasa | Monto (USD) |",
        "|---|---|---|---|",
        f"| Valor CIF | — | — | {res['cif']:,.2f} |",
        f"| Derecho Ad Valorem | CIF | {pct} | {res['derechos']:,.2f} |",
        f"| Base imponible IVA | CIF + Derechos | — | {res['base_iva']:,.2f} |",
        f"| IVA | (CIF + Derechos) | 19% | {res['iva']:,.2f} |",
        f"| **Total tributos** | | | **{res['total_impuestos']:,.2f}** |",
        "",
        f"> **Base legal:** {BASE_LEGAL_IVA}",
        "> ⚠️ La tasa general de Ad Valorem es **6%**; con un **TLC vigente** y certificado "
        "de origen válido puede ser **0%**. Verifique el país de origen y el acuerdo aplicable "
        "antes de liquidar.",
    ]
    if partida:
        lines.insert(1, f"_Subpartida estimada: {partida}_\n")
    return "\n".join(lines)
