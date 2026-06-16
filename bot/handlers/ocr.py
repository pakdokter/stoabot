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

OCR_KONFIRMASI    = 50
OCR_EDIT_NOMINAL  = 51
OCR_EDIT_MENU     = 52
OCR_EDIT_MERCHANT = 53
OCR_EDIT_DATE     = 54
OCR_QRIS_MERCHANT = 55
OCR_QRIS_ITEM     = 56
OCR_QRIS_QTY      = 57
OCR_QRIS_TOTAL    = 58


def _log(user_id, state, action, **kwargs):
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info(f"[OCR] uid={user_id} state={state} action={action} {extra}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    user_id = update.effective_user.id
    # Preserve session_verified dan db_user saat clear
    _preserved = {
        k: context.user_data[k]
        for k in ("session_verified", "db_user")
        if k in context.user_data
    }
    context.user_data.clear()
    context.user_data.update(_preserved)
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
        # QRIS → form input manual
        if result.is_qris:
            return await handle_qris_result(update, context, result)
        return await _show_ocr_result(update, result)
    except Exception as e:
        logger.exception(f"[OCR] uid={user_id} failed: {e}")
        await update.message.reply_text("❌ Gagal membaca struk.\nInput manual: /keluar")
        context.user_data.clear()
        return ConversationHandler.END



async def handle_qris_result(update: Update, context: ContextTypes.DEFAULT_TYPE, result: OcrResult):
    """Tampilkan deteksi QRIS dan minta input manual merchant/item."""
    from bot.utils.formatters import fmt_rupiah, fmt_date

    merchant = result.merchant or "tidak terdeteksi"
    total_str = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
    date_str = fmt_date(result.tx_date) if result.tx_date else "hari ini"

    await update.message.reply_text(
        f"💳 *Terdeteksi: Bukti Pembayaran QRIS*\n\n"
        f"Merchant: *{_esc(merchant)}*\n"
        f"Total: *{total_str}*\n"
        f"Tanggal: *{date_str}*\n\n"
        f"Lengkapi data belanja:\n"
        f"Ketik *nama toko/merchant* yang benar:",
        parse_mode="Markdown",
    )
    return OCR_QRIS_MERCHANT


async def qris_input_merchant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Input nama merchant QRIS."""
    text = update.message.text.strip()
    if len(text) < 2:
        await update.message.reply_text("❌ Nama terlalu pendek.")
        return OCR_QRIS_MERCHANT
    context.user_data["qris_merchant"] = text
    await update.message.reply_text(
        f"🏪 Merchant: *{_esc(text)}*\n\n"
        f"Ketik *nama item* yang dibeli:\n_(tulis singkat, contoh: Kopi Ethiopia, Matcha Latte)_",
        parse_mode="Markdown",
    )
    return OCR_QRIS_ITEM


async def qris_input_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Input nama item QRIS."""
    text = update.message.text.strip()
    if len(text) < 2:
        await update.message.reply_text("❌ Nama item tidak valid.")
        return OCR_QRIS_ITEM
    context.user_data["qris_item"] = text
    await update.message.reply_text(
        f"📦 Item: *{_esc(text)}*\n\nJumlah (qty)?\n_(ketik angka, misal: 1)_",
        parse_mode="Markdown",
    )
    return OCR_QRIS_QTY


async def qris_input_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Input qty QRIS."""
    text = update.message.text.strip()
    try:
        qty = int(text)
        if qty < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Qty tidak valid. Ketik angka, misal: 1")
        return OCR_QRIS_QTY
    context.user_data["qris_qty"] = qty

    result: OcrResult = context.user_data.get("ocr_result")
    total_hint = f"\n_(Terdeteksi: {fmt_rupiah(result.total)})_" if result and result.total else ""
    await update.message.reply_text(
        f"🔢 Qty: *{qty}*\n\nHarga total?{total_hint}\n_(atau ketik `sama` untuk pakai yang terdeteksi)_",
        parse_mode="Markdown",
    )
    return OCR_QRIS_TOTAL


async def qris_input_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Input total QRIS dan simpan."""
    from bot.utils.formatters import parse_amount, fmt_rupiah, fmt_date
    text = update.message.text.strip().lower()
    user_id = update.effective_user.id

    result: OcrResult = context.user_data.get("ocr_result")

    if text in ("sama", "s", "y", "ya"):
        amount = result.total if result else 0
    else:
        amount = parse_amount(text)

    if not amount or amount <= 0:
        await update.message.reply_text("❌ Nominal tidak valid.")
        return OCR_QRIS_TOTAL

    merchant = context.user_data.get("qris_merchant", "QRIS")
    item = context.user_data.get("qris_item", "Belanja")
    qty = context.user_data.get("qris_qty", 1)
    tx_date = result.tx_date if result else date.today()
    qty_str = f" x{qty}" if qty > 1 else ""
    description = f"{merchant} — {item}{qty_str}"

    try:
        async with AsyncSessionLocal() as session:
            tx = Transaction(
                user_id=user_id, type="keluar", amount=amount,
                description=description, transaction_date=tx_date,
            )
            session.add(tx)
            await session.flush()
            await log_create(session, user_id, tx)
            await session.commit()

        async with AsyncSessionLocal() as session2:
            saldo = await get_running_balance(session2, user_id=user_id)

        db_user = context.user_data.get("db_user")
        user_name = db_user.full_name if db_user else str(user_id)
        await sheets_append(
            user_id=user_id, user_name=user_name,
            tx_type="keluar", amount=amount,
            description=description, tx_date=tx_date,
            source="qris",
        )

        await update.message.reply_text(
            f"✅ *Transaksi QRIS berhasil disimpan*\n\n"
            f"Merchant: {_esc(merchant)}\n"
            f"Item: {_esc(item)}{qty_str}\n"
            f"Total: *{fmt_rupiah(amount)}*\n"
            f"Tanggal: {fmt_date(tx_date)}\n\n"
            f"💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"[QRIS] save failed: {e}")
        await update.message.reply_text(f"❌ Gagal menyimpan.\n`{e}`", parse_mode="Markdown")

    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
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
        InlineKeyboardButton("✏️ Edit", callback_data="ocr:edit"),
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
        _log(user_id, "WAITING_CONFIRMATION", "edit_menu_opened")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏪 Nama Toko", callback_data="ocredit:merchant"),
            InlineKeyboardButton("📅 Tanggal", callback_data="ocredit:date"),
        ],[
            InlineKeyboardButton("💰 Nominal", callback_data="ocredit:nominal"),
            InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
        ]])
        try:
            await query.edit_message_text(
                "✏️ *Edit bagian mana?*", parse_mode="Markdown", reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"[OCR] edit menu failed: {e}")
        return OCR_EDIT_MENU

    return OCR_KONFIRMASI


async def ocr_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler tombol pilihan edit."""
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"[OCR] query.answer failed: {e}")
        context.user_data.clear()
        return ConversationHandler.END

    result: OcrResult = context.user_data.get("ocr_result")
    if result is None:
        try: await query.edit_message_text("⏰ Sesi habis. Kirim ulang foto struk.")
        except Exception: pass
        context.user_data.clear()
        return ConversationHandler.END

    field = query.data.split(":")[1]

    if field == "nominal":
        current = fmt_rupiah(result.total) if result.total else "belum terdeteksi"
        try:
            await query.edit_message_text(
                f"💰 Nominal saat ini: *{current}*\n\nMasukkan nominal baru:\n_(Contoh: 45000, 45rb)_",
                parse_mode="Markdown",
            )
        except Exception: pass
        context.user_data["ocr_edit_field"] = "nominal"
        return OCR_EDIT_NOMINAL

    if field == "merchant":
        current = result.merchant or "tidak terdeteksi"
        try:
            await query.edit_message_text(
                f"🏪 Nama toko saat ini: *{_esc(current)}*\n\nMasukkan nama toko yang benar:",
                parse_mode="Markdown",
            )
        except Exception: pass
        context.user_data["ocr_edit_field"] = "merchant"
        return OCR_EDIT_MERCHANT

    if field == "date":
        current = fmt_date(result.tx_date) if result.tx_date else "hari ini"
        try:
            await query.edit_message_text(
                f"📅 Tanggal saat ini: *{current}*\n\nMasukkan tanggal baru:\n_(Format: DD/MM/YYYY, contoh: 07/06/2026)_",
                parse_mode="Markdown",
            )
        except Exception: pass
        context.user_data["ocr_edit_field"] = "date"
        return OCR_EDIT_DATE

    return OCR_KONFIRMASI


async def ocr_edit_merchant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler input nama toko baru."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    result: OcrResult = context.user_data.get("ocr_result")
    if result is None:
        await update.message.reply_text("⏰ Sesi habis.")
        context.user_data.clear()
        return ConversationHandler.END

    if len(text) < 2:
        await update.message.reply_text("❌ Nama terlalu pendek.")
        return OCR_EDIT_MERCHANT

    result.merchant = text
    context.user_data["ocr_result"] = result
    context.user_data["ocr_edit_field"] = None
    return await _show_ocr_confirm(update, result, from_edit=True)


async def ocr_edit_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler input tanggal baru."""
    from bot.utils.formatters import parse_date
    user_id = update.effective_user.id
    text = update.message.text.strip()

    result: OcrResult = context.user_data.get("ocr_result")
    if result is None:
        await update.message.reply_text("⏰ Sesi habis.")
        context.user_data.clear()
        return ConversationHandler.END

    new_date = parse_date(text)
    if not new_date:
        await update.message.reply_text(
            "❌ Format tanggal tidak valid.\nContoh: `07/06/2026`",
            parse_mode="Markdown"
        )
        return OCR_EDIT_DATE

    result.tx_date = new_date
    context.user_data["ocr_result"] = result
    context.user_data["ocr_edit_field"] = None
    return await _show_ocr_confirm(update, result, from_edit=True)


async def _show_ocr_confirm(update: Update, result: OcrResult, from_edit: bool = False):
    """Tampilkan preview terbaru setelah edit."""
    lines = ["✅ *Data diperbarui*\n" if from_edit else "📄 *Hasil Baca Struk*\n"]
    lines.append(f"Toko: *{_esc(result.merchant or 'tidak terdeteksi')}*")
    lines.append(f"Tanggal: *{fmt_date(result.tx_date)}*" if result.tx_date else "Tanggal: _pakai hari ini_")

    if result.items:
        lines.append("\n🛒 *Item:*")
        for item in result.items:
            qty_str = f"({int(item.qty)}x) " if item.qty > 1 else ""
            lines.append(f"  • {_esc(item.name)} {qty_str}— *{fmt_rupiah(item.line_total)}*")
        lines.append(f"\nTotal Item: *{len(result.items)}*")
    else:
        lines.append("\n_Item tidak terdeteksi_")

    if result.total:
        lines.append(f"Total Belanja: *{fmt_rupiah(result.total)}*")
        if result.cash_paid: lines.append(f"_Tunai: {fmt_rupiah(result.cash_paid)}_")
        if result.change: lines.append(f"_Kembali: {fmt_rupiah(result.change)}_")
    else:
        lines.append("\nTotal: _belum terdeteksi_")

    lines.append("\nSimpan sebagai pengeluaran?")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya, simpan", callback_data="ocr:ya"),
        InlineKeyboardButton("✏️ Edit lagi", callback_data="ocr:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="ocr:tidak"),
    ]])

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
    )
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

    logger.info(f"=== BEFORE SAVE ===\nuid={user_id} amount={amount} merchant={merchant!r}\n===")

    if amount <= 0:
        msg = "❌ Nominal belum diset. Gunakan ✏️ Edit nominal."
        try:
            if from_query: await update_or_query.edit_message_text(msg)
            else: await update_or_query.message.reply_text(msg)
        except Exception as e:
            logger.error(f"[OCR] reply failed: {e}")
        context.user_data.clear()
        return ConversationHandler.END

    # Jika ada item terdeteksi → simpan per item sebagai transaksi terpisah
    # Jika tidak ada item → simpan satu transaksi dengan total
    transactions_to_save = []
    if result.items:
        for item in result.items:
            qty_str = f" x{int(item.qty)}" if item.qty > 1 else ""
            desc = f"{merchant} — {item.name}{qty_str}"[:200]
            transactions_to_save.append((item.line_total, desc))
    else:
        transactions_to_save.append((amount, merchant))

    try:
        async with AsyncSessionLocal() as session:
            saved_ids = []
            for tx_amount, tx_desc in transactions_to_save:
                tx = Transaction(
                    user_id=user_id, type="keluar", amount=tx_amount,
                    description=tx_desc, transaction_date=tx_date,
                )
                session.add(tx)
                await session.flush()
                saved_ids.append(tx.id)
            tx_id = saved_ids[0]
            if file_id:
                session.add(Attachment(
                    transaction_id=tx_id, telegram_file_id=file_id,
                    ocr_raw_text=result.raw_text[:2000] if result.raw_text else None,
                    ocr_confidence=result.confidence,
                ))
            await log_create(session, user_id, tx)
            await session.commit()
            logger.info(f"[OCR] saved {len(saved_ids)} tx(s), first_id={tx_id}")

        async with AsyncSessionLocal() as session2:
            saldo = await get_running_balance(session2, user_id=user_id)

        # Simpan ke Google Sheets — per item
        db_user = context.user_data.get("db_user")
        user_name = db_user.full_name if db_user else str(user_id)
        for tx_amount, tx_desc in transactions_to_save:
            await sheets_append(
                user_id=user_id, user_name=user_name,
                tx_type="keluar", amount=tx_amount,
                description=tx_desc, tx_date=tx_date,
                source="struk",
            )

        # Pesan sukses dengan rincian item
        lines = ["✅ *Transaksi berhasil disimpan*\n"]
        if result.items:
            lines.append("🛒 *Rincian (per item):*")
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
            OCR_EDIT_MENU: [
                CallbackQueryHandler(ocr_edit_menu, pattern="^ocredit:"),
                CallbackQueryHandler(ocr_callback, pattern="^ocr:"),
            ],
            OCR_EDIT_NOMINAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_nominal),
                MessageHandler(filters.PHOTO, handle_photo),
                CallbackQueryHandler(ocr_callback, pattern="^ocr:"),
            ],
            OCR_EDIT_MERCHANT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_merchant),
                MessageHandler(filters.PHOTO, handle_photo),
            ],
            OCR_EDIT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ocr_edit_date),
                MessageHandler(filters.PHOTO, handle_photo),
            ],
            OCR_QRIS_MERCHANT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, qris_input_merchant),
            ],
            OCR_QRIS_ITEM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, qris_input_item),
            ],
            OCR_QRIS_QTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, qris_input_qty),
            ],
            OCR_QRIS_TOTAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, qris_input_total),
            ],
        },
        fallbacks=[],
        allow_reentry=True,
        conversation_timeout=600,
    )
