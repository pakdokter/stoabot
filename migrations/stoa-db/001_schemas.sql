-- Postgres induk Stoa: schema per aplikasi + schema shared.
CREATE SCHEMA IF NOT EXISTS shared;
CREATE SCHEMA IF NOT EXISTS stoa;      -- stoabot (users, transactions, ...)
CREATE SCHEMA IF NOT EXISTS menuplan;
CREATE SCHEMA IF NOT EXISTS recon;

-- ── shared: katalog bahan tunggal ───────────────────────────────────
CREATE TABLE IF NOT EXISTS shared.bahan_kategori (
    nama   TEXT PRIMARY KEY,
    urutan INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shared.bahan (
    kode           TEXT PRIMARY KEY,
    nama           TEXT NOT NULL,
    satuan         TEXT CHECK (satuan IN ('g','ml','pcs')),
    jenis          TEXT CHECK (jenis IN ('mentah','frozen','kemasan','pelengkap')),
    kategori       TEXT REFERENCES shared.bahan_kategori(nama) ON UPDATE CASCADE ON DELETE SET NULL,
    kategori_recon TEXT,                     -- 'Belanja Bahan' | 'Kemasan' (kamus bot-recon)
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shared.bahan_alias (
    id         BIGSERIAL PRIMARY KEY,
    bahan_kode TEXT NOT NULL REFERENCES shared.bahan(kode) ON UPDATE CASCADE ON DELETE CASCADE,
    match      TEXT NOT NULL,
    isi        NUMERIC(14,4),                -- isi per kemasan (dari menuplan)
    toko       TEXT,                         -- NULL = semua toko
    sumber     TEXT NOT NULL DEFAULT 'manual'  -- menuplan | recon | stoa | manual
);
CREATE INDEX IF NOT EXISTS idx_alias_bahan ON shared.bahan_alias (bahan_kode);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alias_match
    ON shared.bahan_alias (lower(match), COALESCE(lower(toko), ''));

-- Kolom tambahan dari Menuplan (toko/supplier langganan & merek)
ALTER TABLE shared.bahan ADD COLUMN IF NOT EXISTS toko_default TEXT;
ALTER TABLE shared.bahan ADD COLUMN IF NOT EXISTS merk TEXT;

-- Riwayat harga belanja = VIEW atas stoa.item_prices (diisi stoabot), bahan_kode
-- di-resolve lewat alias saat dibaca. Tidak ada salinan data yang bisa basi.
-- (dibuat di 002_views.sql setelah schema stoa terisi)

CREATE TABLE IF NOT EXISTS shared.shared_rules (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- (Harga manual Menuplan tetap lewat tabel bahan_override milik Menuplan; tidak ada tabel harga manual di sini.)

-- ── recon: kamus bahan bot-recon (pemetaan alias -> nama baku Kas-Buku) ──
-- Menyimpan pemetaan asli recon utuh, termasuk yang kalah konflik di shared.bahan_alias.
CREATE TABLE IF NOT EXISTS recon.kamus_bahan (
    alias     TEXT PRIMARY KEY,           -- huruf kecil, dicocokkan utuh
    nama_baku TEXT NOT NULL,
    kategori  TEXT NOT NULL               -- 'Belanja Bahan' | 'Kemasan'
);

-- kamus overhead bot-recon (alias -> nama baku + kategori resmi), dari kamus/overhead.json
CREATE TABLE IF NOT EXISTS recon.kamus_overhead (
    alias     TEXT PRIMARY KEY,           -- huruf kecil
    nama_baku TEXT NOT NULL,
    kategori  TEXT NOT NULL               -- salah satu kategori resmi reconbot
);
