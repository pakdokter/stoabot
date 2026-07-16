"""
OCR Service - parser struk belanja dari berbagai toko.

Format struk fisik:
  Format A : nama item multi-baris, detail di baris berikutnya
             (Primer Raya, Dinda Frozen Food, Amanah)
  Format B : nama + qty + harga dalam satu baris
             (Indomaret, Alfamart, SB Minimarket)
  Format C : nomor item "N. NAMA", diikuti "QTY x Rp SATUAN  Rp TOTAL"
             (Fadhilah Frozen Foods)
  Format D : nama item, lalu SKU-batch + harga, lalu barcode + qty + total
             Varian 1 (struk panjang): barcode 13 digit + 1 X + harga
             Varian 2 (struk pendek): batch NN/NNN + "1 x harga" atau harga saja
             (MR D.I.Y. / PT Daya Indah Yasa)
  Format E : Format B dengan Disc. per item di baris terpisah
             (Alfamart Selebung, varian dengan diskon per item)
  Format F : nama item, lalu "Nx HARGA TOTAL" (lowercase x, spasi setelah x)
             Mendukung OCR ter-join satu baris panjang (mode one-liner)
             (Harnila Store, kasir app generic)
  Format I : nama item, lalu "N PCS/BH X HARGA = TOTAL", lalu opsional "Disc -X"
             Total dari "Total : NNN" (bukan Sub Total, bukan Chrge Krtu/Byar Debit)
             (SB Minimarket / Sinar Bahagia Pancor)
  Format G : invoice tabel kolom, format IDR titik-koma (NNN.NNN,00)
             Item: QTY UNIT SKU NAMA Rp UNIT_PRICE Rp AMOUNT
             Total dari "Invoice Total : Rp NNN.NNN,00"
             (PT Dineta Jaya, supplier/distributor dengan invoice formal)
  Format H : nama item + harga, lalu "N SET X harga_asli(diskon)", lalu total
             Total dari "Total Bayar NNN" atau "Total NNN"
             (Dapur Kita, toko bahan makanan retail)

Screenshot app:
  Shopee Pesanan Saya     - per separator toko (Selesai/Dikirim)
  Shopee Rincian Pesanan  - per item + biaya tambahan, total dari Total-Subtotal
  Sukanda Jaya            - Harga Satuan + Total per item, SKU angka
  TikTok Shop             - Pesanan dibuat, nama toko "›", item x qty, Total: Rp
  QRIS BCA                - Payment Successful, form input manual toko/item/total
  PT Dineta Jaya (G)      - Invoice formal, kolom Qty-Desc-UnitPrice-Amount

Ditolak (bukan struk belanja):
  Transfer BCA/bank       - Transfer Successful, Beneficiary Name, Reference No.
                            NAMUN jika ada Remarks berisi keterangan barang (angka,
                            satuan kg/gr/pcs, kata pickup/order dll), diperlakukan
                            sebagai belanja via transfer BCA:
                              merchant = Beneficiary Name
                              item     = isi Remarks
                              total    = Transfer Amount (IDR NNN,NNN.00)
                            Jika Remarks kosong atau hanya nama/transfer biasa,
                            ditolak dan bot minta input manual /masuk atau /keluar

Format laporan teks (cmd /laporan_teks, bukan foto):
  Laporan harian staff    - *TGL\\n• TOKO - NOMINAL\\n(TOTAL)
                            Mendukung sub-item pasar: -nama - nominal\\n=total
                            Mendukung "Uang Masuk" sebagai transaksi masuk
"""
import re
import httpx
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from loguru import logger


@dataclass
class ReceiptItem:
    name: str
    qty: float = 1.0
    unit: str = ""
    unit_price: float = 0.0
    line_total: float = 0.0


@dataclass
class OcrResult:
    merchant: Optional[str] = None
    total: Optional[float] = None
    grand_total: Optional[float] = None
    cash_paid: Optional[float] = None
    change: Optional[float] = None
    discount_amount: Optional[float] = None  # diskon/voucher dari struk
    discount: Optional[float] = None
    tx_date: Optional[date] = None
    items: list = field(default_factory=list)  # list of ReceiptItem
    raw_text: str = ""
    confidence: float = 0.0
    provider: str = "ocrspace"
    is_qris: bool = False
    is_shopee_detail: bool = False
    shopee_summary: str = ""
    shopee_item: str = ""
    shopee_qty: int = 1


# ── Keyword lists ──────────────────────────────────────────────────────

TOTAL_KEYWORDS = [
    r'\btotal\b', r'\bgrand\s*total\b', r'\bjumlah\b',
    r'\btagihan\b', r'\bsubtotal\b', r'\bsub\s*total\b',
    r'\bnetto\b', r'\bnet\b',
    r'\btotal\s+belanja\b', r'\bjumlah\s+belanja\b',
    r'\btotal\s+bayar\b', r'\btotal\s+pembayaran\b',
    r'\btotal\s+incl\b', r'\btotal\s+include\b',
    r'\btotal\s+termasuk\b', r'\bitem\(s\)\b',
    r'\btotal\s+item\b',  # Alfamart: "Total Item" = total belanja
    r'\binvoice\s+total\b', r'\bsub\s+total\b',  # Dineta invoice
    # OCR noise variants
    r'\brotal\b', r'\bt0tal\b', r'\bt\*tal\b', r'\btoial\b',
]
PAYMENT_KEYWORDS = [
    r'\btunai\b', r'\bcash\b', r'\bbayar\b', r'\bdibayar\b',
    r'\btransfer\b', r'\bdebit\b', r'\bkredit\b', r'\bkartu\b',
    r'\bqris\b', r'\bova\b', r'\bgopay\b', r'\bshopee\b',
    r'\bdana\b', r'\blinkaja\b', r'\bbrimo\b', r'\bbri\s+brimo\b',
    r'\bshopeepay\b', r'\bovo\b', r'\bmandiri\b',
    # SB Minimarket specific
    r'\bchrge\s+krtu\b', r'\bbyar\s+debit\b', r'\banda\s+hemat\b',
    # OCR noise variants
    r'\brunai\b', r'\btunal\b', r'\btumai\b', r'\bbayaf\b',
    r'\btuna\s+i\b', r'\btunail\b',
]
CHANGE_KEYWORDS = [
    r'\bkembali\b', r'\bkembalian\b', r'\bchange\b', r'\bselisih\b',
    r'\bkenba\b', r'\bkemba\b',  # OCR noise
]

# Kata yang menandakan HEMAT/DISKON — bukan nilai transaksi  
SAVINGS_KEYWORDS = [
    r'\banda\s+hemat\b', r'\bhemat\b', r'\bvoucher\b',
    r'\bpromo\b', r'\bcashback\b',
    r'\bharga\s+jual\b', r'\brga\s+jual\b',
    r'\bdpp\s*=\b', r'\bppn\s*=\b',
    r'\bpwp\b', r'\blp\s+\d\b',
    r'\btotal\s+qty\b', r'\bjml\s+item\s+\d\b',  # "Total Item 3" bukan total belanja jika ada qty
    r'\bppn\s+included\b', r'\bitem\(s\)\b', r'\bqty\(s\)\b',
    r'\bincluded\s+in\s+total\b',
    r'\bppn\s+dibebaskan\b', r'\bpkp\s+dibebaskan\b',
    r'\bharga\s+jual\b', r'\brga\s+jual\b',
    r'\bdpp\s*=', r'\bnpwp\b',
    r'\btotal\s+disc\.?\b',  # Alfamart total disc/item
    r'\btotal\s+poin\b', r'\ba-poin\b',
    r'\bcpm\s+qris\b',  # payment method bukan total
]
DISCOUNT_KEYWORDS = [
    r'\bdiskon\b', r'\bdiscount\b', r'\bdisc\b', r'\bkorting\b',
]
TAX_KEYWORDS = [
    r'\bppn\b', r'\bpajak\b', r'\btax\b', r'\bvat\b',
    r'\bservice\b', r'\bservis\b',
]
SKIP_LINE_PATTERNS = [
    r'\btelp\b', r'\bfax\b', r'\bemail\b', r'\bwww\b', r'\bhttp\b',
    r'\bno\.?\s*struk\b', r'\bno\.?\s*transaksi\b', r'\binvoice\b',
    r'\bkasir\b', r'\boperator\b', r'\bpelanggan\b', r'\bpel\.\b',
    r'\bterima kasih\b', r'\bjangan lupa\b', r'\bdatang kembali\b',
    r'\bpowered by\b', r'\bcopyright\b',
    r'^\s*[-=*_]{3,}\s*$',
    r'\d{2}\.\d{2}\.\d{2}-\d{2}:\d{2}',
    r'\d+/\d+\.\d+\.\d+/',
    r'\bpwp\s+\d{10,}\b',
    r'\blp\s+\d{6,}\b',
    r'\bswa\b', r'\bkontak@\b',
    r'\bnpwp\b',                              # NPWP header perusahaan
    r'\boperator\s+id\b',                    # OPERATOR ID-XSEL
    r'\bcustomer\s+care\b',                  # CUSTOMER CARE
    r'\bpesan\s+whatsapp\b',                 # PESAN WHATSAPP
    r'\bpenukaran\s+barang\b',               # footer MR DIY
    r'\bwebsite\b', r'\bwww\.\b',          # WEBSITE
    r'\byuk,\s+bantu\b',                     # footer
    r'\bgedung\b', r'\bjalan\s+jenderal\b', # alamat HQ
    r'^\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}',  # timestamp MR DIY: 05-06-26 16:04
    r'\bsenin\b.*\bjumat\b',               # jam operasional
    r'\bkualitas\s+pelayanan\b',             # footer MR DIY
    r'\ba-poin\b', r'\bpoin\s+anda\b',     # Alfamart poin
    r'\bexpired\s+pada\b',                   # expired tanggal
    r'\balfagift\b', r'\baltogift\b',        # Alfamart app
    r'\bpotensi\s+poin\b',                   # poin info
    r'\bkritik.*saran\b',                     # kritik saran
    r'\bmember\s*:\s*[a-z*]+',               # MEMBER : BAIQ
    r'\bjazakallahu\b',                       # Fadhilah footer
    r'\bbarang\s+yg\s+sudah\b',            # Fadhilah footer
    r'\bpat\s+di\s+kembalikan\b',          # Fadhilah footer
    r'\bstruk\s+anda\s+akan\b',             # struk dikirim
    r'\bdikirim\s+ke\s+aplikasi\b',         # dikirim ke aplikasi
    r'\bbon\s+[a-z0-9]',                      # Bon nomor struk
    r'\btgl\.\s+\d',                         # Tgl. tanggal
    r'\bv\.\.\.',                            # V...
    r'^\d+\s*[xX]\s*rp',                       # "2 x Rp 55.000" (qty x satuan)
    r'\bcpm\s+qris\b',                       # CPM QRIS (payment method)
    r'^disc\.?\s*[-+]?[\d.,]+',              # "Disc. -1.000" per item bukan item
    r'^voucher\s*:', r'^\([\d.,]+\)',          # "VOUCHER: (8,400)" bukan item
    r'\bken?balian\b',                        # kembalian/kenbalian
    r'^\d{1,2}/\d{2}/\d{4}',                 # tanggal DD/MM/YYYY (Amanah header)
    r'^SI\d+-\d+',                            # nomor struk Amanah "SI01-2606-4786"
    r'^\d{2}-\w{3}-\d{2,4}',                 # tanggal "17-Jun-26" footer struk
    r'\bkasir\s*:',                           # "Kasir : ADMIN"
    r'^instagram\s*:', r'^facebook\s*:',      # social media footer Amanah
    r'\bsms/wa\b',                            # SMS/WA
    r'^-?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'januari|februari|maret|april|mei|juni|juli|agustus|'
    r'september|oktober|november|desember)-',  # "-Jun-2026"
    r'^\d{1,2}-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'januari|februari|maret|april|mei|juni|juli|agustus|'
    r'september|oktober|november|desember)-\d{4}',  # "30-Jun-2026"
]

# Unit-unit umum di struk Indonesia
UNIT_WORDS = {
    'pcs', 'pc', 'psc', 'unit', 'buah', 'bh', 'biji',
    'kg', 'gr', 'gram', 'ltr', 'liter', 'ml',
    'slop', 'pack', 'pak', 'pck', 'pakx', 'box', 'dus', 'karton',
    'lusin', 'rim', 'roll', 'lembar', 'lbr', 'meter', 'mtr',
    'botol', 'btl', 'kaleng', 'klg', 'sachet', 'scht',
    'porsi', 'gelas', 'cup', 'mangkok', 'piring',
    'x', 'pax',
}

# Kata header/footer yang bukan item
NON_ITEM_WORDS = {
    'total', 'subtotal', 'grand', 'tunai', 'cash', 'bayar',
    'kembali', 'change', 'diskon', 'discount', 'pajak', 'tax',
    'ppn', 'service', 'kasir', 'operator', 'struk', 'nota',
    'invoice', 'receipt', 'terima', 'terimakasih', 'tanggal',
    'date', 'time', 'waktu', 'no', 'nomor', 'number',
    # Address words — bukan nama item
    'jl', 'jln', 'jalan', 'gg', 'gang', 'rt', 'rw', 'kel', 'kec',
    'area', 'pertokoan', 'mall', 'ruko', 'rukan', 'komplek',
    'selong', 'lombok', 'mataram', 'timur', 'barat', 'utara', 'selatan',
    # Indomaret/minimarket footer words
    'voucher', 'hemat', 'dpp', 'ppn', 'harga', 'jual', 'layanan',
    'konsumen', 'kontak', 'belanja', 'klikindomaret', 'gratis',
    'ongkir', 'sampai', 'mudah', 'telp', 'wa', 'swf', 'pwp',
    'control', 'option', 'command',  # OCR noise dari UI elements
    'rga', 've', 'lp', 'men', 'maret', 'domaret', 'indomaret',
    # Kota/wilayah
    'jakarta', 'selatan', 'utara', 'barat', 'pusat', 'dki',
    'surabaya', 'bandung', 'medan', 'makassar', 'denpasar',
    'gedung', 'lantai', 'jenderal', 'sudirman', 'thamrin',
    'npwp', 'invoice', 'pt', 'cv', 'tbk',
    # Alfamart/Indomaret footer
    'poin', 'member', 'expired', 'alfagift', 'potensi',
    'struk', 'dikirim', 'aplikasi', 'kritik', 'saran',
    'bon', 'kasir', 'kembalian', 'disc',
    # Fadhilah footer
    'jazakallahu', 'khoir', 'kembalikan', 'hubungi',
    'whatsapp', 'admin', 'pancor', 'lombok', 'tenggara',
}


def _matches_any(text: str, patterns: list) -> bool:
    return any(re.search(p, text.lower()) for p in patterns)


def _extract_money(text: str) -> list:
    """Ekstrak angka yang kemungkinan nominal uang (>= 100).
    Toleran terhadap OCR noise: trailing -, =, tanda baca extra.
    """
    # Bersihkan noise: trailing -, =, karakter non-digit di ujung
    text_clean = re.sub(r'[-=]+$', '', text.strip())
    results = []
    for m in re.finditer(r'\d{1,3}(?:[.,]\d{3})+|\d{1,3}[.,]\d{2}(?!\d)|\d+', text_clean):
        raw_orig = m.group(0)
        # Handle format ribuan: 43,500 / 43.500 / 43,50 (OCR potong digit)
        raw = raw_orig.replace('.', '').replace(',', '')
        if not raw.isdigit():
            continue
        val = float(raw)
        # Jika hanya 4 digit dan asalnya ada koma/titik di posisi ribuan → kalikan 10
        # Contoh: "43,50" → raw=4350 → tapi aslinya 43500
        if len(raw) == 4 and re.match(r'^\d{2}[.,]\d{2}$', raw_orig):
            val = val * 10  # 4350 → 43500
        # Filter: minimal 100, maksimal 100 juta
        if 100 <= val <= 100_000_000:
            results.append((val, m.start()))
    return results  # list of (value, position)


def _extract_qty(text: str) -> Optional[float]:
    """Ekstrak qty dari awal baris (angka kecil 1-999)."""
    m = re.match(r'^\s*(\d{1,3})\s', text)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 999:
            return float(val)
    return None


def _extract_unit(text: str) -> Optional[str]:
    """Ekstrak unit dari teks."""
    words = re.findall(r'[a-zA-Z]+', text.lower())
    for w in words:
        if w in UNIT_WORDS:
            return w.upper()
    return None


def _is_item_name_line(line: str) -> bool:
    """
    Apakah baris ini kemungkinan nama item?
    Kriteria: punya huruf, bukan keyword finansial, tidak pure angka.
    """
    if not re.search(r'[a-zA-Z]', line):
        return False
    if len(line.strip()) < 2:
        return False
    # Skip baris yang hanya "Rp" / "RP" / "rp" (prefix mata uang tanpa nama item)
    if re.match(r'^[Rr][Pp]\.?\s*[\d.,]*\s*$', line.strip()):
        return False
    # Skip baris yang hanya berisi satuan mata uang dan angka
    if re.match(r'^[Rr][Pp]\s+[\d.,]+\s*$', line.strip()):
        return False
    stripped = line.strip()
    if re.match(r'^[^a-zA-Z0-9]', stripped):
        after_punct = re.sub(r'^[^a-zA-Z0-9]+', '', stripped)
        if not re.search(r'[A-Z]{2,}', after_punct):
            return False

    # Skip barcode — baris yang dimulai angka >= 8 digit
    if re.match(r'^\d{8,}', stripped) and not re.search(r'[a-zA-Z]', stripped.split()[0] if stripped.split() else ''):
        return False

    # Skip kode produk — format "XXXX - YY/ZZZ" (kode item toko)
    if re.match(r'^[A-Z]{2,}\d+\s*-\s*\d+', stripped):
        return False
    if _matches_any(line, SKIP_LINE_PATTERNS):
        return False
    if _matches_any(line, TOTAL_KEYWORDS):
        return False
    if _matches_any(line, PAYMENT_KEYWORDS):
        return False
    if _matches_any(line, CHANGE_KEYWORDS):
        return False
    if _matches_any(line, DISCOUNT_KEYWORDS):
        return False
    if _matches_any(line, TAX_KEYWORDS):
        return False
    if _matches_any(line, SAVINGS_KEYWORDS):
        return False

    # Cek apakah kata pertama adalah non-item word
    first_word = re.findall(r'[a-zA-Z]+', line.lower())
    if first_word and first_word[0] in NON_ITEM_WORDS:
        return False

    # Baris yang hanya berisi qty + unit + harga (tanpa nama item)
    # Pattern: "2 SLOP 35,000 70,000" → bukan nama item
    pure_price_pattern = r'^\s*\d{1,3}\s+[a-zA-Z]+\s+[\d.,]+\s+[\d.,]+\s*$'
    if re.match(pure_price_pattern, line):
        return False

    return True


def _parse_item_line_format_b(line: str) -> Optional[ReceiptItem]:
    """
    Parse baris format B.
    Mendukung dua pola:
    - "NAMA QTY UNIT HARGA TOTAL"   (PRIMER RAYA: KLIP REC 650 ML 2 SLOP 35,000 70,000)
    - "NAMA QTY HARGA TOTAL"        (Indomaret: ITUNE MYK.GRG RF2L 3 43100 129,300)
    """
    # Hapus angka dalam kurung (voucher/diskon negatif): (2,100)
    line_clean = re.sub(r'\([\d.,]+\)', '', line).strip()

    money_vals = _extract_money(line_clean)
    if len(money_vals) < 1:
        return None

    # Ambil unit dan posisi pertama angka besar
    first_money_pos = money_vals[0][1]
    text_before_money = line_clean[:first_money_pos].strip()

    # Cari unit di teks sebelum angka
    unit = _extract_unit(text_before_money)

    # Cari qty — angka kecil (1-999) di antara nama dan harga
    # Bersihkan unit dari text_before_money dulu
    name_part = text_before_money
    if unit:
        name_part = re.sub(r'\b' + unit.lower() + r'\b', '', name_part, flags=re.IGNORECASE).strip()

    # Cari qty di akhir name_part (angka kecil sebelum harga)
    qty = 1.0
    qty_match = re.search(r'\b(\d{1,3})\s*$', name_part)
    if qty_match:
        qty_val = int(qty_match.group(1))
        if 1 <= qty_val <= 999:
            qty = float(qty_val)
            name_part = name_part[:qty_match.start()].strip()
    else:
        # Cek juga di seluruh text_before_money (untuk pola Indomaret)
        qty_match2 = re.search(r'\t(\d{1,3})\t|\s{2,}(\d{1,3})\s{2,}', text_before_money)
        if qty_match2:
            qty_val = int(qty_match2.group(1) or qty_match2.group(2))
            if 1 <= qty_val <= 999:
                qty = float(qty_val)
                # Hapus qty dari nama
                name_part = text_before_money[:qty_match2.start()].strip()
                if unit:
                    name_part = re.sub(r'\b' + unit.lower() + r'\b', '', name_part, flags=re.IGNORECASE).strip()

    # Pilih unit_price dan line_total
    if len(money_vals) >= 2:
        line_total = money_vals[-1][0]
        unit_price = money_vals[-2][0]
    else:
        line_total = money_vals[-1][0]
        unit_price = line_total

    name = name_part.strip().rstrip('-').rstrip('.').strip()
    # Hapus nomor urut di awal: "1. ", "2. ", "1) "
    name = re.sub(r'^\d+[\.\)\-]\s*', '', name).strip()
    # Hapus suffix "Rp" / "RP" di akhir nama
    name = re.sub(r'\s+[Rr][Pp]\.?$', '', name).strip()
    if len(name) < 2:
        return None

    return ReceiptItem(
        name=name.upper(),
        qty=qty,
        unit=unit or "",
        unit_price=unit_price,
        line_total=line_total,
    )



def _parse_items_format_fadhilah(lines: list) -> list:
    """
    Parse format Fadhilah Frozen Foods:
      1. NAMA ITEM QTY_INFO    Rp TOTAL
         QTY x Rp SATUAN        (baris detail, dilewati)

    Ciri: baris item diawali nomor "N. " dan harga pakai "Rp " (spasi).
    """
    items = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Pola: "1. NAMA ITEM ..."  dengan atau tanpa harga di baris yang sama
        m = re.match(r'^(\d+)\.\s+(.+)', line)
        if m:
            name_raw = m.group(2).strip()
            # Pisah nama dari harga di baris yang sama: "YOMAS SO JUMBO ISI 12+2  Rp 110.000"
            money_m = re.search(r'\bRp\.?\s+([\d.,]+)\s*$', name_raw)
            if money_m:
                total_raw = money_m.group(1).replace('.','').replace(',','')
                line_total = float(total_raw) if total_raw.isdigit() else None
                name = name_raw[:money_m.start()].strip()
            else:
                # Coba cari harga di baris berikutnya
                line_total = None
                name = name_raw
                if i + 1 < len(lines):
                    nxt_money = _extract_money(lines[i+1])
                    if nxt_money:
                        line_total = nxt_money[-1][0]

            # Bersihkan nama: hapus qty info di akhir seperti "12+2"
            name = re.sub(r'\s+\d+[+]\d+\s*$', '', name).strip()
            name = re.sub(r'\s+[xX]\s*\d+\s*$', '', name).strip()

            # Cari qty dari baris berikutnya: "2 x Rp 55.000"
            qty = 1.0
            j = i + 1
            if j < len(lines):
                qty_m = re.match(r'^(\d+)\s*[xX]\s*Rp', lines[j], re.IGNORECASE)
                if qty_m:
                    qty = float(qty_m.group(1))
                    i = j  # skip baris qty

            if name and len(name) >= 2 and line_total and line_total >= 100:
                items.append(ReceiptItem(
                    name=name.upper()[:60],
                    qty=qty,
                    unit="",
                    unit_price=line_total / qty if qty > 0 else line_total,
                    line_total=line_total,
                ))
        i += 1
    return items





def _parse_harnila_oneliner(text: str) -> list:
    """
    Handle OCR Harnila Store yang ter-join satu baris panjang.
    Contoh: "Terima kasih  Kembali  1x 5,000  Lem eyelash  1x 5.000  Jarum pentul 5K  1x 12.000  12K"
    
    Pola yang dapat dideteksi: pasangan nama-item + "Nx HARGA"
    """
    items = []
    # Temukan semua pasangan qty+harga dalam teks
    # Pola: "nama item ... Nx harga"
    # Tokenize dengan split pada pola qty
    tokens = re.split(r'(\d+x\s+[\d.,]+)', text)
    
    STOP_WORDS = {
        'tunai','kembali','terima','kasih','total','rp','bon',
        'kasir','member','poin','anda','akan','expired','dikirim'
    }

    for i in range(0, len(tokens)-1, 2):
        name_part = tokens[i].strip() if i < len(tokens) else ''
        qty_str = tokens[i+1] if i+1 < len(tokens) else None
        if not qty_str or not re.match(r'\d+x\s+[\d.,]+', qty_str):
            continue

        # Ambil nama: cari segmen teks antara dua stop word terakhir
        words = name_part.split()
        STOP_WORDS_SET = {
            'tunai','kembali','terima','kasih','total','rp','bon',
            'kasir','member','poin','anda','akan','expired','dikirim'
        }
        # Cari posisi stop word dari kanan
        # Nama item adalah kata-kata di antara stop word terakhir dan stop word sebelumnya
        stop_positions = [idx for idx, w in enumerate(words)
                          if w.lower().rstrip('.,;') in STOP_WORDS_SET]

        if stop_positions:
            # Cari blok kata di antara stop positions
            # Coba ambil segmen setelah stop word terakhir dulu
            last_stop = stop_positions[-1]
            candidate = words[last_stop+1:]

            # Jika kosong, cari segmen terpanjang di antara stop words
            if not candidate:
                best = []
                prev_stop = -1
                for sp in stop_positions:
                    seg = words[prev_stop+1:sp]
                    seg_clean = [w for w in seg if not re.match(r'^[\d#.,]+$', w)]
                    if len(seg_clean) > len(best):
                        best = seg_clean
                    prev_stop = sp
                candidate = best
        else:
            candidate = [w for w in words if not re.match(r'^[\d#.,]+$', w)
                         and not re.match(r'^Rp', w, re.IGNORECASE)]

        name_words = [w for w in candidate
                      if not re.match(r'^\d+[.,]\d{3}$', w)
                      and not re.match(r'^#\d+', w)]

        if not name_words:
            continue

        name = ' '.join(name_words).strip()
        
        # Parse qty dan harga
        qty_m = re.match(r'(\d+)x\s+([\d.,]+)', qty_str)
        if not qty_m:
            continue
        qty = float(qty_m.group(1))
        raw_price = qty_m.group(2).replace('.', '').replace(',', '')
        price = float(raw_price) if raw_price.isdigit() else 0.0
        
        if price >= 100 and len(name) >= 2:
            items.append(ReceiptItem(
                name=name.upper()[:60],
                qty=qty,
                unit='',
                unit_price=price,
                line_total=price * qty,
            ))
    
    return items



def _is_format_f(lines: list) -> bool:
    """
    Format F — Harnila Store / kasir app generic.
    Ciri: baris atau teks mengandung "Nx HARGA" (lowercase x, spasi setelah x).
    Contoh: "   1x 12.000    12.000" atau one-liner gabungan.
    """
    full_text = ' '.join(lines)
    return bool(re.search(r'\d+x\s+[\d.,]+', full_text))


def _parse_items_format_f(lines: list) -> list:
    """
    Format F — Harnila Store / kasir app dengan format:
      NAMA ITEM
         Nx HARGA_SATUAN    TOTAL
    """
    SKIP = {'total', 'tunai', 'kembali', 'terima', 'kasih', 'bon',
            'npwp', 'telp', 'wa', 'store', 'toko'}

    # Jika OCR ter-join satu baris panjang, gunakan parser khusus
    full_text = ' '.join(lines)
    if len(lines) <= 3 and re.search(r'\d+x\s+[\d.,]+', full_text):
        return _parse_harnila_oneliner(full_text)

    items = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Baris qty: "1x 12.000   12.000" atau "1x 5.000"
        qty_m = re.match(r'^(\d+)x\s+([\d.,]+)\s*([\d.,]*)\s*$', line)
        if qty_m:
            qty = float(qty_m.group(1))
            raw_price = qty_m.group(2).replace('.', '').replace(',', '')
            raw_total = qty_m.group(3).replace('.', '').replace(',', '') if qty_m.group(3) else ''

            unit_price = float(raw_price) if raw_price.isdigit() else 0.0
            line_total = float(raw_total) if raw_total.isdigit() else unit_price * qty

            # Ambil nama dari baris sebelumnya
            name = None
            j = i - 1
            while j >= 0:
                prev = lines[j].strip()
                if (len(prev) >= 2 and
                    not re.match(r'^\d+x\s', prev) and
                    not re.match(r'^(Total|Tunai|Kembali|Rp)', prev, re.IGNORECASE)):
                    words = set(re.findall(r'[a-z]+', prev.lower()))
                    if not words & SKIP:
                        name = prev
                        break
                j -= 1

            if name and line_total >= 100:
                items.append(ReceiptItem(
                    name=name.upper()[:60],
                    qty=qty,
                    unit='',
                    unit_price=unit_price,
                    line_total=line_total,
                ))

        i += 1
    return items



def _is_fadhilah_format(lines: list) -> bool:
    """Deteksi format Fadhilah: ada baris 'N. NAMA' dan baris 'N x Rp SATUAN  Rp TOTAL'."""
    item_no_count = sum(1 for l in lines if re.match(r'^\d+\.\s+[A-Z]', l))
    has_qty_x = any(re.match(r'^\d+\s*[xX]\s*(Rp|Kp)', l) for l in lines)
    return item_no_count >= 1 and has_qty_x


def _parse_items_format_fadhilah(lines: list) -> list:
    """
    Format C — Fadhilah Frozen Foods:

      1. NAMA ITEM QTY_INFO
         (keterangan tambahan opsional, misal PROMO)
         QTY X Rp SATUAN     Rp TOTAL

    Semua baris antara nomor item dan baris qty-x adalah bagian nama.
    """
    items = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Deteksi baris nomor item: "1. NAMA ..." atau "1. NAMA\tRp TOTAL"
        m = re.match(r'^(\d+)\.\s+(.+)', line)
        if not m:
            i += 1
            continue

        # Kumpulkan nama (bisa multiline sebelum baris qty)
        name_parts = [m.group(2).strip()]
        j = i + 1

        # Lanjutkan ambil baris nama selama bukan baris qty/total/footer
        while j < len(lines):
            nxt = lines[j].strip()
            # Baris qty: "1 X Rp 55.000" atau "2 X Rp 65.000  Rp 130.000"
            if re.match(r'^\d+\s*[xX]\s*(Rp|Kp)', nxt, re.IGNORECASE):
                break
            # Baris total/sub total/footer
            if re.match(r'^(Total|Sub|Bayar|Kembali|JAZAK|Barang)', nxt, re.IGNORECASE):
                break
            # Baris nomor item berikutnya
            if re.match(r'^\d+\.\s+[A-Z]', nxt):
                break
            # Baris yang hanya berisi keterangan seperti "(PROMO)"
            if re.match(r'^\(.*\)\s*$', nxt):
                name_parts.append(nxt)
                j += 1
                continue
            # Baris lain yang masuk akal sebagai lanjutan nama
            if len(nxt) >= 2 and not re.match(r'^Rp', nxt):
                name_parts.append(nxt)
            j += 1

        # Parse baris qty: "2 X Rp 65.000  Rp 130.000" atau "2 X Rp 65.000\tRp 130.000"
        qty = 1.0
        unit_price = 0.0
        line_total = 0.0

        if j < len(lines):
            qty_line = lines[j].strip()
            qty_m = re.match(r'^(\d+)\s*[xX]\s*(Rp|Kp)\.?\s*([\d.,]+)', qty_line, re.IGNORECASE)
            if qty_m:
                qty = float(qty_m.group(1))
                raw_unit = qty_m.group(3).replace('.', '').replace(',', '')
                unit_price = float(raw_unit) if raw_unit.isdigit() else 0.0

                # Cari total di sisa baris yang sama
                total_m = re.search(r'Rp\.?\s*([\d.,]+)\s*$', qty_line)
                if total_m:
                    raw_total = total_m.group(1).replace('.', '').replace(',', '')
                    line_total = float(raw_total) if raw_total.isdigit() else unit_price * qty
                else:
                    line_total = unit_price * qty

                j += 1  # lewati baris qty

        # Bersihkan nama
        name = ' '.join(name_parts)
        name = re.sub(r'\tRp[\d.,\s]+$', '', name)      # hapus harga yang ikut terbaca
        name = re.sub(r'Rp[\d.,\s]+$', '', name).strip() # hapus Rp di akhir
        name = re.sub(r'\s+\d+[+]\d+\s*$', '', name)  # hapus "12+2" di akhir
        name = name.strip()

        if name and len(name) >= 2 and line_total >= 100:
            items.append(ReceiptItem(
                name=name.upper()[:60],
                qty=qty,
                unit='',
                unit_price=unit_price if unit_price >= 100 else line_total / qty,
                line_total=line_total,
            ))

        i = j
    return items


def _parse_items_format_a(lines: list) -> list:
    """
    Parse format A: nama item di baris terpisah dari qty/harga.
    Fix: jangan hapus angka dari nama item (e.g. KLIP REC 650 ML),
    dan skip baris header/alamat.
    """
    header_words = {'telp', 'fax', 'no.', 'no:', 'kasir', 'area', 'jl.',
                    'jln', 'pel.', 'pelanggan', 'tanggal', 'date', 'struk',
                    'terima', 'jangan', 'lupa', 'powered', 'copyright',
                    'nota', 'invoice', 'receipt', 'selong', 'lombok',
                    'pertokoan', 'mall', 'ruko', 'rukan',
                    'wa', 'instagram', 'facebook', 'twitter', 'tgl',
                    'user', 'sc', 'pel', 'instagram'}

    items = []
    i = 0
    while i < len(lines):
        line = lines[i]

        money_in_line = _extract_money(line)
        has_alpha = bool(re.search(r'[a-zA-Z]', line))

        # Handle baris tab-joined: "NAMA\tHARGA" (hasil join dari 2 baris terpisah)
        # Contoh: "Dm rec 650 ml\t33,500" atau "SOVIA 2L\t44,500"
        if '\t' in line and has_alpha:
            tab_parts = line.split('\t', 1)
            tab_name = tab_parts[0].strip()
            tab_rest = tab_parts[1].strip() if len(tab_parts) > 1 else ''
            tab_money = _extract_money(tab_rest)
            tab_words = set(re.findall(r'[a-zA-Z]+', tab_name.lower()))
            if (tab_money and len(tab_name) >= 2 and
                not (tab_words & header_words) and
                not re.match(r'^[A-Z]{2,4}\d+[-\d]*$', tab_name) and
                not re.match(r'^(total|diskon|bayar|kembali|sub\s*total)', tab_name, re.IGNORECASE)):
                # Cari qty dari baris berikutnya
                nqty, nunit = 1.0, ''
                if i + 1 < len(lines):
                    nl = lines[i + 1]
                    nq = _extract_qty(nl); nu = _extract_unit(nl)
                    if nq and nu:
                        nqty, nunit = nq, nu
                        i += 1  # consume detail line
                line_total = tab_money[-1][0]
                if line_total >= 100:
                    items.append(ReceiptItem(
                        name=tab_name.upper()[:60],
                        qty=nqty, unit=nunit,
                        unit_price=line_total / nqty,
                        line_total=line_total,
                    ))
                i += 1
                continue

        # Baris nama item: boleh punya angka kecil (kode produk), tapi bukan harga besar
        is_product_code_only = all(v < 10000 for v, _ in money_in_line)
        if has_alpha and _is_item_name_line(line) and (len(money_in_line) == 0 or (len(money_in_line) <= 2 and is_product_code_only)):
            # Skip baris header/alamat
            words_in_line = set(re.findall(r'[a-zA-Z]+', line.lower()))
            if words_in_line & header_words:
                i += 1
                continue
            # Skip pola nomor struk: SI01-2606-0728, SB-9926F03I4915
            # Hanya skip jika tidak ada spasi (pure kode, bukan nama item dengan angka)
            if re.match(r'^[A-Z]{2,4}\d+[-\d]*$', line.strip()):
                i += 1
                continue
            # Skip baris yang mengandung "X" sebagai operator (header info)
            if re.search(r'sc\s*:', line.lower()) or re.search(r'no\s*:', line.lower()):
                i += 1
                continue

            if i + 1 < len(lines):
                next_line = lines[i + 1]
                next_money = _extract_money(next_line)
                next_qty = _extract_qty(next_line)
                next_unit = _extract_unit(next_line)

                # Pola joined: "NAMA\tHARGA" dalam satu baris (hasil _join_fragmented_lines)
                # Contoh: "Dm rec 650 ml\t33,500"
                if '\t' in line:
                    parts = line.split('\t', 1)
                    part_name = parts[0].strip()
                    part_money = _extract_money(parts[1]) if len(parts) > 1 else []
                    if part_money and len(part_name) >= 2:
                        # Cari qty dari baris berikutnya (N UNIT HARGA)
                        nqty = next_qty or 1.0
                        nunit = next_unit or ''
                        line_total = part_money[-1][0]
                        unit_price = next_money[0][0] if next_money and next_qty else line_total / nqty
                        items.append(ReceiptItem(
                            name=part_name.upper()[:60],
                            qty=nqty,
                            unit=nunit,
                            unit_price=unit_price,
                            line_total=line_total,
                        ))
                        # Skip baris qty berikutnya jika itu baris detail
                        if next_qty and next_unit:
                            i += 2
                        else:
                            i += 1
                        continue

                # Pola 3 baris Primer Raya / Amanah:
                # NAMA
                # HARGA_TOTAL        ← baris hanya berisi angka
                # N  UNIT  HARGA_SATUAN  [HARGA_TOTAL]
                is_total_only_line = (
                    len(next_money) >= 1 and
                    not next_qty and
                    not next_unit and
                    not re.search(r'[a-zA-Z]', next_line)
                )
                if is_total_only_line and i + 2 < len(lines):
                    detail_line = lines[i + 2]
                    detail_money = _extract_money(detail_line)
                    detail_qty = _extract_qty(detail_line)
                    detail_unit = _extract_unit(detail_line)
                    if detail_qty and detail_unit and len(detail_money) >= 1:
                        name = line.strip()
                        qty = detail_qty
                        unit = detail_unit
                        line_total = next_money[-1][0]  # total dari baris 2
                        unit_price = detail_money[0][0] if detail_money else line_total / qty
                        if len(name) >= 2 and line_total >= 100:
                            items.append(ReceiptItem(
                                name=name.upper(),
                                qty=qty,
                                unit=unit or "",
                                unit_price=unit_price,
                                line_total=line_total,
                            ))
                        i += 3
                        continue

                # Baris berikutnya valid: punya harga DAN (qty ATAU unit)
                # Contoh tanpa qty: "PAKx  45.000=  45.000"
                has_detail = len(next_money) >= 1 and (next_qty is not None or next_unit is not None)
                if has_detail:
                    name = line.strip()
                    qty = next_qty or 1.0
                    unit = next_unit

                    if len(next_money) >= 2:
                        unit_price = next_money[-2][0]
                        line_total = next_money[-1][0]
                    else:
                        unit_price = next_money[-1][0]
                        line_total = next_money[-1][0]

                    if len(name) >= 2:
                        items.append(ReceiptItem(
                            name=name.upper(),
                            qty=qty,
                            unit=unit or "",
                            unit_price=unit_price,
                            line_total=line_total,
                        ))
                    i += 2
                    continue

        i += 1
    return items


def _parse_items_format_b(lines: list) -> list:
    """
    Parse format B: nama + detail dalam satu baris.
    Contoh: "KLIP REC 650 ML 2 SLOP 35,000 70,000"
    """
    header_words_b = {'jl', 'jln', 'jalan', 'gg', 'area', 'pertokoan',
                      'mall', 'ruko', 'komplek', 'selong', 'lombok',
                      'mataram', 'telp', 'fax', 'kasir',
                      'wa', 'instagram', 'facebook', 'tgl', 'user',
                      'sc', 'pel', 'no', 'sub'}
    items = []
    for line in lines:
        if not _is_item_name_line(line):
            continue
        words_in_line = set(re.findall(r'[a-zA-Z]+', line.lower()))
        if words_in_line & header_words_b:
            continue
        # Skip nomor struk: SI01-2606-0728, SB-9926F03I4915
        if re.match(r'^[A-Z]{2,4}[0-9-]', line.strip()):
            continue
        money_vals = _extract_money(line)
        if len(money_vals) < 1:
            continue
        # Minimum satu nilai >= 1000 (bukan kode produk/volume)
        if not any(v >= 1000 for v, _ in money_vals):
            continue
        item = _parse_item_line_format_b(line)
        if item and len(item.name) >= 2:
            items.append(item)
    return items



def _parse_items_format_mrdiy(lines: list) -> list:
    """
    Parse format D — MR D.I.Y.

    Pola per item (2 varian):
    Varian 1 (struk panjang):
      NAMA ITEM
      CB023C8025 - 12/24  73,000   ← SKU-batch
      6953901701900  1 X  73,000   ← barcode + qty + total

    Varian 2 (struk pendek):
      NAMA ITEM
      12/24  73,000                ← batch saja
      1 x  75,000                  ← qty + harga (tanpa barcode)
      atau langsung harga saja
    """
    MRDIY_SKIP = {
        'change', 'cash', 'ppn', 'included', 'total', 'qty', 'item',
        'invoice', 'operator', 'penukaran', 'waktu', 'customer', 'care',
        'pesan', 'whatsapp', 'email', 'website', 'yuk', 'bantu',
        'kualitas', 'pelayanan', 'senin', 'jumat', 'gedung', 'jalan',
        'jakarta', 'selatan', 'ruko', 'selong', 'pancor',
    }

    def _is_sku_line(line):
        """SKU MR DIY: CB023C8025 - 12/24 atau 12/24 (batch number)."""
        stripped = line.strip()
        # SKU: huruf+angka + " - " + angka/angka
        if re.match(r'^[A-Z]{2,}\d+[A-Z0-9]*\s+-\s+\d+/\d+', stripped):
            return True
        # Batch saja: "12/24  harga" atau "10/189  harga" (spasi atau tab)
        if re.match(r'^\d+/\d+[\s\t]+[\d,]+', stripped):
            return True
        return False

    def _is_barcode_price_line(line):
        """Baris barcode (7+ digit) atau qty-harga sederhana '1 x harga'."""
        stripped = line.strip()
        if re.match(r'^\d{7,}[\s\t]', stripped):
            return True
        # "1 x  75,000" atau "1 X  22,000" — qty + harga tanpa barcode
        if re.match(r'^\d{1,3}\s+[xX]\s+[\d,]+', stripped):
            return True
        return False

    def _extract_item_price(line):
        money = _extract_money(line)
        if not money:
            return None, None, None
        # Filter barcode di awal
        barcode_val = None
        m_bc = re.match(r'^(\d{7,})', line.strip())
        if m_bc:
            try:
                barcode_val = float(m_bc.group(1).replace(',', '').replace('.', ''))
            except Exception:
                pass
        money = [(v, p) for v, p in money if v != barcode_val]
        if not money:
            return None, None, None

        qty_match = re.search(r'(\d{1,3})\s*[xX]\s*(\d)', line)
        qty = float(qty_match.group(1)) if qty_match else 1.0
        if len(money) >= 2:
            unit_price, line_total = money[-2][0], money[-1][0]
        else:
            unit_price = line_total = money[-1][0]
        return qty, unit_price, line_total

    items = []
    i = 0
    while i < len(lines):
        line = lines[i]
        has_alpha = bool(re.search(r'[A-Za-z]', line))
        is_sku = _is_sku_line(line)
        is_barcode = _is_barcode_price_line(line)

        # Skip baris non-item
        if not has_alpha or is_sku or is_barcode:
            i += 1
            continue
        # Skip "1 x 75,000" — qty-harga bukan nama item
        if re.match(r'^\d+\s+[xX]\s+[\d,]+', line.strip()):
            i += 1
            continue
        words = set(re.findall(r'[a-zA-Z]+', line.lower()))
        if words & MRDIY_SKIP:
            i += 1
            continue
        if not _is_item_name_line(line):
            i += 1
            continue

        name_raw = re.sub(r'\t.*', '', line).strip()
        name_clean = name_raw.rstrip('-').strip()

        # Cari harga: skip baris SKU, ambil barcode/harga
        j = i + 1
        sku_price = None

        # Lewati semua baris SKU, simpan harga dari SKU ter-join
        while j < len(lines) and _is_sku_line(lines[j]):
            sku_money = _extract_money(lines[j])
            valid_sku = [v for v, p in sku_money if 100 <= v < 5_000_000]
            if valid_sku:
                sku_price = valid_sku[-1]
            j += 1

        if j < len(lines) and _is_barcode_price_line(lines[j]):
            qty, unit_price, line_total = _extract_item_price(lines[j])
            if line_total and line_total >= 100 and len(name_clean) >= 2:
                items.append(ReceiptItem(
                    name=name_clean.upper()[:60],
                    qty=qty or 1.0,
                    unit="",
                    unit_price=unit_price or line_total,
                    line_total=line_total,
                ))
            i = j + 1
            continue

        # Baris harga murni: "22,000"
        if j < len(lines) and re.match(r'^[\d,.]+\s*$', lines[j].strip()):
            price_money = _extract_money(lines[j])
            if price_money:
                lt = price_money[-1][0]
                if lt >= 100 and len(name_clean) >= 2:
                    items.append(ReceiptItem(
                        name=name_clean.upper()[:60], qty=1.0, unit='',
                        unit_price=lt, line_total=lt,
                    ))
                i = j + 1
                continue

        # Fallback: harga dari SKU ter-join ("10/189\t22,000\t22,000")
        if sku_price and sku_price >= 100 and len(name_clean) >= 2:
            items.append(ReceiptItem(
                name=name_clean.upper()[:60], qty=1.0, unit='',
                unit_price=sku_price, line_total=sku_price,
            ))
            i = j
            continue

        i += 1
    return items


def _classify_line(line: str) -> str:
    if _matches_any(line, SKIP_LINE_PATTERNS): return 'skip'
    if _matches_any(line, CHANGE_KEYWORDS): return 'change'
    if _matches_any(line, SAVINGS_KEYWORDS): return 'skip'   # hemat/voucher bukan item
    if _matches_any(line, TOTAL_KEYWORDS): return 'total'    # cek total dulu (Total Bayar > Bayar)
    if _matches_any(line, PAYMENT_KEYWORDS): return 'payment'
    if _matches_any(line, DISCOUNT_KEYWORDS): return 'discount'
    if _matches_any(line, TAX_KEYWORDS): return 'tax'
    return 'unknown'



def _join_fragmented_lines(lines: list) -> list:
    """
    Sambungkan baris-baris terfragmentasi dari OCR.
    
    Pattern yang ditangani:
    1. NAMA → QTY UNIT → HARGA TOTAL        (3 baris terpisah)
    2. NAMA → QTY UNIT HARGA → TOTAL        (2+1 baris)
    3. KEYWORD = → ANGKA                    (finansial terpotong)
    4. Baris pure angka/operator sendirian  → sambung ke atas
    """
    if not lines:
        return lines

    import re as _re

    def _has_money(line):
        for m in _re.finditer(r'\d{1,3}(?:[.,]\d{3})+|\d{4,}', line):
            raw = m.group(0).replace('.','').replace(',','')
            if raw.isdigit() and float(raw) >= 1000:
                return True
        return False

    def _has_unit(line):
        units = {'slop','pack','pak','pakx','pcs','pc','btl','klg','kg',
                 'gr','gram','ltr','box','dus','rim','lusin','sachet'}
        words = _re.findall(r'[a-zA-Z]+', line.lower())
        return any(w in units for w in words)

    def _has_qty(line):
        m = _re.match(r'^\s*(\d{1,3})\s', line)
        if m and 1 <= int(m.group(1)) <= 999:
            return True
        return False

    def _is_pure_number_line(line):
        """Baris yang hanya berisi angka/harga."""
        stripped = line.strip()
        return bool(_re.match(r'^[\d.,\s\t]+$', stripped)) and _has_money(stripped)

    def _is_operator_line(line):
        return line.strip() in ['=', ':', '-', '+', '=']

    # ── Pass 1: sambung baris qty+unit (tanpa harga) dengan baris harga berikutnya ──
    result = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Baris operator/pure-number → sambung ke atas
        if (_is_operator_line(line) or _is_pure_number_line(line)) and result:
            result[-1] = result[-1] + '\t' + line
            i += 1
            continue

        # Baris "MI" / "ML" / "MG" pendek setelah baris item → sambung ke atas
        # Contoh: "Diamond Sleeve 1000\tx5" + "MI" → unit yang terpisah
        if re.match(r'^M[IiLlGg]$', line.strip()) and result:
            result[-1] = result[-1] + ' ' + line.strip()
            i += 1
            continue

        # Baris qty+unit TANPA harga → cek baris berikutnya
        if _has_qty(line) and _has_unit(line) and not _has_money(line):
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                # Baris berikutnya adalah angka/harga → gabung
                if _is_pure_number_line(next_line) or _has_money(next_line):
                    result.append(line + '\t' + next_line)
                    i += 2
                    continue

        result.append(line)
        i += 1

    # ── Pass 2: sambung keyword finansial yang angkanya di baris berikutnya ──
    result2 = []
    i = 0
    while i < len(result):
        line = result[i]
        has_financial_kw = any(_re.search(p, line.lower()) for p in [
            r'\btotal\b', r'\btunai\b', r'\bbayar\b', r'\bkembali\b',
            r'\bdiskon\b', r'\bsubtotal\b', r'\brotal\b', r'\brunai\b',
            r'\bcash\b', r'\bchange\b', r'\bkredit\b', r'\bdebit\b',
        ])
        if has_financial_kw and not _has_money(line) and i + 1 < len(result):
            next_line = result[i + 1].strip()
            # Gabung jika baris berikutnya adalah angka (termasuk "0")
            if _is_pure_number_line(next_line) or re.match(r'^\d+$', next_line.strip()):
                result2.append(line + '\t' + next_line)
                i += 2
                continue
        result2.append(line)
        i += 1

    return result2

def _normalize_ocr(text: str) -> str:
    """Normalisasi karakter OCR noise.
    Å→A, å→a, karakter Latin extended → ASCII equivalent.
    """
    import unicodedata
    # Tangani € SEBELUM unicode normalize (€ akan hilang saat encode ASCII)
    text = re.sub(r'(\d+),00€', lambda m: str(int(m.group(1)) * 1000), text)
    text = re.sub(r'(\d+),(\d{2})€', lambda m: str(int(m.group(1)) * 1000 + int(m.group(2)) * 10), text)
    # Normalize unicode ke ASCII closest equivalent
    normalized = unicodedata.normalize('NFKD', text)
    # Encode ke ASCII, ignore non-ASCII, decode back
    result = normalized.encode('ascii', 'ignore').decode('ascii')
    # Bersihkan karakter aneh yang sering muncul di OCR thermal
    result = result.replace('@', '').replace("'i", '').replace("'", '')
    # Bersihkan ":" atau spasi di awal baris item (OCR noise Indomaret)
    lines_out = []
    for line in result.splitlines():
        cleaned = re.sub(r'^[:\s]+(?=[A-Z])', '', line)
        cleaned = re.sub(r'\t+', '\t', cleaned)
        # Normalisasi karakter OCR noise dalam angka
        # "100,00€" → "100000" (MR DIY: ribuan dengan 2 desimal + € noise)
        cleaned = re.sub(r'(\d+),00€', lambda m: str(int(m.group(1)) * 1000), cleaned)
        cleaned = re.sub(r'(\d+),(\d{2})€', lambda m: str(int(m.group(1)) * 1000 + int(m.group(2)) * 10), cleaned)
        # Juga handle angka format "100,00" tanpa € yang muncul di MR DIY (ribuan)
        # Hanya jika nilainya kecil (< 1000) dan tampak seperti ribuan
        cleaned = cleaned.replace('€', '0')   # sisa € fallback → 43,50€ → 43,500
        cleaned = cleaned.replace('к', 'R')   # кр → Rp (Cyrillic к)
        cleaned = cleaned.replace('К', 'R')
        cleaned = re.sub(r'(?<=[\d])O(?=[\d,.])', '0', cleaned)  # huruf O di antara angka
        # "RD363.636" → "Rp363.636" (OCR noise: D bukan p)
        cleaned = re.sub(r'\bRD(\d)', r'Rp\1', cleaned)
        # "Kp" → "Rp" (OCR noise: K bukan R)
        cleaned = re.sub(r'\bKp\s', 'Rp ', cleaned)
        # "Pisc." → "Disc." (OCR noise: P→D)
        cleaned = re.sub(r'\bPisc\.', 'Disc.', cleaned)
        # "Ju1" → "Jul" (OCR noise: 1 bukan l)
        cleaned = re.sub(r'\bJu1\b', 'Jul', cleaned)
        # "31-Ju1-2027" format → skip handled via SKIP_LINE_PATTERNS
        # "lotal/otal Belania/Belanja" → "Total Belanja" (OCR noise)
        cleaned = re.sub(r'\b[lL]?otal\s+[Bb]elan[ji]a', 'Total Belanja', cleaned)
        # "Total Iten" → "Total Item"
        cleaned = re.sub(r'\bTotal\s+Iten\b', 'Total Item', cleaned, flags=re.IGNORECASE)
        # "[unai" → "Tunai"
        cleaned = re.sub(r'^\[unai\b', 'Tunai', cleaned)

        # "236.868,00" → "236868" (invoice: titik=ribuan, koma=desimal)
        content_tmp = re.sub(r'(\d+)\.(\d{3}),(\d{2})\b', lambda m: str(int(m.group(1))*1000 + int(m.group(2))), cleaned)
        cleaned = content_tmp
        # "Rp 315. 000" → "Rp 315000" (spasi setelah titik ribuan)
        cleaned = re.sub(r'(Rp\.?\s*\d+)\.\s+(\d{3})\b', r'\1\2', cleaned)
        cleaned = re.sub(r'(\d+)\.\s+(\d{3})\b', r'\1\2', cleaned)
        # "MI" di baris sendiri setelah nama item → kemungkinan "Ml" (mililiter)
        # handled di join_fragmented
        lines_out.append(cleaned)
    return '\n'.join(lines_out)




def _is_klikindo_screenshot(text: str) -> bool:
    """Deteksi screenshot detail pesanan Sukanda / Klikindomaret / app distributor."""
    indicators = [
        r'\bharga\s+satuan\b',
        r'\bdikirim\s+ke\b',
        r'\bberanda\b',
        r'\bbeli\s+lagi\b',
        r'\b\d{8,}\b',   # SKU angka panjang
    ]
    text_lower = text.lower()
    hits = sum(1 for p in indicators if re.search(p, text_lower))
    # Cukup 1 hit + Harga Satuan untuk format Sukanda/app distributor
    # Karena screenshot bisa terpotong (tanpa header lengkap)
    has_harga_satuan = re.search(r'Harga Satuan', text, re.IGNORECASE) is not None
    has_total_produk = re.search(r'Total\s+Rp[\d.,]+', text) is not None
    return has_harga_satuan and (hits >= 1 or has_total_produk)


def _parse_klikindo_orders(text: str) -> OcrResult:
    """
    Parse screenshot detail pesanan Klikindomaret/Tokopedia.

    Pola per item:
      NAMA ITEM • detail  x1/x2/...
      SKU_ANGKA
      Harga Satuan        RpXXX
      Total               RpXXX
    """
    from datetime import date as _date
    result = OcrResult(raw_text=text)

    # Deteksi nama tujuan dari "Dikirim ke: NAMA TOKO"
    dikirim_m = re.search(r'Dikirim\s+ke\s*:\s*([A-Z][A-Z\s]+?)(?:\t|\n|$)', text)
    if dikirim_m:
        dest = dikirim_m.group(1).strip().title()
        result.merchant = "Sukanda Jaya" 
    elif re.search(r'klikindomaret', text, re.IGNORECASE):
        result.merchant = "Indomaret"
    elif re.search(r'tokopedia', text, re.IGNORECASE):
        result.merchant = "Tokopedia"
    else:
        result.merchant = "Pesanan Online"

    result.tx_date = _date.today()

    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = _join_fragmented_lines(raw_lines)
    items = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Baris "Total   RpXXX" → ini total per item
        total_match = re.match(r'^Total\s+Rp([\d.,]+)$', line, re.IGNORECASE)
        if total_match:
            raw = total_match.group(1).replace('.', '').replace(',', '')
            if raw.isdigit():
                line_total = float(raw)
                # Ambil nama item dari baris sebelumnya (skip SKU dan Harga Satuan)
                name = None
                qty = 1.0
                unit_price = line_total
                # Cari ke belakang
                j = i - 1
                while j >= 0:
                    prev = lines[j]
                    # Harga Satuan line
                    hs = re.match(r'^Harga Satuan\s+Rp([\d.,]+)$', prev, re.IGNORECASE)
                    if hs:
                        raw2 = hs.group(1).replace('.', '').replace(',', '')
                        if raw2.isdigit():
                            unit_price = float(raw2)
                        j -= 1
                        continue
                    # SKU angka murni (8+ digit)
                    if re.match(r'^\d{6,}$', prev.strip()):
                        j -= 1
                        continue
                    # Harga Satuan — sudah diambil, skip
                    if re.match(r'^Harga\s+Satuan', prev, re.IGNORECASE):
                        j -= 1
                        continue
                    # Baris nama item
                    if (re.search(r'[A-Za-z]', prev) and
                        not re.match(r'^(Rp|Total|Harga|Beli|Beranda)', prev, re.IGNORECASE) and
                        not re.match(r'^[xX]\d+\s*$', prev.strip()) and
                        not re.match(r'^\d{1,2}$', prev.strip())):
                        qty_m = re.search(r'[xX](\d+)', prev)
                        if qty_m:
                            qty = float(qty_m.group(1))
                        nc = prev
                        nc = re.sub(r'[\s\t]+\d{6,}.*$', '', nc)    # hapus SKU ter-join (tab/spasi)
                        nc = re.sub(r'[\s\t]+[Mm][IiLlGg]\b', '', nc)  # hapus MI/Ml suffix
                        nc = re.sub(r'\t.*', '', nc)                 # hapus setelah tab
                        nc = re.sub(r'\s+[xX]\d+\s*$', '', nc)     # hapus qty di akhir
                        nc = re.sub(r'^\S+\s*[\u2022\u00b7]\s*', '', nc)  # hapus "Brand • "
                        nc = nc.strip()
                        if len(nc) >= 3:
                            name = nc
                        break
                    break

                if name and line_total >= 100:
                    items.append(ReceiptItem(
                        name=name.upper()[:60],
                        qty=qty,
                        unit="",
                        unit_price=unit_price,
                        line_total=line_total,
                    ))
        i += 1

    total = sum(item.line_total for item in items) if items else None
    result.items = items
    result.total = total
    result.grand_total = total

    score = 0.3
    score += 0.4 if items else 0.0
    score += 0.3 if total else 0.0
    result.confidence = round(score, 2)

    logger.info(f"[OCR] Klikindomaret: {len(items)} items total={total}")
    return result




def _is_shopee_detail(text: str) -> bool:
    """Deteksi halaman Rincian Pesanan Shopee (per order detail)."""
    indicators = [
        r'rincian\s+pesanan',
        r'subtotal\s+produk',
        r'total\s+pesanan',
        r'batalkan\s+pesanan',
        r'hubungi\s+penjual',
        r'subtotal\s+pengiriman',
        r'voucher\s+shopee',
        r'biaya\s+layanan',
    ]
    text_lower = text.lower()
    hits = sum(1 for p in indicators if re.search(p, text_lower))
    return hits >= 3


def _parse_shopee_detail(text: str) -> OcrResult:
    """
    Parse halaman Rincian Pesanan Shopee.

    DUA SKENARIO yang ditangani secara berbeda:

    Skenario A -- 1 item produk:
      Simpan sebagai 1 transaksi dengan total = Total Pesanan (bukan harga produk).
      Komponen biaya (ongkir net, voucher, layanan) ditampilkan di summary
      tapi TIDAK disimpan sebagai transaksi terpisah karena sudah termasuk dalam
      Total Pesanan yang dibayar.

    Skenario B -- lebih dari 1 item produk:
      Setiap item disimpan dengan harga proporsional dari Total Pesanan.
      Proporsional = (harga_item / subtotal_produk) * total_pesanan
      Sehingga sum(line_total semua item) = Total Pesanan.
      Biaya tambahan sudah terdistribusi proporsional ke setiap item.

    Dalam kedua skenario, yang disimpan ke DB selalu Total Pesanan,
    bukan Subtotal Produk.
    """
    from datetime import date as _date
    from bot.utils.formatters import fmt_rupiah
    result = OcrResult(raw_text=text)
    result.tx_date = _date.today()
    result.merchant = "Shopee"
    result.is_shopee_detail = True

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Merchant ──
    merchant = None
    for i, line in enumerate(lines):
        if re.search(r'Star\+|Mall\s*\|', line):
            clean = re.sub(r'(Star\+|Mall\s*\|\s*ORI)\s*\t?\s*', '', line).strip()
            clean = re.sub(r'\t.*', '', clean).strip()
            if clean and len(clean) > 2:
                merchant = clean
                break
        # "COLOOMP >" pola toko tanpa Star+
        if re.search(r'[›>]\s*$', line) and not re.search(r'ubah|hubungi|batalkan|lihat', line, re.IGNORECASE):
            candidate = re.sub(r'\s*[›>]\s*$', '', line).split('\t')[0].strip()
            if 2 <= len(candidate) <= 50:
                merchant = candidate
                break

    result.merchant = (merchant or "Shopee").replace('\t', ' ').strip()

    # ── Ekstrak semua komponen harga ──
    def _parse_rp(pattern, src):
        m = re.search(pattern, src, re.IGNORECASE)
        if m:
            raw = m.group(1).replace('.', '').replace(',', '')
            return float(raw) if raw.isdigit() else 0.0
        return 0.0

    subtotal_produk  = _parse_rp(r'Subtotal\s+Produk\s*[\t:]+Rp([\d.,]+)', text)
    subtotal_ongkir  = _parse_rp(r'Subtotal\s+Pengiriman\s*[\t:]+Rp([\d.,]+)', text)
    diskon_ongkir    = _parse_rp(r'(?:Subtotal\s+)?Diskon\s+Pengiriman\s*[\t:]+-?Rp([\d.,]+)', text)
    voucher_shopee   = _parse_rp(r'Voucher\s+Shopee[^\n]*[\t:]-?Rp([\d.,]+)', text)
    voucher_toko     = _parse_rp(r'Voucher\s+Toko[^\n]*[\t:]-?Rp([\d.,]+)', text)
    biaya_layanan    = _parse_rp(r'Biaya\s+Layanan[^\n]*[\t:]Rp([\d.,]+)', text)

    # ── Total Pesanan (yang benar-benar dibayar) ──
    total_pesanan = 0.0
    total_m = re.search(r'Total\s+Pesanan\s*:\s*Rp([\d.,]+)', text, re.IGNORECASE)
    if total_m:
        raw = total_m.group(1).replace('.', '').replace(',', '')
        if raw.isdigit():
            total_pesanan = float(raw)
    result.total = total_pesanan

    # ── Parse items produk ──
    # ── Noise dari thumbnail gambar produk ──
    # OCR membaca teks di dalam gambar thumbnail (label produk, merk, dll)
    # Ciri: teks pendek (< 6 kata), muncul SEBELUM nama item sebenarnya,
    # sering mengandung nama brand/merk berulang atau kata-kata acak
    # Solusi: anchor detection -- nama item valid harus diikuti oleh:
    # - baris varian (ukuran/warna/qty kecil), ATAU
    # - baris "x1"/"x2", ATAU
    # - baris "Rp..."
    # Teks thumbnail tidak punya anchor ini.

    SKIP_KEYWORDS = re.compile(
        r'subtotal|total|tunai|kembali|diskon|voucher|pengiriman|layanan|'
        r'butuh|bantuan|hubungi|batalkan|penjual|spx|jne|sicepat|anteraja|'
        r'jnt|ninja|flash|alamat|lihat|star\+|mall\s*\|?\s*ori|standard|express|'
        r'pesanan|dibuat|menunggu|sedang|diantarkan|transit|kurir|'
        r'termurah|kemasan|terlaris|terbaru|flash\s*sale|bebas\s*ongkir',
        re.IGNORECASE
    )

    # Pola yang mengindikasikan baris varian (bukan nama item)
    VARIANT_PATTERN = re.compile(
        r'^\d+\s*(kg|gr|gram|ml|liter|ltr|pcs|pack|btl|cm|mm|lusin|dus|karton|'
        r'gr|ons|buah|biji|lembar|helai|pasang|set|unit|box)\s*$',
        re.IGNORECASE
    )

    def _is_qty_line(s):
        return bool(re.match(r'^[xX]\s*\d+$', s.strip()))

    def _is_price_line(s):
        return bool(re.match(r'^Rp[\d.,\s]+', s.strip()))

    def _is_name_candidate(s):
        """Apakah baris ini kandidat nama item? Bukan thumbnail noise."""
        if len(s) < 4: return False
        if SKIP_KEYWORDS.search(s): return False
        if _is_qty_line(s): return False
        if _is_price_line(s): return False
        if VARIANT_PATTERN.match(s.strip()): return False
        # Nama item biasanya mengandung huruf, bukan hanya angka/simbol
        if not re.search(r'[A-Za-z]{2,}', s): return False
        return True

    items_raw = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip merchant name line
        if merchant and merchant.replace('>', '').strip().lower() in line.lower():
            i += 1
            continue

        # Cari nama item yang valid
        if not _is_name_candidate(line):
            i += 1
            continue

        # Bersihkan kandidat nama
        name_raw = line
        # Hapus teks dalam tab (ambil bagian terpanjang)
        if '\t' in name_raw:
            parts = [p.strip() for p in name_raw.split('\t')]
            valid_parts = [p for p in parts if _is_name_candidate(p)]
            name_raw = max(valid_parts, key=len) if valid_parts else parts[0]

        name_raw = re.sub(r'\s*\.{3,}$', '', name_raw).strip()  # hapus "..."
        name_raw = re.sub(r'[…]+$', '', name_raw).strip()
        name_raw = re.sub(r'^[•·\-]+\s*', '', name_raw).strip()

        # Peek ke depan: nama item HARUS diikuti varian atau qty atau harga
        # dalam 3 baris berikutnya
        j = i + 1
        has_anchor = False
        peek_limit = min(j + 4, len(lines))
        for k in range(j, peek_limit):
            nxt = lines[k].strip()
            if _is_qty_line(nxt) or _is_price_line(nxt) or VARIANT_PATTERN.match(nxt):
                has_anchor = True
                break
            if SKIP_KEYWORDS.search(nxt):
                break

        if not has_anchor:
            # Bukan nama item -- kemungkinan teks thumbnail, skip
            i += 1
            continue

        # Ambil qty dan harga
        qty = 1
        price = None

        while j < len(lines):
            nxt = lines[j].strip()

            # Baris qty: "x1", "x2", "x4"
            if _is_qty_line(nxt):
                qty = int(re.search(r'\d+', nxt).group())
                j += 1
                break

            # Baris harga: "Rp290.000"
            if _is_price_line(nxt):
                break

            # Baris varian: "1 kg", "500g", "1000 GR" -- lewati, jangan tambah ke nama
            if VARIANT_PATTERN.match(nxt):
                j += 1
                continue

            # Baris qty inline: "1 kg   x4"
            qty_inline = re.search(r'[xX](\d+)\s*$', nxt)
            if qty_inline:
                qty = int(qty_inline.group(1))
                j += 1
                break

            # Baris tambahan nama (lanjutan dari nama terpotong) -- hati-hati
            # Hanya tambahkan jika sangat pendek DAN bukan thumbnail noise
            if _is_name_candidate(nxt) and len(nxt) <= 20:
                name_raw = (name_raw + ' ' + nxt).strip()
                j += 1
            else:
                break

        # Ambil harga
        if j < len(lines):
            price_line = lines[j].strip()
            if _is_price_line(price_line):
                prices_m = re.findall(r'Rp([\d.,]+)', price_line)
                if prices_m:
                    raw = prices_m[-1].replace('.', '').replace(',', '')
                    if raw.isdigit():
                        price = float(raw)
                j += 1

        if name_raw and len(name_raw) >= 3 and price and price >= 100:
            # Dedup: jangan tambahkan jika nama IDENTIK dengan item sebelumnya
            # Bandingkan lebih panjang (25 karakter) agar "Pitcher" vs "Milk Jug" tidak dianggap sama
            is_dup = any(
                name_raw.lower()[:25] == prev['name'].lower()[:25]
                for prev in items_raw
            )
            if not is_dup:
                items_raw.append({'name': name_raw, 'qty': float(qty), 'price': price})
            i = j
        else:
            i += 1

    # ── SKENARIO A vs B ──────────────────────────────────────────────────────
    # Distribusi total pesanan ke item secara proporsional
    items = []
    n_items = len(items_raw)

    if n_items == 0:
        # Tidak ada item terdeteksi -- simpan sebagai 1 transaksi total
        pass

    elif n_items == 1:
        # SKENARIO A: 1 item
        # line_total = Total Pesanan (bukan harga produk)
        item = items_raw[0]
        items.append(ReceiptItem(
            name=item['name'].upper()[:60],
            qty=item['qty'],
            unit="",
            unit_price=total_pesanan / item['qty'] if item['qty'] > 0 and total_pesanan > 0 else item['price'],
            line_total=total_pesanan if total_pesanan > 0 else item['price'],
        ))

    else:
        # SKENARIO B: multiple items
        # Distribusi Total Pesanan secara proporsional berdasarkan harga item
        sum_prices = sum(d['price'] for d in items_raw)
        for item in items_raw:
            if sum_prices > 0 and total_pesanan > 0:
                ratio = item['price'] / sum_prices
                proportional_total = round(ratio * total_pesanan)
            else:
                proportional_total = item['price']

            # Bersihkan duplikasi kata di nama (misal "58MM 58MM" -> "58MM")
            import re as _re
            clean_name = item['name'].upper()
            words = clean_name.split()
            seen = []
            for w in words:
                if w not in seen:
                    seen.append(w)
            clean_name = ' '.join(seen)[:60]

            items.append(ReceiptItem(
                name=clean_name,
                qty=item['qty'],
                unit="",
                unit_price=round(proportional_total / item['qty']) if item['qty'] > 0 else proportional_total,
                line_total=proportional_total,
            ))

        # Koreksi pembulatan agar sum tepat = total_pesanan
        if total_pesanan > 0 and items:
            diff = int(total_pesanan) - sum(int(i.line_total) for i in items)
            if diff != 0:
                items[-1].line_total += diff
                items[-1].unit_price = items[-1].line_total / items[-1].qty

    result.items = items

    # ── Summary untuk konfirmasi ──────────────────────────────────────────────
    net_ongkir = max(0, subtotal_ongkir - diskon_ongkir)
    toko_display = result.merchant

    if n_items <= 1:
        # Skenario A: tampilkan breakdown lengkap
        summary_lines = [f"🛍 *Shopee -- {toko_display}*\n"]
        if items:
            item = items[0]
            qty_str = f" ({int(item.qty)} pcs)" if item.qty > 1 else ""
            summary_lines.append(f"Produk: {item.name}{qty_str}")
            summary_lines.append(f"Harga produk : Rp{int(items_raw[0]['price'] if items_raw else total_pesanan):,}".replace(',', '.'))
        summary_lines.append("")
        if net_ongkir > 0:
            summary_lines.append(f"Ongkir (net) : Rp{int(net_ongkir):,}".replace(',', '.'))
        if voucher_shopee > 0:
            summary_lines.append(f"Voucher Shopee : -Rp{int(voucher_shopee):,}".replace(',', '.'))
        if voucher_toko > 0:
            summary_lines.append(f"Voucher Toko   : -Rp{int(voucher_toko):,}".replace(',', '.'))
        if biaya_layanan > 0:
            summary_lines.append(f"Biaya layanan  : Rp{int(biaya_layanan):,}".replace(',', '.'))
        summary_lines.append("")
        summary_lines.append(f"*Total dibayar : Rp{int(total_pesanan):,}*".replace(',', '.'))

    else:
        # Skenario B: tampilkan distribusi per item
        summary_lines = [f"🛍 *Shopee -- {toko_display}* ({n_items} item)\n"]
        for item in items:
            qty_str = f" x{int(item.qty)}" if item.qty > 1 else ""
            summary_lines.append(f"  • {item.name}{qty_str}")
            summary_lines.append(f"    Rp{int(item.line_total):,}".replace(',', '.') + " (proporsional dari total)")
        summary_lines.append("")
        if net_ongkir > 0:
            summary_lines.append(f"Ongkir (net)   : Rp{int(net_ongkir):,}".replace(',', '.'))
        if voucher_shopee > 0:
            summary_lines.append(f"Voucher Shopee : -Rp{int(voucher_shopee):,}".replace(',', '.'))
        if voucher_toko > 0:
            summary_lines.append(f"Voucher Toko   : -Rp{int(voucher_toko):,}".replace(',', '.'))
        if biaya_layanan > 0:
            summary_lines.append(f"Biaya layanan  : Rp{int(biaya_layanan):,}".replace(',', '.'))
        summary_lines.append("")
        summary_lines.append(f"*Total dibayar : Rp{int(total_pesanan):,}*".replace(',', '.'))
        summary_lines.append(f"_(didistribusi proporsional ke {n_items} item)_")

    result.shopee_summary = "\n".join(summary_lines)
    result.shopee_item = items[0].name if items else "Item Shopee"
    result.shopee_qty = int(items[0].qty) if items else 1

    score = 0.3
    score += 0.3 if result.total else 0.0
    score += 0.2 if items else 0.0
    score += 0.2 if merchant else 0.0
    result.confidence = round(score, 2)

    logger.info(
        f"[OCR] Shopee detail: merchant={result.merchant!r} "
        f"scenario={'A(1 item)' if n_items <= 1 else f'B({n_items} items)'} "
        f"total={result.total}"
    )
    return result

    from datetime import date as _date
    from bot.utils.formatters import fmt_rupiah
    result = OcrResult(raw_text=text)
    result.tx_date = _date.today()
    result.merchant = "Shopee"
    result.is_shopee_detail = True

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Merchant ──
    merchant = None
    for i, line in enumerate(lines):
        # "Star+\tGranology" atau "Mall | ORI\tBubuqu Official Store"
        if re.search(r'Star\+|Mall\s*\|', line):
            clean = re.sub(r'(Star\+|Mall\s*\|\s*ORI)\s*\t?\s*', '', line).strip()
            clean = re.sub(r'\t.*', '', clean).strip()
            if len(clean) >= 2:
                merchant = clean
                break

    result.merchant = merchant or "Shopee"

    # ── Item produk ──
    # Pola: nama item (mengandung huruf, bukan keyword)
    # diikuti "x1" (bisa inline atau baris berikutnya)
    # diikuti harga "Rp127.500 Rp78.990" (harga coret + harga asli)
    SKIP_KEYWORDS = re.compile(
        r'Subtotal|Total|Batalkan|Hubungi|Voucher|Biaya|Diskon|'
        r'Gratis|SiCepat|Alamat|Pengiriman|stoa|Butuh|Layanan|'
        r'Rincian|Pesanan|varlan|Random|Varian',
        re.IGNORECASE
    )

    items = []
    merchant_line_idx = 0
    for idx, line in enumerate(lines):
        if merchant and len(merchant) > 3 and merchant.lower() in line.lower():
            merchant_line_idx = idx + 1
            break
    i = 0
    while i < len(lines):
        line = lines[i]

        # Skip baris yang jelas bukan item (bukan berdasarkan posisi)
        if SKIP_KEYWORDS.search(line):
            i += 1
            continue
        if re.match(r'^Rp', line) or re.match(r'^\d{1,2}[:.]', line):
            i += 1
            continue
        if not re.search(r'[A-Za-z]{3,}', line):
            i += 1
            continue
        # Skip baris yang mengandung nama merchant
        if merchant and len(merchant) > 3 and merchant.lower() in line.lower():
            i += 1
            continue

        # Ekstrak nama — jika ada tab, ambil bagian terpanjang yang bukan harga/keyword
        # Contoh: "MSH MASTER\tPembersih Mesin Kopi Coffee Clean P..." → ambil nama produk
        if '\t' in line:
            parts = [p.strip() for p in line.split('\t') if p.strip()]
            valid = [p for p in parts
                     if len(p) >= 4
                     and not re.match(r'^Rp', p)
                     and not SKIP_KEYWORDS.search(p)
                     and not re.match(r'^[xX]\d+$', p)]
            name_raw = max(valid, key=len) if valid else parts[0]
        else:
            name_raw = line

        # Bersihkan prefix noise
        name_raw = re.sub(r'^[A-Za-z]{2,6}\s*[-\.]+\s*', '', name_raw)  # "Pi.-"
        name_raw = re.sub(r'^\d+\.\s*', '', name_raw)                    # "1. "
        name_raw = re.sub(r'^[•·\-]+\s*', '', name_raw)                  # bullet
        name_raw = re.sub(r'\t.*$', '', name_raw)                        # hapus sisa tab
        name_raw = re.sub(r'\s*\.{3}\s*$', '', name_raw).strip()         # hapus "..."
        name_raw = re.sub(r'[…]+\s*$', '', name_raw).strip()             # hapus "…"

        # Skip jika nama terlalu pendek atau masih keyword
        if len(name_raw) < 4 or SKIP_KEYWORDS.search(name_raw):
            i += 1
            continue

        # Cari qty di baris berikutnya (x1, x2, X1)
        qty = 1
        price = None
        j = i + 1

        # Gabungkan baris nama yang terpotong (Palm Sugar + Syrup)
        # Jika baris berikutnya bukan qty/harga/keyword → lanjutkan nama
        while j < len(lines):
            nxt = lines[j].strip()
            if re.match(r'^[xX]\d+$', nxt):
                qty = int(re.match(r'^[xX](\d+)$', nxt).group(1))
                j += 1
                break
            if re.match(r'^Rp[\d.,\s]+', nxt) or SKIP_KEYWORDS.search(nxt):
                break
            # Baris nama lanjutan (misal "Syrup", "x1" inline di baris yg sama)
            qty_inline = re.search(r'[xX](\d+)\s*$', nxt)
            if qty_inline:
                qty = int(qty_inline.group(1))
                ext = re.sub(r'\s*[xX]\d+\s*$', '', nxt).strip()
                if ext and not SKIP_KEYWORDS.search(ext):
                    name_raw = (name_raw + ' ' + ext).strip()
                j += 1
                break
            if not SKIP_KEYWORDS.search(nxt) and len(nxt) >= 2:
                name_raw = (name_raw + ' ' + nxt).strip()
                j += 1
            else:
                break

        # Cari harga — ambil harga TERAKHIR di baris harga (harga setelah diskon)
        if j < len(lines):
            price_line = lines[j].strip()
            prices_m = re.findall(r'Rp([\d.,]+)', price_line)
            if prices_m:
                raw = prices_m[-1].replace('.', '').replace(',', '')
                if raw.isdigit():
                    price = float(raw)
                j += 1

        if name_raw and len(name_raw) >= 3 and price and price >= 100:
            items.append(ReceiptItem(
                name=name_raw.upper()[:60],
                qty=float(qty),
                unit="",
                unit_price=price / qty if qty > 0 else price,
                line_total=price,
            ))
            i = j
        else:
            i += 1

    # ── Biaya tambahan = Total Pesanan - Subtotal Produk ──
    subtotal_produk = 0.0
    for line in lines:
        if re.search(r'Subtotal\s+Produk', line, re.IGNORECASE):
            m = re.search(r'Rp([\d.,]+)', line)
            if m:
                raw = m.group(1).replace('.', '').replace(',', '')
                if raw.isdigit():
                    subtotal_produk = float(raw)
            break

    # Total pesanan sudah diparse di bawah, parse dulu untuk bisa hitung biaya
    total_pesanan = 0.0
    total_m_early = re.search(r'Total\s+Pesanan\s*:\s*Rp([\d.,]+)', text, re.IGNORECASE)
    if total_m_early:
        raw = total_m_early.group(1).replace('.', '').replace(',', '')
        if raw.isdigit():
            total_pesanan = float(raw)

    biaya_tambahan = total_pesanan - subtotal_produk
    if biaya_tambahan > 100:  # lebih dari Rp100 baru dicatat
        items.append(ReceiptItem(
            name=f"BIAYA TAMBAHAN (Ongkir, Layanan, dll)",
            qty=1.0,
            unit="",
            unit_price=biaya_tambahan,
            line_total=biaya_tambahan,
        ))

    # ── Total Pesanan ──
    total_m = re.search(r'Total\s+Pesanan\s*:\s*Rp([\d.,]+)', text, re.IGNORECASE)
    if total_m:
        raw = total_m.group(1).replace('.', '').replace(',', '')
        if raw.isdigit():
            result.total = float(raw)

    # ── Parse komponen biaya untuk ditampilkan ──
    def _parse_rp(pattern, text):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace('.', '').replace(',', '')
            return float(raw) if raw.isdigit() else 0.0
        return 0.0

    subtotal_ongkir = _parse_rp(r'Subtotal\s+Pengiriman\s*[\t:]+Rp([\d.,]+)', text)
    diskon_ongkir = _parse_rp(r'(?:Subtotal\s+)?Diskon\s+Pengiriman\s*[\t:]+-?Rp([\d.,]+)', text)
    voucher_shopee = _parse_rp(r'Voucher\s+Shopee[^\n]*[\t:]-?Rp([\d.,]+)', text)
    voucher_toko = _parse_rp(r'Voucher\s+Toko[^\n]*[\t:]-?Rp([\d.,]+)', text)
    biaya_layanan = _parse_rp(r'Biaya\s+Layanan[^\n]*[\t:]Rp([\d.,]+)', text)

    result.items = items

    # ── Summary untuk konfirmasi -- tampilkan komponen harga ──
    summary_lines = [f"Shopee: *{result.merchant or 'Toko'}*\n"]
    for item in items:
        qty_str = f" x{int(item.qty)}" if item.qty > 1 else ""
        summary_lines.append(f"  • {item.name}{qty_str} -- Rp{int(item.line_total):,}".replace(',', '.'))
    summary_lines.append("")
    if subtotal_produk > 0:
        summary_lines.append(f"Subtotal produk : Rp{int(subtotal_produk):,}".replace(',', '.'))
    if subtotal_ongkir > 0:
        net_ongkir = subtotal_ongkir - diskon_ongkir
        if net_ongkir > 0:
            summary_lines.append(f"Ongkir (net)    : Rp{int(net_ongkir):,}".replace(',', '.'))
    if voucher_shopee > 0:
        summary_lines.append(f"Voucher Shopee  : -Rp{int(voucher_shopee):,}".replace(',', '.'))
    if voucher_toko > 0:
        summary_lines.append(f"Voucher Toko    : -Rp{int(voucher_toko):,}".replace(',', '.'))
    if biaya_layanan > 0:
        summary_lines.append(f"Biaya layanan   : Rp{int(biaya_layanan):,}".replace(',', '.'))
    if result.total:
        summary_lines.append(f"\nTotal dibayar   : *Rp{int(result.total):,}*".replace(',', '.'))

    result.shopee_summary = "\n".join(summary_lines)
    result.shopee_item = items[0].name if items else "Item Shopee"
    result.shopee_qty = int(items[0].qty) if items else 1

    score = 0.3
    score += 0.3 if result.total else 0.0
    score += 0.2 if items else 0.0
    score += 0.2 if merchant else 0.0
    result.confidence = round(score, 2)

    logger.info(f"[OCR] Shopee detail: merchant={result.merchant!r} items={len(items)} total={result.total}")
    return result






def _is_bank_transfer(text: str) -> bool:
    """Deteksi screenshot bukti transfer bank BCA."""
    text_lower = text.lower()
    indicators = [
        r'transfer\s+successful',
        r'transfer\s+amount',
        r'beneficiary\s+name',
        r'beneficiary\s+account',
        r'reference\s+no',
        r'source\s+of\s+fund',
        r'transaction\s+type.*transfer',
        r'transfer\s+to\s+bca',
        r'transfer\s+currency',
    ]
    hits = sum(1 for p in indicators if re.search(p, text_lower))
    return hits >= 3


def _try_parse_bca_transfer_belanja(text: str) -> 'OcrResult | None':
    """
    Parse transfer BCA sebagai belanja jika ada Remarks berisi keterangan barang.

    Format BCA Transfer:
      Transfer Successful
      Beneficiary Name   RAUCHSAN ABDI AKBAR
      Transfer Amount    IDR 850,000.00
      Remarks            lucy 5 kg pickup 30 juni

    Logika:
    - Jika Remarks ada dan berisi kata yang terlihat seperti item/barang, parse sebagai belanja
    - Merchant = Beneficiary Name (nama penerima)
    - Amount = Transfer Amount
    - Item = isi Remarks
    - Jika Remarks kosong atau tidak ada, return None (tolak)
    """
    from datetime import date as _date
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    text_lower = text.lower()

    # Ambil Remarks
    remarks = None
    for i, line in enumerate(lines):
        if re.search(r'^remarks\b', line, re.IGNORECASE):
            # Remarks bisa di kolom kanan dari tab
            parts = re.split(r'\t+', line, maxsplit=1)
            if len(parts) > 1:
                remarks = parts[1].strip()
            elif i + 1 < len(lines):
                remarks = lines[i + 1].strip()
            break

    # Tidak ada remarks atau remarks kosong = transfer biasa, tolak
    if not remarks:
        return None

    # Supplier yang dikenal — transfer ke mereka selalu dianggap belanja
    # meski Remarks berisi kode order (SOL, INV, dll)
    KNOWN_SUPPLIERS = [
        'sukanda', 'sukanda jaya', 'primer', 'dinda', 'dineta',
        'fadhilah', 'amanah', 'mak opik', 'grosir', 'supplier',
    ]
    # Cek beneficiary name dari teks (sebelum merchant di-set)
    ben_match = re.search(r'beneficiary\s+name\s*\t*(.+)', text, re.IGNORECASE)
    beneficiary_lower = ben_match.group(1).strip().lower() if ben_match else ''
    is_known_supplier = any(s in beneficiary_lower for s in KNOWN_SUPPLIERS)

    # Kode order murni: SOL2906..., INV..., hanya huruf+angka tanpa spasi/kata
    is_order_code = bool(re.match(r'^[A-Z]{2,5}\d{6,}$', remarks.strip()))
    # Nomor saja
    is_pure_number = bool(re.match(r'^[\d\s\-/]+$', remarks.strip()))

    has_product_clue = (
        not is_order_code and
        not is_pure_number and
        bool(re.search(
            r'\d+\s*(kg|gr|gram|liter|lt|pcs|pack|ikat|ekor|buah|dus|karton)|'
            r'(pickup|ambil|beli|order|pesanan)\b',
            remarks, re.IGNORECASE
        ))
    )

    # Lolos jika: ada keterangan barang ATAU supplier yang dikenal
    if not has_product_clue and not is_known_supplier:
        return None  # Transfer biasa, bukan belanja

    # Ambil Transfer Amount
    amount = None
    for line in lines:
        if re.search(r'transfer\s+amount', line, re.IGNORECASE):
            m = re.search(r'IDR\s+([\d,]+(?:\.\d{2})?)', line, re.IGNORECASE)
            if m:
                raw = m.group(1).replace(',', '').split('.')[0]
                if raw.isdigit():
                    amount = float(raw)
            break

    if not amount or amount <= 0:
        return None

    # Ambil Beneficiary Name sebagai merchant
    merchant = None
    for line in lines:
        if re.search(r'beneficiary\s+name', line, re.IGNORECASE):
            parts = re.split(r'\t+', line, maxsplit=1)
            if len(parts) > 1:
                merchant = parts[1].strip().title()
            break
    if not merchant:
        merchant = "Transfer BCA"

    # Ambil tanggal dari baris tanggal
    tx_date = _date.today()
    for line in lines:
        m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', line)
        if m:
            try:
                from dateutil import parser as dp
                tx_date = dp.parse(line[:20]).date()
                break
            except Exception:
                pass

    # Buat OcrResult
    result = OcrResult(raw_text=text)
    result.merchant = merchant
    result.total = amount
    result.tx_date = tx_date
    result.confidence = 0.85

    # Item: gunakan remarks jika berisi keterangan barang,
    # atau nama merchant jika remarks adalah kode order
    if is_order_code or is_pure_number:
        # Kode order — simpan sebagai "Bayar ke Supplier (kode)"
        item_name = f"Pembayaran {merchant or 'Supplier'}"
    else:
        item_name = remarks.strip().title()

    result.items = [ReceiptItem(
        name=item_name[:60],
        qty=1.0,
        unit="",
        unit_price=amount,
        line_total=amount,
    )]

    logger.info(
        f"[OCR] BCA transfer as belanja: merchant={merchant!r} "
        f"amount={amount} item={item_name!r}"
    )
    return result



def _is_tiktok_order(text: str) -> bool:
    """Deteksi screenshot pesanan TikTok Shop."""
    indicators = [
        r'pesanan\s+dibuat',
        r'tiba\s+pada',
        r'batalkan\s+pesanan',
        r'nomor\s+pesanan',
        r'hubungi\s+penjual',
        r'pengembalian\s+barang\s+gratis',
    ]
    text_lower = text.lower()
    hits = sum(1 for p in indicators if re.search(p, text_lower))
    # TikTok tidak punya "Subtotal Produk" (itu Shopee)
    not_shopee = not re.search(r'subtotal\s+produk', text_lower)
    return hits >= 3 and not_shopee


def _parse_tiktok_order(text: str) -> OcrResult:
    """
    Parse screenshot pesanan TikTok Shop.

    Format:
      COLOOMP >
      Pre-order  Coloomp- Yasmine Atasan...   Rp105.588
      NAVY BLUE, XXXL                         x15
      Nomor pesanan  584640034955953432
      BCA Bank Central Asia   Total: Rp1.408.330
    """
    from datetime import date as _date
    from bot.utils.formatters import fmt_rupiah
    result = OcrResult(raw_text=text)
    result.tx_date = _date.today()
    result.is_shopee_detail = True  # reuse Shopee detail flow

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Merchant/Toko penjual ──
    merchant = None
    # Cari nama toko dari raw_text (sebelum unicode di-strip)
    for raw_line in result.raw_text.splitlines():
        raw_line = raw_line.strip()
        if re.search(r'[\u203a\u2192>\u203e]\s*\t*$', raw_line):
            candidate = re.sub(r'[\u203a\u2192>\u203e\t]+\s*$', '', raw_line).strip()
            if (2 <= len(candidate) <= 40 and
                not re.search(r'ubah|hubungi|pengembalian|pesanan|penjual|support|cari', candidate, re.IGNORECASE)):
                merchant = candidate
                break

    result.merchant = f"TikTok - {merchant}" if merchant else "TikTok Shop"

    # ── Total ──
    total_m = re.search(r'Total:\s*Rp([\d.,]+)', text, re.IGNORECASE)
    if total_m:
        raw = total_m.group(1)
        clean = re.sub(r'\.(\d{3})', r'\1', raw).split(',')[0]
        if clean.isdigit():
            result.total = float(clean)

    # ── Items ──
    # Pola: baris nama item + harga, baris berikutnya variant + qty
    items = []
    SKIP = re.compile(r'pesanan|tiba|sedang|transit|diantarkan|kurir|ubah|batalkan|'
                      r'hubungi|pengembalian|nomor|bank|total|bonus|voucher|pesan|'
                      r'beli|lokal|paylat|whatsapp|support', re.IGNORECASE)

    i = 0
    while i < len(lines):
        line = lines[i]
        if SKIP.search(line):
            i += 1
            continue

        # Baris item: mengandung "Pre-order" atau langsung nama + Rp
        price_m = re.search(r'Rp([\d.,]+)\s*$', line)
        if price_m:
            raw = price_m.group(1)
            clean = re.sub(r'\.(\d{3})', r'\1', raw).split(',')[0]
            price = float(clean) if clean.isdigit() else 0.0

            # Bersihkan nama dari "Pre-order" label
            name = re.sub(r'^Pre-order\s*', '', line, flags=re.IGNORECASE)
            name = re.sub(r'\s*Rp[\d.,]+\s*$', '', name).strip()
            name = re.sub(r'\.{3}\s*$', '', name).strip()

            # Cari qty di baris berikutnya (variant + xN)
            qty = 1.0
            if i + 1 < len(lines):
                next_line = lines[i+1]
                qty_m = re.search(r'x(\d+)\s*$', next_line)
                if qty_m:
                    qty = float(qty_m.group(1))
                    i += 1

            if name and len(name) >= 3 and price >= 100:
                line_total = price * qty
                items.append(ReceiptItem(
                    name=name.upper()[:60],
                    qty=qty,
                    unit="",
                    unit_price=price,
                    line_total=line_total,
                ))

        i += 1

    result.items = items

    # ── Summary ──
    from bot.utils.formatters import fmt_rupiah
    summary_lines = [f"Terdeteksi pesanan TikTok Shop"]
    summary_lines.append(f"Toko: {result.merchant}")
    for item in items:
        qty_str = f" x{int(item.qty)}" if item.qty > 1 else ""
        summary_lines.append(f"  - {item.name}{qty_str} -- {fmt_rupiah(item.unit_price)}/item")
    if result.total:
        summary_lines.append(f"Total: {fmt_rupiah(result.total)}")

    result.shopee_summary = "\n".join(summary_lines)
    result.shopee_item = items[0].name if items else "Item TikTok"
    result.shopee_qty = int(items[0].qty) if items else 1

    score = 0.3
    score += 0.4 if result.total else 0.0
    score += 0.3 if items else 0.0
    result.confidence = round(score, 2)

    logger.info(f"[OCR] TikTok: merchant={result.merchant!r} items={len(items)} total={result.total}")
    return result




def _is_format_h(lines: list) -> bool:
    """
    Format H — Dapur Kita dan toko retail serupa.
    Ciri: ada pola "N SET X harga(diskon)" atau "Total Bayar" + "Total Qty".
    """
    joined = ' '.join(lines).lower()
    return (
        bool(re.search(r'\d+\s*set\s*x\s*[\d.,]+', joined, re.IGNORECASE)) or
        (bool(re.search(r'total\s+bayar', joined)) and
         bool(re.search(r'total\s+qty', joined)))
    )


def _parse_items_format_h(lines: list) -> list:
    """
    Parse Format H — Dapur Kita.

    Pola per item:
      NAMA ITEM [SKU]    193,500       ← nama + harga (kadang ada SKU di tengah)
      1 SET X 199,500(6,000 )          ← qty x harga_asli(diskon)
      193,500                          ← total setelah diskon (opsional)

    Total struk: "Total Bayar NNN" atau "Total NNN" setelah semua item.
    """
    SKIP_H = {
        'total', 'bayar', 'kembali', 'edc', 'cash', 'hemat', 'qty',
        'member', 'silver', 'gold', 'platinum', 'harga', 'ppn', 'terima',
        'kasih', 'complaint', 'kritik', 'saran', 'bertanggung', 'jawab',
        'kekeliruan', 'kekurangan', 'kerusakan', 'meninggalkan', 'free',
        'dapatkan', 'menukarkan', 'struk', 'kami', 'tidak',
    }

    items = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Pola qty: "1 SET X 199,500(6,000)" atau "2 PCS X 50,000"
        qty_m = re.match(
            r'^(\d+)\s+(SET|PCS|KG|GR|BTL|PCK|PACK|BKS|LTR)\s+X\s+([\d.,]+)',
            line, re.IGNORECASE
        )
        if qty_m:
            qty = float(qty_m.group(1))
            unit = qty_m.group(2).upper()
            # Cari harga setelah diskon: ambil dari baris sebelumnya (total)
            # atau dari angka terakhir di baris ini
            price_after_discount = None

            # Cek baris sebelumnya — bisa berisi harga akhir
            if i > 0:
                prev = lines[i-1].strip()
                prev_money = _extract_money(prev)
                # Jika baris sebelumnya hanya angka (total item)
                if prev_money and re.match(r'^[\d,.\s]+$', prev):
                    price_after_discount = prev_money[-1][0]

            # Jika tidak ketemu, ambil dari baris nama item (2 baris ke atas)
            if not price_after_discount and i >= 1:
                prev = lines[i-1].strip()
                prev_money = _extract_money(prev)
                if prev_money:
                    price_after_discount = prev_money[-1][0]

            if price_after_discount and price_after_discount >= 100:
                # Ambil nama dari baris sebelum qty (bersihkan dari kode SKU)
                name_line = lines[i-1].strip() if i > 0 else ""
                # Hapus harga di akhir nama
                name_clean = re.sub(r'\s+[\d,]+\s*$', '', name_line).strip()
                # Hapus kode SKU (pola huruf+angka panjang di tengah)
                name_clean = re.sub(r'\b[A-Z]{2,}-[A-Z0-9]{3,}\b', '', name_clean).strip()
                name_clean = re.sub(r'\s{2,}', ' ', name_clean).strip()

                if len(name_clean) >= 2:
                    items.append(ReceiptItem(
                        name=name_clean.upper()[:60],
                        qty=qty,
                        unit=unit,
                        unit_price=price_after_discount / qty,
                        line_total=price_after_discount,
                    ))

            i += 1
            continue

        i += 1
    return items




def _is_format_i(lines: list) -> bool:
    """
    Format I -- SB Minimarket / Sinar Bahagia Pancor.
    Ciri khas: baris "N PCS X HARGA =" atau "N BH X HARGA ="
    diikuti opsional "Disc -NNN", lalu total dari "Total : NNN"
    """
    for line in lines:
        if re.match(r'^\d+\s+(PCS|BH|BTL|PCK|DOS|LBR|SET|BKS)\s+X\s+[\d.,]+\s*=', line.strip(), re.IGNORECASE):
            return True
    return False


def _parse_items_format_i(lines: list) -> list:
    """
    Parse Format I -- SB Minimarket.

    Pola per item:
      NAMA ITEM [VARIAN]             <- baris nama
      1 PCS X  8,500 =    8,500     <- baris qty x harga = total
      Disc   -1,500                  <- opsional diskon per item

    Total struk: baris "Total : 83,750" (bukan Sub Total, bukan Chrge Krtu)
    """
    SKIP_SB = re.compile(
        r'^(tgl|kasir|user|no|sc)\s*:', re.IGNORECASE
    )
    QTY_PATTERN = re.compile(
        r'^(\d+)\s+(PCS|BH|BTL|PCK|DOS|LBR|SET|BKS)\s+X\s+([\d.,]+)\s*=\s*([\d.,]+)',
        re.IGNORECASE
    )
    DISC_PATTERN = re.compile(r'^Disc\s+[-+]?([\d.,]+)', re.IGNORECASE)

    items = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip header/footer baris
        if SKIP_SB.match(line):
            i += 1
            continue

        # Deteksi baris qty: "1 PCS X 8,500 = 8,500"
        qty_m = QTY_PATTERN.match(line)
        if qty_m:
            qty = float(qty_m.group(1))
            unit = qty_m.group(2).upper()
            line_total_raw = qty_m.group(4).replace('.', '').replace(',', '')
            line_total = float(line_total_raw) if line_total_raw.isdigit() else 0.0
            unit_price_raw = qty_m.group(3).replace('.', '').replace(',', '')
            unit_price = float(unit_price_raw) if unit_price_raw.isdigit() else 0.0

            # Cek disc di baris berikutnya
            disc = 0.0
            if i + 1 < len(lines):
                disc_m = DISC_PATTERN.match(lines[i + 1].strip())
                if disc_m:
                    disc_raw = disc_m.group(1).replace('.', '').replace(',', '')
                    disc_candidate = float(disc_raw) if disc_raw.isdigit() else 0.0
                    # Hanya kurangi disc jika total di baris qty = harga × qty (belum dikurangi)
                    # Jika total sudah lebih kecil dari harga×qty, disc sudah termasuk
                    expected_total = unit_price * qty
                    already_discounted = abs(expected_total - line_total) > 1
                    if not already_discounted:
                        disc = disc_candidate
                    i += 1  # skip baris disc

            net_total = line_total - disc

            # Ambil nama dari baris sebelumnya
            name = None
            j = i - 1
            while j >= 0:
                prev = lines[j].strip()
                if (len(prev) >= 3 and
                    not QTY_PATTERN.match(prev) and
                    not DISC_PATTERN.match(prev) and
                    not SKIP_SB.match(prev) and
                    not re.match(r'^[=\-]{3,}', prev) and
                    not re.match(r'^(sub\s*total|total|chrge|byar|anda\s+hemat)', prev, re.IGNORECASE)):
                    name = prev
                    break
                j -= 1

            if name and net_total >= 100:
                items.append(ReceiptItem(
                    name=name.upper()[:60],
                    qty=qty,
                    unit=unit,
                    unit_price=unit_price,
                    line_total=net_total,
                ))

        i += 1
    return items



def _is_dineta_invoice(text: str) -> bool:
    """Deteksi invoice PT Dineta Jaya."""
    return bool(re.search(r'dineta', text, re.IGNORECASE)) and bool(re.search(r'invoice', text, re.IGNORECASE))


def _parse_dineta_invoice(text: str) -> OcrResult:
    """
    Parse invoice PT Dineta Jaya.

    Format item:
      12 Pack   1011000103   GF Milk ESL FC IP 1000 Ml @12 *   Rp  19.739,00   Rp  236.868,00

    Total dari:
      Invoice Total : Rp   236.868,00
    """
    from datetime import date as _date
    result = OcrResult(raw_text=text)
    result.merchant = "PT Dineta Jaya"

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Tanggal ──
    date_m = re.search(r'Date\s*:\s*(\d{1,2}[-/]\w+[-/]\d{2,4})', text, re.IGNORECASE)
    if date_m:
        try:
            from dateutil import parser as dp
            result.tx_date = dp.parse(date_m.group(1)).date()
        except Exception:
            pass
    if not result.tx_date:
        result.tx_date = _date.today()

    # ── Total dari Invoice Total ──
    total_m = re.search(r'Invoice\s+Total\s*:\s*Rp\s*([\d.,]+)', text, re.IGNORECASE)
    if total_m:
        raw = total_m.group(1)
        # Format '236.868,00': titik=pemisah ribuan, koma=desimal
        clean = re.sub(r'\.(\d{3})', r'\1', raw)  # hapus titik ribuan
        clean = clean.split(',')[0]  # hapus desimal
        if clean.isdigit():
            result.total = float(clean)

    # ── Item dari baris dengan pola QTY UNIT  SKU  NAMA  Rp HARGA  Rp TOTAL ──
    items = []
    for line in lines:
        # Pola: "12 Pack  1011000103  GF Milk ESL FC IP..."
        m = re.match(r'(\d+)\s+(\w+)\s+(\d{8,})\s+(.+?)\s+Rp\s+([\d.,]+)\s+Rp\s+([\d.,]+)', line)
        if m:
            qty = float(m.group(1))
            desc = m.group(4).strip()
            # Format "19.739,00": titik=ribuan, koma=desimal
            def _parse_idr(s):
                clean = re.sub(r'\.(\d{3})', r'\1', s)
                clean = clean.split(',')[0]
                return float(clean) if clean.isdigit() else 0.0
            raw_unit = m.group(5)
            raw_total = m.group(6)
            unit_price = _parse_idr(raw_unit)
            line_total = _parse_idr(raw_total)
            if line_total >= 100:
                items.append(ReceiptItem(
                    name=desc.upper()[:60],
                    qty=qty,
                    unit=m.group(2),
                    unit_price=unit_price,
                    line_total=line_total,
                ))

    result.items = items
    result.confidence = 0.9 if result.total else 0.5
    logger.info(f"[OCR] Dineta: items={len(items)} total={result.total}")
    return result



def _is_qris_receipt(text: str) -> bool:
    """Deteksi bukti pembayaran QRIS dari bank (BCA, Mandiri, BRI, dll)."""
    indicators = [
        r'qris\s+payment\s+successful',
        r'qris\s+payment',
        r'payment\s+successful',
        r'total\s+payment\s+idr',
        r'payment\s+to',
        r'acquirer',
        r'merchant\s+pan',
        r'source\s+of\s+fund',
    ]
    text_lower = text.lower()
    hits = sum(1 for p in indicators if re.search(p, text_lower))
    return hits >= 3


def _parse_qris_receipt(text: str) -> OcrResult:
    """
    Parse bukti pembayaran QRIS dari bank.
    Ekstrak: merchant, nominal, tanggal.
    Flag result.needs_manual_input = True agar bot minta item manual.
    """
    from datetime import date as _date
    result = OcrResult(raw_text=text)
    result.tx_date = _date.today()

    # Merchant dari "Payment to: NAMA"
    merchant_m = re.search(r'Payment\s+to[:\s]+([^\n\t\r]+)', text, re.IGNORECASE)
    if merchant_m:
        result.merchant = merchant_m.group(1).strip()
    else:
        result.merchant = "QRIS"

    # Total dari "Total Payment IDR 80,910.00" atau "IDR 80,910.00"
    total_m = re.search(r'(?:Total\s+Payment\s+)?IDR\s+([\d,\.]+)', text, re.IGNORECASE)
    if total_m:
        raw = total_m.group(1).replace(',', '').replace('.', '')
        # Handle "80,910.00" → 80910
        raw2 = re.sub(r'\.\d{2}$', '', total_m.group(1).replace(',', ''))
        try:
            result.total = float(raw2)
        except Exception:
            pass

    # Tanggal dari format "10 Jun 2026" atau "10/06/2026"
    date_m = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})', text, re.IGNORECASE)
    if date_m:
        try:
            from dateutil import parser as dp
            result.tx_date = dp.parse(date_m.group(0)).date()
        except Exception:
            pass

    # Acquirer/bank sebagai info tambahan
    acquirer_m = re.search(r'Acquirer[:\s]+([^\n\t\r]+)', text, re.IGNORECASE)
    if acquirer_m:
        result.provider = acquirer_m.group(1).strip()

    # Flag bahwa ini butuh input manual item
    result.is_qris = True
    result.items = []  # kosong — akan diisi manual

    score = 0.3
    score += 0.4 if result.total else 0.0
    score += 0.3 if result.merchant else 0.0
    result.confidence = round(score, 2)

    logger.info(f"[OCR] QRIS: merchant={result.merchant!r} total={result.total} date={result.tx_date}")
    return result



def _is_shopee_screenshot(text: str) -> bool:
    """Deteksi apakah ini screenshot halaman pesanan Shopee/marketplace."""
    indicators = [
        r'\bpesanan\s+saya\b',
        r'\bselesai\b.*\bbeli\s+lagi\b',
        r'\bajukan\s+pengembalian\b',
        r'\btotal\s+\d+\s+produk\b',
        r'\bdikemas\b.*\bdikirim\b',
    ]
    text_lower = text.lower()
    hits = sum(1 for p in indicators if re.search(p, text_lower))
    return hits >= 2


def _parse_shopee_orders(text: str) -> OcrResult:
    """
    Parse screenshot halaman Pesanan Saya Shopee.
    Setiap order dipisahkan oleh baris 'NAMA TOKO\tSelesai'.
    Output: satu OcrResult dengan semua order sebagai items,
    total = jumlah semua order.
    """
    from datetime import date as _date
    result = OcrResult(raw_text=text)
    result.merchant = "Shopee"
    result.tx_date = _date.today()

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Deteksi batas setiap order
    order_starts = []
    for i, line in enumerate(lines):
        if not re.search(r'\bSelesai\b|\bDikirim\b|\bDikemas\b', line):
            continue
        # Skip baris header tab
        if re.search(r'Dikemas.*Dikirim|Dikirim.*Selesai', line):
            continue
        # Skip "Pesanan Saya"
        if 'Pesanan Saya' in line:
            continue
        order_starts.append(i)

    if not order_starts:
        return result

    items = []
    total_all = 0.0

    for idx, start in enumerate(order_starts):
        end = order_starts[idx + 1] if idx + 1 < len(order_starts) else len(lines)
        order_lines = lines[start:end]

        if len(order_lines) < 2:
            continue

        # Nama toko: hapus "Mall | ORI", "Star+", "Selesai", "Dikirim"
        toko_raw = order_lines[0]
        toko = re.sub(r'Mall\s*\|\s*ORI\s*\t?', '', toko_raw)
        toko = re.sub(r'Star\+\s*\t?', '', toko)
        toko = re.sub(r'\t?(Selesai|Dikirim|Dikemas)\s*$', '', toko)
        toko = re.sub(r'\t', ' ', toko).strip().strip('.')

        # Total order: "Total N produk: RpXXX.XXX"
        order_total = None
        for l in order_lines:
            m = re.search(r'Total\s+\d+\s+produk:\s*Rp\s*([\d.,]+)', l, re.IGNORECASE)
            if m:
                raw = m.group(1).replace('.', '').replace(',', '')
                if raw.isdigit():
                    order_total = float(raw)
                break

        if not order_total:
            continue

        # Nama produk: baris pertama setelah toko yang punya konten nama
        produk_name = None
        for l in order_lines[1:]:
            # Skip baris harga, tombol, qty
            if re.search(r'^Rp|Ajukan|Beli|Lihat|Total|\bx\d\b|\d+\s*kg\b', l, re.IGNORECASE):
                continue
            if len(l) >= 3 and not re.match(r'^[\d.,]+$', l):
                # Hapus "..." di akhir, nama toko duplikat
                clean = re.sub(r'\.{3}\s*$', '', l).strip()
                clean = re.sub(r'\t.*', '', clean).strip()
                if len(clean) >= 3:
                    produk_name = clean
                    break

        name = f"{toko} — {produk_name}" if produk_name else toko
        total_all += order_total

        items.append(ReceiptItem(
            name=name.upper()[:60],
            qty=1.0,
            unit="",
            unit_price=order_total,
            line_total=order_total,
        ))

    result.items = items
    result.total = total_all if total_all > 0 else None
    result.grand_total = result.total

    score = 0.4 if result.items else 0.0
    score += 0.3 if result.total else 0.0
    score += 0.3  # merchant + date always set
    result.confidence = round(score, 2)

    return result



def _parse_receipt_text(text: str) -> OcrResult:
    # Simpan original sebelum normalize (untuk deteksi unicode seperti ›)
    original_text = text
    text = _normalize_ocr(text)

    # Deteksi format marketplace/app lebih dulu
    if _is_dineta_invoice(text):
        logger.info("[OCR] Dineta invoice detected")
        return _parse_dineta_invoice(text)

    # Deteksi transfer BCA — bedakan transfer biasa vs pembayaran belanja
    if _is_bank_transfer(text):
        belanja = _try_parse_bca_transfer_belanja(text)
        if belanja:
            logger.info(f"[OCR] BCA transfer belanja: merchant={belanja.merchant!r} amount={belanja.total}")
            return belanja
        else:
            logger.info("[OCR] Bank transfer screenshot — bukan struk belanja")
            result = OcrResult(raw_text=text)
            result.merchant = None
            result.confidence = 0.0
            result._is_bank_transfer = True
            return result

    if _is_tiktok_order(text):
        logger.info("[OCR] TikTok Shop order detected")
        result = _parse_tiktok_order(text)
        # Override merchant dari original text (unicode ›)
        if result.merchant == "TikTok Shop":
            for raw_line in original_text.splitlines():
                raw_line = raw_line.strip()
                if re.search(r'[\u203a\u2192>\u203e]\s*\t*$', raw_line):
                    cand = re.sub(r'[\u203a\u2192>\u203e\t]+\s*$', '', raw_line).strip()
                    if (2 <= len(cand) <= 40 and
                        not re.search(r'ubah|hubungi|pengembalian|pesanan|penjual|support|cari', cand, re.IGNORECASE)):
                        result.merchant = f"TikTok - {cand}"
                        if result.shopee_summary:
                            result.shopee_summary = result.shopee_summary.replace("TikTok Shop", result.merchant)
                        break
        return result

    if _is_shopee_detail(text):
        logger.info("[OCR] Shopee detail order detected")
        return _parse_shopee_detail(text)

    if _is_qris_receipt(text):
        logger.info("[OCR] QRIS payment receipt detected")
        return _parse_qris_receipt(text)

    if _is_klikindo_screenshot(text):
        logger.info("[OCR] Klikindomaret/app screenshot detected")
        return _parse_klikindo_orders(text)

    if _is_shopee_screenshot(text):
        logger.info("[OCR] Shopee screenshot detected")
        return _parse_shopee_orders(text)

    result = OcrResult(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = _join_fragmented_lines(lines)

    # ── Merchant ──
    # Deteksi nama toko terkenal dari seluruh teks
    KNOWN_MERCHANTS = {
        # Minimarket
        'indomaret': 'Indomaret',
        'klikindomaret': 'Indomaret',
        'alfamart': 'Alfamart',
        'alfamidi': 'Alfamidi',
        'lawson': 'Lawson',
        'circle k': 'Circle K',
        'familymart': 'FamilyMart',
        'hypermart': 'Hypermart',
        'carrefour': 'Carrefour',
        'lottemart': 'Lotte Mart',
        'transmart': 'Transmart',
        'superindo': 'Super Indo',
        'giant': 'Giant',
        'yogya': 'Yogya',
        'borma': 'Borma',
        'primo': 'Primo',
        # Toko lokal Selong
        'mr d.i.y': 'MR D.I.Y.',
        'mr diy': 'MR D.I.Y.',
        'mrdiy': 'MR D.I.Y.',
        'primer raya': 'Primer Raya',
        'dinda frozen': 'Dinda Frozen Food',
        'fadhilah frozen': 'Fadhilah',
        'fadhila frozen': 'Fadhilah',
        'sb minimarket': 'Sinar Bahagia',
        'toko bahan kue': 'Amanah',
        'stoa space': 'Stoa Space',
        # Toko lain
        'harnila': 'Harnila Store',
        'sumber alfaria': 'Alfamart',
        'pt.sumber alfaria': 'Alfamart',
        'pt. sumber alfaria': 'Alfamart',
        'dineta': 'PT Dineta Jaya',
        'pt. dineta': 'PT Dineta Jaya',
        'pt dineta': 'PT Dineta Jaya',
        'daya indah yasa': 'MR D.I.Y.',
        'pt. daya indah': 'MR D.I.Y.',
        'pt daya indah': 'MR D.I.Y.',
        'daya inoan': 'MR D.I.Y.',
        # Toko retail lain
        'dapur kita': 'Dapur Kita',
        'boga kita': 'Dapur Kita',
    }

    text_lower = text.lower()
    for keyword, merchant_name in KNOWN_MERCHANTS.items():
        if keyword in text_lower:
            result.merchant = merchant_name.replace('\t', ' ').strip()
            break

    # Override khusus: Dineta sering salah karena alamat "Stoa Space" terbaca duluan
    if 'dineta' in text_lower and result.merchant in (None, 'Stoa Space', ''):
        result.merchant = 'PT Dineta Jaya'

    # Override: Indomaret bisa terbaca sebagai CV franchisee-nya
    if (result.merchant not in ('Indomaret',) and
        ('klikindomaret' in text_lower or
         'layanan konsumen' in text_lower and 'indomaret' in text_lower or
         re.search(r'\d{2}\.\d{2}\.\d{2}-\d{2}:\d{2}/\d+\.\d+\.\d+/', text_lower))):
        # Kode struk Indomaret: "01.07.26-08:38/4.3.1/FCFO-330/..."
        result.merchant = 'Indomaret'

    # Jika tidak ada known merchant, cari dari baris awal
    if not result.merchant:
        header_skip = ['telp','fax','no.','no:','kasir','area','jl.',
                       'jln','pel.','pelanggan','tanggal','date','struk']
        for line in lines[:8]:
            if re.match(r'^[\d\s\-\+\(\)\.\/:=,]+$', line): continue
            if len(line) < 3: continue
            if any(w in line.lower() for w in header_skip): continue
            if _matches_any(line, SKIP_LINE_PATTERNS): continue
            # Skip baris yang hanya berisi kata-kata footer/OCR noise
            words = re.findall(r'[a-zA-Z]+', line.lower())
            if all(w in {'atama', 'evard', 'kapuk', 'karta', 'utara', 'domaret',
                         'maret', 'prismatama', 'indomarco'} for w in words if len(w) > 2):
                continue
            result.merchant = line.title()
            break

    # ── Tanggal ──
    # Coba format ISO dulu: YYYY-MM-DD atau YYYY/MM/DD
    iso_match = re.search(r'(20\d{2})[\-\/](\d{2})[\-\/](\d{2})', text)
    if iso_match:
        try:
            from datetime import date as _date
            result.tx_date = _date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except Exception:
            pass

    # Fallback: format DD/MM/YYYY atau DD-MM-YYYY
    if not result.tx_date:
        for m in re.finditer(r'(\d{1,2})[-\/\.](\d{1,2})[-\/\.](\d{4})', text):
            try:
                from dateutil import parser as dp
                result.tx_date = dp.parse(m.group(0), dayfirst=True).date()
                break
            except Exception:
                pass

    # Fallback 2: format DD-MM-YY (2 digit tahun seperti MR DIY: 05-06-26)
    if not result.tx_date:
        for m in re.finditer(r'\b(\d{2})-(\d{2})-(\d{2})\b', text):
            try:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1 <= d <= 31 and 1 <= mo <= 12:
                    from datetime import date as _d
                    result.tx_date = _d(2000 + y, mo, d)
                    break
            except Exception:
                pass

    # ── Financial summary ──
    total_candidates = []
    payment_values = []
    change_values = []
    discount_values = []

    for line in lines:
        money_vals = _extract_money(line)
        if not money_vals:
            continue
        cls = _classify_line(line)
        nums = [v for v, _ in money_vals]

        if cls == 'change':
            change_values.extend(nums)
        elif cls == 'payment':
            payment_values.extend(nums)
        elif cls == 'total':
            priority = 0 if re.search(r'\bgrand\b', line.lower()) else \
                       2 if re.search(r'\bsub\b', line.lower()) else 1
            for n in nums:
                total_candidates.append((n, priority))
        elif cls == 'discount':
            discount_values.extend(nums)

    if change_values:
        result.change = change_values[0]
    if payment_values:
        result.cash_paid = payment_values[0]
    if discount_values:
        result.discount = discount_values[0]
        result.discount_amount = discount_values[0]

    # Deteksi VOUCHER: (8,400) atau ANDA HEMAT sebagai discount_amount
    if not result.discount_amount:
        for line in lines:
            ln = line.lower()
            if re.search(r'voucher\s*:', ln) or re.search(r'anda\s+hemat', ln):
                m_voucher = re.search(r'[(]?([\d,.]+)[)]?\s*$', line)
                if m_voucher:
                    v = _extract_money(m_voucher.group(0))
                    if v:
                        result.discount_amount = v[0][0]
                        break

    if total_candidates:
        total_candidates.sort(key=lambda x: x[1])
        best_p = total_candidates[0][1]
        best = [v for v, p in total_candidates if p == best_p]
        result.grand_total = min(best)
        result.total = result.grand_total

    # Validasi: total tidak boleh == kembalian (change)
    # Tapi total BOLEH == cash (bayar pas / uang pas)
    if result.total and result.change and abs(result.total - result.change) < 1:
        # Cari nilai total alternatif
        for val, pri in sorted(total_candidates, key=lambda x: x[1]):
            if abs(val - result.change) > 1:
                result.total = val
                break
        else:
            result.total = None

    # ── Item extraction ──
    # Prioritas deteksi format:
    # 1. Fadhilah (numbered items "N. NAMA")
    # 2. MR D.I.Y. (nama → SKU → barcode+harga)
    # 3. Format F (Harnila: "Nx HARGA  TOTAL")
    # 4. Format H (Dapur Kita: nama + "N SET X harga(diskon)")
    # 5. Format I (SB Minimarket: nama + "N PCS X HARGA = TOTAL" + opsional Disc)
    # 6. Format A/B (fallback)
    items_fadhilah = _parse_items_format_fadhilah(lines) if _is_fadhilah_format(lines) else []
    items_mrdiy = _parse_items_format_mrdiy(lines)
    items_f = _parse_items_format_f(lines) if _is_format_f(lines) else []
    items_h = _parse_items_format_h(lines) if _is_format_h(lines) else []
    items_i = _parse_items_format_i(lines) if _is_format_i(lines) else []

    if items_fadhilah:
        items = items_fadhilah
    elif items_mrdiy and result.merchant and 'MR' in (result.merchant or '').upper():
        items = items_mrdiy
    elif items_f:
        items = items_f
    elif items_i:
        items = items_i
    elif items_h:
        items = items_h
    elif items_mrdiy and len(items_mrdiy) >= 1:
        # Verifikasi: semua item MR DIY harus punya harga wajar
        mrdiy_ok = all(100 <= i.line_total <= 5_000_000 for i in items_mrdiy)
        if mrdiy_ok:
            items = items_mrdiy
        else:
            items_a = _parse_items_format_a(lines)
            items_b = _parse_items_format_b(lines)
            items = max([items_a, items_b], key=lambda x: len(x))
    else:
        items_a = _parse_items_format_a(lines)
        items_b = _parse_items_format_b(lines)
        items = max([items_a, items_b], key=lambda x: len(x))

    # Validasi: sum(line_total) harus mendekati total
    if items and result.total:
        item_sum = sum(i.line_total for i in items)
        if abs(item_sum - result.total) < result.total * 0.1:  # toleransi 10%
            result.items = items
            logger.info(f"[OCR] items validated: sum={item_sum} total={result.total}")
        else:
            result.items = items
            logger.warning(f"[OCR] item sum={item_sum} != total={result.total}")
    else:
        result.items = items

    logger.info(f"[OCR] items extracted: {len(result.items)}")
    for item in result.items:
        logger.info(f"  - {item.name} qty={item.qty} price={item.unit_price} total={item.line_total}")

    # ── Confidence ──
    score = 0.0
    if result.merchant: score += 0.2
    if result.tx_date: score += 0.2
    if result.total: score += 0.3
    if result.items: score += 0.3
    result.confidence = round(score, 2)

    logger.info(
        f"[OCR-FINAL] merchant={result.merchant!r} date={result.tx_date} "
        f"total={result.total} items={len(result.items)} confidence={result.confidence}"
    )
    return result


async def process_receipt(bot, file_id: str) -> OcrResult:
    try:
        import os
        api_key = os.environ.get("OCR_SPACE_API_KEY", "helloworld")
        file = await bot.get_file(file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        logger.info(f"[OCR] size={len(image_bytes)} api={'custom' if api_key != 'helloworld' else 'demo'}")

        # Compress gambar jika lebih dari 500KB
        if len(image_bytes) > 500_000:
            try:
                from PIL import Image
                import io as _io
                img = Image.open(_io.BytesIO(image_bytes))
                # Resize jika terlalu besar
                max_dim = 1800
                if max(img.size) > max_dim:
                    ratio = max_dim / max(img.size)
                    new_size = (int(img.size[0]*ratio), int(img.size[1]*ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                out = _io.BytesIO()
                img.save(out, format='JPEG', quality=85)
                image_bytes = out.getvalue()
                logger.info(f"[OCR] compressed to {len(image_bytes)} bytes")
            except Exception as ce:
                logger.warning(f"[OCR] compress failed: {ce}")

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.ocr.space/parse/image",
                data={"apikey": api_key, "language": "eng",
                      "isOverlayRequired": "false", "detectOrientation": "true",
                      "scale": "true", "isTable": "true",
                      "OCREngine": "2"},
                files={"file": ("struk.jpg", image_bytes, "image/jpeg")},
            )

        data = response.json()
        if data.get("IsErroredOnProcessing"):
            logger.error(f"[OCR] error: {data.get('ErrorMessage')}")
            return OcrResult(confidence=0.0)

        parsed_results = data.get("ParsedResults", [])
        raw_text = parsed_results[0].get("ParsedText", "") if parsed_results else ""
        logger.info(f"[OCR-RAW] len={len(raw_text)}\n{raw_text}")

        if not raw_text.strip():
            return OcrResult(confidence=0.0)

        return _parse_receipt_text(raw_text)

    except Exception as e:
        logger.exception(f"[OCR] failed: {e}")
        return OcrResult(confidence=0.0)
