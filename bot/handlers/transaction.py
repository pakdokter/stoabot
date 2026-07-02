"""
Transaction handlers menggunakan ConversationHandler.
States:
  MASUK/KELUAR → NOMINAL → KETERANGAN → TANGGAL → KONFIRMASI
  EDIT         → PILIH_TX → PILIH_FIELD → INPUT_NILAI
  HAPUS        → PILIH_TX → KONFIRMASI
"""
from datetime import date
from typing import Optional
import uuid

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from sqlalchemy import select, desc
from loguru import logger

from bot.database import AsyncSessionLocal
from bot.models import Transaction, User
from bot.services.balance import get_running_balance, get_summary
from bot.services.sheets import append_transaction as sheets_append
from bot.services.audit import log_create, log_update, log_delete
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount, parse_date
from bot.handlers.auth import ensure_registered

# ── ConversationHandler states ──
(
    TX_NOMINAL, TX_KETERANGAN, TX_TANGGAL,
    EDIT_PILIH, EDIT_FIELD, EDIT_NILAI,
    HAPUS_KONFIRMASI,
    TX_SUMBER_LAINNYA,
    TX_TANGGAL_MANUAL,
) = range(9)

SUMBER_MASUK = ["Bank Biru", "Kasir", "Lainnya"]


# ──────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────

def _tx_type_emoji(t: str) -> str:
    return "➕" if t == "masuk" else "➖"


async def _recent_transactions(session, limit: int = 10, user_id: int = None) -> list[Transaction]:
    conditions = [Transaction.is_deleted == False]
    if user_id is not None:
        conditions.append(Transaction.user_id == user_id)
    result = await session.execute(
        select(Transaction)
        .where(*conditions)
        .order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
        .limit(limit)
    )
    return result.scalars().all()


def _tx_inline_keyboard(transactions: list[Transaction]) -> InlineKeyboardMarkup:
    buttons = []
    for tx in transactions:
        label = f"{fmt_date(tx.transaction_date)} | {fmt_rupiah(tx.amount)} | {tx.description[:20]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"tx:{tx.id}")])
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ──────────────────────────────────────────────
# /masuk dan /keluar — entry
# ──────────────────────────────────────────────

async def cmd_masuk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    context.user_data["tx_type"] = "masuk"
    await update.message.reply_text("💰 *Catat Pemasukan*\n\nNominal?", parse_mode="Markdown")
    return TX_NOMINAL


async def cmd_keluar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    context.user_data["tx_type"] = "keluar"
    # Tampilkan keyboard pilihan toko
    from bot.handlers.market import show_toko_keyboard, PASAR_TOKO
    return await show_toko_keyboard(update, context)


async def handle_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Nominal tidak valid. Contoh: 150000, 150rb, 1.5jt")
        return TX_NOMINAL
    context.user_data["tx_amount"] = amount

    # Jika masuk → tampilkan pilihan sumber
    if context.user_data.get("tx_type") == "masuk":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏦 Bank Biru", callback_data="sumber:Bank Biru"),
            InlineKeyboardButton("🏪 Kasir", callback_data="sumber:Kasir"),
        ],[
            InlineKeyboardButton("✏️ Lainnya", callback_data="sumber:Lainnya"),
        ]])
        await update.message.reply_text(
            f"💰 *{fmt_rupiah(amount)}*\n\nSumber dana?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return TX_KETERANGAN

    # Keluar → langsung tanya keterangan
    await update.message.reply_text("Keterangan?")
    return TX_KETERANGAN


async def handle_keterangan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc_text = update.message.text.strip()
    if not desc_text:
        await update.message.reply_text("❌ Keterangan tidak boleh kosong.")
        return TX_KETERANGAN
    context.user_data["tx_desc"] = desc_text

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Hari ini", callback_data="tanggal:hari_ini"),
        InlineKeyboardButton("✏️ Tgl lain", callback_data="tanggal:lain"),
    ]])
    await update.message.reply_text(
        "Tanggal transaksi?",
        reply_markup=keyboard,
    )
    return TX_TANGGAL


async def handle_tanggal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tombol Hari ini / Tgl lain."""
    query = update.callback_query
    await query.answer()
    data = query.data  # "tanggal:hari_ini" atau "tanggal:lain"

    if data == "tanggal:hari_ini":
        context.user_data["tx_date"] = date.today()
        return await _do_save_from_context(query, context)

    if data == "tanggal:lain":
        try:
            await query.edit_message_text(
                "Masukkan tanggal:\n_(Format: DD/MM/YYYY, contoh: 15/06/2026)_",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return TX_TANGGAL_MANUAL

    return TX_TANGGAL


async def handle_tanggal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle input tanggal manual (teks DD/MM/YYYY)."""
    text = update.message.text.strip()
    if text in ("-", "skip", ""):
        tx_date = date.today()
    else:
        tx_date = parse_date(text)
        if not tx_date:
            await update.message.reply_text(
                "❌ Format tidak dikenali. Contoh: 15/06/2026\n"
                "Atau ketik `-` untuk hari ini."
            )
            return TX_TANGGAL_MANUAL

    context.user_data["tx_date"] = tx_date
    return await _do_save_from_context(update, context)


async def _do_save_from_context(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    """Simpan transaksi dari context.user_data."""
    from telegram import Update as TgUpdate
    is_query = not isinstance(update_or_query, TgUpdate)

    if is_query:
        tg_user = update_or_query.from_user
        reply_fn = update_or_query.message.reply_text
    else:
        tg_user = update_or_query.effective_user
        reply_fn = update_or_query.message.reply_text
    tx_type = context.user_data["tx_type"]
    amount = context.user_data["tx_amount"]
    desc_text = context.user_data["tx_desc"]
    tx_date = context.user_data.get("tx_date", date.today())

    async with AsyncSessionLocal() as session:
        tx = Transaction(
            user_id=tg_user.id,
            type=tx_type,
            amount=amount,
            description=desc_text,
            transaction_date=tx_date,
        )
        session.add(tx)
        await session.flush()
        await log_create(session, tg_user.id, tx)
        await session.commit()

    async with AsyncSessionLocal() as session2:
        saldo = await get_running_balance(session2, user_id=tg_user.id)

    # Simpan ke Google Sheets
    db_user = context.user_data.get("db_user")
    user_name = db_user.full_name if db_user else str(tg_user.id)
    await sheets_append(
        user_id=tg_user.id, user_name=user_name,
        tx_type=tx_type, amount=amount,
        description=desc_text, tx_date=tx_date,
        source="manual",
    )

    emoji = "✅" if tx_type == "masuk" else "✅"
    await reply_fn(
        f"✅ *Transaksi berhasil disimpan*\n\n"
        f"Jenis: {_tx_type_emoji(tx_type)} {'MASUK' if tx_type == 'masuk' else 'KELUAR'}\n"
        f"Nominal: *{fmt_rupiah(amount)}*\n"
        f"Keterangan: {desc_text}\n"
        f"Tanggal: {fmt_date(tx_date)}\n\n"
        f"💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    return ConversationHandler.END


# ──────────────────────────────────────────────
# /saldo
# ──────────────────────────────────────────────

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return

    user_id = update.effective_user.id
    try:
        async with AsyncSessionLocal() as session:
            summary = await get_summary(session, user_id=user_id)
    except Exception as e:
        logger.error(f"[SALDO] error uid={user_id}: {e}")
        summary = {"saldo": 0, "total_masuk": 0, "total_keluar": 0, "jumlah": 0}

    db_user = context.user_data.get("db_user")
    user_name = db_user.full_name if db_user else update.effective_user.first_name or "User"

    await update.message.reply_text(
        f"💰 *Saldo — {user_name}*\n\n"
        f"Saldo saat ini:\n*{fmt_rupiah(summary['saldo'])}*\n\n"
        f"Total pemasukan:\n{fmt_rupiah(summary['total_masuk'])}\n\n"
        f"Total pengeluaran:\n{fmt_rupiah(summary['total_keluar'])}",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# /riwayat — paginated
# ──────────────────────────────────────────────

PAGE_SIZE = 10

async def cmd_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return

    from datetime import date as _date, timedelta
    from collections import defaultdict
    from decimal import Decimal
    from bot.services.balance import get_summary
    import re as _re

    user_id = update.effective_user.id
    today = _date.today()
    date_from = today - timedelta(days=6)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.user_id == user_id,
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= today,
            )
            .order_by(Transaction.transaction_date, Transaction.created_at)
        )
        txs = result.scalars().all()
        saldo_awal_data = await get_summary(
            session, user_id=user_id,
            date_to=date_from - timedelta(days=1)
        )
        saldo_awal = saldo_awal_data["saldo"]

    if not txs:
        await update.message.reply_text(
            f"Belum ada transaksi dalam 7 hari terakhir\n"
            f"({fmt_date(date_from)} \u2014 {fmt_date(today)})."
        )
        return

    db_user = context.user_data.get("db_user")
    user_name = db_user.full_name if db_user else update.effective_user.first_name or "User"

    by_date = defaultdict(list)
    for tx in txs:
        by_date[tx.transaction_date].append(tx)

    total_masuk = sum(Decimal(str(tx.amount)) for tx in txs if tx.type == "masuk")
    total_keluar = sum(Decimal(str(tx.amount)) for tx in txs if tx.type == "keluar")
    saldo_akhir = saldo_awal + total_masuk - total_keluar

    def _parse_desc(desc):
        if " \u2014 " in desc:
            parts = desc.split(" \u2014 ", 1)
            toko = parts[0].strip()
            item_part = parts[1].strip()
            qty_m = _re.search(r'\s+x(\d+)$|\s+\((\d+)x\)$', item_part)
            if qty_m:
                qty = int(qty_m.group(1) or qty_m.group(2))
                item = item_part[:qty_m.start()].strip()
            else:
                qty = 1
                item = item_part
            return toko, item, qty
        return desc.strip(), "", 1

    SEP = "\u2501" * 20

    lines = [
        f"\U0001F4CB *Riwayat 7 Hari Terakhir*",
        f"\U0001F464 {user_name}",
        f"_{fmt_date(date_from)} \u2014 {fmt_date(today)}_",
        f"",
        f"Saldo Awal: *{fmt_rupiah(saldo_awal)}*",
        SEP,
    ]

    for d in sorted(by_date.keys()):
        day_txs = by_date[d]
        lines.append(f"")
        lines.append(f"\U0001F4C5 *{fmt_date(d)}*")

        toko_groups = defaultdict(list)
        for tx in day_txs:
            toko, item, qty = _parse_desc(tx.description or "")
            toko_groups[toko].append((tx, item, qty))

        for toko, tx_list in toko_groups.items():
            lines.append(f"\U0001F3EA _{toko}_")
            for tx, item, qty in tx_list:
                sign = "+" if tx.type == "masuk" else "-"
                qty_str = f" x{qty}" if qty > 1 else ""
                item_str = f" \u2014 {item}{qty_str}" if item else ""
                lines.append(f"  {sign}{fmt_rupiah(tx.amount)}{item_str}")

        lines.append(SEP)

    lines += [
        f"",
        f"Saldo Akhir: *{fmt_rupiah(saldo_akhir)}*",
        f"Masuk: {fmt_rupiah(total_masuk)} | Keluar: {fmt_rupiah(total_keluar)}",
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n_...terpotong, gunakan /laporan untuk detail_"

    await update.message.reply_text(text, parse_mode="Markdown")

# ──────────────────────────────────────────────
# /cari
# ──────────────────────────────────────────────

async def cmd_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return

    user_id = update.effective_user.id
    keyword = " ".join(context.args).strip() if context.args else ""
    if not keyword:
        await update.message.reply_text("Gunakan: /cari <kata kunci>\nContoh: /cari kopi")
        return

    from sqlalchemy import or_, func
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.user_id == user_id,
                Transaction.description.ilike(f"%{keyword}%")
            )
            .order_by(Transaction.transaction_date, Transaction.created_at)
            .limit(20)
        )
        txs = result.scalars().all()

    if not txs:
        await update.message.reply_text(f"Tidak ditemukan transaksi dengan kata kunci: *{keyword}*", parse_mode="Markdown")
        return

    lines = [f"🔍 Hasil pencarian: *{keyword}*\n"]
    for tx in txs:
        sign = "+" if tx.type == "masuk" else "-"
        lines.append(f"*{fmt_date(tx.transaction_date)}*\n  {sign} {fmt_rupiah(tx.amount)}\n  _{tx.description}_\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────────────────────────
# /edit
# ──────────────────────────────────────────────

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        txs = await _recent_transactions(session, 10, user_id=user_id)

    if not txs:
        await update.message.reply_text("Tidak ada transaksi untuk diedit.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✏️ *Edit Transaksi*\nPilih transaksi yang ingin diubah:",
        reply_markup=_tx_inline_keyboard(txs),
        parse_mode="Markdown",
    )
    return EDIT_PILIH


async def edit_pilih_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ("cancel", "edit_selesai"):
        await query.edit_message_text("✅ Selesai.")
        return ConversationHandler.END

    if query.data == "edit_lagi":
        # Tampilkan ulang daftar transaksi untuk diedit
        user_id = update.effective_user.id
        async with AsyncSessionLocal() as session:
            txs = await _recent_transactions(session, 10, user_id=user_id)
        if not txs:
            await query.edit_message_text("Tidak ada transaksi.")
            return ConversationHandler.END
        await query.edit_message_text(
            "✏️ *Edit Transaksi*\nPilih transaksi yang ingin diubah:",
            reply_markup=_tx_inline_keyboard(txs),
            parse_mode="Markdown",
        )
        return EDIT_PILIH

    tx_id = query.data.split(":")[1]
    context.user_data["edit_tx_id"] = tx_id

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Nominal", callback_data="field:amount")],
        [InlineKeyboardButton("📝 Keterangan", callback_data="field:description")],
        [InlineKeyboardButton("📅 Tanggal", callback_data="field:date")],
        [InlineKeyboardButton("🔄 Jenis (masuk/keluar)", callback_data="field:type")],
        [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
    ])
    await query.edit_message_text("Field mana yang ingin diubah?", reply_markup=keyboard)
    return EDIT_FIELD


async def edit_pilih_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Edit dibatalkan.")
        return ConversationHandler.END

    field = query.data.split(":")[1]
    context.user_data["edit_field"] = field

    prompts = {
        "amount": "Masukkan nominal baru:",
        "description": "Masukkan keterangan baru:",
        "date": "Masukkan tanggal baru (DD/MM/YYYY):",
        "type": "Ketik `masuk` atau `keluar`:",
    }
    await query.edit_message_text(prompts[field], parse_mode="Markdown")
    return EDIT_NILAI


async def edit_input_nilai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data["edit_field"]
    tx_id = context.user_data["edit_tx_id"]
    value = update.message.text.strip()
    tg_user = update.effective_user

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, uuid.UUID(tx_id))
        if not tx or tx.is_deleted:
            await update.message.reply_text("❌ Transaksi tidak ditemukan.")
            return ConversationHandler.END

        from bot.services.audit import _tx_to_dict
        old_vals = _tx_to_dict(tx)

        if field == "amount":
            new_amount = parse_amount(value)
            if not new_amount:
                await update.message.reply_text("❌ Nominal tidak valid.")
                return EDIT_NILAI
            tx.amount = new_amount
        elif field == "description":
            tx.description = value
        elif field == "date":
            new_date = parse_date(value)
            if not new_date:
                await update.message.reply_text("❌ Format tanggal tidak valid.")
                return EDIT_NILAI
            tx.transaction_date = new_date
        elif field == "type":
            if value.lower() not in ("masuk", "keluar"):
                await update.message.reply_text("❌ Ketik `masuk` atau `keluar`.", parse_mode="Markdown")
                return EDIT_NILAI
            tx.type = value.lower()

        await log_update(session, tg_user.id, old_vals, tx)
        await session.commit()

        saldo = await get_running_balance(session, user_id=tg_user.id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Edit lagi", callback_data="edit_lagi"),
        InlineKeyboardButton("✅ Selesai", callback_data="edit_selesai"),
    ]])
    await update.message.reply_text(
        f"✅ *Transaksi berhasil diperbarui*\n\n💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return EDIT_PILIH


# ──────────────────────────────────────────────
# /hapus
# ──────────────────────────────────────────────

async def cmd_hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        txs = await _recent_transactions(session, 10, user_id=user_id)

    if not txs:
        await update.message.reply_text("Tidak ada transaksi untuk dihapus.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🗑️ *Hapus Transaksi*\nPilih transaksi yang ingin dihapus:",
        reply_markup=_tx_inline_keyboard(txs),
        parse_mode="Markdown",
    )
    return HAPUS_KONFIRMASI


async def hapus_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data in ("cancel", "hapus_selesai"):
        await query.edit_message_text("✅ Selesai.")
        return ConversationHandler.END

    if query.data == "hapus_lagi":
        user_id = update.effective_user.id
        async with AsyncSessionLocal() as session:
            txs = await _recent_transactions(session, 10, user_id=user_id)
        if not txs:
            await query.edit_message_text("Tidak ada transaksi.")
            return ConversationHandler.END
        await query.edit_message_text(
            "🗑️ *Hapus Transaksi*\nPilih transaksi yang ingin dihapus:",
            reply_markup=_tx_inline_keyboard(txs),
            parse_mode="Markdown",
        )
        return HAPUS_KONFIRMASI

    tx_id = query.data.split(":")[1]
    tg_user = update.effective_user

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, uuid.UUID(tx_id))
        if not tx or tx.is_deleted:
            await query.edit_message_text("❌ Transaksi tidak ditemukan.")
            return ConversationHandler.END

        from datetime import datetime
        tx.is_deleted = True
        tx.deleted_at = datetime.utcnow()
        await log_delete(session, tg_user.id, tx)
        await session.commit()

        saldo = await get_running_balance(session, user_id=tg_user.id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑️ Hapus lagi", callback_data="hapus_lagi"),
        InlineKeyboardButton("✅ Selesai", callback_data="hapus_selesai"),
    ]])
    await query.edit_message_text(
        f"🗑️ *Transaksi berhasil dihapus*\n\n💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return HAPUS_KONFIRMASI


# ──────────────────────────────────────────────
# Cancel handler (universal)
# ──────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    await update.message.reply_text("❌ Dibatalkan.")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ConversationHandler builders
# ──────────────────────────────────────────────


async def handle_sumber_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler pilihan sumber dana untuk transaksi masuk."""
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        await query.answer()
    except Exception:
        pass

    sumber = query.data.split(":", 1)[1]

    if sumber == "Lainnya":
        try:
            await query.edit_message_text(
                "✏️ Ketik sumber dana:\n_(contoh: Penjualan Kue, Transfer Owner)_",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return TX_SUMBER_LAINNYA

    return await _simpan_masuk(query, context, user_id, sumber, from_query=True)


async def handle_sumber_lainnya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler input sumber lainnya secara manual."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text or len(text) < 2:
        await update.message.reply_text("❌ Sumber tidak boleh kosong.")
        return TX_SUMBER_LAINNYA
    return await _simpan_masuk(update, context, user_id, text, from_query=False)


async def _simpan_masuk(update_or_query, context, user_id, keterangan, from_query):
    """Simpan transaksi masuk."""
    from datetime import date as _date
    amount = context.user_data.get("tx_amount", 0)
    tx_date = _date.today()

    try:
        async with AsyncSessionLocal() as session:
            tx = Transaction(
                user_id=user_id, type="masuk", amount=amount,
                description=keterangan, transaction_date=tx_date,
            )
            session.add(tx)
            await session.flush()
            await log_create(session, user_id, tx)
            await session.commit()

        async with AsyncSessionLocal() as session2:
            saldo = await get_running_balance(session2, user_id=user_id)

        db_user = context.user_data.get("db_user")
        user_name = db_user.full_name if db_user else str(user_id)
        from bot.services.sheets import append_transaction as sheets_append
        await sheets_append(
            user_id=user_id, user_name=user_name,
            tx_type="masuk", amount=amount,
            description=keterangan, tx_date=tx_date,
            source="manual",
        )

        msg = (
            f"✅ *Pemasukan berhasil dicatat*\n\n"
            f"Jenis: ➕ MASUK\n"
            f"Nominal: *{fmt_rupiah(amount)}*\n"
            f"Sumber: {keterangan}\n"
            f"Tanggal: {fmt_date(tx_date)}\n\n"
            f"💰 Saldo saat ini:\n*{fmt_rupiah(saldo)}*"
        )

        try:
            if from_query:
                await update_or_query.edit_message_text(msg, parse_mode="Markdown")
            else:
                await update_or_query.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[TX] reply: {e}")
            try:
                if from_query:
                    await update_or_query.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                pass

    except Exception as e:
        logger.exception(f"[TX-ERROR] masuk uid={user_id}: {e}")
        err = f"❌ Gagal.\n`{type(e).__name__}: {e}`"
        try:
            if from_query:
                await update_or_query.edit_message_text(err, parse_mode="Markdown")
            else:
                await update_or_query.message.reply_text(err, parse_mode="Markdown")
        except Exception:
            pass

    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    return ConversationHandler.END

def build_transaction_conv() -> ConversationHandler:
    from bot.handlers.market import (
        handle_toko_callback, handle_pasar_input,
        handle_pasar_konfirm, handle_pasar_manual_toko,
        handle_pasar_nama_toko,
        show_toko_keyboard,
        PASAR_TOKO, PASAR_TABEL, PASAR_MANUAL, PASAR_KONFIRM, PASAR_NAMA_TOKO,
    )
    return ConversationHandler(
        entry_points=[
            CommandHandler("masuk", cmd_masuk),
            CommandHandler("keluar", cmd_keluar),
        ],
        states={
            # ── Pilih toko ──
            PASAR_TOKO: [
                CallbackQueryHandler(handle_toko_callback, pattern="^toko:"),
                # Tampilkan ulang keyboard jika ada teks random masuk
                MessageHandler(filters.TEXT & ~filters.COMMAND, show_toko_keyboard),
            ],
            # ── Tabel belanja pasar ──
            PASAR_TABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pasar_input),
            ],
            PASAR_KONFIRM: [
                CallbackQueryHandler(handle_pasar_konfirm, pattern="^pasar:"),
            ],
            # ── Input nama toko manual (pilih Lainnya) ──
            PASAR_NAMA_TOKO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pasar_nama_toko),
            ],
            # ── Input nominal untuk toko biasa atau toko manual ──
            PASAR_MANUAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nominal),
            ],
            TX_NOMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nominal)],
            TX_KETERANGAN: [
                CallbackQueryHandler(handle_sumber_callback, pattern="^sumber:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keterangan),
            ],
            TX_TANGGAL: [
                CallbackQueryHandler(handle_tanggal_callback, pattern="^tanggal:"),
            ],
            TX_TANGGAL_MANUAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tanggal),
            ],
            TX_SUMBER_LAINNYA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sumber_lainnya),
            ],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )


def build_edit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("edit", cmd_edit)],
        states={
            EDIT_PILIH: [CallbackQueryHandler(edit_pilih_tx)],
            EDIT_FIELD: [CallbackQueryHandler(edit_pilih_field)],
            EDIT_NILAI: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_input_nilai)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel), CallbackQueryHandler(cmd_cancel, pattern="^cancel$")],
        conversation_timeout=300,
    )


def build_hapus_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("hapus", cmd_hapus)],
        states={
            HAPUS_KONFIRMASI: [CallbackQueryHandler(hapus_konfirmasi)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel), CallbackQueryHandler(cmd_cancel, pattern="^cancel$")],
        conversation_timeout=300,
    )
