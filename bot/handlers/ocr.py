"""
OCR Handler — user kirim foto struk, bot ekstrak & konfirmasi.
"""
from datetime import date

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, MessageHandler,
    CallbackQueryHandler, filters,
)
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import Transaction, Attachment
from bot.services.ocr_service import process_receipt, OcrResult
from bot.services.balance import get_running_balance
from bot.services.audit import log_create
from bot.utils.formatters import fmt_rupiah, fmt_date
from bot.handlers.auth import ensure_registered

# States
OCR_KONFIRMASI, OCR_EDIT_NOMINAL, OCR_EDIT_MERCHANT = range(3)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler utama saat user kirim foto."""
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    await update.message.reply_text("🔍 Memproses struk...")

    try:
        # Ambil foto resolusi tertinggi
        photo = update.message.photo[-1]
        result: OcrResult = await process_receipt(update.get_bot(), photo.file_id)

        context.user_data["ocr_result"] = result
        context.user_data["ocr_file_id"] = photo.file_id

        return await _show_ocr_result(update, result)

    except Exception as e:
        logger.error(f"OCR handler error: {e}")
        await update.message.reply_text(
            "❌ Gagal membaca struk. Silakan input manual dengan /keluar"
        )
        return ConversationHandler.END


async def _show_ocr_result(update: Update, result: OcrResult):
    confidence_emoji = "✅" if result.confidence >= 0.7 else "⚠️" if result.confidence >= 0.4 else "❌"

    lines = [f"📄 *Hasil Baca Struk* {confidence_emoji}\n"]

    if result.merchant:
        lines.append(f"Toko: *{result.merchant}*")
    else:
        lines.append("Toko: _tidak terdeteksi_")

    if result.tx_date:
        lines.append(f"Tanggal: *{fmt_date(result.tx_date)}*")
    else:
        lines.append("Tanggal: _tidak terdeteksi_ (akan pakai hari ini)")

    if result.total:
        lines.append(f"Total: *{fmt_rupiah(result.total)}*")
    else:
        lines.append("Total: _tidak terdeteksi_")

    lines.append(f"\nKepercayaan: {int(result.confidence * 100)}%")
    lines.append("\nSimpan sebagai pengeluaran?")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ya", callback_data="ocr:ya"),
            InlineKeyboardButton("✏️ Edit", callback_data="ocr:edit"),
            InlineKeyboardButton("❌ Tidak", callback_data="ocr:tidak"),
        ]
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return OCR_KONFIRMASI


async def ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "tidak":
        await query.edit_message_text("❌ Transaksi dibatalkan.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "ya":
        return await _save_ocr_transaction(query, context)

    if action == "edit":
        result: OcrResult = context.user_data["ocr_result"]
        current_nominal = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        await query.edit_message_text(
            f"💰 Nominal saat ini: *{current_nominal}*\n\nMasukkan nominal yang benar:",
            parse_mode="Markdown",
        )
        return OCR_EDIT_NOMINAL


async def ocr_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.utils.formatters import parse_amount
    amount = parse_amount(update.message.text)
    if not amount:
        await update.message.reply_text("❌ Nominal tidak valid.")
        return OCR_EDIT_NOMINAL

    result: OcrResult = context.user_data["ocr_result"]
    result.total = amount
    context.user_data["ocr_result"] = result

    await update.message.reply_text(
        f"Keterangan/nama merchant? (kosongkan untuk '{result.merchant or 'Belanja'}')"
    )
    return OCR_EDIT_MERCHANT


async def ocr_edit_merchant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    result: OcrResult = context.user_data["ocr_result"]
    if text:
        result.merchant = text
        context.user_data["ocr_result"] = result

    return await _save_ocr_transaction(update, context)


async def _save_ocr_transaction(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    """Simpan transaksi dari hasil OCR."""
    result: OcrResult = context.user_data["ocr_result"]
    file_id: str = context.user_data.get("ocr_file_id", "")

    # Fallback values
    amount = result.total or 0
    description = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    if amount <= 0:
        msg = "❌ Nominal tidak valid. Gunakan /keluar untuk input manual."
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        context.user_data.clear()
        return ConversationHandler.END

    # Dapatkan user_id
    if hasattr(update_or_query, "from_user"):
        user_id = update_or_query.from_user.id
    else:
        user_id = update_or_query.message.from_user.id

    async with AsyncSessionLocal() as session:
        tx = Transaction(
            user_id=user_id,
            type="keluar",
            amount=amount,
            description=description,
            transaction_date=tx_date,
        )
        session.add(tx)
        await session.flush()

        # Simpan attachment
        if file_id:
            attachment = Attachment(
                transaction_id=tx.id,
                telegram_file_id=file_id,
                ocr_raw_text=result.raw_text[:2000] if result.raw_text else None,
                ocr_confidence=result.confidence,
            )
            session.add(attachment)

        await log_create(session, user_id, tx)
        await session.commit()

        saldo = await get_running_balance(session)

    success_msg = (
        f"✅ *Transaksi berhasil disimpan*\n\n"
        f"Jenis: ➖ KELUAR\n"
        f"Nominal: *{fmt_rupiah(amount)}*\n"
        f"Keterangan: {description}\n"
        f"Tanggal: {fmt_date(tx_date)}\n\n"
        f"💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*"
    )

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(success_msg, parse_mode="Markdown")
    else:
        await update_or_query.message.reply_text(success_msg, parse_mode="Markdown")

    context.user_data.clear()
    return ConversationHandler.END


def build_ocr_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, handle_photo)],
        states={
            OCR_KONFIRMASI: [CallbackQueryHandler(ocr_callback, pattern="^ocr:")],
            OCR_EDIT_NOMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_nominal)],
            OCR_EDIT_MERCHANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_merchant)],
        },
        fallbacks=[],
        conversation_timeout=300,
    )
