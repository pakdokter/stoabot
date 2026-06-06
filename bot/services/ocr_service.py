"""
OCR Service menggunakan OCR.space API
"""
import re
import httpx
from dataclasses import dataclass
from datetime import date
from typing import Optional
from loguru import logger


@dataclass
class OcrResult:
    merchant: Optional[str] = None
    total: Optional[float] = None
    tx_date: Optional[date] = None
    raw_text: str = ""
    confidence: float = 0.0
    provider: str = "ocrspace"


async def process_receipt(bot, file_id: str) -> OcrResult:
    try:
        import os
        api_key = os.environ.get("OCR_SPACE_API_KEY", "helloworld")
        file = await bot.get_file(file_id)
        image_bytes = bytes(await file.download_as_bytearray())

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.ocr.space/parse/image",
                data={
                    "apikey": api_key,
                    "language": "eng",
                    "isOverlayRequired": "false",
                    "detectOrientation": "true",
                    "scale": "true",
                    "OCREngine": "2",
                },
                files={"file": ("struk.jpg", image_bytes, "image/jpeg")},
            )

        data = response.json()
        if data.get("IsErroredOnProcessing"):
            logger.error(f"OCR.space error: {data.get('ErrorMessage')}")
            return OcrResult(confidence=0.0)

        parsed_results = data.get("ParsedResults", [])
        raw_text = parsed_results[0].get("ParsedText", "") if parsed_results else ""
        logger.info(f"OCR raw: {repr(raw_text)}")

        if not raw_text:
            return OcrResult(confidence=0.0)

        return _parse_receipt_text(raw_text)

    except Exception as e:
        logger.error(f"OCR error: {e}")
        return OcrResult(confidence=0.0)


def _extract_amount(text: str) -> Optional[float]:
    """
    Ekstrak angka dari string — toleran terhadap format:
    45.000, 45,000, 45.000=, Rp45.000, 45000
    Skip jika > 9 digit (nomor telepon).
    """
    # Hapus karakter non-angka kecuali titik dan koma
    cleaned = re.sub(r'[^\d.,]', '', text)
    if not cleaned:
        return None
    # Hapus titik/koma sebagai pemisah ribuan
    cleaned = cleaned.replace('.', '').replace(',', '')
    if not cleaned.isdigit():
        return None
    if len(cleaned) > 9:
        return None
    amount = float(cleaned)
    if 100 <= amount <= 99_999_999:
        return amount
    return None


def _parse_receipt_text(text: str) -> OcrResult:
    result = OcrResult(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Merchant ──
    skip_words = ['telp', 'fax', 'no.', 'no:', 'kasir', 'area', 'jl.', 'jln', 'pel.', 'pelanggan']
    for line in lines[:6]:
        if re.match(r'^[\d\s\-\+\(\)\.\/:=]+$', line):
            continue
        if len(line) < 3:
            continue
        if any(w in line.lower() for w in skip_words):
            continue
        result.merchant = line.title()
        break

    # ── Tanggal ──
    for m in re.finditer(r'(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})', text):
        try:
            from dateutil import parser as dp
            result.tx_date = dp.parse(m.group(0), dayfirst=True).date()
            break
        except Exception:
            pass

    # ── Total ──
    # Cari baris per baris, prioritas berdasarkan keyword
    candidates = []  # (prioritas, amount)

    for line in lines:
        ll = line.lower()

        # Skip baris kembali/change
        if re.search(r'\b(kembali|kembalian|change)\b', ll):
            continue

        # Cari semua angka di baris (format bebas: 45.000, 45.000=, dll)
        # Regex: angka dengan opsional titik/koma ribuan
        raw_numbers = re.findall(r'[\d]{1,3}(?:[.,]\d{3})*|\d+', line)

        if re.search(r'\b(total|jumlah|tagihan)\b', ll):
            for raw in raw_numbers:
                amt = _extract_amount(raw)
                if amt:
                    candidates.append((0, amt))

        elif re.search(r'\b(tunai|cash|bayar)\b', ll):
            for raw in raw_numbers:
                amt = _extract_amount(raw)
                if amt:
                    candidates.append((1, amt))

    # Ambil prioritas tertinggi, jika sama ambil yang terkecil
    # (total < tunai, jadi ambil terkecil di prioritas sama)
    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1]))
        result.total = candidates[0][1]

    # Fallback: angka terbesar di struk yang bukan dari baris kembali
    if not result.total:
        all_amounts = []
        for line in lines:
            if re.search(r'\b(kembali|kembalian|change|telp|fax)\b', line.lower()):
                continue
            for raw in re.findall(r'[\d]{1,3}(?:[.,]\d{3})*|\d+', line):
                amt = _extract_amount(raw)
                if amt:
                    all_amounts.append(amt)
        if all_amounts:
            # Ambil nilai terbesar yang lebih kecil dari nilai tunai
            all_amounts.sort()
            result.total = all_amounts[-1]

    # ── Confidence ──
    score = 0.0
    if result.merchant: score += 0.3
    if result.tx_date: score += 0.3
    if result.total: score += 0.4
    result.confidence = round(score, 2)

    return result
