from datetime import date
from decimal import Decimal

from bot.services.kas_report import generate_pdf, generate_xlsx, hitung_saldo, label_kantong, ringkas
from bot.services.rekap_service import parse_note_text, to_kas_rows

D = date(2026, 7, 15)


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


def test_rekap_ke_format_reconbot():
    ents, _ = parse_note_text("+ setoran 1jt\nbeli gula 200rb", D, "Budi")
    rows = to_kas_rows(ents, label_kantong("Widia"))
    # Debit negatif (keluar), Kredit positif (masuk)
    assert rows[0].kredit == Decimal(1_000_000) and rows[0].debit is None
    assert rows[1].debit == Decimal(-200_000) and rows[1].kredit is None
    assert rows[0].ket == "No. Bukti KM-260715-01; Sumber: Budi"
    assert rows[1].ket.startswith("No. Bukti KK-260715-01")
    assert hitung_saldo(rows, Decimal(500_000)) == [Decimal(1_500_000), Decimal(1_300_000)]
    assert ringkas(rows, Decimal(500_000)) == (Decimal(-200_000), Decimal(1_000_000), Decimal(1_300_000))


def test_exports_nonempty():
    ents, _ = parse_note_text("+ setoran 1jt\nbeli gula 200rb", D)
    rows = to_kas_rows(ents, label_kantong("Widia"))
    assert generate_xlsx([("Widia", rows, Decimal(0))])[:2] == b"PK"
    assert generate_pdf("Widia", rows, Decimal(0), "15 Jul 2026")[:4] == b"%PDF"
