"""
Transaction handlers menggunakan ConversationHandler.
Flow disederhanakan: NOMINAL → KETERANGAN → langsung simpan (tanggal = hari ini)
Untuk tanggal berbeda, gunakan /edit
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
from bot.models import Transaction
from bot.services.balance import get_running_balance
from bot.services.audit import log_create, log_update, log_delete
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount, parse_date
from bot.handlers.auth import ensure_registered

(
    TX_NOMINAL, TX_KETERANGAN,
    EDIT_PILIH, EDIT_FIELD, EDIT_NILAI,
    HAPUS_KONFIRMASI,
) = range(6)


def _tx_type_emoji(t: str) -> str:
    return "➕" if t == "masuk" else "➖"


async def _recent_transactions(session, limit: int = 10):
    result = await session.execute(
        select(Transaction)
        .where(Transaction.is_deleted == False)
        .order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
        .limit(limit)
    )
    return result.scalars().all()


def _tx_inline_keyboard(transactions) -> InlineKeyboardMarkup:
    buttons = []
    for tx in transactions:
        label = f"{fmt_date(tx.transaction_date)} | {fmt_rupiah(tx.amount)} | {tx.description[:20]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"tx:{tx.id}")])
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ── /masuk dan /keluar ──

async def cmd_masuk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    context.user_data["tx_type"] = "masuk"
    await update.message.reply_text("💰 *Catat Pemasukan*\n\nNominal?", parse_mode="Markdown")
    return TX_NOMINAL


async def cmd_keluar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    context.user_data["tx_type"] = "keluar"
    await update.message.reply_text("💸 *Catat Pengeluaran*\n\nNominal?", parse_mode="Markdown")
    return TX_NOMINAL


async def handle_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Nominal tidak valid. Contoh: 150000, 150rb, 1.5jt")
        return TX_NOMINAL
    context.user_data["tx_amount"] = amount
    await update.message.reply_text("Keterangan?")
    return TX_KETERANGAN


async def handle_keterangan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc_text = update.message.text.strip()
    if not desc_text:
        await update.message.reply_text("❌ Keterangan tidak boleh kosong.")
        return TX_KETERANGAN

    tg_user = update.effective_user
    tx_type = context.user_data["tx_type"]
    amount = context.user_data["tx_amount"]
    tx_date = date.today()

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
        saldo = await get_running_balance(session)

    await update.message.reply_text(
        f"✅ *Tersimpan*\n\n"
        f"Jenis: {_tx_type_emoji(tx_type)} {'MASUK' if tx_type == 'masuk' else 'KELUAR'}\n"
        f"Nominal: *{fmt_rupiah(amount)}*\n"
        f"Keterangan: {desc_text}\n"
        f"Tanggal: {fmt_date(tx_date)}\n\n"
        f"💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ── /saldo ──

async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return
    async with AsyncSessionLocal() as session:
        from bot.services.balance import get_summary
        summary = await get_summary(session)
    await update.message.reply_text(
        f"💰 *Saldo saat ini:*\n*{fmt_rupiah(summary['saldo'])}*\n\n"
        f"Total pemasukan:\n{fmt_rupiah(summary['total_masuk'])}\n\n"
        f"Total pengeluaran:\n{fmt_rupiah(summary['total_keluar'])}",
        parse_mode="Markdown",
    )


# ── /riwayat ──

PAGE_SIZE = 10

async def cmd_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return
    page = int(context.args[0]) if context.args else 0
    offset = page * PAGE_SIZE
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.is_deleted == False)
            .order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
        txs = result.scalars().all()
    if not txs:
        await update.message.reply_text("Belum ada transaksi tercatat.")
        return
    has_more = len(txs) > PAGE_SIZE
    txs = txs[:PAGE_SIZE]
    from collections import defaultdict
    by_date = defaultdict(list)
    for tx in txs:
        by_date[tx.transaction_date].append(tx)
    lines = [f"📋 *Riwayat Transaksi* (hal. {page + 1})\n"]
    for d in sorted(by_date.keys(), reverse=True):
        lines.append(f"\n*{fmt_date(d)}*")
        for tx in by_date[d]:
            sign = "+" if tx.type == "masuk" else "-"
            lines.append(f"  {sign} {fmt_rupiah(tx.amount)}\n   _{tx.description}_")
    if has_more:
        lines.append(f"\n/riwayat {page + 1} → halaman berikutnya")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /cari ──

async def cmd_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return
    keyword = " ".join(context.args).strip() if context.args else ""
    if not keyword:
        await update.message.reply_text("Gunakan: /cari <kata kunci>\nContoh: /cari kopi")
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.is_deleted == False, Transaction.description.ilike(f"%{keyword}%"))
            .order_by(desc(Transaction.transaction_date))
            .limit(20)
        )
        txs = result.scalars().all()
    if not txs:
        await update.message.reply_text(f"Tidak ditemukan: *{keyword}*", parse_mode="Markdown")
        return
    lines = [f"🔍 Hasil: *{keyword}*\n"]
    for tx in txs:
        sign = "+" if tx.type == "masuk" else "-"
        lines.append(f"*{fmt_date(tx.transaction_date)}* {sign}{fmt_rupiah(tx.amount)}\n_{tx.description}_\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /edit ──

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    async with AsyncSessionLocal() as session:
        txs = await _recent_transactions(session, 10)
    if not txs:
        await update.message.reply_text("Tidak ada transaksi untuk diedit.")
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ *Edit Transaksi*\nPilih transaksi:",
        reply_markup=_tx_inline_keyboard(txs),
        parse_mode="Markdown",
    )
    return EDIT_PILIH


async def edit_pilih_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END
    context.user_data["edit_tx_id"] = query.data.split(":")[1]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Nominal", callback_data="field:amount")],
        [InlineKeyboardButton("📝 Keterangan", callback_data="field:description")],
        [InlineKeyboardButton("📅 Tanggal", callback_data="field:date")],
        [InlineKeyboardButton("🔄 Jenis", callback_data="field:type")],
        [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
    ])
    await query.edit_message_text("Field mana yang ingin diubah?", reply_markup=keyboard)
    return EDIT_FIELD


async def edit_pilih_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ Dibatalkan.")
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
        saldo = await get_running_balance(session)
    await update.message.reply_text(
        f"✅ Diperbarui.\n\n💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ── /hapus ──

async def cmd_hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    async with AsyncSessionLocal() as session:
        txs = await _recent_transactions(session, 10)
    if not txs:
        await update.message.reply_text("Tidak ada transaksi.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🗑️ *Hapus Transaksi*\nPilih:",
        reply_markup=_tx_inline_keyboard(txs),
        parse_mode="Markdown",
    )
    return HAPUS_KONFIRMASI


async def hapus_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END
    tx_id = query.data.split(":")[1]
    tg_user = update.effective_user
    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, uuid.UUID(tx_id))
        if not tx or tx.is_deleted:
            await query.edit_message_text("❌ Tidak ditemukan.")
            return ConversationHandler.END
        from datetime import datetime
        tx.is_deleted = True
        tx.deleted_at = datetime.utcnow()
        await log_delete(session, tg_user.id, tx)
        await session.commit()
        saldo = await get_running_balance(session)
    await query.edit_message_text(
        f"🗑️ Dihapus.\n\n💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── Cancel ──

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Dibatalkan.")
    return ConversationHandler.END


# ── Builders ──

def build_transaction_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("masuk", cmd_masuk),
            CommandHandler("keluar", cmd_keluar),
        ],
        states={
            TX_NOMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nominal)],
            TX_KETERANGAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keterangan)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
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
