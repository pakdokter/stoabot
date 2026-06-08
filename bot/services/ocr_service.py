"""
OCR Service — parser dengan item extraction.
Mendukung dua format OCR:
  Format A: nama item di baris terpisah dari qty/harga
  Format B: nama item + qty/harga dalam satu baris
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
    discount: Optional[float] = None
    tx_date: Optional[date] = None
    items: list = field(default_factory=list)  # list of ReceiptItem
    raw_text: str = ""
    confidence: float = 0.0
    provider: str = "ocrspace"


# ── Keyword lists ──────────────────────────────────────────────────────

TOTAL_KEYWORDS = [
    r'\btotal\b', r'\bgrand\s*total\b', r'\bjumlah\b',
    r'\btagihan\b', r'\bsubtotal\b', r'\bsub\s*total\b',
    r'\bnetto\b', r'\bnet\b',
    r'\btotal\s+belanja\b', r'\bjumlah\s+belanja\b',
    r'\btotal\s+bayar\b', r'\btotal\s+pembayaran\b',
    r'\btotal\s+incl\b', r'\btotal\s+include\b',
    r'\btotal\s+termasuk\b', r'\bitem\(s\)\b',
    # OCR noise variants
    r'\brotal\b', r'\bt0tal\b', r'\bt\*tal\b', r'\btoial\b',
]
PAYMENT_KEYWORDS = [
    r'\btunai\b', r'\bcash\b', r'\bbayar\b', r'\bdibayar\b',
    r'\btransfer\b', r'\bdebit\b', r'\bkredit\b', r'\bkartu\b',
    r'\bqris\b', r'\bova\b', r'\bgopay\b', r'\bshopee\b',
    r'\bdana\b', r'\blinkaja\b',
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
    r'\btotal\s+qty\b', r'\bjml\s+item\b',
    r'\bppn\s+included\b', r'\bitem\(s\)\b', r'\bqty\(s\)\b',
    r'\bincluded\s+in\s+total\b',
    r'\bppn\s+dibebaskan\b', r'\bpkp\s+dibebaskan\b',
    r'\bharga\s+jual\b', r'\brga\s+jual\b',
    r'\bdpp\s*=', r'\bnpwp\b',
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

        # Baris nama item: boleh punya angka kecil (kode produk), tapi bukan harga besar
        is_product_code_only = all(v < 10000 for v, _ in money_in_line)
        if has_alpha and _is_item_name_line(line) and (len(money_in_line) == 0 or (len(money_in_line) <= 2 and is_product_code_only)):
            # Skip baris header/alamat
            words_in_line = set(re.findall(r'[a-zA-Z]+', line.lower()))
            if words_in_line & header_words:
                i += 1
                continue
            # Skip pola nomor struk: SI01-2606-0728, SB-9926F03I4915
            if re.match(r'^[A-Z]{2,4}[0-9-]+', line.strip()):
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
    Parse format MR DIY / toko dengan SKU+barcode terpisah.

    Pola per item:
      NAMA ITEM [detail]        ← baris nama (huruf, mungkin ada volume/GSM)
      SKU - XX/YY               ← kode SKU (baris opsional, diawali huruf+angka+dash)
      BARCODE  QTY x  HARGA  TOTAL  ← barcode diikuti harga

    Alternatif (item sudah ter-join):
      NAMA ITEM  QTY x  HARGA  TOTAL  ← satu baris
    """
    def _is_sku_line(line):
        """Kode SKU: CA615CA170 - 12/48, CB028 - 12/144"""
        return bool(re.match(r'^[A-Z]{2,}\d+[A-Z0-9]*\s*-\s*\d', line.strip()))

    def _is_barcode_price_line(line):
        """Baris barcode + harga: 9840000770623  9,000  9,000"""
        stripped = line.strip()
        # Dimulai dengan angka panjang (barcode) diikuti harga
        return bool(re.match(r'^\d{6,}', stripped)) and len(_extract_money(line)) >= 1

    def _extract_item_price(line):
        """Dari baris barcode+harga atau nama+harga, ambil qty dan harga."""
        money = _extract_money(line)
        if not money:
            return None, None, None

        # Cari pola "QTY x HARGA" atau "QTY X HARGA"
        qty_match = re.search(r'(\d{1,3})\s*[xX]\s*(\d)', line)
        if qty_match:
            qty = float(qty_match.group(1))
        else:
            # Coba ambil angka kecil di awal setelah barcode
            after_barcode = re.sub(r'^\d{6,}\s*', '', line.strip())
            qty_m = re.match(r'^(\d{1,3})\s', after_barcode)
            qty = float(qty_m.group(1)) if qty_m and int(qty_m.group(1)) <= 99 else 1.0

        if len(money) >= 2:
            unit_price = money[-2][0]
            line_total = money[-1][0]
        else:
            unit_price = money[-1][0]
            line_total = money[-1][0]

        return qty, unit_price, line_total

    items = []
    i = 0
    while i < len(lines):
        line = lines[i]
        has_alpha = bool(re.search(r'[A-Za-z]', line))
        is_sku = _is_sku_line(line)
        is_barcode = _is_barcode_price_line(line)
        money = _extract_money(line)

        # Pola: baris nama item (huruf, bukan SKU/barcode)
        if has_alpha and not is_sku and not is_barcode and _is_item_name_line(line):
            # Cek apakah ini item MR DIY: cari baris SKU atau barcode berikutnya
            name_raw = line.strip()

            # Hapus detail volume/GSM yang bukan nama: "1g X 60 CM 2000 GSM" dll
            name_clean = re.sub(r'\b\d+[gG][Mm]?\b', '', name_raw)  # 2000 GSM
            name_clean = re.sub(r'\s+[Xx]\s+\d+\s+[Cc][Mm]\b', '', name_clean)  # X 60 CM
            name_clean = re.sub(r'\b\d+[Mm][Ll]\b', '', name_clean)  # 600ML
            name_clean = re.sub(r'\t.*', '', name_clean)  # hapus setelah tab
            name_clean = name_clean.strip().rstrip('-').strip()

            # Cari baris price di depan (skip SKU jika ada)
            j = i + 1
            # Skip baris SKU
            if j < len(lines) and _is_sku_line(lines[j]):
                j += 1
            # Cek baris barcode+harga
            if j < len(lines) and _is_barcode_price_line(lines[j]):
                qty, unit_price, line_total = _extract_item_price(lines[j])
                if line_total and len(name_clean) >= 2:
                    items.append(ReceiptItem(
                        name=name_clean.upper(),
                        qty=qty or 1.0,
                        unit="",
                        unit_price=unit_price or line_total,
                        line_total=line_total,
                    ))
                i = j + 1
                continue

            # Cek apakah nama sudah punya harga di dalamnya (baris ter-join)
            # Contoh: "MINI DUSTBIN #7749  1  21,500  8979504  21,500"
            # Ambil angka yang bukan barcode (< 8 digit)
            clean_money = [(v, p) for v, p in money if v < 10_000_000 and v >= 100]
            if len(clean_money) >= 1 and not is_sku:
                if len(clean_money) >= 2:
                    line_total = clean_money[-1][0]
                    unit_price = clean_money[-2][0]
                else:
                    line_total = clean_money[-1][0]
                    unit_price = line_total

                qty_m = re.search(r'\b(\d{1,2})\b', name_clean)
                qty = float(qty_m.group(1)) if qty_m and int(qty_m.group(1)) <= 99 else 1.0

                # Bersihkan angka dari nama
                name_only = re.sub(r'\b\d+\b', '', name_clean).strip().rstrip('#').strip()
                if len(name_only) >= 2:
                    items.append(ReceiptItem(
                        name=name_only.upper(),
                        qty=1.0,
                        unit="",
                        unit_price=unit_price,
                        line_total=line_total,
                    ))
                i += 1
                continue

        i += 1
    return items



def _classify_line(line: str) -> str:
    if _matches_any(line, SKIP_LINE_PATTERNS): return 'skip'
    if _matches_any(line, CHANGE_KEYWORDS): return 'change'
    if _matches_any(line, SAVINGS_KEYWORDS): return 'skip'   # hemat/voucher bukan item
    if _matches_any(line, PAYMENT_KEYWORDS): return 'payment'
    if _matches_any(line, TOTAL_KEYWORDS): return 'total'
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
        cleaned = cleaned.replace('€', '0')   # 43,50€ → 43,500
        cleaned = cleaned.replace('к', 'R')   # кр → Rp (Cyrillic к)
        cleaned = cleaned.replace('К', 'R')
        cleaned = re.sub(r'(?<=[\d])O(?=[\d,.])', '0', cleaned)  # huruf O di antara angka
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
    return hits >= 2 and re.search(r'Harga Satuan', text, re.IGNORECASE) is not None


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
        result.merchant = f"Sukanda ({dest})"
    elif re.search(r'klikindomaret', text, re.IGNORECASE):
        result.merchant = "Klikindomaret"
    elif re.search(r'tokopedia', text, re.IGNORECASE):
        result.merchant = "Tokopedia"
    else:
        result.merchant = "Pesanan Online"

    result.tx_date = _date.today()

    lines = [l.strip() for l in text.splitlines() if l.strip()]
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
                    # Baris nama item — ada huruf dan bukan Rp/Total/Harga
                    if (re.search(r'[A-Za-z]', prev) and
                        not re.match(r'^(Rp|Total|Harga|Beli|Beranda)', prev, re.IGNORECASE)):
                        # Ambil qty dari "x1", "x2" dll
                        qty_m = re.search(r'[xX](\d+)\s*$', prev)
                        if qty_m:
                            qty = float(qty_m.group(1))
                        # Bersihkan nama
                        name_clean = re.sub(r'\s+[xX]\d+\s*$', '', prev).strip()
                        name_clean = re.sub(r'\t.*', '', name_clean).strip()
                        # Hapus "Brand • " prefix jika ada
                        name_clean = re.sub(r'^[A-Za-z\s]+ • ', '', name_clean).strip()
                        if len(name_clean) >= 3:
                            name = name_clean
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
    # Normalisasi dulu sebelum parsing
    text = _normalize_ocr(text)

    # Deteksi format marketplace/app lebih dulu
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
        'indomaret': 'Indomaret',
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
        'mr d.i.y': 'MR D.I.Y.',
        'mr diy': 'MR D.I.Y.',
        'mrdiy': 'MR D.I.Y.',
        'primer raya': 'Primer Raya',
        'dinda frozen': 'Dinda Frozen Food',
        'fadhilah frozen': 'Fadhilah Frozen Foods',
        'fadhila frozen': 'Fadhilah Frozen Foods',
        'sb minimarket': 'SB Minimarket',
        'toko bahan kue': 'Toko Bahan Kue Amanah',
        'stoa space': 'Stoa Space',
    }

    text_lower = text.lower()
    for keyword, merchant_name in KNOWN_MERCHANTS.items():
        if keyword in text_lower:
            result.merchant = merchant_name
            break

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
    # Coba Format A dulu (multi-line), lalu Format B (single-line)
    items_a = _parse_items_format_a(lines)
    items_b = _parse_items_format_b(lines)
    items_mrdiy = _parse_items_format_mrdiy(lines)

    # Pilih format dengan item terbanyak
    items = max([items_a, items_b, items_mrdiy], key=lambda x: len(x))

    # Validasi: sum(line_total) harus mendekati total
    if items and result.total:
        item_sum = sum(i.line_total for i in items)
        if abs(item_sum - result.total) < result.total * 0.1:  # toleransi 10%
            result.items = items
            logger.info(f"[OCR] items validated: sum={item_sum} total={result.total}")
        else:
            # Sum tidak cocok — simpan saja tapi log warning
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

        async with httpx.AsyncClient(timeout=30) as client:
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
