-- Dummy data untuk development & testing
-- Jalankan setelah init.sql

-- User admin (ganti id dengan telegram ID Anda)
INSERT INTO users (id, username, full_name, role) VALUES
    (123456789, 'pakdokteeer', 'Roziyan Hidayat', 'admin'),
    (111111111, 'upi_stoaspace', 'Lusiana Valufi', 'staff'),
    (222222222, 'baiq_stoaspace', 'Baiq Widia', 'staff');

-- Transaksi Juli 2026
INSERT INTO transactions (user_id, type, amount, description, category, transaction_date) VALUES
    (123456789, 'masuk',  4500000, 'Penjualan kopi & minuman',  'penjualan', '2026-07-01'),
    (123456789, 'keluar',  850000, 'Belanja bahan baku mingguan', 'bahan_baku', '2026-07-01'),
    (123456789, 'masuk',  3800000, 'Penjualan kopi & makanan',   'penjualan', '2026-07-02'),
    (123456789, 'keluar',  125000, 'Sabun & peralatan kebersihan','operasional', '2026-07-02'),
    (123456789, 'masuk',  5200000, 'Penjualan weekend',          'penjualan', '2026-07-05'),
    (123456789, 'keluar', 2800000, 'Gaji karyawan mingguan',     'gaji', '2026-07-05'),
    (123456789, 'masuk',  4100000, 'Penjualan harian',           'penjualan', '2026-07-08'),
    (123456789, 'keluar',  320000, 'Listrik',                    'utilitas', '2026-07-08'),
    (123456789, 'masuk',  6700000, 'Penjualan + event',          'penjualan', '2026-07-12'),
    (123456789, 'keluar',  750000, 'Belanja bahan baku',         'bahan_baku', '2026-07-12'),
    (123456789, 'masuk',  4200000, 'Penjualan harian',           'penjualan', '2026-07-15'),
    (123456789, 'keluar',   87500, 'Belanja Indomaret (struk)',  'operasional', '2026-07-15'),
    (123456789, 'masuk',   150000, 'Penjualan kopi',             'penjualan', '2026-07-15'),
    (111111111, 'keluar',  450000, 'Belanja sayur & telur',      'bahan_baku', '2026-07-16'),
    (123456789, 'masuk',  5500000, 'Penjualan + catering',       'penjualan', '2026-07-19'),
    (123456789, 'keluar',  180000, 'Internet bulanan',           'utilitas', '2026-07-20'),
    (123456789, 'masuk',  3900000, 'Penjualan harian',           'penjualan', '2026-07-22'),
    (123456789, 'keluar', 2800000, 'Gaji karyawan',              'gaji', '2026-07-25'),
    (123456789, 'masuk',  4800000, 'Penjualan weekend',          'penjualan', '2026-07-26'),
    (123456789, 'keluar',  600000, 'Belanja bahan baku',         'bahan_baku', '2026-07-28');

-- Audit log dummy (opsional)
-- Biasanya dibuat otomatis oleh aplikasi
