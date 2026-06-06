"""
OCR Handler — fixed state numbers (50, 51) agar tidak overlap dengan transaction.py
"""
import traceback
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

OCR_KONFIRMASI = 50
OCR_EDIT_NOMINAL = 51


def _log(user_id, state, action, **kwargs):
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info(f"[OCR] uid={user_id} state={state} action={action} {extra}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    before_keys = list(context.user_data.keys())
    context.user_data.clear()
    logger.info(f"[OCR] uid={user_id} photo_received session_before={before_keys}")
    await update.message.reply_text("🔍 Memproses struk...")
    try:
        photo = update.message.photo[-1]
        result = await process_receipt(update.get_bot(), photo.file_id)
        logger.info(f"[OCR] uid={user_id} ocr_done merchant={result.merchant!r} total={result.total} cash={result.cash_paid} change={result.change}")
        context.user_data["ocr_result"] = result
        context.user_data["ocr_file_id"] = photo.file_id
        return await _show_ocr_result(update, result)
    except Exception as e:
        logger.exception(f"[OCR] uid={user_id} handle_photo failed: {e}")
        await update.message.reply_text("❌ Gagal membaca struk.\nInput manual: /keluar")
        context.user_data.clear()
        return ConversationHandler.END


async def _show_ocr_result(update, result):
    lines = ["📄 *Hasil Baca Struk*\n"]
    lines.append(f"Toko: *{result.merchant or 'tidak terdeteksi'}*")
    lines.append(f"Tanggal: *{fmt_date(result.tx_date)}*" if result.tx_date else "Tanggal: _tidak terdeteksi_ (pakai hari ini)")
    if result.total:
        lines.append(f"Total Belanja: *{fmt_rupiah(result.total)}*")
        if result.cash_paid: lines.append(f"_Tunai: {fmt_rupiah(result.cash_paid)}_")
        if result.change: lines.append(f"_Kembali: {fmt_rupiah(result.change)}_")
    else:
        lines.append("Total: _belum terdeteksi_ — klik Edit Nominal")
    lines.append("\nSimpan sebagai pengeluaran?")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya, simpan", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit nominal", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    return OCR_KONFIRMASI


async def ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"[OCR] uid={user_id} query.answer() failed: {e}")
        try: await query.message.reply_text("⏰ Sesi habis. Kirim ulang foto struk.")
        except Exception: pass
        context.user_data.clear()
        return ConversationHandler.END

    result = context.user_data.get("ocr_result")
    logger.info(f"[OCR] uid={user_id} callback data={query.data!r} result_exists={result is not None} total={result.total if result else 'N/A'} session={list(context.user_data.keys())}")

    if result is None:
        logger.warning(f"[OCR] uid={user_id} ocr_result missing")
        try: await query.edit_message_text("⏰ Sesi habis. Kirim ulang foto struk.")
        except Exception: pass
        context.user_data.clear()
        return ConversationHandler.END

    action = query.data.split(":")[1]

    if action == "tidak":
        try: await query.edit_message_text("❌ Dibatalkan.")
        except Exception: pass
        context.user_data.clear()
        return ConversationHandler.END

    if action == "ya":
        logger.info(f"[OCR] uid={user_id} confirmed total={result.total}")
        return await _do_save(query, context, user_id, from_query=True)

    if action == "edit":
        current = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        logger.info(f"[OCR] uid={user_id} edit requested current={current}")
        try:
            await query.edit_message_text(
                f"✏️ Nominal saat ini: *{current}*\n\nMasukkan nominal yang benar:\n_(Contoh: 45000, 45rb, 1.5jt)_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"[OCR] uid={user_id} edit_message_text failed: {e}")
            try: await query.message.reply_text("✏️ Masukkan nominal baru:\n_(Contoh: 45000)_", parse_mode="Markdown")
            except Exception: pass
        return OCR_EDIT_NOMINAL

    return OCR_KONFIRMASI


async def ocr_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    logger.info(
        f"=== EDIT_AMOUNT RECEIVED ===\n"
        f"User Input: {text!r}\n"
        f"Session Keys: {list(context.user_data.keys())}\n"
        f"ocr_result exists: {'ocr_result' in context.user_data}\n"
        f"==========================="
    )

    result = context.user_data.get("ocr_result")
    if result is None:
        logger.warning(f"[OCR] uid={user_id} ocr_result missing in EDIT_AMOUNT")
        await update.message.reply_text("⏰ Sesi habis. Kirim ulang foto struk.")
        context.user_data.clear()
        return ConversationHandler.END

    amount = parse_amount(text)
    logger.info(f"[OCR] uid={user_id} parse_amount({text!r}) = {amount}")

    if not amount or amount <= 0:
        await update.message.reply_text(
            "❌ Nominal tidak valid.\n\nContoh:\n• `45000`\n• `45rb`\n• `1.5jt`",
            parse_mode="Markdown",
        )
        return OCR_EDIT_NOMINAL

    result.total = amount
    context.user_data["ocr_result"] = result
    merchant = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    logger.info(
        f"=== BEFORE SAVE PREVIEW ===\n"
        f"amount={amount} merchant={merchant!r} tx_date={tx_date}\n"
        f"==========================="
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit lagi", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])
    await update.message.reply_text(
        f"✅ Nominal diperbarui.\n\nToko: *{merchant}*\nTanggal: *{fmt_date(tx_date)}*\nTotal Belanja: *{fmt_rupiah(amount)}*\n\nSimpan?",
        parse_mode="Markdown", reply_markup=keyboard,
    )
    return OCR_KONFIRMASI


async def _do_save(update_or_query, context, user_id, from_query):
    result = context.user_data.get("ocr_result")
    file_id = context.user_data.get("ocr_file_id", "")

    if not result:
        logger.error(f"[OCR] uid={user_id} _do_save: ocr_result is None")
        return ConversationHandler.END

    amount = result.total or 0
    description = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    logger.info(
        f"=== BEFORE SAVE ===\n"
        f"uid={user_id} amount={amount} description={description!r}\n"
        f"tx_date={tx_date} from_query={from_query}\n"
        f"==================="
    )

    if amount <= 0:
        msg = "❌ Nominal belum diset. Gunakan ✏️ Edit nominal."
        logger.warning(f"[OCR] uid={user_id} amount<=0")
        try:
            if from_query: await update_or_query.edit_message_text(msg)
            else: await update_or_query.message.reply_text(msg)
        except Exception as e:
            logger.error(f"[OCR] reply failed: {e}")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        logger.info(f"[OCR] uid={user_id} opening db session")
        async with AsyncSessionLocal() as session:
            tx = Transaction(user_id=user_id, type="keluar", amount=amount,
                           description=description, transaction_date=tx_date)
            session.add(tx)
            await session.flush()
            tx_id = tx.id
            logger.info(f"[OCR] uid={user_id} flushed tx_id={tx_id}")
            if file_id:
                session.add(Attachment(
                    transaction_id=tx_id, telegram_file_id=file_id,
                    ocr_raw_text=result.raw_text[:2000] if result.raw_text else None,
                    ocr_confidence=result.confidence,
                ))
            await log_create(session, user_id, tx)
            await session.commit()
            logger.info(f"[OCR] uid={user_id} committed")

        async with AsyncSessionLocal() as session2:
            saldo = await get_running_balance(session2)

        logger.info(f"[OCR] uid={user_id} save_success saldo={saldo}")
        msg = (
            f"✅ *Transaksi berhasil disimpan*\n\n"
            f"Jenis: ➖ KELUAR\nNominal: *{fmt_rupiah(amount)}*\n"
            f"Keterangan: {description}\nTanggal: {fmt_date(tx_date)}\n\n"
            f"💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*"
        )
        try:
            if from_query: await update_or_query.edit_message_text(msg, parse_mode="Markdown")
            else: await update_or_query.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[OCR] uid={user_id} reply after save failed: {e}")
            try:
                if from_query: await update_or_query.message.reply_text(msg, parse_mode="Markdown")
            except Exception as e2:
                logger.error(f"[OCR] uid={user_id} fallback reply failed: {e2}")

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(
            f"[OCR-SAVE-ERROR] uid={user_id}\n"
            f"  amount={amount} description={description!r} tx_date={tx_date}\n"
            f"  error_type={type(e).__name__}\n"
            f"  error={e}\n"
            f"  stack:\n{tb}"
        )
        error_msg = f"❌ Gagal menyimpan.\n\nError: `{type(e).__name__}: {e}`"
        try:
            if from_query: await update_or_query.edit_message_text(error_msg, parse_mode="Markdown")
            else: await update_or_query.message.reply_text(error_msg, parse_mode="Markdown")
        except Exception as e3:
            logger.error(f"[OCR] error reply failed: {e3}")

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
