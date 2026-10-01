# Guía — desplegar el monitoreo del portal paciente en un hospital (agente 4.5.4)

**Estado: probada en H05 (2026-09-30).** Pasos para sumar el monitoreo de la cola de publicación
del portal paciente (REQ-07, [docs/16](../16-plan-actualizacion-y-despliegue.md)) a un hospital que
ya tiene TecnoMonitor. Solo aplica a las instalaciones con portal paciente (base `ExtensaMPS`).

Qué se mide: por estado, cuántos estudios hay en el RIS (`tbExamination.PublicationState`) y en la
cola de generación de ISO del MPS (`ExtMPS.QUEUE` + `JOBS`), con el más antiguo de cada uno, sobre
los últimos 30 días. El server arma una línea de tiempo por estado y alerta si la cola se demora o
aparecen estudios con error. Contrato: [docs/10 §7.7](../10-contrato-ingesta-agente.md).

## Antes de empezar

- El server de producción tiene que estar actualizado (commit `e97299d` o posterior). Si no, recibe
  la clave nueva y la descarta sin error: no se rompe nada, pero no se ve nada.
- Instalador del agente **4.5.4** (`build.bat` desde `main` del repo del agente).
- De `tecnomonitor-agent/elk/`: `ext_portal_paciente.conf` y, como referencia,
  `ext_tiempo_real-all-sito.bat`.
- Acceso RDP a la VM donde corren Logstash y SQL Server, y SSMS con el usuario que usa Logstash.

## 1. Probar las consultas a mano (SSMS)

1. Abrir `ext_portal_paciente.conf` y copiar las dos consultas (el `SELECT 'RIS' ...` y el
   `SELECT 'MPS' ...`). Correrlas en SSMS con `SET STATISTICS TIME ON;` delante, **con el mismo
   usuario que usa Logstash**.
2. Confirmar:
   - Que corren sin error. Si la del MPS falla con "base inexistente", `ExtensaMPS` está en otra
     instancia: el segundo bloque `jdbc` del `.conf` tiene que apuntar a ese host.
   - Que tardan uno o dos segundos como mucho. Si tardan bastante más, en el paso 2.5 usar el `.bat`
     de 1 hora (`ext_kpis_negocio-all-sito.bat`) en vez del de 5 minutos.
   - Que el MPS lista todos los estados del catálogo, aunque estén en 0.
3. Anotar los códigos y descripciones de los dos catálogos. Los conocidos (H05) están en la tabla
   del final; si aparece uno nuevo, avisar para clasificarlo en el server.

## 2. Logstash

1. Copiar `ext_portal_paciente.conf` a `C:\Estensa\ELK\Configfile\`.
2. **Completar la conexión copiándola de un `.conf` que ya funcione en ese hospital** (por ejemplo
   `ext_dicom_queues.conf`). No dejar nada del repo sin revisar:
   - **El archivo tiene DOS bloques `jdbc` (RIS y MPS). Completar `jdbc_connection_string`,
     `jdbc_user` y `jdbc_password` en los dos.** En H05 el segundo quedó sin credenciales: el RIS
     llegaba, el MPS no, y Logstash no terminaba nunca (ver paso 2.6).
   - En el bloque `output`, reemplazar `<ELASTIC_HOST>` por el host de los otros `.conf` (en H05,
     `127.0.0.1:29200`) y revisar `user`/`password`.
   - Buscar que no quede ningún `<` fuera de los comentarios (líneas con `#`): ni `<PASSWORD>` ni
     `<ELASTIC_HOST>`.
3. Correrlo una vez a mano desde una consola:
   ```
   set JAVA_HOME=
   C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_portal_paciente.conf
   ```
   Tiene que mostrar **dos** líneas `logstash.inputs.jdbc` con su tiempo entre paréntesis (una por
   consulta) y terminar con `Logstash shut down.` en menos de un minuto. Si se queda reintentando,
   una de las dos conexiones está mal: cortar con Ctrl+C y volver al paso 2.2.
4. Verificar el índice:
   ```
   curl -u usuario:clave "http://<host>:29200/ext_portal_paciente/_search?size=40&pretty"
   ```
   Un documento por estado (`RIS_1`, `RIS_4`, `MPS_1`, ...), todos con el mismo `checked_at` reciente.
5. Sumarlo al `.bat` de 5 minutos. Si el hospital ya tiene `ext_tiempo_real-all-sito.bat`,
   **agregar** estas dos líneas antes del `timeout /t 30` final (no pisar el `.bat` del sitio con el
   del repo, puede tener rutas propias):
   ```
   timeout /t 10 /nobreak
   CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_portal_paciente.conf
   ```
   Si no lo tiene, crear la tarea programada de 5 minutos como indica la
   [guía de hospital nuevo](18-guia-configuracion-hospital-nuevo.md) (paso 1.4).
6. En la tarea programada de 5 minutos: **Settings → "Stop the task if it runs longer than" = 4
   minutos.** Si un pipeline se cuelga, la tarea se corta sola. Sin esto, la tarea queda en
   "Running" y Windows descarta cada disparo siguiente (`322 Launch request ignored, instance already
   running` en el History): en H05 eso frenó también el autoenrute DICOM del mismo `.bat`.
7. Ejecutar la tarea a mano una vez y confirmar que vuelve a "Ready". Si queda "Running", terminarla
   (clic derecho → End) y matar el Java que haya quedado:
   ```
   wmic process where "name='java.exe'" get processid,commandline | findstr /i portal_paciente
   taskkill /PID <id> /F
   ```
   Solo ese proceso: puede haber otros Logstash del hospital corriendo.

## 3. Agente

1. Instalar la 4.5.4 encima de la versión actual (la configuración se conserva).
2. En la GUI → tarjeta **Elastic** → sub-tarjeta **"Portal paciente"**: índice `ext_portal_paciente`
   (viene por defecto) y **Test**. Tiene que responder `OK (lectura de Logstash del ...): RIS: ... ·
   MPS: ... (últimos 30 días)`.
   - "accesible, todavía sin datos": Logstash no escribió nunca (volver al paso 2.3).
   - Solo aparece `RIS:`: la consulta del MPS no está corriendo (credenciales del segundo `jdbc`,
     paso 2.2).
   - `403 Forbidden`: el usuario de lectura del agente no tiene permiso sobre el índice nuevo (ver
     paso 2 de la [guía de hospital nuevo](18-guia-configuracion-hospital-nuevo.md)). En H05 no hizo
     falta.
3. **Prender el interruptor de la sub-tarjeta** y guardar los cambios del hospital. El Test funciona
   con el interruptor apagado, pero sin prenderlo el agente no manda nada.
4. Dejar apagado "Portal paciente (directo a SQL)" de la tarjeta SQL: es el camino para hospitales
   sin Elastic (si están los dos, gana Elastic).

## 4. Verificar en el server

1. Pestaña **Software** del hospital → tarjeta **"Portal paciente (Cola de publicación)"**.
2. "Última lectura" tiene que avanzar cada 5 minutos. Si se queda fija, la tarea programada no está
   corriendo el `.bat` (la lectura que se ve es la de la corrida a mano).
3. Con el rango 1H, a la media hora tiene que haber varios puntos. Con una sola lectura, el gráfico
   muestra solo puntos; la línea aparece desde la segunda.
4. Comparar los totales de las fichas con lo que dio SSMS en el paso 1.
5. Revisar que ninguna ficha tenga el círculo azul ("sin clasificar"): es un código que el server no
   conoce. Avisar con el código y la descripción.

## 5. Alertas (no el mismo día)

Dejar pasar al menos un día mirando la tarjeta. Después, en **Configuración → Alertas → Portal
paciente**: prender el interruptor y ajustar las horas (6 por defecto). Son globales, no por
hospital. Al prenderlas, si la cola de algún hospital ya está demorada, en el minuto siguiente se
abre `PORTAL_DEMORA` con su ticket de Asana.

| Alerta | Cuándo se abre | Cuándo se cierra |
|---|---|---|
| `PORTAL_DEMORA` (WARNING) | El estudio más viejo pendiente en la cola del MPS lleva más de las horas configuradas | Cuando deja de haberlo |
| `PORTAL_BLOQUEOS` (WARNING) | Hay estudios con error (bloqueado, fallido o abortado) que entraron a la cola en las últimas 24 h | Cuando ninguno de los estudios con error entró en las últimas 24 h |

## Si algo sale mal

- **Agente:** apagar el interruptor de "Portal paciente" alcanza para que deje de mandar.
- **Logstash:** comentar con `REM` las dos líneas del portal en el `.bat`. El índice puede quedar.
- **Server:** no hace falta tocarlo; sin datos, la tarjeta no aparece.

## Códigos conocidos (H05, 2026-09-30)

| Origen | Código | Descripción | Clase en el server |
|---|---|---|---|
| RIS | 1 | Published | final |
| RIS | 2 | Not published | final |
| RIS | 3 | Revoked | final |
| RIS | 4 | To be published | pendiente |
| RIS | 5 | To be withdrawn | pendiente |
| RIS | NULL | Not Ready / In Progress (informe no definitivo) | no listo |
| MPS | 1 | IDLE | pendiente |
| MPS | 2 | PENDING | pendiente |
| MPS | 3 | CREATING | pendiente |
| MPS | 4 | DONE | final |
| MPS | 5 | FAILED | error |
| MPS | 6 | BLOCKED | error |
| MPS | 7 | ABORTED | error |
| MPS | ¿8? | WAITING | pendiente (por palabra) |
| MPS | 9 | BURNER | final |
| MPS | ¿10? | UNKNOWN | sin clasificar |

Los códigos con `¿?` no se vieron en la captura de H05 (solo las descripciones): confirmarlos.
Las finales y "no listo" no se grafican; las pendientes miden la demora; las de error alertan. La
tabla vive en `_POR_CODIGO` de `dashboard_app/alerts_engine/software/portal_paciente.py`.
