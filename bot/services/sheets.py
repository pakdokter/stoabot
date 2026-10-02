"""
Google Sheets service — simpan transaksi real-time ke Google Sheet.

Sheet structure:
  Sheet "Transaksi" dengan kolom:
  A: Timestamp
  B: Tanggal Transaksi
  C: User ID
  D: Nama User
  E: Jenis (MASUK/KELUAR)
  F: Nominal
  G: Keterangan
  H: Sumber (manual/struk)
"""
import os
import json
from datetime import datetime
from loguru import logger
from typing import Optional

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    logger.warning("[SHEETS] gspread not installed, skipping")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADER = [
    "Timestamp", "Tanggal", "User ID", "Nama User",
    "Jenis", "Nominal", "Keterangan", "Sumber"
]

_client = None
_sheet = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not GSPREAD_AVAILABLE:
        return None

    # Ambil credentials dari env variable (JSON string)
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        logger.warning("[SHEETS] GOOGLE_SHEETS_CREDENTIALS not set")
        return None

    try:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _client = gspread.authorize(creds)
        logger.info("[SHEETS] Google Sheets client initialized")
        return _client
    except Exception as e:
        logger.error(f"[SHEETS] Failed to init client: {e}")
        return None


def _get_sheet(sheet_name: str = "Transaksi"):
    global _sheet
    if _sheet is not None:
        return _sheet

    client = _get_client()
    if not client:
        return None

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        logger.warning("[SHEETS] GOOGLE_SHEET_ID not set")
        return None

    try:
        spreadsheet = client.open_by_key(sheet_id)

        # Cari worksheet "Transaksi", buat jika belum ada
        try:
            ws = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=sheet_name, rows=10000, cols=10)
            ws.append_row(SHEET_HEADER)
            logger.info(f"[SHEETS] Created worksheet '{sheet_name}' with headers")

        # Pastikan header ada
        first_row = ws.row_values(1)
        if not first_row or first_row[0] != "Timestamp":
            ws.insert_row(SHEET_HEADER, 1)
            logger.info("[SHEETS] Headers added")

        _sheet = ws
        logger.info(f"[SHEETS] Connected to worksheet '{sheet_name}'")
        return _sheet

    except Exception as e:
        logger.error(f"[SHEETS] Failed to get sheet: {e}")
        return None


async def append_transaction(
    user_id: int,
    user_name: str,
    tx_type: str,
    amount: float,
    description: str,
    tx_date,
    source: str = "manual",
):
    """
    Simpan satu baris transaksi ke Google Sheet.
    Dipanggil setiap kali transaksi berhasil disimpan ke DB.
    Gagal gracefully — tidak mengganggu alur bot.
    """
    try:
        ws = _get_sheet()
        if not ws:
            return

        now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        date_str = tx_date.strftime("%d/%m/%Y") if hasattr(tx_date, 'strftime') else str(tx_date)
        jenis = "MASUK" if tx_type == "masuk" else "KELUAR"

        row = [
            now,
            date_str,
            str(user_id),
            user_name,
            jenis,
            float(amount),
            description,
            source,
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"[SHEETS] Appended: uid={user_id} {jenis} {amount} '{description}'")

    except Exception as e:
        # Graceful fail — jangan crash bot karena sheets error
        logger.error(f"[SHEETS] append_transaction failed: {e}")
