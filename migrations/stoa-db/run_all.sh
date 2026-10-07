#!/usr/bin/env bash
# Jalankan SENDIRI di terminal (butuh: railway login, gh login, psql/pg_dump versi 18).
# Urutan: salin schema stoa & menuplan -> migrasi bahan/alias -> view -> shared_rules -> verifikasi.
# Database lama hanya DIBACA. Kredensial hanya di memori proses, tidak ditulis ke file.
#   bash run_all.sh            # simulasi bahan saja (aman)
#   bash run_all.sh --apply    # jalankan semuanya
set -euo pipefail
cd "$(dirname "$0")"
export PATH=/opt/homebrew/opt/postgresql@18/bin:$PATH
APPLY="${1:-}"

P_NEW=5fb0c092-8366-4af7-a2ad-7b58de006e18;  S_NEW=548bd762-0105-4b8a-b89f-57b81fee4583
P_MEN=574f8501-1c32-4059-ae67-25b4c07934c5;  S_MEN=555b1932-2e2b-4452-bf8e-757eb073955f
P_STO=fa5daabc-bc24-45c9-a68e-b0bc00bea3bb;  S_STO=d57b666d-1bbc-47ca-81c2-740380e9cd6c

proxy_url(){ railway variables --json -p "$1" -e production -s "$2" | python3 -c "
import sys,json,urllib.parse as u
d=json.load(sys.stdin)
print('postgresql://%s:%s@%s:%s/%s?sslmode=require'%(u.quote(d['PGUSER']),u.quote(d['PGPASSWORD']),d['RAILWAY_TCP_PROXY_DOMAIN'],d['RAILWAY_TCP_PROXY_PORT'],d['PGDATABASE']),end='')"; }
export NEW_URL="$(proxy_url $P_NEW $S_NEW)"
export MENUPLAN_URL="$(proxy_url $P_MEN $S_MEN)"
export STOA_URL="$(railway variables --json -p $P_STO -e production -s $S_STO | python3 -c "import sys,json;print(json.load(sys.stdin)['DATABASE_PUBLIC_URL'],end='')")"

if [ -z "${RECON_BAHAN:-}" ]; then
  T="$(mktemp -d)"; gh repo clone pakdokter/reconbankbot "$T/r" -- -q; export RECON_BAHAN="$T/r/kamus/bahan.json"
fi
[ -f venv/bin/python ] || true
PY="${PYTHON:-python3}"; $PY -c "import psycopg2" 2>/dev/null || { echo "pip install psycopg2-binary dulu"; exit 1; }

psql "$NEW_URL" -v ON_ERROR_STOP=1 -q -f 001_schemas.sql

if [ "$APPLY" != "--apply" ]; then
  $PY migrate_bahan.py; echo; echo "Simulasi selesai. Jalankan: bash run_all.sh --apply"; exit 0
fi

SRC_URL="$STOA_URL"     bash copy_app_schema.sh stoa
SRC_URL="$MENUPLAN_URL" bash copy_app_schema.sh menuplan
$PY migrate_bahan.py --apply
# shared_rules milik bot-recon (di DB stoabot lama, schema public): salin hanya jika belum ada
if [ "$(psql "$NEW_URL" -Atc 'select count(*) from shared.shared_rules')" = "0" ]; then
  psql "$STOA_URL" -Atc "select count(*) from public.shared_rules" >/dev/null 2>&1 && \
  pg_dump "$STOA_URL" --data-only --table=public.shared_rules --no-owner \
    | perl -pe 's/public\.shared_rules/shared.shared_rules/g' | psql "$NEW_URL" -v ON_ERROR_STOP=1 -q
fi
psql "$NEW_URL" -v ON_ERROR_STOP=1 -q -f 002_views.sql

echo "=== verifikasi jumlah baris (lama -> baru) ==="
for t in users transactions attachments audit_logs market_items item_prices; do
  echo "stoa.$t: $(psql "$STOA_URL" -Atc "select count(*) from public.$t") -> $(psql "$NEW_URL" -Atc "select count(*) from stoa.$t")"
done
for t in $(psql "$MENUPLAN_URL" -Atc "select tablename from pg_tables where schemaname='public' order by 1"); do
  echo "menuplan.$t: $(psql "$MENUPLAN_URL" -Atc "select count(*) from public.$t") -> $(psql "$NEW_URL" -Atc "select count(*) from menuplan.$t")"
done
echo "shared.bahan: $(psql "$NEW_URL" -Atc 'select count(*) from shared.bahan') | alias: $(psql "$NEW_URL" -Atc 'select count(*) from shared.bahan_alias')"
echo "shared.harga_belanja (view): $(psql "$NEW_URL" -Atc 'select count(*), count(bahan_kode) from shared.harga_belanja')"
