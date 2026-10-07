#!/usr/bin/env bash
# Salin SEMUA tabel schema public dari database lama ke schema baru di Postgres induk.
# Sumber hanya DIBACA (pg_dump). Pakai: SRC_URL=... NEW_URL=... ./copy_app_schema.sh stoa
#   stoa     <- DB stoabot lama     | menuplan <- DB menuplan lama | (recon: shared_rules -> shared)
# Setelah itu: psql "$NEW_URL" -f 002_views.sql
set -euo pipefail
SCHEMA="${1:?schema tujuan: stoa | menuplan}"
: "${SRC_URL:?}" "${NEW_URL:?}"
case "$SCHEMA" in stoa|menuplan) ;; *) echo "schema harus stoa atau menuplan"; exit 1;; esac
# harga_manual (schema menuplan) dibuat oleh 001_schemas.sql, jadi tidak dihitung
if [ "$(psql "$NEW_URL" -Atc "select count(*) from information_schema.tables where table_schema='$SCHEMA' and table_name<>'harga_manual'")" != "0" ]; then
  echo "schema $SCHEMA sudah berisi tabel -> dilewati (cegah dobel)"; exit 0
fi
pg_dump "$SRC_URL" --schema=public --no-owner --no-privileges \
  | perl -pe "BEGIN{\$d=0} if(\$d){\$d=0 if /^\\\\\\.\$/; next} \$d=1 if /^COPY .* FROM stdin;/; s/\\bpublic\\./${SCHEMA}./g; \$_='' if /^CREATE SCHEMA public/ || /^COMMENT ON SCHEMA public/" \
  | psql "$NEW_URL" -v ON_ERROR_STOP=1 -f -
N=$(psql "$NEW_URL" -Atc "select count(*) from information_schema.tables where table_schema='$SCHEMA' and table_name<>'harga_manual'")
P=$(psql "$NEW_URL" -Atc "select count(*) from information_schema.tables where table_schema='public'")
echo "$SCHEMA: $N tabel"
[ "$N" != "0" ] && [ "$P" = "0" ] || { echo "GAGAL: tabel salah tempat (schema $SCHEMA=$N, public=$P)"; exit 1; }
