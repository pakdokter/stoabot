"""
OCR Handler — diperbaiki:
- Handle expired callback
- Handle foto dikirim saat state lain aktif
- State machine yang benar
- Edit nominal tidak stuck
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
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount
from bot.handlers.auth import ensure_registered

OCR_KONFIRMASI, OCR_EDIT_NOMINAL = range(2)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    # Reset state lama jika ada
    context.user_data.clear()
    await update.message.reply_text("🔍 Memproses struk...")

    try:
        photo = update.message.photo[-1]
        result: OcrResult = await process_receipt(update.get_bot(), photo.file_id)
        context.user_data["ocr_result"] = result
        context.user_data["ocr_file_id"] = photo.file_id
        return await _show_ocr_result(update, result)
    except Exception as e:
        logger.error(f"OCR handler error: {e}")
        await update.message.reply_text(
            "❌ Gagal membaca struk. Input manual dengan /keluar"
        )
        return ConversationHandler.END


async def _show_ocr_result(update: Update, result: OcrResult):
    lines = ["📄 *Hasil Baca Struk*\n"]
    lines.append(f"Toko: *{result.merchant or 'tidak terdeteksi'}*")
    lines.append(f"Tanggal: *{fmt_date(result.tx_date) if result.tx_date else 'hari ini'}*")

    if result.total:
        lines.append(f"Total: *{fmt_rupiah(result.total)}*")
    else:
        lines.append("Total: _belum terdeteksi_")

    lines.append("\nSimpan sebagai pengeluaran?")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit nominal", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
    )
    return OCR_KONFIRMASI


async def ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Handle expired callback
    try:
        await query.answer()
    except Exception:
        await query.message.reply_text(
            "⏰ Sesi habis. Kirim ulang foto struk."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Guard: pastikan ocr_result masih ada
    if "ocr_result" not in context.user_data:
        await query.edit_message_text("⏰ Sesi habis. Kirim ulang foto struk.")
        return ConversationHandler.END

    action = query.data.split(":")[1]

    if action == "tidak":
        await query.edit_message_text("❌ Dibatalkan.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "ya":
        return await _save(query, context, from_query=True)

    if action == "edit":
        result: OcrResult = context.user_data["ocr_result"]
        current = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        await query.edit_message_text(
            f"💰 Nominal saat ini: *{current}*\n\nMasukkan nominal yang benar:",
            parse_mode="Markdown",
        )
        return OCR_EDIT_NOMINAL


async def ocr_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard: pastikan masih dalam sesi yang valid
    if "ocr_result" not in context.user_data:
        await update.message.reply_text(
            "⏰ Sesi habis. Kirim ulang foto struk."
        )
        return ConversationHandler.END

    amount = parse_amount(update.message.text.strip())
    if not amount or amount <= 0:
        await update.message.reply_text(
            "❌ Nominal tidak valid.\nContoh: 45000, 45rb, 1.5jt"
        )
        return OCR_EDIT_NOMINAL

    context.user_data["ocr_result"].total = amount
    return await _save(update, context, from_query=False)


async def _save(update_or_query, context: ContextTypes.DEFAULT_TYPE, from_query: bool):
    result: OcrResult = context.user_data.get("ocr_result")
    if not result:
        return ConversationHandler.END

    file_id = context.user_data.get("ocr_file_id", "")
    amount = result.total or 0
    description = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    if amount <= 0:
        msg = "❌ Nominal tidak valid. Gunakan /keluar untuk input manual."
        if from_query:
            await update_or_query.edit_message_text(msg)
        else:
            await update_or_query.message.reply_text(msg)
        context.user_data.clear()
        return ConversationHandler.END

    user_id = (
        update_or_query.from_user.id
        if from_query
        else update_or_query.effective_user.id
    )

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

        if file_id:
            session.add(Attachment(
                transaction_id=tx.id,
                telegram_file_id=file_id,
                ocr_raw_text=result.raw_text[:2000] if result.raw_text else None,
                ocr_confidence=result.confidence,
            ))

        await log_create(session, user_id, tx)
        await session.commit()

    # Hitung saldo setelah commit
    async with AsyncSessionLocal() as session:
        saldo = await get_running_balance(session)

    msg = (
        f"✅ *Tersimpan*\n\n"
        f"Jenis: ➖ KELUAR\n"
        f"Nominal: *{fmt_rupiah(amount)}*\n"
        f"Keterangan: {description}\n"
        f"Tanggal: {fmt_date(tx_date)}\n\n"
        f"💰 Saldo: *{fmt_rupiah(saldo)}*"
    )

    if from_query:
        await update_or_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update_or_query.message.reply_text(msg, parse_mode="Markdown")

    context.user_data.clear()
    return ConversationHandler.END


def build_ocr_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, handle_photo)],
        states={
            OCR_KONFIRMASI: [CallbackQueryHandler(ocr_callback, pattern="^ocr:")],
            OCR_EDIT_NOMINAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_nominal),
                # Foto baru saat edit → reset dan proses ulang
                MessageHandler(filters.PHOTO, handle_photo),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
        conversation_timeout=300,
    )
