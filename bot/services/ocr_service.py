"""
OCR Service menggunakan OCR.space API
Lebih akurat dari Tesseract untuk struk Indonesia.
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
    """Download foto dari Telegram, kirim ke OCR.space, parse hasilnya."""
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

        raw_text = ""
        parsed_results = data.get("ParsedResults", [])
        if parsed_results:
            raw_text = parsed_results[0].get("ParsedText", "")

        if not raw_text:
            return OcrResult(confidence=0.0)

        return _parse_receipt_text(raw_text)

    except Exception as e:
        logger.error(f"OCR error: {e}")
        return OcrResult(confidence=0.0)


def _parse_receipt_text(text: str) -> OcrResult:
    result = OcrResult(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Merchant: baris pertama yang bukan angka ──
    for line in lines[:4]:
        if not re.match(r'^[\d\s\-\+\(\)\.\/]+$', line) and len(line) > 2:
            result.merchant = line.title()
            break

    # ── Tanggal ──
    date_patterns = [
        r'(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})',
        r'(\d{4})[\/\-\.](\d{1,2})[\/\-\.](\d{1,2})',
    ]
    for pattern in date_patterns:
        m = re.search(pattern, text)
        if m:
            try:
                from dateutil import parser as dp
                result.tx_date = dp.parse(m.group(0), dayfirst=True).date()
                break
            except Exception:
                pass

    # ── Total: cari kata kunci total/bayar/tagihan ──
    # Khusus hindari nomor telepon (>=10 digit berturut-turut)
    total_patterns = [
        r'(?:total|grand\s*total|jumlah|tagihan|bayar|tunai|cash)\s*[:\-]?\s*(?:rp\.?\s*)?(\d[\d\.]+)',
        r'(?:rp\.?\s*)(\d[\d\.]{4,})\s*$',
    ]
    for pattern in total_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            raw = m.group(1).replace('.', '').replace(',', '')
            # Skip jika lebih dari 10 digit (kemungkinan nomor telepon)
            if len(raw) > 10:
                continue
            try:
                amount = float(raw)
                if 100 <= amount <= 99_999_999:
                    result.total = amount
                    break
            except ValueError:
                pass
        if result.total:
            break

    # Hitung confidence
    score = 0.0
    if result.merchant:
        score += 0.3
    if result.tx_date:
        score += 0.3
    if result.total:
        score += 0.4
    result.confidence = round(score, 2)

    return result
