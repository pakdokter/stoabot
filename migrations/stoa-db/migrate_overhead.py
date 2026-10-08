"""Muat kamus/overhead.json bot-recon ke recon.kamus_overhead (idempoten).
Env: NEW_URL (Postgres induk), RECON_OVERHEAD (path overhead.json)."""
import json, os
import psycopg2

d = json.load(open(os.environ["RECON_OVERHEAD"], encoding="utf-8"))["data"]
rows = {}
for e in d:
    for a in [e["nama"]] + list(e.get("alias", [])):
        rows[a.strip().lower()] = (e["nama"], e["kategori"])
with psycopg2.connect(os.environ["NEW_URL"]) as c, c.cursor() as cur:
    for alias, (nama, kat) in rows.items():
        cur.execute("INSERT INTO recon.kamus_overhead VALUES (%s,%s,%s) ON CONFLICT (alias) DO UPDATE "
                    "SET nama_baku=EXCLUDED.nama_baku, kategori=EXCLUDED.kategori", (alias, nama, kat))
print(f"recon.kamus_overhead: {len(rows)} alias dari {len(d)} entri")
