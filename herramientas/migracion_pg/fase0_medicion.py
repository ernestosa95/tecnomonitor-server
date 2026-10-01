"""
Fase 0 de la migración a PostgreSQL (docs/14 §9): mediciones de SOLO LECTURA sobre una copia de
monitor_hospitales.db. No escribe en la base (la abre en modo read-only).

Uso:
    python3 fase0_medicion.py /ruta/monitor_copia.db --out informe_fase0.md

Mide:
  A. Tamaño real de cada tabla e índice (dbstat), filas y rango de fechas.
  B. reportes_historicos por mes: filas, bytes de JSON, filas ya resumidas por maintenance.py.
  C. Composición del JSON (último día): cuánto pesa cada bloque, VMs y listas por hospital.
  D. Repetición entre reportes consecutivos del mismo hospital: qué porcentaje no cambia y qué
     campos cambian (candidatos a métricas tipadas).
  E. Compresión real (zlib, lzma, zstd, zstd con diccionario) por reporte y por archivo diario.
  F. Estimación del modelo nuevo (métricas tipadas + inventario solo cuando cambia).
  G. software_monitoring por app (filas/día y bytes) y reportes_uso.
  H. Tiempos de las lecturas calientes actuales.
"""
import argparse
import json
import lzma
import random
import sqlite3
import statistics
import time
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta

try:
    import zstandard
except ImportError:  # el informe lo dice y sigue con zlib/lzma
    zstandard = None

MB = 1024 * 1024
RESUMIDA = '%"_is_compressed": true%'   # marca que deja maintenance.py


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------
def abrir(ruta):
    conn = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def cargar(js):
    if js is None:
        return None
    if isinstance(js, (dict, list)):
        return js
    try:
        return json.loads(js)
    except (TypeError, ValueError):
        return None


def tam(obj):
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode())


def fmt_b(n):
    if n is None:
        return "-"
    for u, d in (("GB", 1024 ** 3), ("MB", MB), ("KB", 1024)):
        if n >= d:
            return f"{n / d:,.2f} {u}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{n} B"


def fmt_n(n):
    return f"{n:,}".replace(",", ".")


def pct(a, b):
    return f"{100 * a / b:.1f} %" if b else "-"


def tabla(filas, cab):
    out = ["| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    out += ["| " + " | ".join(str(c) for c in f) + " |" for f in filas]
    return "\n".join(out)


def parse_ts(v):
    if isinstance(v, datetime):
        return v
    s = str(v or "")
    try:
        return datetime.fromisoformat(s.replace(" ", "T")[:26])
    except ValueError:
        return None


def aplanar(obj, prefijo="", salida=None, clave_lista=True):
    """
    Hojas del JSON como {ruta: valor}. En las listas de objetos se usa como índice el `id`/`name`
    del elemento si lo tiene (así una VM sigue siendo la misma aunque cambie de posición).
    """
    if salida is None:
        salida = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            aplanar(v, f"{prefijo}.{k}" if prefijo else str(k), salida, clave_lista)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            ident = None
            if clave_lista and isinstance(v, dict):
                for k in ("id", "vm_id", "name", "nombre", "hostname", "mount", "device", "label", "url"):
                    if v.get(k) not in (None, ""):
                        ident = f"{k}={v.get(k)}"
                        break
            aplanar(v, f"{prefijo}[{ident if ident else i}]", salida, clave_lista)
    else:
        salida[prefijo] = obj
    return salida


def normalizar_ruta(ruta):
    """virtual_layer[id=ARP03APPV].telemetry.cpu -> virtual_layer[].telemetry.cpu"""
    out, dentro = [], False
    for ch in ruta:
        if ch == "[":
            dentro = True
            out.append("[]")
        elif ch == "]":
            dentro = False
        elif not dentro:
            out.append(ch)
    return "".join(out)


def medir(fn):
    t0 = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# secciones
# ---------------------------------------------------------------------------
def seccion_tablas(conn, inf):
    inf.append("## A. Tablas e índices\n")
    try:
        filas = conn.execute(
            "SELECT name, SUM(pgsize) AS b FROM dbstat GROUP BY name ORDER BY b DESC").fetchall()
        total = sum(r["b"] for r in filas)
        inf.append(tabla([(r["name"], fmt_b(r["b"]), pct(r["b"], total)) for r in filas[:25]],
                         ["Tabla / índice", "Tamaño", "% del archivo"]))
        inf.append(f"\nTotal según dbstat: **{fmt_b(total)}**.")
        libres = conn.execute("PRAGMA freelist_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
        inf.append(f"Páginas libres dentro del archivo (reusables, no devueltas al disco): {fmt_b(libres)}.\n")
    except sqlite3.Error as e:
        inf.append(f"_dbstat no disponible: {e}_\n")

    filas = []
    for t in ("reportes_historicos", "software_monitoring", "reportes_uso", "alertas"):
        try:
            r = conn.execute(f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM {t}" if t != "alertas"
                             else "SELECT COUNT(*), MIN(start_time), MAX(start_time) FROM alertas").fetchone()
            filas.append((t, fmt_n(r[0]), r[1], r[2]))
        except sqlite3.Error as e:
            filas.append((t, f"error: {e}", "", ""))
    inf.append(tabla(filas, ["Tabla", "Filas", "Desde", "Hasta"]) + "\n")


def seccion_meses(conn, inf):
    inf.append("## B. reportes_historicos por mes\n")
    (filas, seg) = medir(lambda: conn.execute(f"""
        SELECT substr(timestamp, 1, 7) AS mes, COUNT(*) AS n, COUNT(DISTINCT hospital_id) AS h,
               SUM(LENGTH(full_json_data)) AS b,
               SUM(CASE WHEN full_json_data LIKE '{RESUMIDA}' THEN 1 ELSE 0 END) AS res
        FROM reportes_historicos GROUP BY mes ORDER BY mes""").fetchall())
    inf.append(tabla([(r["mes"], fmt_n(r["n"]), r["h"], fmt_b(r["b"]), fmt_n(r["res"]),
                       pct(r["res"], r["n"])) for r in filas],
                     ["Mes", "Reportes", "Hospitales", "JSON", "Resumidas (con pérdida)", "% resumidas"]))
    total_b = sum(r["b"] or 0 for r in filas)
    total_n = sum(r["n"] for r in filas)
    inf.append(f"\nTotal: {fmt_n(total_n)} reportes, {fmt_b(total_b)} de JSON "
               f"(promedio {fmt_b(total_b // max(total_n, 1))} por reporte). Recorrido: {seg:.0f} s.\n")
    return filas


def muestra_ultimo_dia(conn, horas=24):
    hasta = conn.execute("SELECT MAX(timestamp) FROM reportes_historicos").fetchone()[0]
    desde = (parse_ts(hasta) - timedelta(hours=horas)).isoformat(sep=" ")
    filas = conn.execute("""
        SELECT hospital_id, timestamp, full_json_data FROM reportes_historicos
        WHERE timestamp >= ? ORDER BY hospital_id, timestamp""", (desde,)).fetchall()
    return [(r["hospital_id"], r["timestamp"], r["full_json_data"]) for r in filas], desde, hasta


def seccion_composicion(muestra, inf):
    inf.append("## C. Composición del JSON (último día)\n")
    por_bloque, por_sub = Counter(), Counter()
    vms, listas = [], defaultdict(list)
    tot = 0
    for _, _, js in muestra:
        d = cargar(js)
        if not isinstance(d, dict):
            continue
        tot += tam(d)
        for k, v in d.items():
            por_bloque[k] += tam(v)
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    por_sub[f"{k}.{k2}"] += tam(v2)
        vl = d.get("virtual_layer")
        if isinstance(vl, list):
            vms.append(len(vl))
            for vm in vl:
                if isinstance(vm, dict):
                    for k, v in aplanar_listas(vm):
                        listas[k].append(v)
    n = len(muestra) or 1
    inf.append(f"Muestra: {fmt_n(len(muestra))} reportes, {fmt_b(tot)} ({fmt_b(tot // n)} por reporte).\n")
    inf.append(tabla([(k, fmt_b(v // n), pct(v, tot)) for k, v in por_bloque.most_common()],
                     ["Bloque", "Promedio por reporte", "% del JSON"]) + "\n")
    inf.append(tabla([(k, fmt_b(v // n), pct(v, tot)) for k, v in por_sub.most_common(20)],
                     ["Sub-bloque", "Promedio por reporte", "% del JSON"]) + "\n")
    if vms:
        inf.append(f"VMs por reporte: promedio {statistics.mean(vms):.1f}, máximo {max(vms)}.")
        for k, vals in sorted(listas.items()):
            inf.append(f"- Lista `virtual_layer[].{k}`: promedio {statistics.mean(vals):.1f} elementos por VM, máximo {max(vals)}.")
    inf.append("")


def aplanar_listas(vm, prefijo=""):
    for k, v in vm.items():
        ruta = f"{prefijo}.{k}" if prefijo else k
        if isinstance(v, list):
            yield ruta, len(v)
        elif isinstance(v, dict):
            yield from aplanar_listas(v, ruta)


def seccion_repeticion(muestra, inf):
    inf.append("## D. Repetición entre reportes consecutivos\n")
    por_hosp = defaultdict(list)
    for h, ts, js in muestra:
        por_hosp[h].append((parse_ts(ts), js))
    hojas_tot = hojas_iguales = bytes_tot = bytes_iguales = pares = 0
    cambia, aparece = Counter(), Counter()
    bytes_cambiantes = []
    for h, filas in por_hosp.items():
        filas.sort(key=lambda x: x[0] or datetime.min)
        prev = None
        for _, js in filas:
            d = cargar(js)
            if not isinstance(d, dict):
                prev = None
                continue
            d = {k: v for k, v in d.items() if k != "envelope"}   # envelope: timestamp, siempre cambia
            hojas = aplanar(d)
            if prev is not None:
                pares += 1
                b_cambio = 0
                for ruta, v in hojas.items():
                    tb = len(ruta) + len(json.dumps(v, ensure_ascii=False))
                    hojas_tot += 1
                    bytes_tot += tb
                    rn = normalizar_ruta(ruta)
                    aparece[rn] += 1
                    if ruta in prev and prev[ruta] == v:
                        hojas_iguales += 1
                        bytes_iguales += tb
                    else:
                        cambia[rn] += 1
                        b_cambio += tb
                bytes_cambiantes.append(b_cambio)
            prev = hojas
    if not pares:
        inf.append("_Sin pares de reportes consecutivos en la muestra._\n")
        return {}
    inf.append(f"{fmt_n(pares)} pares de reportes consecutivos de {len(por_hosp)} hospitales (sin `envelope`).\n")
    inf.append(f"- Hojas iguales al reporte anterior: **{pct(hojas_iguales, hojas_tot)}**.")
    inf.append(f"- Bytes iguales al reporte anterior: **{pct(bytes_iguales, bytes_tot)}**.")
    inf.append(f"- Lo que cambia por reporte: promedio {fmt_b(int(statistics.mean(bytes_cambiantes)))} "
               f"(mediana {fmt_b(int(statistics.median(bytes_cambiantes)))}).\n")
    filas = [(f"`{r}`", pct(c, aparece[r])) for r, c in cambia.most_common(40)]
    inf.append("Campos que más cambian (candidatos a métricas tipadas):\n")
    inf.append(tabla(filas, ["Campo", "Cambia en"]) + "\n")
    estables = [r for r, n in aparece.items() if cambia[r] / n < 0.01]
    inf.append(f"Campos que cambian en menos del 1 % de los reportes (inventario): {fmt_n(len(estables))} de "
               f"{fmt_n(len(aparece))} rutas distintas.\n")
    return {"pares": pares, "bytes_cambio": statistics.mean(bytes_cambiantes),
            "rutas_cambiantes": [r for r, c in cambia.items() if c / aparece[r] >= 0.01]}


def seccion_compresion(muestra, inf):
    inf.append("## E. Compresión\n")
    jsons = [js if isinstance(js, str) else json.dumps(js) for _, _, js in muestra if js]
    if not jsons:
        inf.append("_Sin datos._\n")
        return {}
    random.seed(7)
    crudo = sum(len(j.encode()) for j in jsons)
    filas = []
    resultados = {}

    def por_fila(nombre, fn):
        t0 = time.perf_counter()
        comp = sum(len(fn(j.encode())) for j in jsons)
        filas.append((nombre + " (cada reporte por separado)", fmt_b(comp), f"{crudo / comp:.1f}x",
                      f"{time.perf_counter() - t0:.1f} s"))
        resultados[nombre] = crudo / comp

    por_fila("zlib-6", lambda b: zlib.compress(b, 6))
    if zstandard:
        por_fila("zstd-3", zstandard.ZstdCompressor(level=3).compress)
        mitad = len(jsons) // 2
        entrenamiento = [j.encode() for j in random.sample(jsons[:mitad] or jsons, min(2000, mitad or len(jsons)))]
        try:
            dic = zstandard.train_dictionary(112_640, entrenamiento)
            cd = zstandard.ZstdCompressor(level=3, dict_data=dic)
            prueba = jsons[mitad:] or jsons
            cr = sum(len(j.encode()) for j in prueba)
            cp = sum(len(cd.compress(j.encode())) for j in prueba)
            filas.append(("zstd-3 + diccionario (cada reporte)", fmt_b(int(cp * crudo / cr)), f"{cr / cp:.1f}x", "-"))
            resultados["zstd-dic"] = cr / cp
        except zstandard.ZstdError as e:
            filas.append((f"zstd + diccionario: {e}", "-", "-", "-"))

    # Archivo frío: un archivo por hospital y día (lo que propone docs/14 §6)
    por_hosp = defaultdict(list)
    for h, _, js in muestra:
        if js:
            por_hosp[h].append(js if isinstance(js, str) else json.dumps(js))
    archivo = "\n".join("\n".join(v) for v in por_hosp.values()).encode()

    def archivo_completo(nombre, fn):
        t0 = time.perf_counter()
        comp = sum(len(fn("\n".join(v).encode())) for v in por_hosp.values())
        filas.append((nombre + " (archivo por hospital y día)", fmt_b(comp), f"{len(archivo) / comp:.1f}x",
                      f"{time.perf_counter() - t0:.1f} s"))
        resultados["archivo_" + nombre] = len(archivo) / comp

    archivo_completo("zlib-9", lambda b: zlib.compress(b, 9))
    archivo_completo("lzma", lambda b: lzma.compress(b, preset=6))
    if zstandard:
        archivo_completo("zstd-19", zstandard.ZstdCompressor(level=19).compress)
        archivo_completo("zstd-19 long", zstandard.ZstdCompressor(
            compression_params=zstandard.ZstdCompressionParameters.from_level(19, window_log=27)).compress)
    inf.append(f"Muestra: {fmt_n(len(jsons))} reportes, {fmt_b(crudo)} sin comprimir.\n")
    inf.append(tabla(filas, ["Método", "Comprimido", "Tasa", "Tiempo"]) + "\n")
    if not zstandard:
        inf.append("_Sin el módulo `zstandard`: solo zlib y lzma._\n")
    return resultados


def seccion_estimacion(n_dia, bytes_json_dia, rep, comp, muestra, inf):
    inf.append("## F. Estimación del modelo nuevo\n")
    if not rep:
        inf.append("_Sin datos de repetición._\n")
        return
    n_campos = len(rep["rutas_cambiantes"])
    # Por reporte, cuántos valores cambiantes hay en promedio (hojas cuyas rutas cambian >=1%)
    valores = []
    rutas = set(rep["rutas_cambiantes"])
    for _, _, js in muestra[:3000]:
        d = cargar(js)
        if isinstance(d, dict):
            valores.append(sum(1 for r in aplanar({k: v for k, v in d.items() if k != "envelope"})
                               if normalizar_ruta(r) in rutas))
    vpr = statistics.mean(valores) if valores else 0
    # Fila tipada en Postgres: ~8 B por valor numérico + ~40 B de encabezado/clave por fila; se
    # supone una fila por entidad (host, cada VM, cada disco) ≈ vpr / 4 filas por reporte.
    bytes_reporte = vpr * 8 + max(1, vpr / 4) * 40
    dia_tipado = bytes_reporte * n_dia
    inf.append(f"- Reportes por día (último día): {fmt_n(n_dia)}. JSON por día hoy: {fmt_b(bytes_json_dia)}.")
    inf.append(f"- Campos que cambian (≥ 1 % de los reportes): {n_campos} rutas; en promedio "
               f"**{vpr:.0f} valores cambiantes por reporte**.")
    inf.append(f"- Métricas tipadas sin comprimir (estimado): {fmt_b(int(bytes_reporte))} por reporte → "
               f"**{fmt_b(int(dia_tipado))} por día**, {fmt_b(int(dia_tipado * 365))} por año.")
    inf.append(f"- Con compresión columnar 10x (TimescaleDB, supuesto): {fmt_b(int(dia_tipado * 36.5))} por año.")
    for clave, nombre in (("archivo_zstd-19 long", "zstd-19 long"), ("archivo_zstd-19", "zstd-19"),
                          ("archivo_lzma", "lzma")):
        if clave in comp:
            anio = bytes_json_dia * 365 / comp[clave]
            inf.append(f"- Crudo archivado con {nombre} ({comp[clave]:.1f}x medido): **{fmt_b(int(anio))} por año**.")
            break
    if "zstd-dic" in comp:
        inf.append(f"- Crudo en la base, comprimido por fila con diccionario ({comp['zstd-dic']:.1f}x): "
                   f"{fmt_b(int(bytes_json_dia * 30 / comp['zstd-dic']))} para 30 días.")
    inf.append("")


def seccion_software(conn, inf):
    inf.append("## G. software_monitoring y reportes_uso (últimos 30 días)\n")
    hasta = conn.execute("SELECT MAX(timestamp) FROM software_monitoring").fetchone()[0]
    if not hasta:
        inf.append("_Sin datos._\n")
        return
    desde = (parse_ts(hasta) - timedelta(days=30)).isoformat(sep=" ")
    filas = conn.execute("""
        SELECT app_name, COUNT(*) AS n, COUNT(DISTINCT hospital_id) AS h,
               SUM(LENGTH(extra_data)) AS b
        FROM software_monitoring WHERE timestamp >= ? GROUP BY app_name ORDER BY n DESC""", (desde,)).fetchall()
    inf.append(tabla([(r["app_name"], fmt_n(r["n"] // 30), r["h"], fmt_b((r["b"] or 0) // 30)) for r in filas],
                     ["app_name", "Filas por día", "Hospitales", "extra_data por día"]) + "\n")
    r = conn.execute("SELECT COUNT(*), SUM(LENGTH(kpi_json_data)), COUNT(DISTINCT hospital_id) FROM reportes_uso").fetchone()
    inf.append(f"reportes_uso: {fmt_n(r[0])} filas, {fmt_b(r[1] or 0)}, {r[2]} hospitales.\n")


def seccion_tiempos(conn, inf):
    inf.append("## H. Lecturas calientes actuales\n")
    hosp = conn.execute("""SELECT hospital_id FROM reportes_historicos
                           WHERE timestamp >= (SELECT datetime(MAX(timestamp), '-1 day') FROM reportes_historicos)
                           GROUP BY hospital_id ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
    hid = hosp[0] if hosp else None
    filas = []

    def t(nombre, sql, params=(), parsear=False):
        def correr():
            rows = conn.execute(sql, params).fetchall()
            if parsear:
                for r in rows:
                    cargar(r[-1])
            return len(rows)
        try:
            n, s = medir(correr)
            filas.append((nombre, fmt_n(n), f"{s * 1000:,.0f} ms".replace(",", ".")))
        except sqlite3.Error as e:
            filas.append((nombre, "error", str(e)))

    t("Motor de alertas: último reporte por hospital (+ JSON)", """
        SELECT h.hospital_id, h.full_json_data FROM reportes_historicos h
        INNER JOIN (SELECT hospital_id, MAX(timestamp) AS max_t FROM reportes_historicos GROUP BY hospital_id) m
        ON h.hospital_id = m.hospital_id AND h.timestamp = m.max_t""", parsear=True)
    if hid:
        t(f"Gráfico 30 días de {hid} (LIMIT 15000, + JSON)", """
            SELECT timestamp, full_json_data FROM reportes_historicos
            WHERE hospital_id = ? AND timestamp >= (SELECT datetime(MAX(timestamp), '-30 day') FROM reportes_historicos)
            ORDER BY timestamp LIMIT 15000""", (hid,), parsear=True)
        t(f"Resumen: todo reportes_uso de {hid} (+ JSON)",
          "SELECT timestamp, kpi_json_data FROM reportes_uso WHERE hospital_id = ?", (hid,), parsear=True)
        t(f"Pestaña Software 7 días de {hid}", """
            SELECT app_name, component_id, metric_value, extra_data FROM software_monitoring
            WHERE hospital_id = ? AND timestamp >= (SELECT datetime(MAX(timestamp), '-7 day') FROM software_monitoring)""",
          (hid,), parsear=True)
    inf.append(tabla(filas, ["Lectura", "Filas", "Tiempo"]) + "\n")
    inf.append("_Tiempos en esta PC con la copia en disco local; en el server pueden ser mayores._\n")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--out", default="informe_fase0.md")
    ap.add_argument("--horas", type=int, default=24, help="ventana de la muestra (por defecto, el último día)")
    ap.add_argument("--sin-meses", action="store_true", help="saltear B (recorre toda la tabla)")
    a = ap.parse_args()

    conn = abrir(a.db)
    inf = [f"# Fase 0 — mediciones sobre `{a.db}`\n",
           f"Generado {datetime.now():%Y-%m-%d %H:%M}. Solo lectura. Ver docs/14 §9.\n"]
    pasos = [("A", lambda: seccion_tablas(conn, inf))]
    if not a.sin_meses:
        pasos.append(("B", lambda: seccion_meses(conn, inf)))
    for nombre, fn in pasos:
        print(f"[{nombre}] …", flush=True)
        fn()

    print("[C-F] muestra …", flush=True)
    muestra, desde, hasta = muestra_ultimo_dia(conn, a.horas)
    inf.append(f"_Muestra para C a F: reportes entre {desde} y {hasta}._\n")
    seccion_composicion(muestra, inf)
    rep = seccion_repeticion(muestra, inf)
    comp = seccion_compresion(muestra, inf)
    bytes_dia = sum(len(js if isinstance(js, str) else json.dumps(js)) for _, _, js in muestra) * 24 / a.horas
    seccion_estimacion(int(len(muestra) * 24 / a.horas), int(bytes_dia), rep, comp, muestra, inf)
    print("[G] …", flush=True)
    seccion_software(conn, inf)
    print("[H] …", flush=True)
    seccion_tiempos(conn, inf)

    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(inf))
    print(f"Informe: {a.out}")


if __name__ == "__main__":
    main()
