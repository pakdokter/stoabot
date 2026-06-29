"""
Parser laporan belanja harian dalam format teks staff.

Format yang didukung:
  *8-9 Juni
  (Tidak Belanja)

  *10 Juni
  • Uang masuk - 1.000.000
  • Dinda - 185.000
  • Ayam - 292.000
  (477.000)

  *15 Juni
  • Ayam - 217.000
  • Belanja Pasar
  -Jamur 1.5 - 40.000
  -Bamer Kupas 2kg - 90.000
  =210.000
  (427.000)

Aturan parsing:
  - Baris "*DD Bulan" atau "*DD-DD Bulan" = tanggal (bintang di depan)
  - "Uang masuk" = transaksi masuk (type='masuk')
  - "• TOKO - NOMINAL" = pengeluaran per toko (type='keluar')
  - "Belanja Pasar" + baris "-item - nominal" + "=total" = pasar (keluar)
  - "(TOTAL)" di akhir = total harian, diabaikan (sudah terhitung per item)
  - "(Tidak Belanja)" = hari tanpa transaksi, skip
  - "#Total..." = footer keseluruhan, diabaikan
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


BULAN_ID = {
    'jan': 1, 'januari': 1,
    'feb': 2, 'februari': 2,
    'mar': 3, 'maret': 3,
    'apr': 4, 'april': 4,
    'mei': 5, 'may': 5,
    'jun': 6, 'juni': 6,
    'jul': 7, 'juli': 7,
    'agu': 8, 'agus': 8, 'agustus': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'okt': 10, 'oktober': 10,
    'nov': 11, 'november': 11,
    'des': 12, 'desember': 12,
}


@dataclass
class ParsedTransaction:
    tx_date: date
    tx_type: str          # 'masuk' atau 'keluar'
    amount: float
    description: str      # "Dinda" atau "Pasar - Jamur 1.5" dll
    category: str = ""    # "pasar" untuk item pasar
    raw_line: str = ""    # baris asli untuk debugging


@dataclass
class ParseResult:
    transactions: list[ParsedTransaction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_days: list[str] = field(default_factory=list)
    total_masuk: float = 0.0
    total_keluar: float = 0.0


def parse_nominal(s: str) -> float:
    """Parse string nominal ke float. Mendukung 185.000, 185,000, 185rb, dll."""
    s = s.strip().replace(' ', '')
    # Hapus Rp prefix
    s = re.sub(r'^[Rr][Pp]\.?\s*', '', s)
    # Handle rb/jt
    multiplier = 1
    if re.search(r'(rb|ribu)$', s, re.IGNORECASE):
        multiplier = 1000
        s = re.sub(r'(rb|ribu)$', '', s, flags=re.IGNORECASE)
    elif re.search(r'(jt|juta)$', s, re.IGNORECASE):
        multiplier = 1_000_000
        s = re.sub(r'(jt|juta)$', '', s, flags=re.IGNORECASE)
    # Hapus titik dan koma (format Indonesia: 1.000.000 atau 1,000,000)
    s = s.replace('.', '').replace(',', '')
    try:
        return float(s) * multiplier
    except ValueError:
        return 0.0


def parse_date_line(line: str, year: int = None) -> Optional[date]:
    """
    Parse baris tanggal. Format: "*10 Juni" atau "*8-9 Juni" atau "*15 Juni 2026".
    Untuk range tanggal, ambil tanggal pertama.
    """
    line = line.lstrip('*').strip()
    # Hapus tanda kurung jika ada
    line = re.sub(r'[()]', '', line).strip()

    # Pattern: "DD-DD Bulan" atau "DD Bulan" atau "DD Bulan YYYY"
    m = re.match(
        r'^(\d{1,2})(?:-\d{1,2})?\s+(\w+)(?:\s+(\d{4}))?',
        line, re.IGNORECASE
    )
    if not m:
        return None

    day = int(m.group(1))
    month_str = m.group(2).lower()[:4]  # "juni" -> "juni"
    year_str = m.group(3)

    # Cari bulan
    month = None
    for k, v in BULAN_ID.items():
        if month_str.startswith(k[:3]):
            month = v
            break
    if not month:
        return None

    if year_str:
        year = int(year_str)
    elif year is None:
        year = date.today().year

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_date_line(line: str) -> bool:
    """Apakah baris ini adalah baris tanggal."""
    clean = line.lstrip('*').strip()
    return bool(re.match(r'^\d{1,2}(?:-\d{1,2})?\s+\w+', clean))


def _is_skip_day(line: str) -> bool:
    """Apakah baris ini menandakan hari tanpa transaksi."""
    return bool(re.search(r'tidak\s+belanja', line, re.IGNORECASE))


def _is_total_line(line: str) -> bool:
    """Baris total harian seperti (477.000) atau total keseluruhan #Total."""
    s = line.strip()
    return (
        (s.startswith('(') and s.endswith(')') and re.search(r'\d', s)) or
        s.startswith('#')
    )


def _parse_item_line(line: str) -> tuple[str, float] | None:
    """
    Parse baris item: "• Dinda - 185.000" atau "• Uang masuk - 1.000.000"
    atau "Dinda¹ - 24.000" atau "Parkir Fadilah - 2.000"
    Return (description, amount) atau None.
    """
    # Hapus bullet dan whitespace
    line = re.sub(r'^[•\-–—]\s*', '', line).strip()
    # Hapus superscript angka dari nama toko (Dinda¹, Dinda²)
    line = re.sub(r'[¹²³⁴⁵⁶⁷⁸⁹⁰]+', '', line)

    # Split di " - " terakhir
    # Cari " - ANGKA" di akhir
    m = re.search(r'\s*[-–]\s*([\d.,]+(?:rb|jt)?)\s*$', line)
    if not m:
        return None

    amount = parse_nominal(m.group(1))
    if amount <= 0:
        return None

    desc = line[:m.start()].strip()
    if not desc:
        return None

    return desc, amount


def _parse_pasar_block(pasar_lines: list[str], tx_date: date) -> list[ParsedTransaction]:
    """
    Parse blok belanja pasar:
    -Jamur 1.5 - 40.000
    -Bamer Kupas 2kg - 90.000
    =210.000
    """
    txs = []
    total_line = None

    for line in pasar_lines:
        line = line.strip()
        if line.startswith('='):
            total_line = line[1:].strip()
            continue

        # Item pasar: "-nama - harga"
        if line.startswith('-'):
            item_line = line.lstrip('-').strip()
            parsed = _parse_item_line('• ' + item_line)
            if parsed:
                desc, amount = parsed
                txs.append(ParsedTransaction(
                    tx_date=tx_date,
                    tx_type='keluar',
                    amount=amount,
                    description=f"Pasar - {desc}",
                    category='pasar',
                    raw_line=line,
                ))

    # Jika ada total pasar tapi tidak ada item ter-parse,
    # simpan sebagai satu transaksi pasar
    if not txs and total_line:
        total_amount = parse_nominal(total_line)
        if total_amount > 0:
            txs.append(ParsedTransaction(
                tx_date=tx_date,
                tx_type='keluar',
                amount=total_amount,
                description="Belanja Pasar",
                category='pasar',
                raw_line='='+total_line,
            ))

    return txs


def parse_report_text(text: str, year: int = None) -> ParseResult:
    """
    Parse laporan belanja harian format teks staff.
    Return ParseResult dengan list transaksi per item.
    """
    result = ParseResult()
    if year is None:
        year = date.today().year

    lines = [l.rstrip() for l in text.splitlines()]

    current_date: Optional[date] = None
    i = 0
    in_pasar = False
    pasar_lines: list[str] = []
    pasar_date: Optional[date] = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip kosong dan footer
        if not stripped or _is_total_line(stripped):
            # Flush pasar jika ada
            if in_pasar and pasar_lines and pasar_date:
                result.transactions.extend(
                    _parse_pasar_block(pasar_lines, pasar_date)
                )
                in_pasar = False
                pasar_lines = []
            i += 1
            continue

        # Baris tanggal: *10 Juni atau *8-9 Juni
        if stripped.startswith('*') or (
            not stripped.startswith(('•', '-', '=', '(', '#')) and
            _is_date_line(stripped)
        ):
            # Flush pasar sebelum pindah tanggal
            if in_pasar and pasar_lines and pasar_date:
                result.transactions.extend(
                    _parse_pasar_block(pasar_lines, pasar_date)
                )
                in_pasar = False
                pasar_lines = []

            parsed_date = parse_date_line(stripped, year)
            if parsed_date:
                current_date = parsed_date
            i += 1
            continue

        # Tidak belanja
        if _is_skip_day(stripped):
            if current_date:
                result.skipped_days.append(str(current_date))
            i += 1
            continue

        # Dalam blok pasar
        if in_pasar:
            if stripped.startswith('-') or stripped.startswith('='):
                pasar_lines.append(stripped)
                i += 1
                continue
            else:
                # Keluar dari blok pasar
                result.transactions.extend(
                    _parse_pasar_block(pasar_lines, pasar_date)
                )
                in_pasar = False
                pasar_lines = []
                # Jangan increment i, proses baris ini lagi

        # Baris item: "• TOKO - NOMINAL" atau "-Parkir - 2.000" di luar blok pasar
        if stripped.startswith('•') or stripped.startswith('–') or (
            stripped.startswith('-') and not in_pasar
        ):
            if current_date is None:
                result.errors.append(f"Item tanpa tanggal: {stripped}")
                i += 1
                continue

            item_part = re.sub(r'^[•–]\s*', '', stripped)

            # Deteksi "Belanja Pasar" (tanpa nominal di baris ini)
            if re.match(r'^belanja\s+pasar\s*$', item_part, re.IGNORECASE):
                in_pasar = True
                pasar_lines = []
                pasar_date = current_date
                i += 1
                continue

            # Item normal: "TOKO - NOMINAL"
            parsed = _parse_item_line(stripped)
            if parsed:
                desc, amount = parsed
                # Deteksi uang masuk
                is_masuk = bool(re.search(r'uang\s+masuk', desc, re.IGNORECASE))
                tx_type = 'masuk' if is_masuk else 'keluar'

                result.transactions.append(ParsedTransaction(
                    tx_date=current_date,
                    tx_type=tx_type,
                    amount=amount,
                    description=desc if not is_masuk else "Uang Masuk",
                    category='masuk' if is_masuk else '',
                    raw_line=stripped,
                ))
            else:
                result.errors.append(f"Tidak terbaca: {stripped}")

            i += 1
            continue

        i += 1

    # Flush pasar terakhir
    if in_pasar and pasar_lines and pasar_date:
        result.transactions.extend(_parse_pasar_block(pasar_lines, pasar_date))

    # Hitung total
    for tx in result.transactions:
        if tx.tx_type == 'masuk':
            result.total_masuk += tx.amount
        else:
            result.total_keluar += tx.amount

    return result
