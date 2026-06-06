"""
Transaction handlers — diperbaiki:
- Hapus state TX_TANGGAL (tanggal = hari ini)
- allow_reentry=True untuk semua conversation
- Guard pada semua state
- State numbers tidak overlap
"""
from datetime import date
import uuid

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from sqlalchemy import select, desc

from bot.database import AsyncSessionLocal
from bot.models import Transaction
from bot.services.balance import get_running_balance, get_summary
from bot.services.audit import log_create, log_update, log_delete
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount, parse_date
from bot.handlers.auth import ensure_registered

# State numbers — tidak overlap dengan OCR (0,1) atau belanja (20,21)
TX_NOMINAL, TX_KETERANGAN = 10, 11
EDIT_PILIH, EDIT_FIELD, EDIT_NILAI = 12, 13, 14
HAPUS_KONFIRMASI = 15
BELANJA_ITEM, BELANJA_KETERANGAN = 20, 21


def _tx_emoji(t: str) -> str:
    return "➕" if t == "masuk" else "➖"


async def _recent_transactions(session, limit: int = 10):
    result = await session.execute(
        select(Transaction)
        .where(Transaction.is_deleted == False)
        .order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
        .limit(limit)
    )
    return result.scalars().all()


def _tx_keyboard(transactions) -> InlineKeyboardMarkup:
    buttons = []
    for tx in transactions:
        label = f"{fmt_date(tx.transaction_date)} | {fmt_rupiah(tx.amount)} | {tx.description[:20]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"tx:{tx.id}")])
    buttons.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ── /masuk /keluar ──

async def cmd_masuk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["tx_type"] = "masuk"
    await update.message.reply_text("💰 *Catat Pemasukan*\n\nNominal?", parse_mode="Markdown")
    return TX_NOMINAL


async def cmd_keluar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["tx_type"] = "keluar"
    await update.message.reply_text("💸 *Catat Pengeluaran*\n\nNominal?", parse_mode="Markdown")
    return TX_NOMINAL


async def handle_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text.strip())
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Nominal tidak valid.\nContoh: 150000, 150rb, 1.5jt")
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
    tx_type = context.user_data.get("tx_type", "keluar")
    amount = context.user_data.get("tx_amount", 0)

    if not amount:
        await update.message.reply_text("❌ Sesi berakhir. Mulai ulang dengan /masuk atau /keluar.")
        context.user_data.clear()
        return ConversationHandler.END

    async with AsyncSessionLocal() as session:
        tx = Transaction(
            user_id=tg_user.id,
            type=tx_type,
            amount=amount,
            description=desc_text,
            transaction_date=date.today(),
        )
        session.add(tx)
        await session.flush()
        await log_create(session, tg_user.id, tx)
        await session.commit()

    async with AsyncSessionLocal() as session:
        saldo = await get_running_balance(session)

    await update.message.reply_text(
        f"✅ *Tersimpan*\n\n"
        f"Jenis: {_tx_emoji(tx_type)} {'MASUK' if tx_type == 'masuk' else 'KELUAR'}\n"
        f"Nominal: *{fmt_rupiah(amount)}*\n"
        f"Keterangan: {desc_text}\n"
        f"Tanggal: {fmt_date(date.today())}\n\n"
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
            .offset(offset).limit(PAGE_SIZE + 1)
        )
        txs = result.scalars().all()

    if not txs:
        await update.message.reply_text("Belum ada transaksi.")
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
        lines.append(f"\n/riwayat {page + 1} → berikutnya")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /cari ──

async def cmd_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return
    keyword = " ".join(context.args).strip() if context.args else ""
    if not keyword:
        await update.message.reply_text("Gunakan: /cari <kata kunci>")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.is_deleted == False, Transaction.description.ilike(f"%{keyword}%"))
            .order_by(desc(Transaction.transaction_date)).limit(20)
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
        await update.message.reply_text("Tidak ada transaksi.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "✏️ *Edit Transaksi*\nPilih:",
        reply_markup=_tx_keyboard(txs), parse_mode="Markdown",
    )
    return EDIT_PILIH


async def edit_pilih_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        return ConversationHandler.END

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
    try:
        await query.answer()
    except Exception:
        return ConversationHandler.END

    if query.data == "cancel":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    field = query.data.split(":")[1]
    context.user_data["edit_field"] = field
    prompts = {
        "amount": "Masukkan nominal baru:",
        "description": "Masukkan keterangan baru:",
        "date": "Masukkan tanggal (DD/MM/YYYY):",
        "type": "Ketik `masuk` atau `keluar`:",
    }
    await query.edit_message_text(prompts[field], parse_mode="Markdown")
    return EDIT_NILAI


async def edit_input_nilai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    tx_id = context.user_data.get("edit_tx_id")

    if not field or not tx_id:
        await update.message.reply_text("❌ Sesi berakhir. Mulai ulang dengan /edit.")
        context.user_data.clear()
        return ConversationHandler.END

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
            if not new_amount or new_amount <= 0:
                await update.message.reply_text("❌ Nominal tidak valid.")
                return EDIT_NILAI
            tx.amount = new_amount
        elif field == "description":
            if not value:
                await update.message.reply_text("❌ Keterangan tidak boleh kosong.")
                return EDIT_NILAI
            tx.description = value
        elif field == "date":
            new_date = parse_date(value)
            if not new_date:
                await update.message.reply_text("❌ Format tanggal tidak valid.\nContoh: 15/06/2026")
                return EDIT_NILAI
            tx.transaction_date = new_date
        elif field == "type":
            if value.lower() not in ("masuk", "keluar"):
                await update.message.reply_text("❌ Ketik `masuk` atau `keluar`.", parse_mode="Markdown")
                return EDIT_NILAI
            tx.type = value.lower()

        await log_update(session, tg_user.id, old_vals, tx)
        await session.commit()

    async with AsyncSessionLocal() as session:
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
    context.user_data.clear()
    await update.message.reply_text(
        "🗑️ *Hapus Transaksi*\nPilih:",
        reply_markup=_tx_keyboard(txs), parse_mode="Markdown",
    )
    return HAPUS_KONFIRMASI


async def hapus_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        return ConversationHandler.END

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

    async with AsyncSessionLocal() as session:
        saldo = await get_running_balance(session)

    await query.edit_message_text(
        f"🗑️ Dihapus.\n\n💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── /belanja ──

async def cmd_belanja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["belanja_items"] = []
    await update.message.reply_text(
        "🛒 *Catat Belanja*\n\n"
        "Masukkan item satu per satu.\n"
        "Format: `nama item harga`\n"
        "Contoh: `ayam slice 45000`\n\n"
        "Ketik *selesai* jika sudah.",
        parse_mode="Markdown"
    )
    return BELANJA_ITEM


async def belanja_input_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() in ("selesai", "done", "ok", "finish"):
        items = context.user_data.get("belanja_items", [])
        if not items:
            await update.message.reply_text("❌ Belum ada item. Masukkan minimal 1 item.")
            return BELANJA_ITEM

        total = sum(i["amount"] for i in items)
        lines = ["📋 *Ringkasan Belanja:*"]
        for i in items:
            lines.append(f"  - {i['name']}: {fmt_rupiah(i['amount'])}")
        lines.append("─" * 20)
        lines.append(f"*Total: {fmt_rupiah(total)}*")
        lines.append("\nKeterangan? (nama toko/keperluan)")

        context.user_data["belanja_total"] = total
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return BELANJA_KETERANGAN

    # Parse "nama item harga" — angka di akhir adalah harga
    parts = text.rsplit(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "❌ Format: `nama item harga`\nContoh: `ayam slice 45000`",
            parse_mode="Markdown"
        )
        return BELANJA_ITEM

    amount = parse_amount(parts[1])
    if not amount or amount <= 0:
        await update.message.reply_text(
            "❌ Harga tidak valid.\nContoh: `ayam slice 45000`",
            parse_mode="Markdown"
        )
        return BELANJA_ITEM

    name = parts[0].strip().title()
    context.user_data["belanja_items"].append({"name": name, "amount": amount})
    total_sejauh_ini = sum(i["amount"] for i in context.user_data["belanja_items"])

    await update.message.reply_text(
        f"✅ *{name}* — {fmt_rupiah(amount)}\n"
        f"_Total sejauh ini: {fmt_rupiah(total_sejauh_ini)}_\n\n"
        f"Lanjut atau ketik *selesai*.",
        parse_mode="Markdown"
    )
    return BELANJA_ITEM


async def belanja_keterangan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc_text = update.message.text.strip()
    tg_user = update.effective_user
    total = context.user_data.get("belanja_total", 0)
    items = context.user_data.get("belanja_items", [])

    if not total or not items:
        await update.message.reply_text("❌ Sesi berakhir. Mulai ulang dengan /belanja.")
        context.user_data.clear()
        return ConversationHandler.END

    item_list = ", ".join(f"{i['name']} {fmt_rupiah(i['amount'])}" for i in items)
    full_desc = f"{desc_text} ({item_list})" if desc_text else item_list

    async with AsyncSessionLocal() as session:
        tx = Transaction(
            user_id=tg_user.id,
            type="keluar",
            amount=total,
            description=full_desc[:200],
            transaction_date=date.today(),
        )
        session.add(tx)
        await session.flush()
        await log_create(session, tg_user.id, tx)
        await session.commit()

    async with AsyncSessionLocal() as session:
        saldo = await get_running_balance(session)

    await update.message.reply_text(
        f"✅ *Tersimpan*\n\n"
        f"Jenis: ➖ KELUAR\n"
        f"Total: *{fmt_rupiah(total)}*\n"
        f"Keterangan: {desc_text}\n"
        f"Tanggal: {fmt_date(date.today())}\n\n"
        f"💰 Saldo: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown"
    )
    context.user_data.clear()
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
        allow_reentry=True,
        conversation_timeout=300,
    )


def build_hapus_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("hapus", cmd_hapus)],
        states={
            HAPUS_KONFIRMASI: [CallbackQueryHandler(hapus_konfirmasi)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel), CallbackQueryHandler(cmd_cancel, pattern="^cancel$")],
        allow_reentry=True,
        conversation_timeout=300,
    )


def build_belanja_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("belanja", cmd_belanja)],
        states={
            BELANJA_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, belanja_input_item)],
            BELANJA_KETERANGAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, belanja_keterangan)],
        },
        fallbacks=[CommandHandler("batal", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )
