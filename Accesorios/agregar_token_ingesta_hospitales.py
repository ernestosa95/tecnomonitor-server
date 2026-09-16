"""
Migración: agrega la columna `ingest_token_hash` (hash SHA-256 del token de
ingesta por hospital) + índice único a hospitales_metadata. Ver
docs/11-plan-auth-ingesta-agente.md.

`Base.metadata.create_all()` (database.py) NO agrega columnas nuevas a una
tabla que ya existe -- solo crea tablas que faltan. Por eso este script
aparte, mismo patrón que el resto de Accesorios/. Es idempotente: correrlo
de nuevo no rompe nada.
"""
import sqlite3
import os

DB_NAME = "monitor_hospitales.db"

if not os.path.exists(DB_NAME):
    print(f"❌ No se encontró {DB_NAME}. Asegúrate de estar en la carpeta correcta.")
else:
    print(f"🔄 Conectando a {DB_NAME}...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE hospitales_metadata ADD COLUMN ingest_token_hash TEXT")
        print("✅ Columna 'ingest_token_hash' agregada correctamente.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("ℹ️ La columna 'ingest_token_hash' ya existía. No se hicieron cambios.")
        else:
            print(f"❌ Error SQL: {e}")

    try:
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_hospitales_ingest_token_hash
            ON hospitales_metadata (ingest_token_hash)
        """)
        print("✅ Índice único 'idx_hospitales_ingest_token_hash' creado (o ya existía).")
    except sqlite3.OperationalError as e:
        print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada.")
