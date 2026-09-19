"""
Migración: agrega la columna `reaperturas` (contador de cuántas veces se
reabrió un incidente) a la tabla `alertas`. Ver docs/12-ultima-milla-alertas-asana.md.

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
        cursor.execute("ALTER TABLE alertas ADD COLUMN reaperturas INTEGER DEFAULT 0")
        print("✅ Columna 'reaperturas' agregada correctamente.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("ℹ️ La columna 'reaperturas' ya existía. No se hicieron cambios.")
        else:
            print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada.")
