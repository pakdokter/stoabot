from datetime import date
from decimal import Decimal

from bot.services.rekap_service import (
    COLUMNS, build_rows, generate_pdf, generate_xlsx, parse_note_text, totals,
)

D = date(2026, 7, 15)


def test_nine_columns():
    assert len(COLUMNS) == 9


def test_parse_lines_and_types():
    ents, skipped = parse_note_text(
        "beli kopi 150rb\n+ penjualan hari ini 2jt\ngaji barista Rp 1.500.000\nhalo semua", D, "Budi")
    assert [(e.type, e.amount) for e in ents] == [
        ("keluar", 150000), ("masuk", 2_000_000), ("keluar", 1_500_000)]
    assert ents[0].uraian == "beli kopi" and ents[2].kategori == "Gaji"
    assert skipped == ["halo semua"]


def test_explicit_date_overrides_forward_date():
    ents, _ = parse_note_text("listrik 10/07 350000", D)
    assert ents[0].tanggal == date(2026, 7, 10) and ents[0].amount == 350000


def test_rows_running_balance_and_totals():
    ents, _ = parse_note_text("+ setoran 1jt\nbeli gula 200rb", D)
    rows = build_rows(ents, Decimal(500000))
    assert all(len(r) == 9 for r in rows)
    assert [r[8] for r in rows] == [500000, 1_500_000, 1_300_000]
    assert rows[1][2] == "KM-260715-01" and rows[2][2] == "KK-260715-01"
    assert totals(rows) == (1_000_000, 200_000, 1_300_000)


def test_exports_nonempty():
    ents, _ = parse_note_text("+ setoran 1jt\nbeli gula 200rb", D)
    rows = build_rows(ents)
    assert generate_xlsx(rows)[:2] == b"PK"
    assert generate_pdf(rows)[:4] == b"%PDF"
