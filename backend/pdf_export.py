"""
Exportación de respuestas a PDF con branding Toxiro Digital.
REQ-11: genera un informe en PDF de la consulta, respuesta y fuentes.
"""
import re
from datetime import datetime, timezone
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Paleta Toxiro ────────────────────────────────────────────────────────────
NAVY    = colors.HexColor("#1a2744")
ACCENT  = colors.HexColor("#2563eb")
BG_GREY = colors.HexColor("#f0f4f8")
MID     = colors.HexColor("#64748b")
WHITE   = colors.white

MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
QUERY_TYPE_LABELS = {
    "arancelaria": "Arancelaria",
    "tramite":     "Trámite aduanero",
    "normativa":   "Normativa",
    "general":     "General",
}
CONTENT_TYPE_LABELS = {
    "circular":      "Circular",
    "resolucion":    "Resolución",
    "arancel":       "Arancel",
    "procedimiento": "Procedimiento",
    "ley":           "Ley",
    "decreto":       "Decreto",
    "normativa":     "Normativa",
    "publicacion":   "Publicación",
    "pdf":           "Documento PDF",
    "word":          "Documento Word",
}


def _strip_markdown(text: str) -> str:
    """Convierte markdown a texto plano legible en PDF."""
    if not text:
        return ""
    text = re.sub(r"^#{1,6}\s+(.+)$", lambda m: m.group(1).upper(), text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)
    # Líneas decorativas con caracteres de bloque/caja Unicode que Claude usa como separadores
    text = re.sub(r"^[\s─-╿▀-▟■-◿=_~]{4,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^(\d+)\.\s+", r"\1. ", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s+", "  ", text, flags=re.MULTILINE)
    lines = [l for l in text.split("\n") if not (l.strip().startswith("|") and l.strip().endswith("|"))]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _safe(text: str) -> str:
    """Escapa caracteres especiales de ReportLab."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _make_styles(col_w: float) -> dict:
    return {
        "title": ParagraphStyle(
            "rpt_title",
            fontName="Helvetica-Bold",
            fontSize=18,
            textColor=WHITE,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "rpt_subtitle",
            fontName="Helvetica",
            fontSize=10,
            textColor=colors.HexColor("#cbd5e1"),
            alignment=TA_RIGHT,
        ),
        "meta_label": ParagraphStyle(
            "rpt_meta_label",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=MID,
            spaceAfter=1,
        ),
        "meta_value": ParagraphStyle(
            "rpt_meta_value",
            fontName="Helvetica",
            fontSize=10,
            textColor=NAVY,
            spaceAfter=6,
        ),
        "section_header": ParagraphStyle(
            "rpt_section_header",
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=ACCENT,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "query_text": ParagraphStyle(
            "rpt_query_text",
            fontName="Helvetica-BoldOblique",
            fontSize=11,
            textColor=NAVY,
            leading=16,
        ),
        "body": ParagraphStyle(
            "rpt_body",
            fontName="Helvetica",
            fontSize=10,
            textColor=colors.HexColor("#1e293b"),
            leading=15,
            spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "rpt_bullet",
            fontName="Helvetica",
            fontSize=10,
            textColor=colors.HexColor("#1e293b"),
            leading=14,
            leftIndent=12,
            spaceAfter=2,
        ),
        "source_item": ParagraphStyle(
            "rpt_source_item",
            fontName="Helvetica",
            fontSize=9,
            textColor=colors.HexColor("#334155"),
            leading=13,
            leftIndent=8,
            spaceAfter=3,
        ),
        "disclaimer": ParagraphStyle(
            "rpt_disclaimer",
            fontName="Helvetica-Oblique",
            fontSize=8,
            textColor=MID,
            leading=11,
            spaceBefore=10,
        ),
    }


def generate_pdf(
    query: str,
    answer: str,
    sources: list[dict],
    query_type: str,
    user_name: str = "",
    user_company: str = "",
) -> bytes:
    """
    Genera el PDF de informe de consulta y retorna el contenido binario.

    Returns:
        bytes — contenido PDF listo para enviar como respuesta HTTP.
    """
    buf = BytesIO()
    page_w, _ = A4
    margin = 18 * mm
    col_w  = page_w - 2 * margin

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=12 * mm,
        bottomMargin=20 * mm,
        title="Informe AgentIA Aduanas — Toxiro Digital",
        author="Toxiro Digital",
    )

    st    = _make_styles(col_w)
    story = []

    # ── Fecha legible ─────────────────────────────────────────────────────────
    now   = datetime.now(timezone.utc)
    now_str = f"{now.day} de {MONTHS_ES[now.month]} de {now.year}, {now.strftime('%H:%M')} UTC"

    # ── Cabecera ──────────────────────────────────────────────────────────────
    header_table = Table(
        [[Paragraph("AgentIA Aduanas", st["title"]),
          Paragraph("Toxiro Digital", st["subtitle"])]],
        colWidths=[col_w - 42 * mm, 42 * mm],
    )
    header_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING",   (0, 0), (0, 0),   16),
        ("RIGHTPADDING",  (-1, 0), (-1, -1), 14),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4 * mm))

    # ── Metadatos ─────────────────────────────────────────────────────────────
    meta_rows = [("Fecha de generación", now_str)]
    if user_name:
        meta_rows.append(("Usuario", user_name))
    if user_company:
        meta_rows.append(("Empresa", user_company))
    meta_rows.append(("Tipo de consulta", QUERY_TYPE_LABELS.get(query_type, query_type.title())))

    meta_data = [
        [Paragraph(_safe(k), st["meta_label"]), Paragraph(_safe(v), st["meta_value"])]
        for k, v in meta_rows
    ]
    meta_table = Table(meta_data, colWidths=[35 * mm, col_w - 35 * mm])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), BG_GREY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 5 * mm))

    # ── Consulta ──────────────────────────────────────────────────────────────
    story.append(Paragraph("CONSULTA", st["section_header"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=6))

    query_box = Table(
        [[Paragraph(_safe(query), st["query_text"])]],
        colWidths=[col_w],
    )
    query_box.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ("BOX",           (0, 0), (-1, -1), 1.5, ACCENT),
    ]))
    story.append(query_box)
    story.append(Spacer(1, 5 * mm))

    # ── Respuesta ─────────────────────────────────────────────────────────────
    story.append(Paragraph("RESPUESTA", st["section_header"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=8))

    for line in _strip_markdown(answer).split("\n"):
        line = line.rstrip()
        if not line:
            story.append(Spacer(1, 3 * mm))
        elif line.startswith("• "):
            story.append(Paragraph(_safe(line), st["bullet"]))
        else:
            story.append(Paragraph(_safe(line), st["body"]))

    # ── Fuentes ───────────────────────────────────────────────────────────────
    if sources:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("FUENTES CONSULTADAS", st["section_header"]))
        story.append(HRFlowable(width="100%", thickness=1, color=MID, spaceAfter=6))

        for src in sources:
            type_str = CONTENT_TYPE_LABELS.get(src.get("content_type", ""), "Documento")
            title    = src.get("title", "Documento sin título")
            source   = src.get("source", "")
            date     = src.get("date", "")
            url      = src.get("url", "")

            story.append(Paragraph(_safe(f"[{type_str}] {title}"), st["source_item"]))
            if source:
                story.append(Paragraph(_safe(f"  Origen: {source}"), st["source_item"]))
            if date:
                story.append(Paragraph(_safe(f"  Fecha: {date}"), st["source_item"]))
            if url:
                story.append(Paragraph(_safe(f"  URL: {url}"), st["source_item"]))
            story.append(Spacer(1, 2 * mm))

    # ── Disclaimer ────────────────────────────────────────────────────────────
    story.append(KeepTogether([
        Spacer(1, 6 * mm),
        HRFlowable(width="100%", thickness=0.5, color=MID),
        Paragraph(
            "Este informe es generado automáticamente por AgentIA Aduanas de Toxiro Digital. "
            "La información tiene carácter referencial y no constituye asesoría legal. "
            "Verifique siempre la normativa vigente en www.aduana.cl y "
            "www.diariooficial.interior.gob.cl",
            st["disclaimer"],
        ),
    ]))

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MID)
        canvas.drawString(margin, 10 * mm, "Toxiro Digital | agentia.toxiro.cl")
        canvas.drawRightString(page_w - margin, 10 * mm, f"Página {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
