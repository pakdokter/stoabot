"""
PDF Statement Generator
Menghasilkan rekening koran format bank profesional menggunakan ReportLab.
"""
import io
from datetime import date
from decimal import Decimal
from typing import list

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable,
)

from bot.config import settings
from bot.models import Transaction
from bot.utils.formatters import fmt_rupiah, fmt_date_full, fmt_date


# ──────────────────────────────────────────────
# Styles
# ──────────────────────────────────────────────

def _build_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#1a1a2e"),
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#4a4a6a"),
            alignment=TA_CENTER,
            spaceAfter=2,
        ),
        "header": ParagraphStyle(
            "header",
            parent=styles["Normal"],
            fontSize=9,
            textColor=colors.white,
            alignment=TA_CENTER,
        ),
        "cell": ParagraphStyle(
            "cell",
            parent=styles["Normal"],
            fontSize=8,
            textColor=colors.HexColor("#1a1a2e"),
        ),
        "cell_right": ParagraphStyle(
            "cell_right",
            parent=styles["Normal"],
            fontSize=8,
            alignment=TA_RIGHT,
        ),
        "summary_label": ParagraphStyle(
            "summary_label",
            parent=styles["Normal"],
            fontSize=9,
            textColor=colors.HexColor("#4a4a6a"),
        ),
        "summary_value": ParagraphStyle(
            "summary_value",
            parent=styles["Normal"],
            fontSize=9,
            fontName="Helvetica-Bold",
            alignment=TA_RIGHT,
        ),
    }


# ──────────────────────────────────────────────
# Build PDF
# ──────────────────────────────────────────────

def generate_statement_pdf(
    transactions: list[Transaction],
    date_from: date,
    date_to: date,
    saldo_awal: Decimal = Decimal("0"),
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = _build_styles()
    story = []

    # ── Header bisnis ──
    story.append(Paragraph(settings.business_name.upper(), styles["title"]))
    if settings.business_address:
        story.append(Paragraph(settings.business_address, styles["subtitle"]))
    if settings.business_phone:
        story.append(Paragraph(settings.business_phone, styles["subtitle"]))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a1a2e")))
    story.append(Spacer(1, 0.2 * cm))

    # ── Periode ──
    story.append(Paragraph(
        f"LAPORAN KEUANGAN PERIODE {fmt_date_full(date_from).upper()} s/d {fmt_date_full(date_to).upper()}",
        styles["subtitle"],
    ))
    story.append(Spacer(1, 0.5 * cm))

    # ── Ringkasan ──
    total_masuk = sum(Decimal(str(t.amount)) for t in transactions if t.type == "masuk")
    total_keluar = sum(Decimal(str(t.amount)) for t in transactions if t.type == "keluar")
    saldo_akhir = saldo_awal + total_masuk - total_keluar

    summary_data = [
        ["Saldo Awal Periode", fmt_rupiah(saldo_awal)],
        ["Total Pemasukan", fmt_rupiah(total_masuk)],
        ["Total Pengeluaran", fmt_rupiah(total_keluar)],
        ["Saldo Akhir Periode", fmt_rupiah(saldo_akhir)],
    ]
    summary_table = Table(summary_data, colWidths=[8 * cm, 7 * cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f5fb")),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 3), (-1, 3), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 0), (-1, 2), [colors.HexColor("#f5f5fb"), colors.white]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#ccccdd")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#ccccdd")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.6 * cm))

    # ── Tabel transaksi ──
    header = ["Tgl", "Keterangan", "Masuk (Rp)", "Keluar (Rp)", "Saldo (Rp)"]
    col_widths = [2.2 * cm, 7.8 * cm, 3.2 * cm, 3.2 * cm, 3.2 * cm]

    rows = [header]
    running = saldo_awal
    for tx in sorted(transactions, key=lambda t: t.transaction_date):
        if tx.type == "masuk":
            running += Decimal(str(tx.amount))
            masuk = fmt_rupiah(tx.amount)
            keluar = "-"
        else:
            running -= Decimal(str(tx.amount))
            masuk = "-"
            keluar = fmt_rupiah(tx.amount)

        rows.append([
            fmt_date(tx.transaction_date),
            tx.description[:50],
            masuk,
            keluar,
            fmt_rupiah(running),
        ])

    tx_table = Table(rows, colWidths=col_widths, repeatRows=1)
    tx_table.setStyle(TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        # Body
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 7.5),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9f9fd")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#ccccdd")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#ddddee")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tx_table)

    # ── Footer ──
    story.append(Spacer(1, 0.5 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#ccccdd")))
    from datetime import datetime
    story.append(Paragraph(
        f"Dokumen ini dibuat otomatis oleh sistem pada {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        ParagraphStyle("footer", parent=getSampleStyleSheet()["Normal"],
                       fontSize=7, textColor=colors.grey, alignment=TA_CENTER),
    ))

    doc.build(story)
    return buffer.getvalue()
