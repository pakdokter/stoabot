"""
Admin Audit Handler — edit, hapus, dan tambah transaksi milik user lain.

Hanya bisa diakses oleh TELEGRAM_ADMIN_IDS.

Alur:
  /audit
  → Pilih user
  → Pilih aksi: Edit / Hapus / Tambah
  → Edit/Hapus: pilih transaksi dari riwayat user
  → Tambah: isi nominal, keterangan, tanggal
"""

import uuid
from datetime import date, datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import select
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)

from bot.config import settings
from bot.database import AsyncSessionLocal
from bot.models import Transaction, User
from bot.services.balance import get_running_balance
from bot.services.audit import log_create, log_update, log_delete, _tx_to_dict
from bot.utils.formatters import fmt_rupiah, fmt_date, parse_amount, parse_date

# States
(
    AUDIT_PILIH_USER,
    AUDIT_PILIH_AKSI,
    AUDIT_PILIH_TX,
    AUDIT_PILIH_FIELD,
    AUDIT_INPUT_NILAI,
    AUDIT_TAMBAH_NOMINAL,
    AUDIT_TAMBAH_KETERANGAN,
    AUDIT_TAMBAH_TANGGAL,
    AUDIT_KONFIRM_HAPUS,
) = range(20, 29)


def _esc(t: str) -> str:
    return str(t).replace('\t', ' ').replace('_', r'\_').replace('*', r'\*').replace('[', r'\[')


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


# ── Entry ─────────────────────────────────────────────────────────────────────

async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/audit — entry point, hanya untuk admin."""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Perintah ini hanya untuk admin.")
        return ConversationHandler.END

    # Daftar semua user aktif
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.is_active == True).order_by(User.full_name)
        )
        users = result.scalars().all()

    if not users:
        await update.message.reply_text("Tidak ada user terdaftar.")
        return ConversationHandler.END

    rows = [[InlineKeyboardButton(
        f"{'👑' if u.id in settings.admin_ids else '🧑'} {u.full_name}",
        callback_data=f"aud_user:{u.id}"
    )] for u in users]
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="aud:batal")])

    await update.message.reply_text(
        "🔧 *Admin Audit*\n\nPilih user yang ingin dikoreksi:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AUDIT_PILIH_USER


# ── Pilih User ────────────────────────────────────────────────────────────────

async def audit_pilih_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "aud:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    target_id = int(query.data.split(":")[1])
    context.user_data["audit_target_id"] = target_id

    async with AsyncSessionLocal() as session:
        u = await session.get(User, target_id)
        target_name = u.full_name if u else str(target_id)
    context.user_data["audit_target_name"] = target_name

    # Hitung saldo untuk konteks
    async with AsyncSessionLocal() as session:
        saldo = await get_running_balance(session, target_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit transaksi", callback_data="aud_aksi:edit")],
        [InlineKeyboardButton("🗑️ Hapus transaksi", callback_data="aud_aksi:hapus")],
        [InlineKeyboardButton("➕ Tambah transaksi", callback_data="aud_aksi:tambah")],
        [InlineKeyboardButton("◀ Pilih user lain", callback_data="aud_aksi:back")],
        [InlineKeyboardButton("❌ Batal", callback_data="aud:batal")],
    ])

    await query.edit_message_text(
        f"🔧 *Audit: {_esc(target_name)}*\n"
        f"💰 Saldo saat ini: *{fmt_rupiah(saldo)}*\n\n"
        f"Pilih aksi:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return AUDIT_PILIH_AKSI


# ── Pilih Aksi ────────────────────────────────────────────────────────────────

async def audit_pilih_aksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "aud:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    if data == "aud_aksi:back":
        return await cmd_audit_from_query(query, context)

    aksi = data.split(":")[1]
    context.user_data["audit_aksi"] = aksi
    target_id = context.user_data["audit_target_id"]
    target_name = context.user_data["audit_target_name"]

    if aksi == "tambah":
        await query.edit_message_text(
            f"➕ *Tambah Transaksi — {_esc(target_name)}*\n\n"
            f"Jenis? Ketik `masuk` atau `keluar`:",
            parse_mode="Markdown",
        )
        return AUDIT_TAMBAH_NOMINAL

    # Edit atau hapus: tampilkan 10 transaksi terakhir user
    return await _show_user_transactions(query, context, target_id, target_name, aksi)


async def cmd_audit_from_query(query, context):
    """Re-show user list dari callback."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.is_active == True).order_by(User.full_name)
        )
        users = result.scalars().all()

    rows = [[InlineKeyboardButton(
        f"{'👑' if u.id in settings.admin_ids else '🧑'} {u.full_name}",
        callback_data=f"aud_user:{u.id}"
    )] for u in users]
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="aud:batal")])

    await query.edit_message_text(
        "🔧 *Admin Audit*\n\nPilih user:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AUDIT_PILIH_USER


async def _show_user_transactions(query, context, target_id, target_name, aksi, page=0):
    """Tampilkan daftar transaksi user untuk dipilih."""
    LIMIT = 8
    OFFSET = page * LIMIT

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.user_id == target_id, Transaction.is_deleted == False)
            .order_by(Transaction.transaction_date.desc(), Transaction.created_at.desc())
            .offset(OFFSET).limit(LIMIT)
        )
        txs = result.scalars().all()

    if not txs:
        await query.edit_message_text(
            f"📭 *{_esc(target_name)}* tidak punya transaksi.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    rows = []
    for tx in txs:
        sym = "➕" if tx.type == "masuk" else "➖"
        desc_short = (tx.description or "")[:28]
        label = f"{sym} {fmt_date(tx.transaction_date)} {desc_short} {fmt_rupiah(tx.amount)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"aud_tx:{tx.id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Sebelumnya", callback_data=f"aud_page:{page-1}"))
    if len(txs) == LIMIT:
        nav.append(InlineKeyboardButton("Berikutnya ▶", callback_data=f"aud_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="aud:batal")])

    aksi_label = "diedit" if aksi == "edit" else "dihapus"
    await query.edit_message_text(
        f"🔧 *{_esc(target_name)}* — pilih transaksi yang ingin {aksi_label}:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AUDIT_PILIH_TX


# ── Pilih Transaksi ───────────────────────────────────────────────────────────

async def audit_pilih_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "aud:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    # Pagination
    if data.startswith("aud_page:"):
        page = int(data.split(":")[1])
        target_id = context.user_data["audit_target_id"]
        target_name = context.user_data["audit_target_name"]
        aksi = context.user_data["audit_aksi"]
        return await _show_user_transactions(query, context, target_id, target_name, aksi, page)

    tx_id = data.split(":")[1]
    context.user_data["audit_tx_id"] = tx_id
    aksi = context.user_data["audit_aksi"]
    target_name = context.user_data["audit_target_name"]

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, uuid.UUID(tx_id))
        if not tx:
            await query.edit_message_text("❌ Transaksi tidak ditemukan.")
            return ConversationHandler.END

    tx_info = (
        f"📋 *Transaksi*\n"
        f"User    : {_esc(target_name)}\n"
        f"Tanggal : {fmt_date(tx.transaction_date)}\n"
        f"Jenis   : {'MASUK' if tx.type == 'masuk' else 'KELUAR'}\n"
        f"Nominal : *{fmt_rupiah(tx.amount)}*\n"
        f"Ket.    : {_esc(tx.description or '-')}"
    )

    if aksi == "hapus":
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑️ Ya, hapus", callback_data="aud_konfirm:hapus"),
            InlineKeyboardButton("❌ Batal", callback_data="aud:batal"),
        ]])
        await query.edit_message_text(
            tx_info + "\n\n⚠️ *Yakin ingin menghapus transaksi ini?*",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return AUDIT_KONFIRM_HAPUS

    # Edit: pilih field
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Nominal", callback_data="aud_field:amount"),
         InlineKeyboardButton("📅 Tanggal", callback_data="aud_field:date")],
        [InlineKeyboardButton("📝 Keterangan", callback_data="aud_field:description"),
         InlineKeyboardButton("🔄 Jenis", callback_data="aud_field:type")],
        [InlineKeyboardButton("❌ Batal", callback_data="aud:batal")],
    ])
    await query.edit_message_text(
        tx_info + "\n\n*Edit bagian mana?*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return AUDIT_PILIH_FIELD


# ── Edit Field ────────────────────────────────────────────────────────────────

async def audit_pilih_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "aud:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    field = query.data.split(":")[1]
    context.user_data["audit_field"] = field

    prompts = {
        "amount": "Masukkan nominal baru (contoh: 150000, 150rb):",
        "date": "Masukkan tanggal baru (DD/MM/YYYY):",
        "description": "Masukkan keterangan baru:",
        "type": "Ketik `masuk` atau `keluar`:",
    }
    await query.edit_message_text(prompts[field], parse_mode="Markdown")
    return AUDIT_INPUT_NILAI


async def audit_input_nilai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("audit_field")
    tx_id = context.user_data.get("audit_tx_id")
    target_name = context.user_data.get("audit_target_name", "user")
    value = update.message.text.strip()

    if not field or not tx_id:
        await update.message.reply_text("⏰ Sesi habis. Gunakan /audit lagi.")
        return ConversationHandler.END

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, uuid.UUID(tx_id))
        if not tx:
            await update.message.reply_text("❌ Transaksi tidak ditemukan.")
            return ConversationHandler.END

        old_vals = _tx_to_dict(tx)

        if field == "amount":
            new_val = parse_amount(value)
            if not new_val:
                await update.message.reply_text("❌ Nominal tidak valid.")
                return AUDIT_INPUT_NILAI
            tx.amount = new_val
        elif field == "date":
            new_date = parse_date(value)
            if not new_date:
                await update.message.reply_text("❌ Format tanggal tidak valid. Contoh: 15/07/2026")
                return AUDIT_INPUT_NILAI
            tx.transaction_date = new_date
        elif field == "description":
            tx.description = value[:200]
        elif field == "type":
            if value.lower() not in ("masuk", "keluar"):
                await update.message.reply_text("❌ Ketik `masuk` atau `keluar`.", parse_mode="Markdown")
                return AUDIT_INPUT_NILAI
            tx.type = value.lower()

        await log_update(session, update.effective_user.id, old_vals, tx)
        await session.commit()

        saldo = await get_running_balance(session, tx.user_id)

    await update.message.reply_text(
        f"✅ *Transaksi {_esc(target_name)} berhasil diperbarui*\n"
        f"💰 Saldo {_esc(target_name)}: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── Hapus ─────────────────────────────────────────────────────────────────────

async def audit_konfirm_hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "aud:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    tx_id = context.user_data.get("audit_tx_id")
    target_name = context.user_data.get("audit_target_name", "user")

    async with AsyncSessionLocal() as session:
        tx = await session.get(Transaction, uuid.UUID(tx_id))
        if not tx:
            await query.edit_message_text("❌ Transaksi tidak ditemukan.")
            return ConversationHandler.END

        tx.is_deleted = True
        tx.deleted_at = datetime.now(timezone.utc)
        await log_delete(session, update.effective_user.id, tx)
        await session.commit()
        saldo = await get_running_balance(session, tx.user_id)

    await query.edit_message_text(
        f"🗑️ *Transaksi {_esc(target_name)} berhasil dihapus*\n"
        f"💰 Saldo {_esc(target_name)}: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── Tambah Transaksi ──────────────────────────────────────────────────────────

async def audit_tambah_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """State pertama tambah: terima jenis transaksi, lalu minta nominal."""
    text = update.message.text.strip().lower()

    if text not in ("masuk", "keluar"):
        await update.message.reply_text("❌ Ketik `masuk` atau `keluar`.", parse_mode="Markdown")
        return AUDIT_TAMBAH_NOMINAL

    context.user_data["audit_tambah_type"] = text
    await update.message.reply_text(
        f"Nominal (contoh: 150000, 150rb, 1.5jt):"
    )
    return AUDIT_TAMBAH_KETERANGAN


async def audit_tambah_keterangan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima nominal, minta keterangan."""
    value = update.message.text.strip()
    amount = parse_amount(value)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Nominal tidak valid.")
        return AUDIT_TAMBAH_KETERANGAN

    context.user_data["audit_tambah_amount"] = amount
    await update.message.reply_text("Keterangan / nama toko:")
    return AUDIT_TAMBAH_TANGGAL


async def audit_tambah_tanggal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima keterangan, minta tanggal."""
    context.user_data["audit_tambah_desc"] = update.message.text.strip()[:200]
    await update.message.reply_text(
        "Tanggal transaksi (DD/MM/YYYY):\n"
        "_(kosongkan untuk hari ini)_",
        parse_mode="Markdown",
    )
    return AUDIT_PILIH_AKSI  # reuse state untuk simpan


async def audit_tambah_simpan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima tanggal dan simpan transaksi baru."""
    text = update.message.text.strip()
    if text in ("-", "", "skip"):
        tx_date = date.today()
    else:
        tx_date = parse_date(text)
        if not tx_date:
            await update.message.reply_text("❌ Format tidak valid. Contoh: 15/07/2026 atau `-` untuk hari ini.")
            return AUDIT_PILIH_AKSI

    target_id = context.user_data.get("audit_target_id")
    target_name = context.user_data.get("audit_target_name", "user")
    tx_type = context.user_data.get("audit_tambah_type", "keluar")
    amount = context.user_data.get("audit_tambah_amount", 0)
    desc = context.user_data.get("audit_tambah_desc", "")

    async with AsyncSessionLocal() as session:
        tx = Transaction(
            user_id=target_id,
            type=tx_type,
            amount=amount,
            description=desc,
            transaction_date=tx_date,
        )
        session.add(tx)
        await session.flush()
        await log_create(session, update.effective_user.id, tx)
        await session.commit()
        saldo = await get_running_balance(session, target_id)

    sym = "➕" if tx_type == "masuk" else "➖"
    await update.message.reply_text(
        f"✅ *Transaksi berhasil ditambahkan ke {_esc(target_name)}*\n\n"
        f"{sym} {tx_type.upper()} — *{fmt_rupiah(amount)}*\n"
        f"Ket. : {_esc(desc)}\n"
        f"Tgl. : {fmt_date(tx_date)}\n\n"
        f"💰 Saldo {_esc(target_name)}: *{fmt_rupiah(saldo)}*",
        parse_mode="Markdown",
    )
    logger.info(f"[AUDIT] admin {update.effective_user.id} added tx for {target_id}: {tx_type} {amount} '{desc}'")
    return ConversationHandler.END


# ── ConversationHandler ───────────────────────────────────────────────────────

def build_audit_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("audit", cmd_audit)],
        states={
            AUDIT_PILIH_USER: [
                CallbackQueryHandler(audit_pilih_user, pattern="^aud_user:|^aud:"),
            ],
            AUDIT_PILIH_AKSI: [
                CallbackQueryHandler(audit_pilih_aksi, pattern="^aud_aksi:|^aud:"),
                # State ini dipakai juga untuk terima tanggal tambah
                MessageHandler(filters.TEXT & ~filters.COMMAND, audit_tambah_simpan),
            ],
            AUDIT_PILIH_TX: [
                CallbackQueryHandler(audit_pilih_tx, pattern="^aud_tx:|^aud_page:|^aud:"),
            ],
            AUDIT_PILIH_FIELD: [
                CallbackQueryHandler(audit_pilih_field, pattern="^aud_field:|^aud:"),
            ],
            AUDIT_INPUT_NILAI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, audit_input_nilai),
            ],
            AUDIT_TAMBAH_NOMINAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, audit_tambah_nominal),
            ],
            AUDIT_TAMBAH_KETERANGAN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, audit_tambah_keterangan),
            ],
            AUDIT_TAMBAH_TANGGAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, audit_tambah_tanggal),
            ],
            AUDIT_KONFIRM_HAPUS: [
                CallbackQueryHandler(audit_konfirm_hapus, pattern="^aud_konfirm:|^aud:"),
            ],
        },
        fallbacks=[CommandHandler("batal", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
        conversation_timeout=300,
    )
