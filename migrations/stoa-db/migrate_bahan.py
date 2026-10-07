"""
Migrasi satu kali ke Postgres induk (shared.*). Sumber lama TIDAK diubah (read-only).

Env (isi sendiri, jangan commit):
  NEW_URL       Postgres induk (stoa-db)
  MENUPLAN_URL  DB menuplan lama      (tabel bahan, bahan_kategori, bahan_alias)
  STOA_URL      DB stoabot lama       (tabel market_items, item_prices)
  RECON_BAHAN   path kamus/bahan.json bot-recon

Default = DRY-RUN (hanya laporan, tidak menulis). Tambah --apply untuk menulis.
"""
import json, os, re, sys
import psycopg2

APPLY = "--apply" in sys.argv


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(s)).strip("_")[:60] or "bahan"


def fetch(url, sql):
    with psycopg2.connect(url, connect_timeout=10) as c, c.cursor() as cur:
        c.set_session(readonly=True)
        cur.execute(sql)
        return cur.fetchall()


def main():
    bahan, alias, kategori = {}, {}, {}   # kode -> dict ; (match,toko) -> dict
    konflik, tak_terpetakan = [], []

    # 1) menuplan = sumber utama struktur bahan (kode, satuan, jenis, kategori, isi)
    if os.environ.get("MENUPLAN_URL"):
        u = os.environ["MENUPLAN_URL"]
        for nama, urut in fetch(u, "SELECT nama, urutan FROM bahan_kategori"):
            kategori[nama] = urut
        for kode, nama, satuan, jenis, kat in fetch(u, "SELECT kode,nama,satuan,jenis,kategori FROM bahan"):
            bahan[kode] = dict(kode=kode, nama=nama, satuan=satuan, jenis=jenis, kategori=kat, kategori_recon=None)
        for kode, match, isi, toko in fetch(u, "SELECT bahan_kode,match,isi,toko FROM bahan_alias"):
            alias[(norm(match), norm(toko or ""))] = dict(kode=kode, match=match, isi=isi, toko=toko, sumber="menuplan")

    # nama baku -> kode, untuk mencocokkan kamus recon dengan bahan menuplan
    by_nama = {norm(b["nama"]): k for k, b in bahan.items()}
    for (m, _t), a in alias.items():
        by_nama.setdefault(m, a["kode"])

    def tambah_alias(match, kode, sumber, isi=None, toko=None):
        key = (norm(match), norm(toko or ""))
        if not key[0]:
            return
        ada = alias.get(key)
        if ada and ada["kode"] != kode:
            konflik.append((match, ada["kode"], kode, sumber))
            return
        if not ada:
            alias[key] = dict(kode=kode, match=match, isi=isi, toko=toko, sumber=sumber)

    # 2) kamus recon: nama baku + alias -> gabung ke bahan yang sama jika nama/alias cocok
    p = os.environ.get("RECON_BAHAN")
    if p:
        for e in json.load(open(p, encoding="utf-8"))["data"]:
            nama, kat_recon = e["nama"], e.get("kategori")
            kode = by_nama.get(norm(nama)) or next(
                (alias[(norm(a), "")]["kode"] for a in e.get("alias", []) if (norm(a), "") in alias), None)
            if kode is None:
                kode = "rc_" + slug(nama)
                bahan[kode] = dict(kode=kode, nama=nama, satuan=None, jenis=None, kategori=None, kategori_recon=kat_recon)
                by_nama[norm(nama)] = kode
            else:
                bahan[kode]["kategori_recon"] = kat_recon
            for a in [nama] + list(e.get("alias", [])):
                tambah_alias(a, kode, "recon")

    # 3) stoabot: nama item belanja -> bahan lewat alias; sisanya dilaporkan
    harga = []
    if os.environ.get("STOA_URL"):
        u = os.environ["STOA_URL"]
        for nm, raw, toko, unit, price, total, qty, tgl, txid in fetch(
            u, "SELECT item_name,item_name_raw,toko,unit,unit_price,total_price,qty,transaction_date,transaction_id FROM item_prices"):
            k = alias.get((norm(raw or nm), norm(toko or ""))) or alias.get((norm(raw or nm), "")) \
                or alias.get((norm(nm), norm(toko or ""))) or alias.get((norm(nm), ""))
            kode = k["kode"] if k else None
            if kode is None:
                tak_terpetakan.append(raw or nm)
            harga.append((kode, nm, raw, toko, unit, qty, price, total, tgl, txid))

    # ── laporan ──
    from collections import Counter
    print(f"bahan: {len(bahan)} | alias: {len(alias)} | kategori: {len(kategori)} | baris harga: {len(harga)}")
    print(f"konflik alias (satu nama -> dua bahan, DILEWATI): {len(konflik)}")
    for m, k1, k2, s in konflik[:50]:
        print(f"  - '{m}': {k1} vs {k2} (dari {s})")
    c = Counter(tak_terpetakan)
    print(f"item belanja tanpa bahan ({len(c)} nama unik, {len(tak_terpetakan)} baris); 25 teratas:")
    for n, j in c.most_common(25):
        print(f"  - {n}  x{j}")

    if not APPLY:
        print("\nDRY-RUN: tidak ada yang ditulis. Jalankan ulang dengan --apply setelah laporan dicek.")
        return

    with psycopg2.connect(os.environ["NEW_URL"]) as c, c.cursor() as cur:
        for n, u_ in kategori.items():
            cur.execute("INSERT INTO shared.bahan_kategori VALUES (%s,%s) ON CONFLICT DO NOTHING", (n, u_))
        for b in bahan.values():
            cur.execute("""INSERT INTO shared.bahan (kode,nama,satuan,jenis,kategori,kategori_recon)
                           VALUES (%(kode)s,%(nama)s,%(satuan)s,%(jenis)s,%(kategori)s,%(kategori_recon)s)
                           ON CONFLICT (kode) DO NOTHING""", b)
        for a in alias.values():
            cur.execute("""INSERT INTO shared.bahan_alias (bahan_kode,match,isi,toko,sumber)
                           VALUES (%(kode)s,%(match)s,%(isi)s,%(toko)s,%(sumber)s) ON CONFLICT DO NOTHING""", a)
        for h in harga:
            cur.execute("""INSERT INTO shared.harga_belanja
                (bahan_kode,item_name,item_name_raw,toko,unit,qty,unit_price,total_price,transaction_date,transaction_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", h)
    print("\nAPPLY selesai.")


if __name__ == "__main__":
    main()
