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

-- Riwayat harga dari belanja (diisi stoabot; asal: item_prices)
CREATE TABLE IF NOT EXISTS shared.harga_belanja (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bahan_kode       TEXT REFERENCES shared.bahan(kode) ON UPDATE CASCADE ON DELETE SET NULL,
    item_name        TEXT NOT NULL,
    item_name_raw    TEXT,
    toko             TEXT,
    unit             TEXT,
    qty              NUMERIC(12,3) NOT NULL DEFAULT 1,
    unit_price       NUMERIC(15,2),
    total_price      NUMERIC(15,2),
    transaction_date DATE NOT NULL,
    transaction_id   UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hb_bahan_tgl ON shared.harga_belanja (bahan_kode, transaction_date DESC);

CREATE TABLE IF NOT EXISTS shared.shared_rules (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── menuplan: harga manual yang merujuk harga belanja ───────────────
CREATE TABLE IF NOT EXISTS menuplan.harga_manual (
    bahan_kode TEXT PRIMARY KEY REFERENCES shared.bahan(kode) ON UPDATE CASCADE ON DELETE CASCADE,
    harga      NUMERIC(15,2) NOT NULL CHECK (harga >= 0),  -- per satuan bahan (g/ml/pcs)
    catatan    TEXT,
    diubah_oleh TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Harga efektif: manual kalau ada, kalau tidak harga belanja terakhir.
CREATE OR REPLACE VIEW menuplan.v_harga_efektif AS
SELECT b.kode,
       b.nama,
       hb.harga_belanja,
       hb.tgl_belanja,
       hm.harga                               AS harga_manual,
       COALESCE(hm.harga, hb.harga_belanja)   AS harga_efektif,
       CASE WHEN hm.harga IS NOT NULL THEN 'manual' ELSE 'belanja' END AS sumber
FROM shared.bahan b
LEFT JOIN LATERAL (
    SELECT h.unit_price AS harga_belanja, h.transaction_date AS tgl_belanja
    FROM shared.harga_belanja h
    WHERE h.bahan_kode = b.kode AND h.unit_price IS NOT NULL
    ORDER BY h.transaction_date DESC, h.created_at DESC
    LIMIT 1
) hb ON TRUE
LEFT JOIN menuplan.harga_manual hm ON hm.bahan_kode = b.kode;

-- ── recon: kamus bahan bot-recon (pemetaan alias -> nama baku Kas-Buku) ──
-- Menyimpan pemetaan asli recon utuh, termasuk yang kalah konflik di shared.bahan_alias.
CREATE TABLE IF NOT EXISTS recon.kamus_bahan (
    alias     TEXT PRIMARY KEY,           -- huruf kecil, dicocokkan utuh
    nama_baku TEXT NOT NULL,
    kategori  TEXT NOT NULL               -- 'Belanja Bahan' | 'Kemasan'
);
