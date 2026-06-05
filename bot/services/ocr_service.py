"""
OCR Service
Mendukung dua provider: Tesseract (lokal) dan Google Cloud Vision (cloud).
Pipeline: download file → preprocess → OCR → parse merchant/date/total → return result
"""
import re
import io
from dataclasses import dataclass
from datetime import date
from typing import Optional

from loguru import logger
from PIL import Image, ImageFilter, ImageEnhance
from telegram import Bot

from bot.config import settings


@dataclass
class OcrResult:
    merchant: Optional[str] = None
    total: Optional[float] = None
    tx_date: Optional[date] = None
    raw_text: str = ""
    confidence: float = 0.0
    provider: str = "unknown"


# ──────────────────────────────────────────────
# Image preprocessing
# ──────────────────────────────────────────────

def preprocess_image(img: Image.Image) -> Image.Image:
    """Tingkatkan kualitas gambar untuk OCR."""
    img = img.convert("L")  # grayscale
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    # Scale up kecil
    w, h = img.size
    if w < 800:
        scale = 800 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


# ──────────────────────────────────────────────
# Text parsing
# ──────────────────────────────────────────────

MERCHANT_PATTERNS = [
    r"(?:toko|minimarket|supermarket|indomaret|alfamart|hypermart|hero|giant|"
    r"circle\s*k|lawson|family\s*mart|swalayan|mart|store)\s*[:\-]?\s*([\w\s]+)",
    r"^([\w\s]{3,30})(?:\n|$)",  # baris pertama sebagai merchant fallback
]

DATE_PATTERNS = [
    r"(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})",
    r"(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})",
    r"(\d{1,2})\s+(jan|feb|mar|apr|mei|may|jun|jul|ags|aug|sep|oct|okt|nov|dec|des)\w*\s+(\d{4})",
]

TOTAL_PATTERNS = [
    r"(?:total|jumlah|grand\s*total|bayar|tagihan)\s*[:\-]?\s*(?:rp\.?\s*)?(\d[\d.,]+)",
    r"(?:rp\.?\s*)(\d[\d.,]+)\s*$",  # Rp di akhir baris
    r"(\d{4,}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*$",
]


def _parse_amount_from_text(text: str) -> Optional[float]:
    text = text.strip().replace(" ", "")
    text = text.replace(".", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date_from_match(groups: tuple) -> Optional[date]:
    from dateutil import parser as dp
    try:
        return dp.parse(" ".join(str(g) for g in groups), dayfirst=True).date()
    except Exception:
        return None


def parse_receipt_text(text: str) -> OcrResult:
    result = OcrResult(raw_text=text)
    lines = text.strip().splitlines()

    # --- Merchant ---
    for pattern in MERCHANT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            result.merchant = m.group(1).strip().title() if len(m.groups()) >= 1 else lines[0].strip().title()
            break

    # --- Date ---
    for pattern in DATE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result.tx_date = _parse_date_from_match(m.groups())
            if result.tx_date:
                break

    # --- Total ---
    for pattern in TOTAL_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            amount = _parse_amount_from_text(m.group(1))
            if amount and amount > 100:
                result.total = amount
                break

    # Confidence heuristic
    score = 0.0
    if result.merchant:
        score += 0.3
    if result.tx_date:
        score += 0.3
    if result.total:
        score += 0.4
    result.confidence = round(score, 2)

    return result


# ──────────────────────────────────────────────
# Tesseract provider
# ──────────────────────────────────────────────

async def ocr_tesseract(image_bytes: bytes) -> OcrResult:
    import pytesseract
    img = Image.open(io.BytesIO(image_bytes))
    img = preprocess_image(img)
    text = pytesseract.image_to_string(img, lang="ind+eng")
    result = parse_receipt_text(text)
    result.provider = "tesseract"
    return result


# ──────────────────────────────────────────────
# Google Vision provider
# ──────────────────────────────────────────────

async def ocr_google_vision(image_bytes: bytes) -> OcrResult:
    from google.cloud import vision
    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f"Google Vision error: {response.error.message}")
    annotations = response.text_annotations
    raw_text = annotations[0].description if annotations else ""
    result = parse_receipt_text(raw_text)
    result.provider = "google_vision"
    # Google Vision generally more confident
    result.confidence = min(result.confidence + 0.15, 1.0)
    return result


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

async def process_receipt(bot: Bot, file_id: str) -> OcrResult:
    """Download foto dari Telegram, OCR, return OcrResult."""
    try:
        file = await bot.get_file(file_id)
        image_bytes = await file.download_as_bytearray()

        if settings.ocr_provider == "google":
            return await ocr_google_vision(bytes(image_bytes))
        else:
            return await ocr_tesseract(bytes(image_bytes))

    except Exception as e:
        logger.error(f"OCR error: {e}")
        return OcrResult(raw_text="", confidence=0.0)
