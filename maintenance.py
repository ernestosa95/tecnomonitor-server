import sqlite3
import os
import json
import time
from datetime import datetime, timedelta
from statistics import mean

# --- CONFIGURACIÓN ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "monitor_hospitales.db")

DIAS_RETENCION_DETALLE = 7    
DIAS_RETENCION_TOTAL = 365    
BATCH_SIZE = 1000  # <--- NUEVO: Procesa de a poco para no bloquear

def conectar_db():
    if not os.path.exists(DB_NAME): return None
    # Aumentamos el timeout y activamos WAL
    conn = sqlite3.connect(DB_NAME, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def procesar_bloque_30min(cursor, hospital_id, registros):
    if not registros or len(registros) < 2: return 

    ultimo_id, _, ultimo_ts, ultimo_json_str = registros[-1]
    
    try:
        data_plantilla = json.loads(ultimo_json_str)
        if not data_plantilla: return
    except: return 

    valores_cpu_host, valores_ram_host, valores_temp_amb = [], [], []
    valores_vms = {} 

    for _, _, _, json_str in registros:
        try:
            d = json.loads(json_str)
            if not d: continue
            phy = d.get('physical_layer') or {}
            tele = phy.get('telemetry') or {}
            
            cpu_val = (tele.get('cpu') or {}).get('usage_percent')
            if cpu_val is not None: valores_cpu_host.append(float(cpu_val))
            
            ram_val = (tele.get('ram') or {}).get('used_gb')
            if ram_val is not None: valores_ram_host.append(float(ram_val))
            
            for t in (phy.get('sensors') or {}).get('temperatures') or []:
                if "Ambient" in t.get('name', ''):
                    valores_temp_amb.append(float(t.get('value', 0)))
                    break
        except: continue 

    # --- INYECCIÓN SEGURA (Blindada contra NoneType) ---
    try:
        if 'physical_layer' not in data_plantilla or data_plantilla['physical_layer'] is None:
            data_plantilla['physical_layer'] = {'telemetry': {'cpu': {}, 'ram': {}}, 'sensors': {}}
        
        phy_p = data_plantilla['physical_layer']
        if 'telemetry' not in phy_p or phy_p['telemetry'] is None: 
            phy_p['telemetry'] = {'cpu': {}, 'ram': {}}
        
        # CPU & RAM
        if valores_cpu_host:
            if 'cpu' not in phy_p['telemetry'] or phy_p['telemetry']['cpu'] is None: phy_p['telemetry']['cpu'] = {}
            phy_p['telemetry']['cpu']['usage_percent'] = round(mean(valores_cpu_host), 2)
        
        if valores_ram_host:
            if 'ram' not in phy_p['telemetry'] or phy_p['telemetry']['ram'] is None: phy_p['telemetry']['ram'] = {}
            phy_p['telemetry']['ram']['used_gb'] = round(mean(valores_ram_host), 2)

        # Etiquetamos el registro para no volver a comprimirlo ---
        data_plantilla['_is_compressed'] = True

        # SQL e Insert
        nuevo_json = json.dumps(data_plantilla)
        sql_status = (phy_p.get('sensors') or {}).get('status', 'Unknown')
        sql_cpu = (phy_p['telemetry'].get('cpu') or {}).get('usage_percent', 0)
        sql_ram = (phy_p['telemetry'].get('ram') or {}).get('used_gb', 0)
        
        cursor.execute("""
            INSERT INTO reportes_historicos (hospital_id, timestamp, host_status, host_cpu_usage, host_ram_usage, full_json_data)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (hospital_id, ultimo_ts, sql_status, sql_cpu, sql_ram, nuevo_json))
        
        ids_a_borrar = [str(r[0]) for r in registros]
        cursor.execute(f"DELETE FROM reportes_historicos WHERE id IN ({','.join('?'*len(ids_a_borrar))})", ids_a_borrar)
    except Exception as e:
        print(f"⚠️ Error en bloque {hospital_id}: {e}")

def ejecutar_mantenimiento():
    print(f"--- 🧹 MANTENIMIENTO LIGHT: {datetime.now()} ---")
    conn = conectar_db()
    if not conn: return
    try:
        cursor = conn.cursor()
        fecha_limite = datetime.now() - timedelta(days=DIAS_RETENCION_DETALLE)
        
        # Procesamos de a BATCH_SIZE para no bloquear la DB
        cursor.execute("""
            SELECT id, hospital_id, timestamp, full_json_data 
            FROM reportes_historicos 
            WHERE timestamp < ? AND full_json_data NOT LIKE '%"_is_compressed": true%'
            ORDER BY hospital_id, timestamp ASC LIMIT ?
        """, (fecha_limite, BATCH_SIZE))
        
        rows = cursor.fetchall()
        if not rows:
            print("✅ Nada que comprimir por ahora.")
            return

        bloque_actual, llave_actual = [], None
        for row in rows:
            rid, hid, ts_str, js = row 
            try:
                ts = datetime.fromisoformat(ts_str) if 'T' in ts_str else datetime.strptime(ts_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
            except: continue
            
            nueva_llave = (hid, ts.replace(minute=(0 if ts.minute < 30 else 30), second=0, microsecond=0))
            if nueva_llave != llave_actual and bloque_actual:
                procesar_bloque_30min(cursor, llave_actual[0], bloque_actual)
                bloque_actual = []
            
            llave_actual = nueva_llave
            bloque_actual.append(row)
        
        # =========================================================
        # 🛡️ FIX: Procesar el último bloque que quedó atrapado en memoria
        # al terminar el ciclo for.
        # =========================================================
        if bloque_actual and llave_actual:
            procesar_bloque_30min(cursor, llave_actual[0], bloque_actual)
        
        conn.commit()
        print(f"🚀 Batch de {len(rows)} registros procesado.")
    finally:
        conn.close()

def iniciar_programacion():
    # Ejecución inmediata la primera vez para limpiar el backlog de a poco
    while True:
        ahora = datetime.now()
        # Si estamos en la ventana de mantenimiento (23:00 a 03:00) 
        # o si quieres limpiar el backlog ahora:
        ejecutar_mantenimiento()
        
        # Pausa de 5 segundos entre batches para dejar trabajar a la API
        time.sleep(5) 
        
        # Lógica de espera hasta las 23:00 (simplificada para que pruebes ahora)
        if ahora.hour == 23: 
             print("Dormimos hasta mañana...")
             time.sleep(3600 * 20)

if __name__ == "__main__":
    iniciar_programacion()