from decimal import Decimal
from datetime import date


def fmt_rupiah(amount: float | Decimal) -> str:
    """Format angka ke format Rupiah Indonesia. Contoh: 1500000 → Rp1.500.000"""
    amount = int(amount)
    return f"Rp{amount:,.0f}".replace(",", ".")


def fmt_date(d: date) -> str:
    """Format tanggal ke DD Mon YYYY."""
    MONTHS = [
        "", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
        "Jul", "Ags", "Sep", "Okt", "Nov", "Des"
    ]
    return f"{d.day:02d} {MONTHS[d.month]} {d.year}"


def fmt_date_full(d: date) -> str:
    MONTHS = [
        "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember"
    ]
    return f"{d.day} {MONTHS[d.month]} {d.year}"


def fmt_type(t: str) -> str:
    return "➕ MASUK" if t == "masuk" else "➖ KELUAR"


def parse_amount(text: str) -> float | None:
    """
    Parse input nominal dari user.
    Mendukung: 150000, 150.000, 1,5jt, 1.5jt, 1500rb, dll.
    """
    text = text.lower().strip().replace(" ", "")

    multiplier = 1
    has_suffix = False

    if text.endswith("juta"):
        multiplier = 1_000_000
        text = text[:-4]
        has_suffix = True
    elif text.endswith("jt"):
        multiplier = 1_000_000
        text = text[:-2]
        has_suffix = True
    elif text.endswith("ribu"):
        multiplier = 1_000
        text = text[:-4]
        has_suffix = True
    elif text.endswith("rb"):
        multiplier = 1_000
        text = text[:-2]
        has_suffix = True

    if has_suffix:
        # Dengan suffix juta/rb: titik = desimal (e.g. 1.5jt → 1.5 × 1_000_000)
        text = text.replace(",", ".")
    else:
        # Tanpa suffix: titik/koma adalah pemisah ribuan
        text = text.replace(".", "").replace(",", "")

    try:
        return float(text) * multiplier
    except ValueError:
        return None


def parse_date(text: str) -> date | None:
    """Parse tanggal dari input user. Format: DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD."""
    from dateutil import parser as dateparser
    text = text.strip()
    if not text:
        return None
    try:
        return dateparser.parse(text, dayfirst=True).date()
    except Exception:
        return None
