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
) = range(7)


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
    context.user_data["tx_desc"] = desc_text
    await update.message.reply_text("Tanggal? (kosongkan jika hari ini, format: DD/MM/YYYY)")
    return TX_TANGGAL


async def handle_tanggal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("-", "skip", ""):
        tx_date = date.today()
    else:
        tx_date = parse_date(text)
        if not tx_date:
            await update.message.reply_text("❌ Format tanggal tidak dikenali. Contoh: 15/07/2026 atau kosongkan.")
            return TX_TANGGAL

    context.user_data["tx_date"] = tx_date

    # Simpan transaksi
    tg_user = update.effective_user
    tx_type = context.user_data["tx_type"]
    amount = context.user_data["tx_amount"]
    desc_text = context.user_data["tx_desc"]

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
    await update.message.reply_text(
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

    async with AsyncSessionLocal() as session:
        summary = await get_summary(session, user_id=user_id)

    await update.message.reply_text(
        f"💰 *Saldo saat ini:*\n*{fmt_rupiah(summary['saldo'])}*\n\n"
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

    user_id = update.effective_user.id
    today = _date.today()
    date_from = today - timedelta(days=6)  # 7 hari terakhir

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(
                Transaction.is_deleted == False,
                Transaction.user_id == user_id,
                Transaction.transaction_date >= date_from,
                Transaction.transaction_date <= today,
            )
            .order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
        )
        txs = result.scalars().all()

    if not txs:
        await update.message.reply_text(
            f"Belum ada transaksi dalam 7 hari terakhir\n"
            f"({fmt_date(date_from)} — {fmt_date(today)})."
        )
        return

    by_date = defaultdict(list)
    for tx in txs:
        by_date[tx.transaction_date].append(tx)

    total_masuk = sum(tx.amount for tx in txs if tx.type == "masuk")
    total_keluar = sum(tx.amount for tx in txs if tx.type == "keluar")

    lines = [
        f"📋 *Riwayat 7 Hari Terakhir*",
        f"_{fmt_date(date_from)} — {fmt_date(today)}_\n",
        f"Masuk: *{fmt_rupiah(total_masuk)}* | Keluar: *{fmt_rupiah(total_keluar)}*",
        "─────────────────",
    ]

    for d in sorted(by_date.keys(), reverse=True):
        lines.append(f"\n*{fmt_date(d)}*")
        for tx in by_date[d]:
            sign = "+" if tx.type == "masuk" else "-"
            lines.append(f"  {sign} {fmt_rupiah(tx.amount)}\n   _{tx.description}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
            .order_by(desc(Transaction.transaction_date))
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

    if query.data == "cancel":
        await query.edit_message_text("❌ Edit dibatalkan.")
        return ConversationHandler.END

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

        saldo = await get_running_balance(session2, user_id=user_id)

    await update.message.reply_text(
        f"✅ Transaksi berhasil diperbarui.\n\n💰 Saldo saat ini: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    _p = {k: context.user_data[k] for k in ("session_verified","db_user") if k in context.user_data}
    context.user_data.clear()
    context.user_data.update(_p)
    return ConversationHandler.END


# ──────────────────────────────────────────────
# /hapus
# ──────────────────────────────────────────────

async def cmd_hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END

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

    if query.data == "cancel":
        await query.edit_message_text("❌ Penghapusan dibatalkan.")
        return ConversationHandler.END

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

        saldo = await get_running_balance(session2, user_id=user_id)

    await query.edit_message_text(
        f"🗑️ Transaksi berhasil dihapus.\n\n💰 Saldo saat ini: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


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

def build_transaction_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("masuk", cmd_masuk),
            CommandHandler("keluar", cmd_keluar),
        ],
        states={
            TX_NOMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nominal)],
            TX_KETERANGAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keterangan)],
            TX_TANGGAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tanggal)],
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
