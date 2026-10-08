"""
Laporan buku kas seragam — meniru format buku kas reconbot (rekonbuku.py / reconcile.py),
dalam Excel dan PDF. Dipakai /statement (per kantong) dan /rekap.

Kolom (10, sama dengan reconbot):
  Tanggal | Items / Activities | Kategori Transaksi | Debit | Kredit | Saldo Kumulatif |
  Subjek Transaksi | Objek Transaksi | Keterangan Tambahan | Catatan Pribadi

Konvensi reconbot yang dipertahankan:
  - Debit  = uang KELUAR, ditulis sebagai angka NEGATIF
  - Kredit = uang MASUK, angka positif
  - Saldo Kumulatif = rumus  =D{r}+E{r}+F{r-1}
  - Baris 1 = header, baris 2 = "Saldo Awal Bulan", transaksi mulai baris 3,
    satu baris kosong, lalu footer (Saldo Awal, Total Debit, Total Kredit, Saldo Akhir)

Tambahan khusus kantong: nama pemilik kantong (nama sheet "Kantong <Nama>", label Subjek/Objek,
judul PDF, dan baris terakhir footer). Baris 1 tetap header sehingga file bisa dibaca reconbot.
"""
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from bot.config import settings
from bot.utils.formatters import fmt_date

STD_HEADER = [
    "Tanggal", "Items / Activities", "Kategori Transaksi", "Debit", "Kredit", "Saldo Kumulatif",
    "Subjek Transaksi", "Objek Transaksi", "Keterangan Tambahan", "Catatan Pribadi",
]
STD_WIDTHS = [12, 34, 20, 15, 15, 18, 20, 24, 36, 30]
NUMBER_FORMAT = '_-"Rp"* #,##0.00_-;("Rp"* #,##0.00);_-"Rp"* "-"??_-;_-@_-'

# Kategori stoabot -> daftar kategori resmi reconbot (_OFFICIAL_LAYER1_CATEGORIES).
# Yang tidak ada di sini SENGAJA dikosongkan (konvensi reconbot: diputuskan manual).
KATEGORI_MAP = {
    "pasar": "Belanja Bahan",
    "bahan baku": "Belanja Bahan",
    "operasional": "Overhead",
    "penjualan": "Penjualan",
}


@dataclass
class KasRow:
    tanggal: date
    uraian: str
    debit: Optional[Decimal] = None    # uang keluar, NEGATIF
    kredit: Optional[Decimal] = None   # uang masuk, positif
    kategori: str = ""
    subjek: str = ""
    objek: str = ""
    ket: str = ""
    catatan: str = ""


def label_kantong(nama: str) -> str:
    return f"Kantong {nama.strip()}" if nama and nama.strip() else "Kantong"


def map_kategori(kategori: Optional[str]) -> str:
    return KATEGORI_MAP.get((kategori or "").strip().lower(), "")


_RE_DARI_KANTONG = re.compile(r"\bdari\s+kantong\s+([A-Za-z]+)", re.I)
_RE_KE_KANTONG = re.compile(r"\b(?:ke|masuk)\s+kantong\s+([A-Za-z]+)", re.I)


# Keterangan uang masuk yang jelas menyebut sumbernya (tanpa pola "dari kantong X").
SUMBER_DIKENAL = {"kasir": "Kasir", "bank biru": "Bank Biru", "owner": "Owner", "bos": "Owner", "stoa space": "Stoa Space"}


def _sumber_dikenal() -> dict[str, str]:
    """Sumber tetap + rekening bank dari settings (nama sheet reconbot, apa adanya)."""
    peta = dict(SUMBER_DIKENAL)
    for b in settings.bank_labels:
        peta[b.lower()] = b
    return peta


def pihak(uraian: str, tx_type: str, owner_label: str) -> tuple[str, str]:
    """(Subjek, Objek) dari keterangan stoabot. Konvensi reconbot: Subjek = pengirim, Objek = penerima.
    Keluar: kantong -> toko/pihak ("Toko — Item"). Masuk: pihak/kantong lain -> kantong ini."""
    teks = (uraian or "").strip()
    if tx_type == "keluar":
        m = _RE_KE_KANTONG.search(teks)
        if m:
            return owner_label, f"Kantong {m.group(1).title()}"
        toko = teks.split(" — ")[0].strip() if " — " in teks else ""
        return owner_label, toko or "-"
    m = _RE_DARI_KANTONG.search(teks)
    if m:
        return f"Kantong {m.group(1).title()}", owner_label
    return _sumber_dikenal().get(teks.lower(), "-"), owner_label


_RE_QTY = re.compile(
    r"(?:\bx\s*\d+(?:[.,]\d+)?\b|\(\s*\d+\s*item\s*\)|\b\d+(?:[.,]\d+)?\s*(?:kg|gr?|gram|ml|l|ltr|pcs|pack|bks|biji|ikat)\b|\b\d+[/¼½¾⅛]+\d*\b|[¼½¾⅛])",
    re.I)


def normalisasi_item(teks: str) -> str:
    """Nama item dari keterangan 'Toko — Item', tanpa jumlah/ukuran, huruf kecil."""
    item = (teks or "").split(" — ", 1)[-1]
    item = _RE_QTY.sub(" ", item.lower())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", item).split())


class KamusKategori:
    """Kategori belanja dari katalog bahan bersama (shared.bahan + bahan_alias).
    HANYA bahan yang sudah punya kategori reconbot (Belanja Bahan / Kemasan) yang dipakai;
    bahan menuplan tanpa kategori recon (mis. obat, alat tulis) sengaja tidak dipetakan."""

    def __init__(self, alias_ke_kategori: dict[str, str]):
        self.exact = {normalisasi_item(a): k for a, k in alias_ke_kategori.items() if normalisasi_item(a)}
        # alias >= 5 huruf yang muncul utuh (batas kata) di nama item; alias terpanjang menang
        self.frasa = sorted((a for a in self.exact if len(a) >= 5), key=len, reverse=True)

    def cari(self, uraian: str) -> str:
        item = normalisasi_item(uraian)
        if not item:
            return ""
        if item in self.exact:
            return self.exact[item]
        padded = f" {item} "
        for a in self.frasa:
            if f" {a} " in padded:
                return self.exact[a]
        return ""


def kategori_transfer(subjek: str, objek: str, owner_label: str) -> str:
    """"Transaksi Internal" kalau lawan transaksinya Kasir atau kantong lain (bukan toko/pihak luar),
    supaya reconbot memperlakukannya sebagai kandidat pencocokan transfer antar sheet."""
    lawan = objek if subjek == owner_label else subjek
    if lawan == owner_label or not lawan or lawan == "-":
        return ""
    l = lawan.lower()
    internal = l in ("kasir", "bank biru") or l.startswith("kantong ") or l in {b.lower() for b in settings.bank_labels}
    return "Transaksi Internal" if internal else ""


async def muat_kamus_kategori(session) -> "KamusKategori":
    """Muat sumber kategori dari Postgres induk. Urutan prioritas (yang belakangan menang):
    shared.bahan (hanya yang sudah berkategori recon) < recon.kamus_bahan < recon.kamus_overhead,
    sehingga pemetaan asli reconbot selalu menang. Sumber yang gagal dimuat (mis. DB lama tanpa schema
    shared/recon) dilewati; laporan tetap jalan, kategorinya saja yang tidak terisi otomatis."""
    from sqlalchemy import text
    sumber = (
        "SELECT a.match, b.kategori_recon FROM shared.bahan_alias a JOIN shared.bahan b ON b.kode = a.bahan_kode "
        "WHERE b.kategori_recon IN ('Belanja Bahan', 'Kemasan')",
        "SELECT alias, kategori FROM recon.kamus_bahan",
        "SELECT alias, kategori FROM recon.kamus_overhead",
    )
    peta: dict[str, str] = {}
    for sql in sumber:
        try:
            for alias, kategori in (await session.execute(text(sql))).all():
                if alias and kategori:
                    peta[alias] = kategori
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
    return KamusKategori(peta)


def dari_transaksi(tx, owner_label: str, kamus: "KamusKategori | None" = None) -> KasRow:
    """Transaction stoabot -> KasRow. keluar = Debit negatif, masuk = Kredit positif."""
    amt = Decimal(str(tx.amount))
    keluar = tx.type == "keluar"
    subjek, objek = pihak(tx.description, tx.type, owner_label)
    return KasRow(
        tanggal=tx.transaction_date, uraian=tx.description,
        debit=-amt if keluar else None, kredit=None if keluar else amt,
        kategori=(map_kategori(tx.category)
                  or (kamus.cari(tx.description) if kamus and keluar else "")
                  or kategori_transfer(subjek, objek, owner_label)),
        subjek=subjek, objek=objek,
        ket="Struk terlampir" if getattr(tx, "attachments", None) else "",
    )


def periode_label(date_from: date, date_to: date) -> str:
    return fmt_date(date_from) if date_from == date_to else f"{fmt_date(date_from)} s/d {fmt_date(date_to)}"


def hitung_saldo(rows: list[KasRow], saldo_awal: Decimal) -> list[Decimal]:
    """Saldo kumulatif per baris (urutan input dipertahankan)."""
    saldo = Decimal(saldo_awal)
    out = []
    for r in rows:
        saldo += (r.debit or 0) + (r.kredit or 0)
        out.append(saldo)
    return out


def ringkas(rows: list[KasRow], saldo_awal: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    total_debit = Decimal(sum((r.debit or 0) for r in rows))
    total_kredit = Decimal(sum((r.kredit or 0) for r in rows))
    return total_debit, total_kredit, Decimal(saldo_awal) + total_debit + total_kredit


def _nama_sheet(owner: str) -> str:
    """Nama sheet = nama pemilik SAJA, tanpa awalan "Kantong". reconbot mencocokkan Subjek/Objek ke
    nama sheet lewat token terpanjang (resolve_account_sheet); awalan "Kantong" yang dipakai semua
    sheet membuat semua kantong terpetakan ke sheet pertama."""
    s = re.sub(r"[\[\]:*?/\\]", " ", (owner or "").strip() or "Kantong")
    return re.sub(r"\s+", " ", s).strip()[:31]


def _tulis_sheet(ws, owner: str, rows: list[KasRow], saldo_awal: Decimal):
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    for c, v in enumerate(STD_HEADER, start=1):
        ws.cell(row=1, column=c, value=v).font = Font(bold=True)
    opening = float(saldo_awal)
    ws.cell(row=2, column=2, value="Saldo Awal")
    ws.cell(row=2, column=3, value="Saldo Awal Bulan")
    ws.cell(row=2, column=4 if opening < 0 else 5, value=opening)
    ws.cell(row=2, column=6, value=opening)
    ws.cell(row=2, column=7, value="-")
    ws.cell(row=2, column=8, value="-")
    for c in (4, 5, 6):
        ws.cell(row=2, column=c).number_format = NUMBER_FORMAT

    r = 3
    for row in rows:
        vals = [
            row.tanggal, row.uraian, row.kategori or None,
            float(row.debit) if row.debit else None,
            float(row.kredit) if row.kredit else None,
            f"=D{r}+E{r}+F{r - 1}",
            row.subjek or None, row.objek or None, row.ket or None, row.catatan or None,
        ]
        for c, v in enumerate(vals, start=1):
            ws.cell(row=r, column=c, value=v)
        ws.cell(row=r, column=1).number_format = "dd/mm/yyyy"
        for c in (4, 5, 6):
            ws.cell(row=r, column=c).number_format = NUMBER_FORMAT
        r += 1
    last = r - 1
    r += 1  # satu baris kosong
    footer = [
        ("Saldo Awal", 6, "=F2"),
        ("Total Debit (Uang Keluar)", 4, f"=SUM(D3:D{last})"),
        ("Total Kredit (Uang Masuk)", 5, f"=SUM(E3:E{last})"),
        ("Saldo Akhir", 6, f"=F2+D{r + 1}+E{r + 2}"),
    ]
    for label, col, val in footer:
        ws.cell(row=r, column=2, value=label)
        cell = ws.cell(row=r, column=col, value=val)
        cell.number_format = NUMBER_FORMAT
        r += 1
    ws.cell(row=r, column=2, value="Pemilik Kantong").font = Font(bold=True)
    ws.cell(row=r, column=3, value=owner)
    for c, w in enumerate(STD_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:J{max(last, 2)}"


def generate_xlsx(kantong: list[tuple[str, list[KasRow], Decimal]]) -> bytes:
    """Satu workbook, satu sheet per kantong: [(nama_pemilik, rows, saldo_awal), ...]."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    dipakai: set[str] = set()
    for owner, rows, saldo_awal in kantong:
        nama = base = _nama_sheet(owner)
        i = 2
        while nama.lower() in dipakai:
            nama = f"{base[:28]} {i}"
            i += 1
        dipakai.add(nama.lower())
        _tulis_sheet(wb.create_sheet(nama), owner, rows, saldo_awal)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _uang(v) -> str:
    if v is None or v == 0:
        return "-"
    n = int(round(abs(float(v))))
    teks = f"Rp {n:,}".replace(",", ".")
    return f"({teks})" if v < 0 else teks


def generate_pdf(owner: str, rows: list[KasRow], saldo_awal: Decimal, periode: str) -> bytes:
    """PDF landscape dengan tabel 10 kolom yang sama + footer, judul memuat nama pemilik kantong."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    cell = ParagraphStyle("c", parent=ss["Normal"], fontSize=6.5, leading=8)
    judul = ParagraphStyle("t", parent=ss["Heading1"], fontSize=14, spaceAfter=2)
    meta = ParagraphStyle("m", parent=ss["Normal"], fontSize=9, leading=12)

    saldo = hitung_saldo(rows, saldo_awal)
    data = [STD_HEADER]
    data.append(["", Paragraph("Saldo Awal", cell), Paragraph("Saldo Awal Bulan", cell),
                 _uang(saldo_awal) if saldo_awal < 0 else "", _uang(saldo_awal) if saldo_awal >= 0 else "",
                 _uang(saldo_awal), "-", "-", "", ""])
    for r, s in zip(rows, saldo):
        data.append([
            fmt_date(r.tanggal), Paragraph(r.uraian, cell), Paragraph(r.kategori or "", cell),
            _uang(r.debit), _uang(r.kredit), _uang(s),
            Paragraph(r.subjek or "", cell), Paragraph(r.objek or "", cell),
            Paragraph(r.ket or "", cell), Paragraph(r.catatan or "", cell),
        ])
    td, tk, akhir = ringkas(rows, saldo_awal)
    data.append(["", "", "", "", "", "", "", "", "", ""])
    for label, col, val in (("Saldo Awal", 5, saldo_awal), ("Total Debit (Uang Keluar)", 3, td),
                            ("Total Kredit (Uang Masuk)", 4, tk), ("Saldo Akhir", 5, akhir)):
        row = [""] * 10
        row[1] = label
        row[col] = _uang(val)
        data.append(row)
    n_total = len(data)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=0.8 * cm, rightMargin=0.8 * cm,
                            topMargin=0.8 * cm, bottomMargin=0.8 * cm)
    widths = [1.8, 4.3, 2.6, 2.2, 2.2, 2.4, 2.6, 2.8, 3.0, 2.8]
    scale = (landscape(A4)[0] - 1.6 * cm) / (sum(widths) * cm)
    table = Table(data, colWidths=[w * cm * scale for w in widths], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 6.5),
        ("FONTSIZE", (0, 1), (-1, -1), 6.5),
        ("ALIGN", (3, 1), (5, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, n_total - 6), 0.3, colors.grey),
        ("FONTNAME", (1, n_total - 4), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, n_total - 1), (-1, n_total - 1), colors.HexColor("#eeeeee")),
    ]))
    doc.build([
        Paragraph(settings.business_name, judul),
        Paragraph("LAPORAN BUKU KAS", meta),
        Paragraph(f"<b>Pemilik Kantong:</b> {owner}", meta),
        Paragraph(f"<b>Periode:</b> {periode}", meta),
        Spacer(1, 0.3 * cm),
        table,
    ])
    return buf.getvalue()
