"""
Kas-Buku service — export transaksi ke Excel dengan format "Kas-Buku" standar.

Format kolom (persis contoh Kas-Buku Januari 2025 yang benar):
  A Tanggal            -> datetime asli, number_format dd/mm/yyyy (JANGAN string ISO,
                          karena string "2026-01-02" dibaca terbalik oleh Excel locale ID)
  B Items / Activities -> nama item / aktivitas
  C Kategori Transaksi -> Belanja Bahan, Penjualan, Gaji Bulan Ini, dll
  D Debit              -> uang KELUAR (nilai negatif)
  E Kredit             -> uang MASUK (nilai positif)
  F Saldo Kumulatif    -> formula =D{r}+E{r}+F{r-1}
  G Subjek Transaksi   -> "Kasir" (keluar) / "Penjualan Cash" (masuk) / "-"
  H Objek Transaksi    -> vendor / pihak lawan
  I Keterangan Tambahan
  J Catatan Pribadi

Nama sheet & nama file: "Kas-Buku <Bulan> <Tahun>" — BUKAN "Mutasi" / "Kas_Buku".

Baris pertama setelah header = Saldo Awal Bulan (dari saldo sebelum periode).
"""
import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from loguru import logger

# Bulan Indonesia untuk penamaan sheet & file
_BULAN_ID = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni",
    7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}

HEADER = [
    "Tanggal", "Items / Activities", "Kategori Transaksi", "Debit", "Kredit",
    "Saldo Kumulatif", "Subjek Transaksi", "Objek Transaksi",
    "Keterangan Tambahan", "Catatan Pribadi",
]

DATE_FMT = "dd/mm/yyyy"
MONEY_FMT = "#,##0"


def _classify(description: str, is_masuk: bool, category: str | None):
    """
    Petakan transaksi -> (kategori, item, objek, keterangan).
    Konservatif: default 'Belanja Bahan' untuk keluar, 'Penjualan' untuk masuk.
    """
    d = (description or "").lower().strip()
    item = (description or "").strip() or ("Penjualan" if is_masuk else "Pengeluaran")

    if is_masuk:
        if d.startswith("penjualan") or category == "masuk" and "jual" in d:
            return "Penjualan", "Penjualan", "Kasir", "Settled to Kasir"
        if "photobooth" in d or "photobox" in d or "photo booth" in d:
            return "Penjualan", item, "Kasir", "Settled to Kasir"
        if d.startswith("lebih") or d.startswith("plus"):
            return "Transaksi Internal", item, "-", "Selisih lebih kas"
        if "refund" in d or "batal" in d or "sisa tf" in d or "split bayar" in d or "tip" in d:
            return "Transaksi Internal", item, "Customer", "Pengembalian / titipan customer"
        if "bank" in d:
            return "Transaksi Internal", item, "BANK BIRU", "Masuk dari Bank Biru"
        if "ojan" in d or "roziy" in d or "pemilik" in d or "modal" in d:
            return "Modal & Setoran Pemilik", item, "Ahmad Roziyan", "Setoran pemilik"
        return "Transaksi Internal", item, "-", "Uang masuk internal"

    # ── keluar ──
    if "setor" in d and ("bank" in d or "tunai" in d):
        return "Transaksi Internal", item, "BANK BIRU", "Solved Kasir to Bank Biru"
    if d.startswith("gaji"):
        return "Gaji Bulan Ini", item, "-", "Pembayaran gaji"
    if "es batu" in d:
        return "Belanja Bahan", "Es Batu", "Toko Abadi (Es Batu)", "Paid Off to Toko Es Batu"
    if "telur" in d:
        return "Belanja Bahan", item, "Pasar", "Paid Off to Pasar (Telur)"
    if "creamer" in d:
        return "Belanja Bahan", "Creamer", "Alfamart", "Paid Off to Alfamart"
    if any(k in d for k in ("uht", "ultramilk", "skm", "primer")):
        return "Belanja Bahan", item, "Primer", "Paid Off to Primer Raya"
    if "detergen" in d or "downy" in d or "laundry" in d:
        return "Overhead", item, "Mira Laundry", "Paid Off to Laundry"
    if "gas" in d:
        return "Belanja Utilitas", item, "TENANT LAIN", "Gas"
    if any(k in d for k in ("stiker", "print", "thermal", "kertas")):
        return "Marketing", item, "Percetakan Cahaya Mandiri", "Paid Off to Percetakan"
    if "parkir" in d:
        return "Overhead", item, "TENANT LAIN", "Parkir"
    if "konsumsi" in d or "makan" in d:
        return "Konsumsi dan Liburan", item, "TENANT LAIN", "Konsumsi internal"
    if "rokok" in d or "pribadi" in d:
        return "Pengeluaran Pribadi", item, "TENANT LAIN", "Pengeluaran pribadi"
    if category == "pasar" or any(k in d for k in (
        "selada", "kentang", "pecel", "nasi", "sedotan", "pangsit", "risol",
        "biji kopi", "cola", "mineral", "ayam", "geruz", "sayur", "buah", "bahan")):
        obj = "Pasar"
        if "geruz" in d:
            obj = "Geruz"
        elif "ayam" in d:
            obj = "Dapur / Ayam"
        return "Belanja Bahan", item, obj, f"Paid Off to {obj}"
    if any(k in d for k in ("kantong", "ganti uang", "tarik tunai", "tf cust", "gagal qris",
                            "minus", "customer", "arin", "kiki", "sekar", "opik")):
        return "Transaksi Internal", item, "-", "Transaksi internal / titipan"
    return "Belanja Bahan", item, "TENANT LAIN", "Unrecognized"


def generate_kasbuku_xlsx(
    txs,
    month: int,
    year: int,
    saldo_awal: float = 0.0,
) -> tuple[bytes, str]:
    """
    Bangun file Kas-Buku .xlsx dari daftar Transaction untuk satu bulan.

    txs               : iterable Transaction (punya .type, .amount, .description,
                        .category, .transaction_date), diurutkan by tanggal.
    saldo_awal        : saldo kas sebelum tanggal 1 bulan ini.
    Returns (bytes_file, nama_file).
    """
    bulan_nama = _BULAN_ID.get(month, str(month))
    sheet_title = f"Kas-Buku {bulan_nama} {year}"

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title

    hdr_fill = PatternFill("solid", fgColor="1F4E78")
    hdr_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center")

    for c, h in enumerate(HEADER, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = center
        cell.border = border

    # Baris Saldo Awal Bulan (Kredit = saldo awal, tanpa tanggal)
    r = 2
    saw = ["", "Saldo Awal", "Saldo Awal Bulan", None, float(saldo_awal),
           float(saldo_awal), "-", "-", "", ""]
    for c, v in enumerate(saw, 1):
        cell = ws.cell(row=r, column=c, value=v)
        cell.border = border
        if c in (4, 5, 6):
            cell.number_format = MONEY_FMT
    ws.cell(row=r, column=2).font = Font(bold=True)
    r += 1

    for tx in txs:
        is_masuk = (tx.type == "masuk")
        amount = abs(float(tx.amount))
        kat, item, obj, ket = _classify(tx.description, is_masuk, tx.category)

        if is_masuk:
            debit_v = None
            kredit_v = amount
            subjek = "Penjualan Cash"
        else:
            debit_v = -amount          # keluar -> Debit negatif
            kredit_v = None
            subjek = "Kasir"

        # Tanggal sebagai datetime ASLI (bukan string) -> tidak terbalik di Excel
        td = tx.transaction_date
        tgl = datetime(td.year, td.month, td.day)

        saldo_formula = f"=D{r}+E{r}+F{r-1}"
        rowvals = [tgl, item, kat, debit_v, kredit_v, saldo_formula,
                   subjek, obj, ket, ""]
        for c, v in enumerate(rowvals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = border
            if c == 1:
                cell.number_format = DATE_FMT
            elif c in (4, 5, 6):
                cell.number_format = MONEY_FMT
        r += 1

    widths = [14, 30, 24, 13, 13, 16, 16, 24, 30, 18]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"Kas-Buku_{bulan_nama}_{year}.xlsx"
    logger.info(f"[KASBUKU] generated '{filename}' rows={r-2}")
    return buf.getvalue(), filename
