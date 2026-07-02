"""
Item Alias Resolver -- canonical name lookup dari alias OCR/input.

Cara kerja:
1. Bot membaca sheet "Katalog Item" saat startup dan setiap 30 menit
2. Alias table disimpan di RAM sebagai dict: alias -> canonical_name
3. Setiap item name dinormalisasi lalu dicari di alias table
4. Jika tidak ketemu, gunakan normalized name langsung

Sheet "Katalog Item" format:
  Kolom A: canonical_name  (diisi Ojan, ini nama resmi)
  Kolom B: alias_1
  Kolom C: alias_2
  Kolom D: alias_3
  ... dst sampai kolom K (max 10 alias)

Kecepatan: O(1) lookup, tidak ada DB query per transaksi.
Refresh: setiap 30 menit atau dipaksa via /refresh_katalog.
"""

import re
import time
import asyncio
from typing import Optional
from loguru import logger


# ── In-memory alias table ──────────────────────────────────────────────────────

# Format: { normalized_alias: canonical_name }
_alias_table: dict[str, str] = {}
_last_refresh: float = 0
_REFRESH_INTERVAL = 30 * 60  # 30 menit
_SHEET_NAME = "Katalog Item"

# Lock untuk mencegah double refresh
_refresh_lock = asyncio.Lock()


def _normalize(name: str) -> str:
    """Normalisasi untuk lookup -- lowercase, strip angka/unit, strip whitespace."""
    name = name.lower().strip()
    # Hapus angka + unit di akhir (2kg, 1/2, 500gr, dll)
    name = re.sub(r'\s*\d+([./]\d+)?\s*(kg|gr|gram|ml|ltr|liter|pcs|pack|btl|ikat|ekor|buah|dus|slop|set|x)?\s*$', '', name, flags=re.IGNORECASE)
    # Hapus karakter noise
    name = re.sub(r'[@*#¹²³]', '', name)
    # Hapus kode SKU (angka panjang di awal)
    name = re.sub(r'^\d{6,}\s+', '', name)
    # Hapus kode produk alfanumerik (CB023, DY-91228)
    name = re.sub(r'\b[a-z]{1,3}\d+[a-z0-9\-]*\b', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip().rstrip('-.,')
    return name


def resolve_alias(raw_name: str) -> str:
    """
    Resolve nama item ke canonical name.
    Jika tidak ada alias, kembalikan normalized name.
    Sangat cepat -- O(1) dict lookup.
    """
    normalized = _normalize(raw_name)
    canonical = _alias_table.get(normalized)
    if canonical:
        return canonical
    # Coba partial match: cek apakah normalized adalah prefix dari alias yang ada
    for alias, canon in _alias_table.items():
        if normalized and alias.startswith(normalized) and len(alias) - len(normalized) <= 5:
            return canon
    # Tidak ketemu -- kembalikan normalized, title case
    return normalized.title() if normalized else raw_name.strip().title()


def needs_refresh() -> bool:
    return time.time() - _last_refresh > _REFRESH_INTERVAL


async def load_alias_table(sheets_client, spreadsheet_id: str, force: bool = False) -> int:
    """
    Load alias table dari Google Sheets ke memory.
    Thread-safe dengan asyncio lock.
    Returns jumlah alias yang dimuat.
    """
    global _alias_table, _last_refresh

    if not force and not needs_refresh():
        return len(_alias_table)

    async with _refresh_lock:
        # Double-check setelah dapat lock
        if not force and not needs_refresh():
            return len(_alias_table)

        try:
            # Run blocking gspread call di thread pool
            loop = asyncio.get_event_loop()
            rows = await loop.run_in_executor(
                None, _fetch_sheet_rows, sheets_client, spreadsheet_id
            )

            new_table: dict[str, str] = {}
            count = 0

            for row in rows:
                if not row or not row[0].strip():
                    continue
                canonical = row[0].strip()
                # Kolom B-K = alias
                aliases = [row[i].strip() for i in range(1, min(len(row), 11)) if i < len(row) and row[i].strip()]

                # Canonical itu sendiri juga alias ke dirinya
                canonical_norm = _normalize(canonical)
                if canonical_norm:
                    new_table[canonical_norm] = canonical

                for alias in aliases:
                    alias_norm = _normalize(alias)
                    if alias_norm:
                        new_table[alias_norm] = canonical
                        count += 1

            _alias_table = new_table
            _last_refresh = time.time()
            logger.info(f"[ALIAS] loaded {len(_alias_table)} aliases ({count} user-defined) from sheet")
            return len(_alias_table)

        except Exception as e:
            logger.warning(f"[ALIAS] failed to load alias table: {e}")
            return len(_alias_table)  # Tetap pakai yang lama


def _fetch_sheet_rows(sheets_client, spreadsheet_id: str) -> list[list[str]]:
    """Blocking gspread call -- dijalankan di executor."""
    try:
        sh = sheets_client.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet(_SHEET_NAME)
        except Exception:
            # Sheet belum ada -- buat dengan header
            ws = sh.add_worksheet(_SHEET_NAME, rows=200, cols=11)
            ws.update([
                ["canonical_name", "alias_1", "alias_2", "alias_3", "alias_4",
                 "alias_5", "alias_6", "alias_7", "alias_8", "alias_9", "alias_10"]
            ], "A1")
            logger.info(f"[ALIAS] created sheet '{_SHEET_NAME}'")
            return []

        # Skip baris header (baris 1)
        all_rows = ws.get_all_values()
        return all_rows[1:] if len(all_rows) > 1 else []

    except Exception as e:
        logger.error(f"[ALIAS] sheet fetch failed: {e}")
        return []


# ── Auto-suggest alias baru ────────────────────────────────────────────────────

# Track nama-nama baru yang belum ada di alias table
_unseen_names: set[str] = set()


def track_unseen(raw_name: str) -> None:
    """Catat nama item yang tidak match alias manapun -- untuk saran penambahan alias."""
    normalized = _normalize(raw_name)
    if normalized and normalized not in _alias_table:
        _unseen_names.add(normalized)


def get_unseen_names() -> list[str]:
    """Ambil daftar nama yang belum ada di alias table."""
    return sorted(_unseen_names)


def clear_unseen() -> None:
    _unseen_names.clear()


# ── Resolve dengan tracking ────────────────────────────────────────────────────

def resolve_and_track(raw_name: str) -> str:
    """
    Resolve alias + track nama yang belum dikenal.
    Gunakan ini di market.py dan ocr.py.
    """
    normalized = _normalize(raw_name)
    canonical = _alias_table.get(normalized)

    if canonical:
        return canonical

    # Tidak ada di alias table
    track_unseen(raw_name)

    # Kembalikan normalized title case
    return normalized.title() if normalized else raw_name.strip().title()
