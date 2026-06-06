"""
OCR Service — parser berbasis konteks struk.
BUG 1 FIX: hapus fallback sum(item_values) — penyebab 197588.
           Semua angka dari baris unknown (termasuk nomor struk 196962)
           di-sum jadi 196962+45000+50000+5000 = angka aneh.
           Fix: jika tidak ada keyword total, return None, user edit manual.
"""
import re
import httpx
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from loguru import logger


@dataclass
class ReceiptItem:
    name: str
    qty: float = 1.0
    price: float = 0.0


@dataclass
class OcrResult:
    merchant: Optional[str] = None
    total: Optional[float] = None
    grand_total: Optional[float] = None
    cash_paid: Optional[float] = None
    change: Optional[float] = None
    tx_date: Optional[date] = None
    items: list = field(default_factory=list)
    raw_text: str = ""
    confidence: float = 0.0
    provider: str = "ocrspace"


TOTAL_KEYWORDS = [
    r'\btotal\b', r'\bgrand\s*total\b', r'\bjumlah\b',
    r'\btagihan\b', r'\bsubtotal\b', r'\bsub\s*total\b',
    r'\bnetto\b', r'\bnet\b',
]
PAYMENT_KEYWORDS = [
    r'\btunai\b', r'\bcash\b', r'\bbayar\b', r'\bdibayar\b',
    r'\btransfer\b', r'\bdebit\b', r'\bkredit\b', r'\bkartu\b',
    r'\bqris\b', r'\bova\b', r'\bgopay\b', r'\bshopee\b',
    r'\bdana\b', r'\blinkaja\b',
]
CHANGE_KEYWORDS = [
    r'\bkembali\b', r'\bkembalian\b', r'\bchange\b',
    r'\bkembalikan\b', r'\bselisih\b',
]
DISCOUNT_KEYWORDS = [
    r'\bdiskon\b', r'\bdiscount\b', r'\bdisc\b', r'\bkorting\b',
]
TAX_KEYWORDS = [
    r'\bppn\b', r'\bpajak\b', r'\btax\b', r'\bvat\b',
    r'\bservice\b', r'\bservis\b',
]
SKIP_LINE_PATTERNS = [
    r'\btelp\b', r'\bfax\b', r'\bemail\b', r'\bwww\b', r'\bhttp\b',
    r'\bno\.?\s*struk\b', r'\bno\.?\s*transaksi\b', r'\binvoice\b',
    r'\bkasir\b', r'\boperator\b', r'\bpelanggan\b', r'\bpel\.\b',
    r'\bterima kasih\b', r'\bjangan lupa\b', r'\bdatang kembali\b',
    r'\bpowered by\b', r'\bcopyright\b',
    r'^\s*[-=*_]{3,}\s*$',
]


def _matches_any(text: str, patterns: list) -> bool:
    return any(re.search(p, text.lower()) for p in patterns)


def _extract_numbers(text: str) -> list:
    results = []
    for m in re.finditer(r'\d{1,3}(?:[.,]\d{3})+|\d+', text):
        raw = m.group(0).replace('.', '').replace(',', '')
        if raw.isdigit() and 1 <= len(raw) <= 9:
            val = float(raw)
            if val >= 100:
                results.append(val)
    return results


def _classify_line(line: str) -> str:
    if _matches_any(line, SKIP_LINE_PATTERNS): return 'skip'
    if _matches_any(line, CHANGE_KEYWORDS): return 'change'
    if _matches_any(line, PAYMENT_KEYWORDS): return 'payment'
    if _matches_any(line, TOTAL_KEYWORDS): return 'total'
    if _matches_any(line, DISCOUNT_KEYWORDS): return 'discount'
    if _matches_any(line, TAX_KEYWORDS): return 'tax'
    return 'unknown'


def _parse_receipt_text(text: str) -> OcrResult:
    result = OcrResult(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    logger.info(f"[OCR-PARSE] lines={len(lines)}")
    logger.info(f"[OCR-PARSE] raw=\n{repr(text[:600])}")

    header_skip = ['telp', 'fax', 'no.', 'no:', 'kasir', 'area', 'jl.',
                   'jln', 'pel.', 'pelanggan', 'tanggal', 'date', 'struk']
    for line in lines[:8]:
        if re.match(r'^[\d\s\-\+\(\)\.\/:=,]+$', line): continue
        if len(line) < 3: continue
        if any(w in line.lower() for w in header_skip): continue
        if _matches_any(line, SKIP_LINE_PATTERNS): continue
        result.merchant = line.title()
        break

    for m in re.finditer(r'(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})', text):
        try:
            from dateutil import parser as dp
            result.tx_date = dp.parse(m.group(0), dayfirst=True).date()
            break
        except Exception:
            pass

    total_candidates = []
    payment_values = []
    change_values = []

    for line in lines:
        numbers = _extract_numbers(line)
        if not numbers:
            continue
        cls = _classify_line(line)
        logger.info(f"[OCR-LINE] cls={cls!r} line={line!r} numbers={numbers}")

        if cls == 'change':
            change_values.extend(numbers)
        elif cls == 'payment':
            payment_values.extend(numbers)
        elif cls == 'total':
            priority = 0 if re.search(r'\bgrand\b', line.lower()) else 2 if re.search(r'\bsub\b', line.lower()) else 1
            for n in numbers:
                total_candidates.append((n, priority))
                logger.info(f"[OCR-CANDIDATE] value={n} priority={priority} line={line!r}")

    if change_values:
        result.change = change_values[0]
    if payment_values:
        result.cash_paid = payment_values[0]

    logger.info(f"[OCR-SUMMARY] total_candidates={total_candidates} payment={payment_values} change={change_values}")

    if total_candidates:
        total_candidates.sort(key=lambda x: x[1])
        best_priority = total_candidates[0][1]
        best = [v for v, p in total_candidates if p == best_priority]
        result.grand_total = min(best)
        result.total = result.grand_total
        logger.info(f"[OCR-SELECTED] total={result.total} source=keyword priority={best_priority}")
    else:
        result.total = None
        logger.warning(f"[OCR-SELECTED] total=None source=no_keyword — semua angka: {[_extract_numbers(l) for l in lines if _extract_numbers(l)]}")

    if result.total and result.cash_paid and abs(result.total - result.cash_paid) < 1:
        logger.warning(f"[OCR-VALIDATE] total={result.total} == cash_paid, cari alternatif")
        for val, pri in sorted(total_candidates, key=lambda x: x[1]):
            if abs(val - result.cash_paid) > 1:
                result.total = val
                logger.info(f"[OCR-VALIDATE] alternatif={val}")
                break
        else:
            result.total = None

    if result.total and result.change and abs(result.total - result.change) < 1:
        logger.warning(f"[OCR-VALIDATE] total={result.total} == change, reset None")
        result.total = None

    score = 0.0
    if result.merchant: score += 0.3
    if result.tx_date: score += 0.3
    if result.total: score += 0.4
    result.confidence = round(score, 2)

    logger.info(f"[OCR-FINAL] merchant={result.merchant!r} date={result.tx_date} total={result.total} cash={result.cash_paid} change={result.change}")
    return result


async def process_receipt(bot, file_id: str) -> OcrResult:
    try:
        import os
        api_key = os.environ.get("OCR_SPACE_API_KEY", "helloworld")
        file = await bot.get_file(file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        logger.info(f"[OCR-REQUEST] size={len(image_bytes)} api={'custom' if api_key != 'helloworld' else 'demo'}")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.ocr.space/parse/image",
                data={"apikey": api_key, "language": "eng", "isOverlayRequired": "false",
                      "detectOrientation": "true", "scale": "true", "OCREngine": "2"},
                files={"file": ("struk.jpg", image_bytes, "image/jpeg")},
            )

        data = response.json()
        logger.info(f"[OCR-RESPONSE] status={response.status_code} error={data.get('IsErroredOnProcessing')}")

        if data.get("IsErroredOnProcessing"):
            logger.error(f"[OCR-ERROR] {data.get('ErrorMessage')}")
            return OcrResult(confidence=0.0)

        parsed_results = data.get("ParsedResults", [])
        raw_text = parsed_results[0].get("ParsedText", "") if parsed_results else ""
        logger.info(f"[OCR-RAW] len={len(raw_text)}\n{raw_text}")

        if not raw_text.strip():
            return OcrResult(confidence=0.0)

        return _parse_receipt_text(raw_text)

    except Exception as e:
        logger.exception(f"[OCR-EXCEPTION] {e}")
        return OcrResult(confidence=0.0)
