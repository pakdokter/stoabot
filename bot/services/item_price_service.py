"""
Item Price Service — catat dan analisis harga item dari setiap transaksi.

Setiap kali ada transaksi keluar dengan item detail (dari OCR atau input pasar/toko),
harga per item direkam ke tabel item_prices.

Normalisasi nama item:
- lowercase
- strip angka qty di akhir (misal "BERAS 5KG" -> "beras")
- strip kode SKU (misal "1011000103 GF MILK ESL" -> "gf milk esl")
- strip whitespace berlebih

Analisis yang tersedia:
- harga min/max per item per bulan
- toko termurah per item
- trend harga naik/turun
- frekuensi pembelian

Sync ke Google Sheets:
- sheet "Katalog Harga" dengan kolom: Item, Toko, Bulan, Min, Max, Avg, Frekuensi
"""

import re
import uuid
from datetime import date, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import ItemPrice, Transaction


# ── Normalisasi nama item ──────────────────────────────────────────────────────

# Kata yang tidak informatif untuk nama item
_STOP_WORDS = {
    'pack', 'pck', 'pcs', 'slop', 'set', 'box', 'dus', 'karton',
    'btl', 'btl', 'ltr', 'liter', 'kg', 'gr', 'gram', 'ml',
    'x', 'dan', 'dan', 'the', 'a',
}

# Prefix kode SKU angka panjang (Indomaret, Sukanda, dll)
_SKU_PATTERN = re.compile(r'^\d{6,}\s+')

# Angka di akhir nama (ukuran/qty)
_TRAILING_NUMBER = re.compile(r'\s+\d+(\.\d+)?\s*(kg|gr|gram|ml|ltr|liter|pcs|pack|btl|x)?\s*$', re.IGNORECASE)

# Kode produk: huruf kapital + angka (CB023C8025, DY-91228, dll)
_PRODUCT_CODE = re.compile(r'\b[A-Z]{2,}\d+[A-Z0-9\-]*\b')


def normalize_item_name(raw_name: str) -> str:
    """
    Normalisasi nama item untuk deduplication.

    Contoh:
      "TROPICAL MNYK BTL 2L" -> "tropical mnyk"
      "GF MILK ESL FC IP 1000 ML @12 *" -> "gf milk esl fc ip"
      "BAMER KUPAS 2KG" -> "bamer kupas"
      "1011000103 GF Milk ESL" -> "gf milk esl"
    """
    name = raw_name.strip()

    # Hapus asterisk dan karakter noise
    name = re.sub(r'[@*#]', '', name)

    # Hapus prefix SKU angka
    name = _SKU_PATTERN.sub('', name)

    # Lowercase
    name = name.lower()

    # Hapus angka di akhir (ukuran)
    name = _TRAILING_NUMBER.sub('', name)

    # Hapus kode produk (huruf+angka)
    name = re.sub(r'\b[a-z]{1,2}\d+[a-z0-9\-]*\b', '', name)

    # Hapus angka murni
    name = re.sub(r'\b\d+(\.\d+)?\b', '', name)

    # Bersihkan whitespace
    name = re.sub(r'\s+', ' ', name).strip()

    # Hapus trailing noise
    name = name.rstrip('-.,').strip()

    return name if len(name) >= 2 else raw_name.lower().strip()[:64]


def parse_unit_from_name(raw_name: str) -> tuple[str, float]:
    """
    Extract unit dan qty dari nama item.
    Returns (unit, qty).
    Contoh: "BAMER KUPAS 2KG" -> ("kg", 2.0)
    """
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*(kg|gr|gram|ml|ltr|liter|pcs|pack|btl|ikat|ekor|buah|dus|slop|set)\b',
        raw_name, re.IGNORECASE
    )
    if m:
        qty = float(m.group(1))
        unit = m.group(2).lower()
        return unit, qty
    return '', 1.0


# ── Insert ke item_prices ──────────────────────────────────────────────────────

async def record_item_price(
    session: AsyncSession,
    item_name_raw: str,
    toko: Optional[str],
    total_price: float,
    qty: float = 1.0,
    unit: str = '',
    transaction_date: Optional[date] = None,
    transaction_id: Optional[uuid.UUID] = None,
) -> Optional[ItemPrice]:
    """
    Catat harga satu item ke tabel item_prices.
    Hitung unit_price otomatis dari total_price / qty.
    """
    if not item_name_raw or total_price <= 0:
        return None

    item_name = normalize_item_name(item_name_raw)
    if not item_name:
        return None

    # Jika unit tidak diisi, coba parse dari nama raw
    if not unit:
        parsed_unit, parsed_qty = parse_unit_from_name(item_name_raw)
        if parsed_unit:
            unit = parsed_unit
            if parsed_qty > 1 and qty == 1.0:
                qty = parsed_qty

    unit_price = total_price / qty if qty > 0 else total_price
    tx_date = transaction_date or date.today()

    record = ItemPrice(
        item_name=item_name[:128],
        item_name_raw=item_name_raw[:128],
        toko=toko[:64] if toko else None,
        unit=unit[:32] if unit else None,
        unit_price=round(unit_price, 2),
        total_price=round(total_price, 2),
        qty=qty,
        transaction_date=tx_date,
        transaction_id=transaction_id,
    )
    session.add(record)
    logger.debug(f"[PRICE] recorded: {item_name!r} @ {unit_price:,.0f}/{unit or 'unit'} from {toko}")
    return record


async def record_items_from_transaction(
    session: AsyncSession,
    tx: Transaction,
    items: list[dict],
    toko: Optional[str] = None,
) -> int:
    """
    Catat semua item dari satu transaksi ke item_prices.
    items: list of dict dengan keys: name, qty, unit, unit_price, total (atau line_total)
    Returns jumlah item yang direcord.
    """
    count = 0
    for item in items:
        name = item.get('name') or item.get('item_name', '')
        total = item.get('total') or item.get('line_total') or item.get('total_price', 0)
        qty = item.get('qty', 1.0)
        unit = item.get('unit', '')
        unit_price = item.get('unit_price', 0)

        if not name or total <= 0:
            continue

        # Jika unit_price tersedia, gunakan itu; jika tidak hitung dari total/qty
        if unit_price <= 0 and qty > 0:
            unit_price = total / qty

        record = await record_item_price(
            session=session,
            item_name_raw=name,
            toko=toko or tx.description.split(' -- ')[0] if tx.description else None,
            total_price=float(total),
            qty=float(qty),
            unit=str(unit),
            transaction_date=tx.transaction_date,
            transaction_id=tx.id,
        )
        if record:
            count += 1

    return count


# ── Query harga ────────────────────────────────────────────────────────────────

async def get_price_summary(
    session: AsyncSession,
    item_name_query: str,
    months: int = 3,
) -> list[dict]:
    """
    Ambil ringkasan harga item N bulan terakhir.
    item_name_query: bisa partial match.
    """
    normalized = normalize_item_name(item_name_query)
    cutoff = date.today() - timedelta(days=months * 30)

    result = await session.execute(text("""
        SELECT
            item_name,
            toko,
            unit,
            DATE_TRUNC('month', transaction_date)::date AS bulan,
            MIN(unit_price) AS harga_min,
            MAX(unit_price) AS harga_max,
            ROUND(AVG(unit_price)) AS harga_avg,
            COUNT(*) AS frekuensi,
            MAX(transaction_date) AS terakhir_beli
        FROM item_prices
        WHERE item_name ILIKE :query
          AND transaction_date >= :cutoff
        GROUP BY item_name, toko, unit, DATE_TRUNC('month', transaction_date)
        ORDER BY item_name, bulan DESC, toko
    """), {"query": f"%{normalized}%", "cutoff": cutoff})

    rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


async def get_cheapest_toko(
    session: AsyncSession,
    item_name_query: str,
    months: int = 3,
) -> list[dict]:
    """Toko termurah untuk item tertentu dalam N bulan terakhir."""
    normalized = normalize_item_name(item_name_query)
    cutoff = date.today() - timedelta(days=months * 30)

    result = await session.execute(text("""
        SELECT
            toko,
            ROUND(AVG(unit_price)) AS harga_rata,
            MIN(unit_price) AS harga_min,
            COUNT(*) AS frekuensi
        FROM item_prices
        WHERE item_name ILIKE :query
          AND transaction_date >= :cutoff
          AND toko IS NOT NULL
        GROUP BY toko
        ORDER BY harga_rata ASC
    """), {"query": f"%{normalized}%", "cutoff": cutoff})

    return [dict(r._mapping) for r in result.fetchall()]


# ── Sync ke Google Sheets ──────────────────────────────────────────────────────

async def sync_price_catalog_to_sheets(
    session: AsyncSession,
    sheets_client,
    spreadsheet_id: str,
    months: int = 6,
) -> int:
    """
    Sync ringkasan harga item ke sheet 'Katalog Harga'.
    Dibuat/update sekali seminggu atau on-demand.
    Returns jumlah baris yang ditulis.
    """
    cutoff = date.today() - timedelta(days=months * 30)

    result = await session.execute(text("""
        SELECT
            item_name,
            toko,
            unit,
            TO_CHAR(DATE_TRUNC('month', transaction_date), 'Mon YYYY') AS bulan,
            MIN(unit_price) AS harga_min,
            MAX(unit_price) AS harga_max,
            ROUND(AVG(unit_price)) AS harga_avg,
            COUNT(*) AS frekuensi,
            MAX(transaction_date) AS terakhir_beli
        FROM item_prices
        WHERE transaction_date >= :cutoff
        GROUP BY item_name, toko, unit, DATE_TRUNC('month', transaction_date)
        ORDER BY item_name, DATE_TRUNC('month', transaction_date) DESC, toko
    """), {"cutoff": cutoff})

    rows = result.fetchall()
    if not rows:
        return 0

    try:
        # Buka atau buat sheet Katalog Harga
        try:
            ws = sheets_client.open_by_key(spreadsheet_id).worksheet("Katalog Harga")
        except Exception:
            sh = sheets_client.open_by_key(spreadsheet_id)
            ws = sh.add_worksheet("Katalog Harga", rows=1000, cols=10)

        # Header
        header = ["Item", "Toko", "Unit", "Bulan", "Harga Min", "Harga Max", "Harga Avg", "Frekuensi", "Terakhir Beli"]
        data = [header]

        for r in rows:
            data.append([
                r.item_name,
                r.toko or '',
                r.unit or '',
                r.bulan,
                int(r.harga_min),
                int(r.harga_max),
                int(r.harga_avg),
                r.frekuensi,
                str(r.terakhir_beli),
            ])

        ws.clear()
        ws.update(data, "A1")
        logger.info(f"[PRICE] synced {len(data)-1} rows to Katalog Harga sheet")
        return len(data) - 1

    except Exception as e:
        logger.error(f"[PRICE] sheets sync failed: {e}")
        return 0
