"""
OCR Service — parser dengan item extraction.
Mendukung dua format OCR:
  Format A: nama item di baris terpisah dari qty/harga
  Format B: nama item + qty/harga dalam satu baris
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
    unit: str = ""
    unit_price: float = 0.0
    line_total: float = 0.0


@dataclass
class OcrResult:
    merchant: Optional[str] = None
    total: Optional[float] = None
    grand_total: Optional[float] = None
    cash_paid: Optional[float] = None
    change: Optional[float] = None
    discount: Optional[float] = None
    tx_date: Optional[date] = None
    items: list = field(default_factory=list)
    raw_text: str = ""
    confidence: float = 0.0
    provider: str = "ocrspace"


TOTAL_KEYWORDS = [
    r'\btotal\b', r'\bgrand\s*total\b', r'\bjumlah\b',
    r'\btagihan\b', r'\bsubtotal\b', r'\bsub\s*total\b',
    r'\bnetto\b', r'\bnet\b',
    r'\brotal\b', r'\bt0tal\b', r'\bt\*tal\b', r'\btoial\b',
]
PAYMENT_KEYWORDS = [
    r'\btunai\b', r'\bcash\b', r'\bbayar\b', r'\bdibayar\b',
    r'\btransfer\b', r'\bdebit\b', r'\bkredit\b', r'\bkartu\b',
    r'\bqris\b', r'\bova\b', r'\bgopay\b', r'\bshopee\b',
    r'\bdana\b', r'\blinkaja\b',
    r'\brunai\b', r'\btunal\b', r'\btumai\b', r'\bbayaf\b',
]
CHANGE_KEYWORDS = [
    r'\bkembali\b', r'\bkembalian\b', r'\bchange\b', r'\bselisih\b',
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

UNIT_WORDS = {
    'pcs', 'pc', 'psc', 'unit', 'buah', 'bh', 'biji',
    'kg', 'gr', 'gram', 'ltr', 'liter', 'ml',
    'slop', 'pack', 'pak', 'pck', 'box', 'dus', 'karton',
    'lusin', 'rim', 'roll', 'lembar', 'lbr', 'meter', 'mtr',
    'botol', 'btl', 'kaleng', 'klg', 'sachet', 'scht',
    'porsi', 'gelas', 'cup', 'mangkok', 'piring',
}

NON_ITEM_WORDS = {
    'total', 'subtotal', 'grand', 'tunai', 'cash', 'bayar',
    'kembali', 'change', 'diskon', 'discount', 'pajak', 'tax',
    'ppn', 'service', 'kasir', 'operator', 'struk', 'nota',
    'invoice', 'receipt', 'terima', 'terimakasih', 'tanggal',
    'date', 'time', 'waktu', 'no', 'nomor', 'number',
    'jl', 'jln', 'jalan', 'gg', 'gang', 'rt', 'rw', 'kel', 'kec',
    'area', 'pertokoan', 'mall', 'ruko', 'rukan', 'komplek',
    'selong', 'lombok', 'mataram', 'timur', 'barat', 'utara', 'selatan',
}


def _matches_any(text: str, patterns: list) -> bool:
    return any(re.search(p, text.lower()) for p in patterns)


def _extract_money(text: str) -> list:
    text_clean = re.sub(r'[-=]+$', '', text.strip())
    results = []
    for m in re.finditer(r'\d{1,3}(?:[.,]\d{3})+|\d+', text_clean):
        raw = m.group(0).replace('.', '').replace(',', '')
        if raw.isdigit():
            val = float(raw)
            if val >= 100:
                results.append((val, m.start()))
    return results


def _extract_qty(text: str) -> Optional[float]:
    m = re.match(r'^\s*(\d{1,3})\s', text)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 999:
            return float(val)
    return None


def _extract_unit(text: str) -> Optional[str]:
    words = re.findall(r'[a-zA-Z]+', text.lower())
    for w in words:
        if w in UNIT_WORDS:
            return w.upper()
    return None


def _is_item_name_line(line: str) -> bool:
    if not re.search(r'[a-zA-Z]', line):
        return False
    if len(line.strip()) < 2:
        return False
    if _matches_any(line, SKIP_LINE_PATTERNS):
        return False
    if _matches_any(line, TOTAL_KEYWORDS):
        return False
    if _matches_any(line, PAYMENT_KEYWORDS):
        return False
    if _matches_any(line, CHANGE_KEYWORDS):
        return False
    if _matches_any(line, DISCOUNT_KEYWORDS):
        return False
    if _matches_any(line, TAX_KEYWORDS):
        return False
    first_word = re.findall(r'[a-zA-Z]+', line.lower())
    if first_word and first_word[0] in NON_ITEM_WORDS:
        return False
    pure_price_pattern = r'^\s*\d{1,3}\s+[a-zA-Z]+\s+[\d.,]+\s+[\d.,]+\s*$'
    if re.match(pure_price_pattern, line):
        return False
    return True


def _parse_item_line_format_b(line: str) -> Optional[ReceiptItem]:
    money_vals = _extract_money(line)
    if len(money_vals) < 1:
        return None
    if len(money_vals) >= 2:
        line_total = money_vals[-1][0]
        unit_price = money_vals[-2][0]
    else:
        line_total = money_vals[-1][0]
        unit_price = line_total
    name_end = money_vals[0][1] if money_vals else len(line)
    name_part = line[:name_end].strip()
    unit = _extract_unit(name_part)
    if unit:
        name_part = re.sub(r'\b' + unit.lower() + r'\b', '', name_part, flags=re.IGNORECASE).strip()
    qty_match = re.search(r'\b(\d{1,3})\b', name_part)
    qty = 1.0
    if qty_match:
        qty_val = int(qty_match.group(1))
        if 1 <= qty_val <= 999:
            qty = float(qty_val)
            name_part = name_part[:qty_match.start()] + name_part[qty_match.end():]
    name = name_part.strip().rstrip('-').strip()
    if len(name) < 2:
        return None
    return ReceiptItem(name=name.upper(), qty=qty, unit=unit or "", unit_price=unit_price, line_total=line_total)


def _parse_items_format_a(lines: list) -> list:
    header_words = {'telp', 'fax', 'no.', 'no:', 'kasir', 'area', 'jl.',
                    'jln', 'pel.', 'pelanggan', 'tanggal', 'date', 'struk',
                    'terima', 'jangan', 'lupa', 'powered', 'copyright',
                    'nota', 'invoice', 'receipt', 'selong', 'lombok',
                    'pertokoan', 'mall', 'ruko', 'rukan',
                    'wa', 'instagram', 'facebook', 'twitter', 'tgl',
                    'user', 'sc', 'pel', 'instagram'}
    items = []
    i = 0
    while i < len(lines):
        line = lines[i]
        money_in_line = _extract_money(line)
        has_alpha = bool(re.search(r'[a-zA-Z]', line))
        is_product_code_only = all(v < 10000 for v, _ in money_in_line)
        if has_alpha and _is_item_name_line(line) and (len(money_in_line) == 0 or (len(money_in_line) <= 2 and is_product_code_only)):
            words_in_line = set(re.findall(r'[a-zA-Z]+', line.lower()))
            if words_in_line & header_words:
                i += 1
                continue
            if re.match(r'^[A-Z]{2,4}[0-9-]+', line.strip()):
                i += 1
                continue
            if re.search(r'\bsc\s*:', line.lower()) or re.search(r'\bno\s*:', line.lower()):
                i += 1
                continue
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                next_money = _extract_money(next_line)
                next_qty = _extract_qty(next_line)
                if len(next_money) >= 1 and next_qty is not None:
                    name = line.strip()
                    qty = next_qty
                    unit = _extract_unit(next_line)
                    if len(next_money) >= 2:
                        unit_price = next_money[-2][0]
                        line_total = next_money[-1][0]
                    else:
                        unit_price = next_money[-1][0]
                        line_total = next_money[-1][0]
                    if len(name) >= 2:
                        items.append(ReceiptItem(name=name.upper(), qty=qty, unit=unit or "", unit_price=unit_price, line_total=line_total))
                    i += 2
                    continue
        i += 1
    return items


def _parse_items_format_b(lines: list) -> list:
    header_words_b = {'jl', 'jln', 'jalan', 'gg', 'area', 'pertokoan',
                      'mall', 'ruko', 'komplek', 'selong', 'lombok',
                      'mataram', 'telp', 'fax', 'kasir',
                      'wa', 'instagram', 'facebook', 'tgl', 'user',
                      'sc', 'pel', 'no', 'sub'}
    items = []
    for line in lines:
        if not _is_item_name_line(line):
            continue
        words_in_line = set(re.findall(r'[a-zA-Z]+', line.lower()))
        if words_in_line & header_words_b:
            continue
        if re.match(r'^[A-Z]{2,4}[0-9-]', line.strip()):
            continue
        money_vals = _extract_money(line)
        if len(money_vals) < 1:
            continue
        if not any(v >= 1000 for v, _ in money_vals):
            continue
        item = _parse_item_line_format_b(line)
        if item and len(item.name) >= 2:
            items.append(item)
    return items


def _classify_line(line: str) -> str:
    if _matches_any(line, SKIP_LINE_PATTERNS): return 'skip'
    if _matches_any(line, CHANGE_KEYWORDS): return 'change'
    if _matches_any(line, PAYMENT_KEYWORDS): return 'payment'
    if _matches_any(line, TOTAL_KEYWORDS): return 'total'
    if _matches_any(line, DISCOUNT_KEYWORDS): return 'discount'
    if _matches_any(line, TAX_KEYWORDS): return 'tax'
    return 'unknown'


def _join_fragmented_lines(lines: list) -> list:
    if not lines:
        return lines
    result = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        is_operator = line in ['=', ':', '-', '+']
        is_pure_number = bool(re.match(r'^[\d.,]+$', line)) and len(line) <= 10
        is_short_unit = line.upper() in {'SLOP', 'PACK', 'PAK', 'PCS', 'BTL', 'KLG', 'PAKX'}
        if (is_operator or is_pure_number or is_short_unit) and result:
            result[-1] = result[-1] + '  ' + line
        else:
            result.append(line)
        i += 1
    result2 = []
    i = 0
    while i < len(result):
        line = result[i]
        has_financial_kw = any(re.search(p, line.lower()) for p in [
            r'\btotal\b', r'\btunai\b', r'\bbayar\b', r'\bkembali\b',
            r'\bdiskon\b', r'\bsubtotal\b', r'\brotal\b', r'\brunai\b',
        ])
        numbers_in_line = bool(re.search(r'\d{3,}', line))
        if has_financial_kw and not numbers_in_line and i + 1 < len(result):
            next_line = result[i + 1].strip()
            next_is_number = bool(re.match(r'^[=\s]*[\d.,]+\s*$', next_line))
            if next_is_number:
                result2.append(line + '  ' + next_line)
                i += 2
                continue
        result2.append(line)
        i += 1
    return result2


def _normalize_ocr(text: str) -> str:
    import unicodedata
    normalized = unicodedata.normalize('NFKD', text)
    result = normalized.encode('ascii', 'ignore').decode('ascii')
    result = result.replace("'i", '').replace("'", '')
    return result


def _parse_receipt_text(text: str) -> OcrResult:
    text = _normalize_ocr(text)
    result = OcrResult(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = _join_fragmented_lines(lines)

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
    discount_values = []

    for line in lines:
        money_vals = _extract_money(line)
        if not money_vals:
            continue
        cls = _classify_line(line)
        nums = [v for v, _ in money_vals]
        if cls == 'change':
            change_values.extend(nums)
        elif cls == 'payment':
            payment_values.extend(nums)
        elif cls == 'total':
            priority = 0 if re.search(r'\bgrand\b', line.lower()) else \
                       2 if re.search(r'\bsub\b', line.lower()) else 1
            for n in nums:
                total_candidates.append((n, priority))
        elif cls == 'discount':
            discount_values.extend(nums)

    if change_values: result.change = change_values[0]
    if payment_values: result.cash_paid = payment_values[0]
    if discount_values: result.discount = discount_values[0]

    if total_candidates:
        total_candidates.sort(key=lambda x: x[1])
        best_p = total_candidates[0][1]
        best = [v for v, p in total_candidates if p == best_p]
        result.grand_total = min(best)
        result.total = result.grand_total

    if result.total and result.change and abs(result.total - result.change) < 1:
        for val, pri in sorted(total_candidates, key=lambda x: x[1]):
            if abs(val - result.change) > 1:
                result.total = val
                break
        else:
            result.total = None

    items_a = _parse_items_format_a(lines)
    items_b = _parse_items_format_b(lines)
    items = items_a if len(items_a) >= len(items_b) else items_b

    if items and result.total:
        item_sum = sum(i.line_total for i in items)
        result.items = items
        if abs(item_sum - result.total) >= result.total * 0.1:
            logger.warning(f"[OCR] item sum={item_sum} != total={result.total}")
    else:
        result.items = items

    logger.info(f"[OCR] items extracted: {len(result.items)}")
    for item in result.items:
        logger.info(f"  - {item.name} qty={item.qty} price={item.unit_price} total={item.line_total}")

    score = 0.0
    if result.merchant: score += 0.2
    if result.tx_date: score += 0.2
    if result.total: score += 0.3
    if result.items: score += 0.3
    result.confidence = round(score, 2)

    logger.info(
        f"[OCR-FINAL] merchant={result.merchant!r} date={result.tx_date} "
        f"total={result.total} items={len(result.items)} confidence={result.confidence}"
    )
    return result


async def process_receipt(bot, file_id: str) -> OcrResult:
    try:
        import os
        api_key = os.environ.get("OCR_SPACE_API_KEY", "helloworld")
        file = await bot.get_file(file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        logger.info(f"[OCR] size={len(image_bytes)} api={'custom' if api_key != 'helloworld' else 'demo'}")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.ocr.space/parse/image",
                data={"apikey": api_key, "language": "eng",
                      "isOverlayRequired": "false", "detectOrientation": "true",
                      "scale": "true", "isTable": "true",
                      "OCREngine": "2"},
                files={"file": ("struk.jpg", image_bytes, "image/jpeg")},
            )

        data = response.json()
        if data.get("IsErroredOnProcessing"):
            logger.error(f"[OCR] error: {data.get('ErrorMessage')}")
            return OcrResult(confidence=0.0)

        parsed_results = data.get("ParsedResults", [])
        raw_text = parsed_results[0].get("ParsedText", "") if parsed_results else ""
        logger.info(f"[OCR-RAW] len={len(raw_text)}\n{raw_text}")

        if not raw_text.strip():
            return OcrResult(confidence=0.0)

        return _parse_receipt_text(raw_text)

    except Exception as e:
        logger.exception(f"[OCR] failed: {e}")
        return OcrResult(confidence=0.0)
      
