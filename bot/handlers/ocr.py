"""
OCR Handler — user kirim foto struk, bot ekstrak & konfirmasi.
"""
from datetime import date
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, CallbackQueryHandler, filters
from loguru import logger
from bot.database import AsyncSessionLocal
from bot.models import Transaction, Attachment
from bot.services.ocr_service import process_receipt, OcrResult
from bot.services.balance import get_running_balance
from bot.services.audit import log_create
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount
from bot.handlers.auth import ensure_registered

OCR_KONFIRMASI, OCR_EDIT_NOMINAL = range(2)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    await update.message.reply_text("🔍 Memproses struk...")
    try:
        photo = update.message.photo[-1]
        result = await process_receipt(update.get_bot(), photo.file_id)
        context.user_data["ocr_result"] = result
        context.user_data["ocr_file_id"] = photo.file_id
        return await _show_ocr_result(update, result)
    except Exception as e:
        logger.error(f"OCR error: {e}")
        await update.message.reply_text("❌ Gagal membaca struk. Input manual: /keluar")
        return ConversationHandler.END

async def _show_ocr_result(update, result):
    lines = ["📄 *Hasil Baca Struk*\n"]
    lines.append(f"Toko: *{result.merchant or 'tidak terdeteksi'}*")
    lines.append(f"Tanggal: *{fmt_date(result.tx_date) if result.tx_date else 'hari ini'}*")
    lines.append(f"Total: *{fmt_rupiah(result.total) if result.total else 'belum terdeteksi'}*")
    lines.append("\nSimpan sebagai pengeluaran?")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit nominal", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    return OCR_KONFIRMASI

async def ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "tidak":
        await query.edit_message_text("❌ Dibatalkan.")
        context.user_data.clear()
        return ConversationHandler.END
    if action == "ya":
        return await _save(query, context)
    if action == "edit":
        result = context.user_data["ocr_result"]
        current = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        await query.edit_message_text(f"💰 Nominal saat ini: *{current}*\n\nMasukkan nominal:", parse_mode="Markdown")
        return OCR_EDIT_NOMINAL

async def ocr_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)
    if not amount:
        await update.message.reply_text("❌ Tidak valid. Contoh: 45000")
        return OCR_EDIT_NOMINAL
    context.user_data["ocr_result"].total = amount
    return await _save(update, context)

async def _save(update_or_query, context):
    result = context.user_data["ocr_result"]
    file_id = context.user_data.get("ocr_file_id", "")
    amount = result.total or 0
    description = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()
    if amount <= 0:
        msg = "❌ Nominal tidak valid. Gunakan /keluar."
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        context.user_data.clear()
        return ConversationHandler.END
    user_id = update_or_query.from_user.id if hasattr(update_or_query, "from_user") else update_or_query.message.from_user.id
    async with AsyncSessionLocal() as session:
        tx = Transaction(user_id=user_id, type="keluar", amount=amount, description=description, transaction_date=tx_date)
        session.add(tx)
        await session.flush()
        if file_id:
            session.add(Attachment(transaction_id=tx.id, telegram_file_id=file_id, ocr_raw_text=result.raw_text[:2000] if result.raw_text else None, ocr_confidence=result.confidence))
        await log_create(session, user_id, tx)
        await session.commit()
        saldo = await get_running_balance(session)
    msg = f"✅ *Tersimpan*\n\nJenis: ➖ KELUAR\nNominal: *{fmt_rupiah(amount)}*\nKeterangan: {description}\nTanggal: {fmt_date(tx_date)}\n\n💰 Saldo: *{fmt_rupiah(saldo)}*"
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update_or_query.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

def build_ocr_conv():
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, handle_photo)],
        states={
            OCR_KONFIRMASI: [CallbackQueryHandler(ocr_callback, pattern="^ocr:")],
            OCR_EDIT_NOMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_nominal)],
        },
        fallbacks=[],
        conversation_timeout=300,
    )
