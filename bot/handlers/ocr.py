"""
OCR Handler — production-ready state machine.
"""
from datetime import date
from loguru import logger

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from bot.database import AsyncSessionLocal
from bot.models import Transaction, Attachment
from bot.services.ocr_service import process_receipt, OcrResult
from bot.services.balance import get_running_balance
from bot.services.audit import log_create
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount
from bot.handlers.auth import ensure_registered

OCR_KONFIRMASI = 0
OCR_EDIT_NOMINAL = 1


def _log(user_id: int, state: str, action: str, **kwargs):
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info(f"[OCR] uid={user_id} state={state} action={action} {extra}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    user_id = update.effective_user.id
    context.user_data.clear()
    _log(user_id, "IDLE", "photo_received")

    await update.message.reply_text("🔍 Memproses struk...")

    try:
        photo = update.message.photo[-1]
        _log(user_id, "OCR_PROCESSING", "calling_ocr", file_id=photo.file_id[:20])

        result: OcrResult = await process_receipt(update.get_bot(), photo.file_id)

        _log(user_id, "OCR_PROCESSING", "ocr_done",
             merchant=result.merchant, total=result.total,
             cash=result.cash_paid, change=result.change,
             confidence=result.confidence)

        context.user_data["ocr_result"] = result
        context.user_data["ocr_file_id"] = photo.file_id
        context.user_data["ocr_state"] = "WAITING_CONFIRMATION"

        return await _show_ocr_result(update, result)

    except Exception as e:
        logger.exception(f"[OCR] uid={user_id} OCR failed: {e}")
        await update.message.reply_text(
            "❌ Gagal membaca struk.\n\nInput manual:\n/keluar untuk mencatat pengeluaran"
        )
        context.user_data.clear()
        return ConversationHandler.END


async def _show_ocr_result(update: Update, result: OcrResult):
    lines = ["📄 *Hasil Baca Struk*\n"]
    lines.append(f"Toko: *{result.merchant or 'tidak terdeteksi'}*")

    if result.tx_date:
        lines.append(f"Tanggal: *{fmt_date(result.tx_date)}*")
    else:
        lines.append("Tanggal: _tidak terdeteksi_ (pakai hari ini)")

    if result.total:
        lines.append(f"Total Belanja: *{fmt_rupiah(result.total)}*")
        if result.cash_paid:
            lines.append(f"_Tunai: {fmt_rupiah(result.cash_paid)}_")
        if result.change:
            lines.append(f"_Kembali: {fmt_rupiah(result.change)}_")
    else:
        lines.append("Total: _belum terdeteksi_ — perlu edit manual")

    lines.append("\nSimpan sebagai pengeluaran?")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya, simpan", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit nominal", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard,
    )
    return OCR_KONFIRMASI


async def ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id

    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"[OCR] uid={user_id} query.answer() failed: {e}")
        try:
            await query.message.reply_text("⏰ Sesi habis. Kirim ulang foto struk.")
        except Exception:
            pass
        context.user_data.clear()
        return ConversationHandler.END

    _log(user_id, "WAITING_CONFIRMATION", "callback_received", data=query.data)

    result: OcrResult = context.user_data.get("ocr_result")
    if result is None:
        logger.warning(f"[OCR] uid={user_id} ocr_result missing from context")
        try:
            await query.edit_message_text("⏰ Sesi habis. Kirim ulang foto struk.")
        except Exception:
            pass
        context.user_data.clear()
        return ConversationHandler.END

    action = query.data.split(":")[1]

    if action == "tidak":
        _log(user_id, "WAITING_CONFIRMATION", "cancelled")
        try:
            await query.edit_message_text("❌ Transaksi dibatalkan.")
        except Exception:
            pass
        context.user_data.clear()
        return ConversationHandler.END

    if action == "ya":
        _log(user_id, "WAITING_CONFIRMATION", "confirmed", total=result.total)
        return await _do_save(query, context, user_id, from_query=True)

    if action == "edit":
        current = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        _log(user_id, "WAITING_CONFIRMATION", "edit_requested", current=current)
        try:
            await query.edit_message_text(
                f"✏️ Nominal saat ini: *{current}*\n\n"
                f"Masukkan nominal yang benar:\n"
                f"_(Contoh: 45000, 45rb, 1.5jt)_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"[OCR] uid={user_id} edit_message_text failed: {e}")
            await query.message.reply_text("✏️ Masukkan nominal baru:", parse_mode="Markdown")
        context.user_data["ocr_state"] = "EDIT_AMOUNT"
        return OCR_EDIT_NOMINAL

    logger.warning(f"[OCR] uid={user_id} unknown action: {action}")
    return OCR_KONFIRMASI


async def ocr_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    _log(user_id, "EDIT_AMOUNT", "input_received", text=text)

    result: OcrResult = context.user_data.get("ocr_result")
    if result is None:
        logger.warning(f"[OCR] uid={user_id} ocr_result missing in EDIT_AMOUNT")
        await update.message.reply_text("⏰ Sesi habis. Kirim ulang foto struk.")
        context.user_data.clear()
        return ConversationHandler.END

    amount = parse_amount(text)
    if not amount or amount <= 0:
        _log(user_id, "EDIT_AMOUNT", "invalid_amount", text=text)
        await update.message.reply_text(
            "❌ Nominal tidak valid.\n\nContoh:\n• `45000`\n• `45rb`\n• `1.5jt`",
            parse_mode="Markdown",
        )
        return OCR_EDIT_NOMINAL

    result.total = amount
    context.user_data["ocr_result"] = result
    _log(user_id, "EDIT_AMOUNT", "amount_updated", amount=amount)

    merchant = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit lagi", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])

    await update.message.reply_text(
        f"✅ Nominal diperbarui.\n\n"
        f"Toko: *{merchant}*\n"
        f"Tanggal: *{fmt_date(tx_date)}*\n"
        f"Total Belanja: *{fmt_rupiah(amount)}*\n\n"
        f"Simpan?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    context.user_data["ocr_state"] = "WAITING_CONFIRMATION"
    return OCR_KONFIRMASI


async def _do_save(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                   user_id: int, from_query: bool):
    result: OcrResult = context.user_data.get("ocr_result")
    file_id: str = context.user_data.get("ocr_file_id", "")

    if not result:
        logger.error(f"[OCR] uid={user_id} _do_save called with no ocr_result")
        return ConversationHandler.END

    amount = result.total or 0
    description = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    _log(user_id, "CONFIRM_SAVE", "saving", amount=amount, date=tx_date)

    if amount <= 0:
        msg = "❌ Nominal belum diset. Gunakan ✏️ Edit nominal."
        try:
            if from_query:
                await update_or_query.edit_message_text(msg)
            else:
                await update_or_query.message.reply_text(msg)
        except Exception as e:
            logger.error(f"[OCR] uid={user_id} reply failed: {e}")
        context.user_data.clear()
        return ConversationHandler.END

    try:
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
            tx_id = tx.id

            if file_id:
                session.add(Attachment(
                    transaction_id=tx_id,
                    telegram_file_id=file_id,
                    ocr_raw_text=result.raw_text[:2000] if result.raw_text else None,
                    ocr_confidence=result.confidence,
                ))

            await log_create(session, user_id, tx)
            await session.commit()
            _log(user_id, "CONFIRM_SAVE", "db_saved", tx_id=str(tx_id))

        # Session BARU untuk hitung saldo — hindari stale read
        async with AsyncSessionLocal() as session2:
            saldo = await get_running_balance(session2)

        _log(user_id, "COMPLETED", "save_success", saldo=saldo)

        msg = (
            f"✅ *Transaksi berhasil disimpan*\n\n"
            f"Jenis: ➖ KELUAR\n"
            f"Nominal: *{fmt_rupiah(amount)}*\n"
            f"Keterangan: {description}\n"
            f"Tanggal: {fmt_date(tx_date)}\n\n"
            f"💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*"
        )

        try:
            if from_query:
                await update_or_query.edit_message_text(msg, parse_mode="Markdown")
            else:
                await update_or_query.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[OCR] uid={user_id} reply after save failed: {e}")
            try:
                if from_query:
                    await update_or_query.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                pass

    except Exception as e:
        logger.exception(f"[OCR] uid={user_id} database save failed: {e}")
        error_msg = "❌ Gagal menyimpan. Coba lagi atau gunakan /keluar"
        try:
            if from_query:
                await update_or_query.edit_message_text(error_msg)
            else:
                await update_or_query.message.reply_text(error_msg)
        except Exception:
            pass

    context.user_data.clear()
    return ConversationHandler.END


def build_ocr_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, handle_photo)],
        states={
            OCR_KONFIRMASI: [
                CallbackQueryHandler(ocr_callback, pattern="^ocr:"),
                MessageHandler(filters.PHOTO, handle_photo),
            ],
            OCR_EDIT_NOMINAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_nominal),
                MessageHandler(filters.PHOTO, handle_photo),
                CallbackQueryHandler(ocr_callback, pattern="^ocr:"),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
        conversation_timeout=600,
    )
