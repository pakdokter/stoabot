"""
PDF Statement Generator
Menghasilkan rekening koran format bank profesional menggunakan ReportLab.
"""
import io
from datetime import date, datetime
from decimal import Decimal

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
# Helpers
# ──────────────────────────────────────────────

def _split_description(desc: str) -> tuple[str, str]:
    """
    Pisahkan deskripsi transaksi menjadi (sumber/toko, keterangan).

    Format dari struk: "Primer Raya — KIIP REC 650 ML (2x)"
    Format dari manual: "Bank Biru" / "Kasir" / "teks bebas"
    Format Shopee/Sukanda: "Sukanda (Stoa Space) — DIAMOND UHT..."
    """
    sep = " — "
    if sep in desc:
        parts = desc.split(sep, 1)
        sumber = parts[0].strip()
        keterangan = parts[1].strip()
    else:
        # Tidak ada separator — seluruh deskripsi jadi sumber, keterangan kosong
        sumber = desc.strip()
        keterangan = ""
    return sumber, keterangan


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
        "user_label": ParagraphStyle(
            "user_label",
            parent=styles["Normal"],
            fontSize=11,
            textColor=colors.HexColor("#1a1a2e"),
            fontName="Helvetica-Bold",
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
            fontSize=7.5,
            textColor=colors.HexColor("#1a1a2e"),
        ),
        "cell_right": ParagraphStyle(
            "cell_right",
            parent=styles["Normal"],
            fontSize=7.5,
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
        "footer": ParagraphStyle(
            "footer",
            parent=styles["Normal"],
            fontSize=7,
            textColor=colors.grey,
            alignment=TA_CENTER,
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
    user_name: str = "",
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
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
    story.append(Spacer(1, 0.2 * cm))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a1a2e")))
    story.append(Spacer(1, 0.2 * cm))

    # ── Nama user ──
    if user_name:
        story.append(Paragraph(f"Laporan Keuangan: {user_name}", styles["user_label"]))
        story.append(Spacer(1, 0.1 * cm))

    # ── Periode ──
    story.append(Paragraph(
        f"PERIODE {fmt_date_full(date_from).upper()} s/d {fmt_date_full(date_to).upper()}",
        styles["subtitle"],
    ))
    story.append(Spacer(1, 0.4 * cm))

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
    story.append(Spacer(1, 0.5 * cm))

    # ── Tabel transaksi ──
    # Kolom: Tgl | Sumber/Toko | Keterangan | Masuk | Keluar | Saldo
    header = ["Tgl", "Sumber / Toko", "Keterangan", "Masuk (Rp)", "Keluar (Rp)", "Saldo (Rp)"]
    col_widths = [1.8*cm, 4.5*cm, 5.0*cm, 2.8*cm, 2.8*cm, 2.8*cm]

    rows = [header]
    running = saldo_awal
    for tx in sorted(transactions, key=lambda t: (t.transaction_date, t.created_at)):
        sumber, keterangan = _split_description(tx.description or "")

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
            sumber[:35],
            keterangan[:40] if keterangan else "-",
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
        ("FONTSIZE", (0, 1), (-1, -1), 7),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),  # kolom nominal rata kanan
        ("ALIGN", (0, 1), (0, -1), "CENTER"),  # tanggal center
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9f9fd")]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#ccccdd")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#ddddee")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tx_table)

    # ── Footer ──
    story.append(Spacer(1, 0.4 * cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#ccccdd")))
    story.append(Paragraph(
        f"Dokumen ini dibuat otomatis oleh sistem pada {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        styles["footer"],
    ))

    doc.build(story)
    return buffer.getvalue()
