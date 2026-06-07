"""
OCR Handler — tampilkan item detail + total.
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
from bot.services.sheets import append_transaction as sheets_append
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
    context.user_data.clear()
    _log(user_id, "IDLE", "photo_received")
    await update.message.reply_text("🔍 Memproses struk...")
    try:
        photo = update.message.photo[-1]
        result: OcrResult = await process_receipt(update.get_bot(), photo.file_id)
        _log(user_id, "OCR_DONE", "result",
             merchant=result.merchant, total=result.total,
             items=len(result.items), confidence=result.confidence)
        context.user_data["ocr_result"] = result
        context.user_data["ocr_file_id"] = photo.file_id
        return await _show_ocr_result(update, result)
    except Exception as e:
        logger.exception(f"[OCR] uid={user_id} failed: {e}")
        await update.message.reply_text("❌ Gagal membaca struk.\nInput manual: /keluar")
        context.user_data.clear()
        return ConversationHandler.END


async def _show_ocr_result(update: Update, result: OcrResult):
    """Tampilkan hasil OCR dengan rincian item."""
    lines = ["📄 *Hasil Baca Struk*\n"]
    lines.append(f"Toko: *{result.merchant or 'tidak terdeteksi'}*")

    if result.tx_date:
        lines.append(f"Tanggal: *{fmt_date(result.tx_date)}*")
    else:
        lines.append("Tanggal: _tidak terdeteksi_ (pakai hari ini)")

    # ── Rincian item ──
    if result.items:
        lines.append("\n🛒 *Item:*")
        for item in result.items:
            qty_str = f"({int(item.qty)}x) " if item.qty > 1 else ""
            lines.append(f"  • {item.name} {qty_str}— *{fmt_rupiah(item.line_total)}*")
        lines.append(f"\nTotal Item: *{len(result.items)}*")
    else:
        lines.append("\n_Item tidak terdeteksi_")

    # ── Total belanja ──
    if result.total:
        lines.append(f"Total Belanja: *{fmt_rupiah(result.total)}*")
        if result.cash_paid:
            lines.append(f"_Tunai: {fmt_rupiah(result.cash_paid)}_")
        if result.change:
            lines.append(f"_Kembali: {fmt_rupiah(result.change)}_")
    else:
        lines.append("\nTotal: _belum terdeteksi_ — klik Edit Nominal")

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
        logger.warning(f"[OCR] uid={user_id} query.answer failed: {e}")
        try: await query.message.reply_text("⏰ Sesi habis. Kirim ulang foto struk.")
        except Exception: pass
        context.user_data.clear()
        return ConversationHandler.END

    result: OcrResult = context.user_data.get("ocr_result")
    _log(user_id, "WAITING_CONFIRMATION", "callback",
         data=query.data, result_exists=result is not None,
         total=result.total if result else None)

    if result is None:
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
        return await _do_save(query, context, user_id, from_query=True)

    if action == "edit":
        current = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        try:
            await query.edit_message_text(
                f"✏️ Nominal saat ini: *{current}*\n\n"
                f"Masukkan nominal yang benar:\n_(Contoh: 45000, 45rb, 1.5jt)_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"[OCR] edit_message_text failed: {e}")
            try: await query.message.reply_text("✏️ Masukkan nominal baru:", parse_mode="Markdown")
            except Exception: pass
        return OCR_EDIT_NOMINAL

    return OCR_KONFIRMASI


async def ocr_edit_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    logger.info(f"=== EDIT_AMOUNT RECEIVED ===\nInput: {text!r}\nSession: {list(context.user_data.keys())}\n===")

    result: OcrResult = context.user_data.get("ocr_result")
    if result is None:
        await update.message.reply_text("⏰ Sesi habis. Kirim ulang foto struk.")
        context.user_data.clear()
        return ConversationHandler.END

    amount = parse_amount(text)
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

    # Tampilkan preview dengan item jika ada
    lines = [f"✅ Nominal diperbarui.\n"]
    if result.items:
        lines.append("🛒 *Item:*")
        for item in result.items:
            qty_str = f"({int(item.qty)}x) " if item.qty > 1 else ""
            lines.append(f"  • {item.name} {qty_str}— *{fmt_rupiah(item.line_total)}*")
        lines.append(f"\nTotal Item: *{len(result.items)}*")
    lines.append(f"Toko: *{merchant}*")
    lines.append(f"Tanggal: *{fmt_date(tx_date)}*")
    lines.append(f"Total Belanja: *{fmt_rupiah(amount)}*\n\nSimpan?")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit lagi", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
    return OCR_KONFIRMASI


async def _do_save(update_or_query, context, user_id, from_query):
    result: OcrResult = context.user_data.get("ocr_result")
    file_id = context.user_data.get("ocr_file_id", "")

    if not result:
        logger.error(f"[OCR] uid={user_id} _do_save: no ocr_result")
        return ConversationHandler.END

    amount = result.total or 0
    merchant = result.merchant or "Belanja (struk)"
    tx_date = result.tx_date or date.today()

    # Buat deskripsi lengkap dengan item
    if result.items:
        item_desc = ", ".join(
            f"{item.name}{'x'+str(int(item.qty)) if item.qty > 1 else ''}"
            for item in result.items
        )
        description = f"{merchant} ({item_desc})"
    else:
        description = merchant

    description = description[:200]

    logger.info(f"=== BEFORE SAVE ===\nuid={user_id} amount={amount} desc={description!r}\n===")

    if amount <= 0:
        msg = "❌ Nominal belum diset. Gunakan ✏️ Edit nominal."
        try:
            if from_query: await update_or_query.edit_message_text(msg)
            else: await update_or_query.message.reply_text(msg)
        except Exception as e:
            logger.error(f"[OCR] reply failed: {e}")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        async with AsyncSessionLocal() as session:
            tx = Transaction(
                user_id=user_id, type="keluar", amount=amount,
                description=description, transaction_date=tx_date,
            )
            session.add(tx)
            await session.flush()
            tx_id = tx.id
            if file_id:
                session.add(Attachment(
                    transaction_id=tx_id, telegram_file_id=file_id,
                    ocr_raw_text=result.raw_text[:2000] if result.raw_text else None,
                    ocr_confidence=result.confidence,
                ))
            await log_create(session, user_id, tx)
            await session.commit()
            logger.info(f"[OCR] saved tx_id={tx_id}")

        async with AsyncSessionLocal() as session2:
            saldo = await get_running_balance(session2, user_id=user_id)

        # Simpan ke Google Sheets
        db_user = context.user_data.get("db_user")
        user_name = db_user.full_name if db_user else str(user_id)
        await sheets_append(
            user_id=user_id, user_name=user_name,
            tx_type="keluar", amount=amount,
            description=description, tx_date=tx_date,
            source="struk",
        )

        # Pesan sukses dengan rincian item
        lines = ["✅ *Transaksi berhasil disimpan*\n"]
        if result.items:
            lines.append("🛒 *Rincian:*")
            for item in result.items:
                qty_str = f"({int(item.qty)}x) " if item.qty > 1 else ""
                lines.append(f"  • {item.name} {qty_str}— {fmt_rupiah(item.line_total)}")
            lines.append(f"\nTotal Item: *{len(result.items)}*")
        lines.append(f"Jenis: ➖ KELUAR")
        lines.append(f"Total Belanja: *{fmt_rupiah(amount)}*")
        lines.append(f"Tanggal: {fmt_date(tx_date)}")
        lines.append(f"\n💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*")

        msg = "\n".join(lines)

        try:
            if from_query: await update_or_query.edit_message_text(msg, parse_mode="Markdown")
            else: await update_or_query.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[OCR] reply after save failed: {e}")
            try:
                if from_query: await update_or_query.message.reply_text(msg, parse_mode="Markdown")
            except Exception: pass

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"[OCR-ERROR] uid={user_id}\n{tb}")
        error_msg = f"❌ Gagal menyimpan.\n\nError: `{type(e).__name__}: {e}`"
        try:
            if from_query: await update_or_query.edit_message_text(error_msg, parse_mode="Markdown")
            else: await update_or_query.message.reply_text(error_msg, parse_mode="Markdown")
        except Exception: pass

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
