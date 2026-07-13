# migrar_username.py
import sqlite3

DB_PATH = "monitor_hospitales.db"  # ajustá si tu archivo tiene otro nombre/ruta

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("PRAGMA table_info(users)")
columnas = [c[1] for c in cur.fetchall()]

if "username" not in columnas:
    cur.execute("ALTER TABLE users ADD COLUMN username VARCHAR")
    conn.commit()
    print("✅ Columna 'username' agregada.")
else:
    print("ℹ️ La columna 'username' ya existe, no se hizo nada.")

conn.close()