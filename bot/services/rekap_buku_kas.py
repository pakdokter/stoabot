"""
Rekap buku kas 9 kolom ("REKAP BUKU KAS") — format yang DIBACA reconbot sebagai Catatan Grup
(catatan_grup.py: header 'No. Bukti', 'Uraian', 'Sumber', 'Debit (Masuk)', 'Kredit (Keluar)').

PERHATIAN: arti Debit/Kredit di sini KEBALIKAN dari buku kas 10 kolom (kas_report.py):
  Debit (Masuk) = uang masuk, Kredit (Keluar) = uang keluar, keduanya angka positif.
Isi sel berupa angka (bukan rumus), karena reconbot membacanya dengan data_only=True.
Jangan diubah tanpa menjalankan pembaca asli reconbot (catatan_grup.parse_catatan_file).

Kolom: 1 No | 2 Tanggal | 3 No. Bukti | 4 Uraian | 5 Kategori | 6 Sumber | 7 Debit (Masuk)
       8 Kredit (Keluar) | 9 Saldo
"""
import io
from decimal import Decimal

from bot.config import settings
from bot.services.rekap_service import RekapEntry
from bot.utils.formatters import fmt_date, fmt_rupiah

COLUMNS = [
    "No", "Tanggal", "No. Bukti", "Uraian", "Kategori",
    "Sumber", "Debit (Masuk)", "Kredit (Keluar)", "Saldo",
]


def build_rows(entries: list[RekapEntry], saldo_awal: Decimal = Decimal(0)) -> list[list]:
    """Susun baris 9 kolom, urut tanggal. Baris pertama (No kosong) = Saldo Awal."""
    ordered = sorted(entries, key=lambda e: e.tanggal)  # stable: urutan forward dipertahankan
    counters: dict[str, int] = {}
    saldo = Decimal(saldo_awal)
    rows: list[list] = [["", "", "", "Saldo Awal", "", "", None, None, saldo]]
    for i, e in enumerate(ordered, 1):
        prefix = f"{'KM' if e.type == 'masuk' else 'KK'}-{e.tanggal:%y%m%d}"
        counters[prefix] = counters.get(prefix, 0) + 1
        amt = Decimal(str(e.amount))
        debit = amt if e.type == "masuk" else None
        kredit = amt if e.type == "keluar" else None
        saldo += (debit or 0) - (kredit or 0)
        rows.append([
            i, e.tanggal, f"{prefix}-{counters[prefix]:02d}", e.uraian,
            e.kategori, e.sumber, debit, kredit, saldo,
        ])
    return rows


def totals(rows: list[list]) -> tuple[Decimal, Decimal, Decimal]:
    debit = sum((r[6] or 0) for r in rows[1:])
    kredit = sum((r[7] or 0) for r in rows[1:])
    return Decimal(debit), Decimal(kredit), rows[-1][8]


def _periode(rows: list[list]) -> str:
    dates = [r[1] for r in rows[1:]]
    if not dates:
        return ""
    lo, hi = min(dates), max(dates)
    return fmt_date(lo) if lo == hi else f"{fmt_date(lo)} s/d {fmt_date(hi)}"


def generate_xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Buku Kas"
    ws["A1"] = settings.business_name
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "REKAP BUKU KAS"
    ws["A2"].font = Font(bold=True, size=12)
    ws["A3"] = f"Periode: {_periode(rows)}"

    hdr_row = 5
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c, name in enumerate(COLUMNS, 1):
        cell = ws.cell(row=hdr_row, column=c, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1A1A2E")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    r0 = hdr_row + 1
    for i, row in enumerate(rows):
        r = r0 + i
        for c, val in enumerate(row, 1):
            if isinstance(val, Decimal):
                val = float(val)
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = border
            if c in (7, 8, 9):
                cell.number_format = "#,##0"
            elif c == 2:
                cell.number_format = "DD/MM/YYYY"
            elif c == 4:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        if i == 0:
            ws.cell(row=r, column=4).font = Font(bold=True)
        else:
            # Saldo = saldo sebelumnya + debit - kredit (formula, mudah diaudit)
            ws.cell(row=r, column=9, value=f"=I{r-1}+N(G{r})-N(H{r})")

    last = r0 + len(rows) - 1
    tot = last + 1
    ws.cell(row=tot, column=4, value="TOTAL").font = Font(bold=True)
    ws.cell(row=tot, column=7, value=f"=SUM(G{r0+1}:G{last})")
    ws.cell(row=tot, column=8, value=f"=SUM(H{r0+1}:H{last})")
    ws.cell(row=tot, column=9, value=f"=I{last}")
    for c in range(1, 10):
        cell = ws.cell(row=tot, column=c)
        cell.font = Font(bold=True)
        cell.border = border
        if c >= 7:
            cell.number_format = "#,##0"

    for col, w in zip("ABCDEFGHI", (5, 12, 16, 40, 14, 18, 16, 16, 16)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = ws.cell(row=r0, column=1)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_pdf(rows: list[list]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    cell = ParagraphStyle("c", parent=ss["Normal"], fontSize=8)
    title = ParagraphStyle("t", parent=ss["Heading1"], fontSize=14, spaceAfter=2)

    def money(v):
        return "" if v is None else fmt_rupiah(v).replace("Rp", "")

    data = [COLUMNS]
    for r in rows:
        data.append([
            r[0], fmt_date(r[1]) if r[1] else "", r[2],
            Paragraph(str(r[3]), cell), Paragraph(str(r[4]), cell), Paragraph(str(r[5]), cell),
            money(r[6]), money(r[7]), money(r[8]),
        ])
    d, k, s = totals(rows)
    data.append(["", "", "", "TOTAL", "", "", money(d), money(k), money(s)])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4), leftMargin=1 * cm, rightMargin=1 * cm,
        topMargin=1 * cm, bottomMargin=1 * cm,
    )
    widths = [1 * cm, 2.3 * cm, 3.2 * cm, 6.5 * cm, 2.8 * cm, 3.2 * cm, 2.9 * cm, 2.9 * cm, 2.9 * cm]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (6, 1), (8, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eeeeee")),
    ]))
    doc.build([
        Paragraph(settings.business_name, title),
        Paragraph(f"REKAP BUKU KAS — {_periode(rows)}", ss["Normal"]),
        Spacer(1, 0.3 * cm),
        table,
    ])
    return buf.getvalue()
