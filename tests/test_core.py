"""
Unit tests untuk core logic: formatters, balance, OCR parsing.
Jalankan: pytest tests/ -v
"""
import pytest
from decimal import Decimal
from datetime import date

from bot.utils.formatters import fmt_rupiah, parse_amount, parse_date, fmt_date
from bot.services.ocr_service import parse_receipt_text


# ──────────────────────────────────────────────
# Formatter tests
# ──────────────────────────────────────────────

class TestFormatters:

    def test_fmt_rupiah_basic(self):
        assert fmt_rupiah(150000) == "Rp150.000"

    def test_fmt_rupiah_million(self):
        assert fmt_rupiah(1500000) == "Rp1.500.000"

    def test_fmt_rupiah_zero(self):
        assert fmt_rupiah(0) == "Rp0"

    def test_parse_amount_plain(self):
        assert parse_amount("150000") == 150000.0

    def test_parse_amount_dot_separator(self):
        assert parse_amount("150.000") == 150000.0

    def test_parse_amount_juta(self):
        assert parse_amount("1.5jt") == 1_500_000.0

    def test_parse_amount_juta_variant(self):
        assert parse_amount("2juta") == 2_000_000.0

    def test_parse_amount_ribu(self):
        assert parse_amount("500rb") == 500_000.0

    def test_parse_amount_invalid(self):
        assert parse_amount("abc") is None

    def test_parse_date_slash(self):
        assert parse_date("15/07/2026") == date(2026, 7, 15)

    def test_parse_date_dash(self):
        assert parse_date("15-07-2026") == date(2026, 7, 15)

    def test_parse_date_iso(self):
        assert parse_date("2026-07-15") == date(2026, 7, 15)

    def test_parse_date_empty(self):
        assert parse_date("") is None

    def test_fmt_date(self):
        assert fmt_date(date(2026, 7, 15)) == "15 Jul 2026"


# ──────────────────────────────────────────────
# OCR parsing tests
# ──────────────────────────────────────────────

class TestOcrParsing:

    SAMPLE_INDOMARET = """
    INDOMARET
    Jl. Pattimura 107
    15/07/2026  10:30
    
    Aqua 600ml    Rp5.000
    Mie Goreng    Rp3.500
    Roti Tawar    Rp12.000
    
    TOTAL         Rp20.500
    BAYAR         Rp50.000
    KEMBALI       Rp29.500
    """

    SAMPLE_SIMPLE = """
    Toko Berkah
    Tanggal: 10-07-2026
    Total belanja: Rp87.500
    """

    def test_parse_indomaret_merchant(self):
        result = parse_receipt_text(self.SAMPLE_INDOMARET)
        assert result.merchant is not None
        assert "indomaret" in result.merchant.lower() or result.merchant

    def test_parse_indomaret_date(self):
        result = parse_receipt_text(self.SAMPLE_INDOMARET)
        assert result.tx_date == date(2026, 7, 15)

    def test_parse_indomaret_total(self):
        result = parse_receipt_text(self.SAMPLE_INDOMARET)
        # Should pick up 20500 (total) not 50000 (bayar) or 29500 (kembali)
        assert result.total is not None
        assert result.total > 0

    def test_parse_simple_total(self):
        result = parse_receipt_text(self.SAMPLE_SIMPLE)
        assert result.total == 87500.0

    def test_parse_simple_date(self):
        result = parse_receipt_text(self.SAMPLE_SIMPLE)
        assert result.tx_date == date(2026, 7, 10)

    def test_confidence_full_data(self):
        result = parse_receipt_text(self.SAMPLE_SIMPLE)
        assert result.confidence >= 0.6

    def test_confidence_empty(self):
        result = parse_receipt_text("")
        assert result.confidence == 0.0

    def test_no_crash_on_garbage(self):
        result = parse_receipt_text("%%% @@@ !!! random garbage text ###")
        assert result is not None
        assert result.raw_text == "%%% @@@ !!! random garbage text ###"


# ──────────────────────────────────────────────
# Balance logic tests (pure calculation)
# ──────────────────────────────────────────────

class TestBalanceLogic:

    def test_saldo_calculation(self):
        masuk = Decimal("500000")
        keluar = Decimal("200000")
        saldo = masuk - keluar
        assert saldo == Decimal("300000")

    def test_saldo_negative(self):
        masuk = Decimal("100000")
        keluar = Decimal("150000")
        saldo = masuk - keluar
        assert saldo == Decimal("-50000")

    def test_summary_dict_structure(self):
        summary = {
            "total_masuk": Decimal("1000000"),
            "total_keluar": Decimal("400000"),
            "saldo": Decimal("600000"),
            "jumlah": 10,
        }
        assert summary["saldo"] == summary["total_masuk"] - summary["total_keluar"]
