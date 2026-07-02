"""
Handler fitur Pasar — catat belanja pasar per item dengan tabel interaktif.

Alur:
  /keluar → pilih "🏪 Pasar" → tabel isian (Nama Item | Qty | Harga)
  → submit → bot simpan per item, update katalog market_items

Katalog item (market_items) diupdate otomatis setiap transaksi pasar baru,
sehingga autocomplete makin lengkap seiring waktu.
"""
import json
import re
from datetime import date

from loguru import logger
from sqlalchemy import select, func
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import ContextTypes, ConversationHandler

from bot.database import AsyncSessionLocal
from bot.models import Transaction, MarketItem, User
from bot.services.balance import get_running_balance
from bot.services.sheets import append_transaction as sheets_append
from bot.services.audit import log_create
from bot.handlers.auth import ensure_registered
from bot.utils.formatters import fmt_rupiah, fmt_date


# ── Toko yang sering dipakai ──────────────────────────────────────────────────
TOKO_FAVORIT = [
    ("🥦 Pasar",        "pasar"),
    ("🏪 Primer Raya",  "Primer Raya"),
    ("🏪 Indomaret",    "Indomaret"),
    ("🏪 Alfamart",     "Alfamart"),
    ("🥩 Dinda Frozen", "Dinda Frozen Food"),
    ("🧁 Amanah",       "Amanah"),
    ("🐟 Fadhilah",     "Fadhilah"),
    ("🏬 MR D.I.Y.",    "MR D.I.Y."),
    ("📦 Dineta",       "PT Dineta Jaya"),
    ("🛒 Lainnya",      "lainnya"),
]

# States untuk ConversationHandler
PASAR_TOKO      = "pasar_toko"
PASAR_TABEL     = "pasar_tabel"
PASAR_MANUAL    = "pasar_manual"    # input nominal untuk toko biasa
PASAR_KONFIRM   = "pasar_konfirm"
PASAR_NAMA_TOKO = "pasar_nama_toko" # input nama toko manual (pilih Lainnya)


def _esc(text: str) -> str:
    return str(text).replace('_', r'\_').replace('*', r'\*').replace('`', r'\`').replace('[', r'\[')


# ── Entry point: cmd_keluar mengirim keyboard toko ─────────────────────────────
async def show_toko_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan keyboard pilihan toko saat /keluar dipanggil."""
    rows = []
    row = []
    for label, key in TOKO_FAVORIT:
        row.append(InlineKeyboardButton(label, callback_data=f"toko:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await update.message.reply_text(
        "💸 *Catat Pengeluaran*\n\nPilih toko:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return PASAR_TOKO


async def handle_toko_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses pilihan toko dari keyboard."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "toko:pasar" atau "toko:Indomaret" dll
    if not data.startswith("toko:"):
        return PASAR_TOKO

    toko = data[5:]
    context.user_data["pasar_toko"] = toko

    if toko == "pasar":
        return await show_pasar_table(update, context)
    elif toko == "lainnya":
        await query.edit_message_text("Ketik nama toko:")
        return PASAR_NAMA_TOKO
    else:
        # Toko dari daftar → langsung tampilkan form item
        context.user_data["pasar_toko"] = toko
        return await show_toko_item_form(query, context, toko)


async def show_pasar_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tampilkan tabel isian pasar.
    Strategi: kirim tabel sebagai pesan teks dengan format yang bisa diketik balik.
    Format input: "nama item, qty, harga" per baris.
    """
    query = update.callback_query

    # Ambil katalog item terakhir dipakai
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MarketItem)
            .order_by(MarketItem.use_count.desc(), MarketItem.last_used.desc())
            .limit(20)
        )
        catalog = result.scalars().all()

    # Format katalog sebagai referensi
    catalog_text = ""
    if catalog:
        catalog_text = "\n\n📋 *Item yang pernah dibeli:*\n"
        for item in catalog:
            price_hint = f" (~{fmt_rupiah(item.last_price)})" if item.last_price else ""
            unit_hint = f" /{item.unit}" if item.unit else ""
            catalog_text += f"  `{item.name}`{unit_hint}{price_hint}\n"

    msg = (
        "🏪 *Belanja Pasar*\n\n"
        "Ketik item belanja, satu baris per item:\n"
        "`nama item, qty, harga`\n\n"
        "*Contoh:*\n"
        "`tomat, 1 kg, 8000`\n"
        "`bawang merah, 500 gr, 15000`\n"
        "`telur, 1 pcs, 28000`\n"
        "`bayam, 2 ikat, 4000`"
        f"{catalog_text}\n"
        "Kirim semua sekaligus, lalu bot akan rekap."
    )

    if query:
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

    return PASAR_TABEL


async def handle_pasar_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse input tabel pasar dari teks user."""
    # Guard: bisa dipanggil via callback query (tidak ada message)
    if not update.message or not update.message.text:
        return PASAR_TABEL
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Input kosong. Ketik ulang.")
        return PASAR_TABEL

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    items = []
    errors = []

    for i, line in enumerate(lines, 1):
        parsed = _parse_pasar_line(line)
        if parsed:
            items.append(parsed)
        else:
            errors.append(f"Baris {i}: `{_esc(line)}`")

    if not items:
        await update.message.reply_text(
            "❌ Tidak ada item yang terbaca.\n"
            "Format: `nama item, qty, harga`\n"
            "Contoh: `tomat, 1 kg, 8000`",
            parse_mode="Markdown",
        )
        return PASAR_TABEL

    # Simpan ke context dan tampilkan preview
    context.user_data["pasar_items"] = items
    total = sum(i["total"] for i in items)

    toko = context.user_data.get("pasar_toko", "Pasar")
    is_pasar = toko == "pasar"
    toko_label = "Pasar" if is_pasar else toko

    preview_lines = [f"📋 *Rekap {_esc(toko_label)}*\n"]
    for item in items:
        qty_str = f"{item['qty']} {item['unit']}".strip()
        if qty_str and qty_str not in ("1.0", "1"):
            qty_str = f" ({qty_str})"
        else:
            qty_str = ""
        preview_lines.append(
            f"  • {_esc(item['name'])}{qty_str} — *{fmt_rupiah(item['total'])}*"
        )
    preview_lines.append(f"\n💰 *Total: {fmt_rupiah(total)}*")

    if errors:
        preview_lines.append(f"\n⚠️ Tidak terbaca ({len(errors)} baris):")
        for e in errors[:3]:
            preview_lines.append(f"  {e}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="pasar:simpan"),
        InlineKeyboardButton("✏️ Edit ulang", callback_data="pasar:edit"),
        InlineKeyboardButton("❌ Batal", callback_data="pasar:batal"),
    ]])

    await update.message.reply_text(
        "\n".join(preview_lines),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return PASAR_KONFIRM


def _parse_pasar_line(line: str) -> dict | None:
    """
    Parse satu baris input pasar.
    Format fleksibel: "nama, qty unit, harga" atau "nama, qty, harga"
    Contoh valid:
      "tomat, 1 kg, 8000"
      "bawang 500gr 15000"
      "telur 1 pcs 28rb"
      "bayam 2 ikat 4000"
    """
    # Coba split dengan koma dulu
    if "," in line:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            name = parts[0]
            qty_unit = parts[1]
            harga_str = parts[2]
        elif len(parts) == 2:
            name = parts[0]
            qty_unit = "1"
            harga_str = parts[1]
        else:
            return None
    else:
        # Tidak ada koma — coba deteksi harga di akhir
        # "tomat 1 kg 8000" atau "tomat 8000"
        tokens = line.split()
        if len(tokens) < 2:
            return None
        # Harga = token terakhir yang bisa jadi angka
        harga_str = tokens[-1]
        remaining = tokens[:-1]
        name = " ".join(remaining)
        qty_unit = ""

    # Parse harga
    harga = _parse_harga(harga_str)
    if not harga or harga <= 0:
        return None

    # Parse qty dan unit dari qty_unit
    qty, unit = _parse_qty_unit(qty_unit)

    # Nama minimal 2 karakter
    name = name.strip()
    if len(name) < 2:
        return None

    return {
        "name": name.title(),
        "qty": qty,
        "unit": unit,
        "unit_price": harga / qty if qty else harga,
        "total": harga,
    }


def _parse_harga(s: str) -> float:
    """Parse string harga ke float. Mendukung 8000, 8rb, 8.000, 8k."""
    s = s.strip().lower()
    s = s.replace("rp", "").replace(".", "").replace(",", "").strip()
    multiplier = 1
    if s.endswith("rb") or s.endswith("ribu"):
        multiplier = 1000
        s = re.sub(r"(rb|ribu)$", "", s).strip()
    elif s.endswith("jt") or s.endswith("juta"):
        multiplier = 1_000_000
        s = re.sub(r"(jt|juta)$", "", s).strip()
    elif s.endswith("k"):
        multiplier = 1000
        s = s[:-1].strip()
    try:
        return float(s) * multiplier
    except ValueError:
        return 0.0


def _parse_qty_unit(s: str) -> tuple[float, str]:
    """Parse qty dan unit. Contoh: '1 kg', '500 gr', '2 ikat', '3'."""
    s = s.strip()
    if not s or s == "1":
        return 1.0, ""
    m = re.match(r"^([\d.,]+)\s*([a-zA-Z]*)", s)
    if m:
        try:
            qty = float(m.group(1).replace(",", "."))
        except ValueError:
            qty = 1.0
        unit = m.group(2).strip().lower()
        return qty, unit
    return 1.0, s


async def handle_pasar_konfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proses konfirmasi simpan/edit/batal belanja pasar."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pasar:batal":
        await query.edit_message_text("❌ Dibatalkan.")
        return ConversationHandler.END

    if data == "pasar:edit":
        await query.edit_message_text(
            "✏️ Ketik ulang item belanja:\n`nama item, qty, harga`",
            parse_mode="Markdown",
        )
        return PASAR_TABEL

    if data == "pasar:simpan":
        return await _simpan_pasar(update, context)

    return PASAR_KONFIRM


async def _simpan_pasar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simpan semua item pasar ke database."""
    query = update.callback_query
    items = context.user_data.get("pasar_items", [])
    user_id = update.effective_user.id
    tx_date = date.today()

    if not items:
        await query.edit_message_text("❌ Tidak ada item untuk disimpan.")
        return ConversationHandler.END

    saved_ids = []
    total_all = 0.0
    toko = context.user_data.get("pasar_toko", "pasar")
    is_pasar = toko == "pasar"
    toko_label = "Pasar" if is_pasar else toko
    category = "pasar" if is_pasar else None

    try:
        async with AsyncSessionLocal() as session:
            db_user = await session.get(User, user_id)
            if not db_user:
                await query.edit_message_text("❌ User tidak ditemukan.")
                return ConversationHandler.END

            for item in items:
                desc = f"{toko_label} — {item['name']}"
                qty_str = f" x{item['qty']} {item['unit']}".rstrip()
                if item["qty"] != 1 or item["unit"]:
                    desc += qty_str

                tx = Transaction(
                    user_id=user_id,
                    type="keluar",
                    amount=item["total"],
                    description=desc[:200],
                    category=category,
                    transaction_date=tx_date,
                )
                session.add(tx)
                await session.flush()

                # Record ke item_prices untuk tracking harga
                try:
                    from bot.services.item_price_service import record_item_price
                    await record_item_price(
                        session=session,
                        item_name_raw=item['name'],
                        toko=toko_label,
                        total_price=float(item['total']),
                        qty=float(item.get('qty', 1)),
                        unit=str(item.get('unit', '')),
                        transaction_date=tx_date,
                        transaction_id=tx.id,
                    )
                except Exception as ep:
                    logger.warning(f"[PRICE] market record failed: {ep}")

                # Update katalog market_items
                existing = await session.execute(
                    select(MarketItem).where(
                        func.lower(MarketItem.name) == item["name"].lower()
                    )
                )
                catalog_item = existing.scalar_one_or_none()
                if catalog_item:
                    catalog_item.use_count += 1
                    catalog_item.last_price = item["unit_price"]
                    catalog_item.last_used = tx_date
                    if item["unit"]:
                        catalog_item.unit = item["unit"]
                else:
                    catalog_item = MarketItem(
                        name=item["name"].title(),
                        unit=item["unit"] or None,
                        last_price=item["unit_price"],
                        use_count=1,
                        last_used=tx_date,
                    )
                    session.add(catalog_item)

                await log_create(session, user_id, tx)
                saved_ids.append(tx.id)
                total_all += item["total"]

            await session.commit()

            saldo = await get_running_balance(session, user_id)

        logger.info(f"[PASAR] saved {len(saved_ids)} items, total={total_all}, uid={user_id}")

        # Google Sheets — satu baris per item
        for item, tx_id in zip(items, saved_ids):
            desc = f"Pasar — {item['name']}"
            try:
                await sheets_append(
                    user_id=user_id,
                    user_name=update.effective_user.full_name or "",
                    tx_type="keluar",
                    amount=item["total"],
                    description=desc,
                    tx_date=tx_date,
                )
            except Exception as e:
                logger.warning(f"[PASAR] Sheets append failed: {e}")

    except Exception as e:
        logger.exception(f"[PASAR] save error: {e}")
        await query.edit_message_text("❌ Gagal menyimpan. Coba lagi.")
        return ConversationHandler.END

    # Pesan sukses
    lines = [f"✅ *{toko_label} Tersimpan*\n"]
    for item in items:
        qty_str = f"{item['qty']} {item['unit']}".strip()
        if qty_str and qty_str != "1.0":
            qty_str = f" ({qty_str})"
        else:
            qty_str = ""
        lines.append(f"  • {_esc(item['name'])}{qty_str} — {fmt_rupiah(item['total'])}")
    lines.append(f"\n💰 *Total: {fmt_rupiah(total_all)}*")
    lines.append(f"📅 {fmt_date(tx_date)}")
    lines.append(f"\n💳 Saldo: *{fmt_rupiah(saldo)}*")

    try:
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
    except Exception:
        plain = "\n".join(lines).replace("*", "").replace("_", "").replace("`", "")
        await query.message.reply_text(plain)

    return ConversationHandler.END


async def handle_pasar_nama_toko(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima nama toko dari user, lalu tampilkan form item."""
    if not update.message or not update.message.text:
        return PASAR_NAMA_TOKO
    toko = update.message.text.strip()
    if len(toko) < 2:
        await update.message.reply_text("❌ Nama toko terlalu pendek. Ketik ulang:")
        return PASAR_NAMA_TOKO
    context.user_data["pasar_toko"] = toko
    return await show_toko_item_form(update.message, context, toko)


async def show_toko_item_form(msg_or_query, context, toko: str):
    """
    Tampilkan form input item untuk toko non-pasar.
    Format sama dengan pasar: "Nama item, qty, harga" per baris.
    Kalau hanya 1 item tanpa qty, cukup: "nama, harga"
    """
    text = (
        f"💸 *{_esc(toko)}*\n\n"
        "Ketik item belanja:\n"
        "`nama item, qty, harga`\n\n"
        "*Contoh satu item:*\n"
        "`Minyak goreng, 3, 138600`\n\n"
        "*Contoh beberapa item:*\n"
        "`Minyak goreng, 3, 138600`\n"
        "`Beras premium 5kg, 1, 74500`\n\n"
        "_Jika hanya nominal total, cukup tulis:_\n"
        "`91000`"
    )
    # Bisa dari query atau message
    if hasattr(msg_or_query, 'edit_message_text'):
        await msg_or_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await msg_or_query.reply_text(text, parse_mode="Markdown")
    return PASAR_TABEL


# ── /harga dan /sync_harga ────────────────────────────────────────────────────

async def cmd_harga(update, context):
    """
    /harga [nama item] — cari harga item dari database.
    Contoh: /harga bamer, /harga minyak goreng
    """
    from bot.database import AsyncSessionLocal
    from bot.services.item_price_service import get_price_summary, get_cheapest_toko
    from bot.utils.formatters import fmt_rupiah

    args = context.args
    if not args:
        await update.message.reply_text(
            "Ketik nama item yang ingin dicari harganya.\n"
            "Contoh: `/harga bamer`\nAtau: `/harga minyak goreng`",
            parse_mode="Markdown",
        )
        return

    query = " ".join(args)
    await update.message.reply_text(f"🔍 Mencari harga untuk *{_esc(query)}*...", parse_mode="Markdown")

    async with AsyncSessionLocal() as session:
        summary = await get_price_summary(session, query, months=6)
        cheapest = await get_cheapest_toko(session, query, months=6)

    if not summary:
        await update.message.reply_text(
            f"❌ Tidak ditemukan data harga untuk *{_esc(query)}*.\n\n"
            "_Data harga direkam otomatis setiap transaksi baru._",
            parse_mode="Markdown",
        )
        return

    # Group by item_name
    from collections import defaultdict
    by_item = defaultdict(list)
    for row in summary:
        by_item[row['item_name']].append(row)

    lines = [f"💰 *Harga: {_esc(query)}*\n"]

    for item_name, rows in list(by_item.items())[:3]:  # max 3 item
        lines.append(f"📦 *{_esc(item_name.title())}*")
        # Tampilkan 3 bulan terakhir
        for row in rows[:3]:
            unit_str = f"/{row['unit']}" if row.get('unit') else ""
            lines.append(
                f"  {row['bulan']}: "
                f"min {fmt_rupiah(row['harga_min'])}{unit_str}, "
                f"max {fmt_rupiah(row['harga_max'])}{unit_str}"
            )
        lines.append("")

    if cheapest:
        lines.append("🏪 *Toko termurah (6 bln):*")
        for i, t in enumerate(cheapest[:3], 1):
            lines.append(f"  {i}. {_esc(t['toko'] or '-')} — avg {fmt_rupiah(t['harga_rata'])}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_sync_harga(update, context):
    """/sync_harga — sync katalog harga ke Google Sheets."""
    import os
    from bot.database import AsyncSessionLocal
    from bot.services.item_price_service import sync_price_catalog_to_sheets
    from bot.services.sheets import _get_client

    await update.message.reply_text("⏳ Syncing katalog harga ke Sheets...")

    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not spreadsheet_id:
        await update.message.reply_text("❌ GOOGLE_SHEET_ID tidak dikonfigurasi.")
        return

    try:
        client = _get_client()
        async with AsyncSessionLocal() as session:
            count = await sync_price_catalog_to_sheets(
                session, client, spreadsheet_id, months=6
            )
        if count > 0:
            await update.message.reply_text(
                f"✅ *{count} baris* berhasil ditulis ke sheet *Katalog Harga*.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("⚠️ Tidak ada data harga untuk di-sync.")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal sync: {e}")


async def cmd_refresh_katalog(update, context):
    """/refresh_katalog -- reload alias table dari sheet Katalog Item."""
    import os
    from bot.services.alias_resolver import load_alias_table
    from bot.services.sheets import _get_client

    await update.message.reply_text("⏳ Memuat ulang katalog alias...")

    sid = os.environ.get("GOOGLE_SHEET_ID")
    if not sid:
        await update.message.reply_text("❌ GOOGLE_SHEET_ID tidak dikonfigurasi.")
        return
    try:
        gc = _get_client()
        count = await load_alias_table(gc, sid, force=True)
        await update.message.reply_text(
            f"✅ *Katalog alias dimuat ulang*\n"
            f"Total alias aktif: *{count}*\n\n"
            "_Semua transaksi baru akan menggunakan canonical name dari sheet Katalog Item._",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal: {e}")
