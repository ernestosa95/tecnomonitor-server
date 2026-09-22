# Guía — configurar un hospital nuevo (agente 4.5.2)

**Estado: borrador (2026-09-22).** Primer volcado de los pasos en orden; falta completar con
el detalle real de instalar un hospital nuevo de punta a punta (capturas, nombres exactos de
carpetas por sitio, tiempos esperados). Pensada para alguien de mi equipo que nunca hizo esta
instalación — si algo no queda claro haciendo el primer hospital nuevo, es un bug de esta guía,
avisar para corregirla.

Contexto general (qué es cada pieza y cómo se conectan) en
[17-arquitectura-flujo-datos.md](./17-arquitectura-flujo-datos.md) — leer primero si es la
primera vez que se toca esto.

## Antes de empezar

- Acceso RDP/consola a la VM del hospital donde corre (o va a correr) Logstash — normalmente la
  misma VM que SQL Server.
- Credencial de SQL Server con permisos de lectura sobre las bases de Extensa (la misma que ya
  usan los demás `.conf` del sitio, ver `sql.user`/`sql.pass` en la config del agente si el
  hospital ya tenía el camino SQL directo).
- Acceso admin al panel de TecnoMonitor (rol Admin o Ingeniería) para dar de alta el hospital y
  generar su token.
- Acceso a la configuración de seguridad del clúster de Elasticsearch de ese hospital (para el
  paso 2) — quién administra esto puede variar por sitio.

## Paso 1 — Pipelines de Logstash (índices de Elasticsearch)

Los `.conf` y `.bat` viven en `tecnomonitor-agent/elk/` del repo del agente. Cubren tres
cajones de cadencia distinta:

| Cajón (`.bat`) | Cadencia | `.conf` que agrupa |
|---|---|---|
| `ext_tiempo_real-all-sito.bat` | cada 5 min | `ext_dicom_queues.conf` (autoenrute DICOM) |
| `ext_kpis_negocio-all-sito.bat` | cada 1 hora | `ext_ris_metrics.conf`, `ext_pacs_metrics.conf`, `ext_users_metrics.conf` |
| `ext_checkdb-all-sito.bat` | cada 30 min | `ext_checkdb.conf` (integridad de bases, solo chequea si SQL Server se reinició) |

1. **Copiar los `.conf` y `.bat`** a la carpeta de configuración de Logstash del hospital
   (ej. `C:\Estensa\ELK\Configfile\`), junto a los pipelines que ya existan ahí.
2. **Editar los placeholders de conexión de cada `.conf` copiado:**
   - `jdbc_connection_string` — el host de SQL Server. **Si Logstash y SQL Server corren en la
     misma VM (caso más común), usar `localhost`, no un nombre de host.** Esto no es opcional:
     en P03 el `.conf` de checkdb se desplegó con `SRVDB-ESTENSA` (un placeholder que nunca fue
     un nombre real) y quedó colgado ~11 horas reintentando conectar sin salir nunca — revisar
     contra un `.conf` que ya esté funcionando en ese mismo sitio (ej. `ext_ris_metrics.conf` si
     ya estaba instalado) antes de asumir el valor de la plantilla.
   - `<PASSWORD>` → la contraseña real de `sql.user`.
   - `<ELASTIC_HOST>` (bloque `output`) → la IP/host del clúster de Elastic de ese hospital.
3. **Probar cada `.conf` aislado, antes de programar ninguna tarea:**
   ```
   logstash.bat -f ext_ris_metrics.conf --config.test_and_exit
   logstash.bat -f ext_ris_metrics.conf --path.data C:\Estensa\ELK\L\data_test_ris
   ```
   Confirmar en Kibana Dev Tools (`GET <indice>/_search`) que aparecen documentos con la forma
   esperada. Repetir por cada `.conf` nuevo antes de seguir — más fácil de diagnosticar uno por
   uno que los tres juntos dentro de un `.bat`.
4. **Crear una Tarea Programada de Windows por cajón** (no por `.conf` — los `.conf` de un mismo
   cajón van juntos en una sola tarea). Lo más seguro es exportar (`.xml`) una tarea que ya
   funcione en ese hospital, importarla, y solo cambiarle el nombre, la ruta del `.bat` y el
   desencadenador.
   - **Revisar dos pestañas a mano en cada tarea nueva, no confiar en que la plantilla las traiga bien:**
     - **Conditions** → destildar *"Start the task only if the computer is on AC power"* (y
       *"Stop if the computer switches to battery power"*). Es el default de Windows pensado
       para notebooks; en un servidor hace que los disparos se pierdan en silencio, sin dejar
       rastro ni en el History ni como corrida fallida.
     - **Settings** → tildar *"Run task as soon as possible after a scheduled start is missed"*,
       y confirmar que **"If the task is already running, then the following rule applies"**
       esté en **"Do not start a new instance"** — necesario para el cajón de `checkdb`
       (un CHECKDB real puede tardar horas) y no hace daño en los demás.
   - Confirmar en el **History** de la tarea que aparecen corridas nuevas sin tocar "Run" a mano.

## Paso 2 — Dar permiso al usuario de lectura (`selogger`) sobre los índices nuevos

El agente lee Elasticsearch con un usuario de solo lectura (`selogger` en los hospitales ya
configurados) que tiene un rol con una lista explícita de índices permitidos. **Un índice nuevo
no hereda permiso solo por existir** — hay que agregarlo a mano al rol en la configuración de
seguridad del clúster (Kibana → Stack Management → Roles, o el mecanismo equivalente si es
OpenSearch/otro), si no, el agente va a fallar con `403 Forbidden` al leerlo aunque Logstash ya
esté escribiendo bien (nos pasó exactamente esto en P03 con `ext_checkdb`).

Índices a agregar al rol de `selogger` (los que correspondan según qué se instaló):
`ext_dicom_queues`, `ext_ris_metrics_hourly`, `ext_pacs_metrics_hourly`,
`ext_users_metrics_hourly`, `ext_checkdb`, más el índice de logs de Suitestensa si aplica
(`elastic.index_pattern`, patrón `se-es-logging-*` por default).

## Paso 3 — Dar de alta el hospital y generar el token

1. En el panel de TecnoMonitor: **Configuración → Hospitales → + Nuevo hospital**. Completar
   `hospital_id` (el mismo que va a usar el agente, ej. `P03`), nombre, provincia, Asana project
   ID.
2. Al crear el hospital, el server devuelve un **token de ingesta en texto plano una sola vez**
   (`POST /api/hospitales-metadata`) — copiarlo ya, no se puede volver a ver después (el server
   solo guarda su hash). Si se pierde, no hay drama: **Configuración → Hospitales → regenerar
   token** (`POST /api/hospitales-metadata/{hid}/regenerar-token`) genera uno nuevo e invalida el
   anterior.

## Paso 4 — Configurar el agente

1. Instalar `TecnoMonitor_v4.5.2_Setup.exe` en la VM del hospital.
2. Abrir `TecnoMonitorConfig.exe` (pide UAC). Primera vez: genera un código de acceso propio de
   ese equipo, **anotarlo** — no se vuelve a mostrar.
3. Completar `Hospital ID` (el mismo del paso 3), `Auth Token` (el del paso 3, tal cual, sin
   editar), y la URL del server central.
4. Habilitar y completar cada tarjeta según lo que tenga este hospital: Proxmox/iDRAC/WMI, Mirth,
   SSL, y en la tarjeta de **Elastic** — `enabled_ris_metrics` (KPIs de RIS/PACS/usuarios) y
   `enabled_checkdb` (integridad de bases) si se instalaron esos pipelines en el Paso 1, apuntando
   a los mismos nombres de índice que Logstash está publicando (los defaults ya coinciden si no
   se cambiaron en el `.conf`).
5. **Usar el botón "Test conexión" de cada tarjeta antes de guardar** — más rápido que esperar al
   primer ciclo real para descubrir un problema de credenciales o de permisos (ver Paso 2).
6. Guardar. En modo servicio, reinicia el servicio solo.

## Paso 5 — Confirmar que los datos llegan

No dar por bueno un ✅ en la GUI del agente — confirmar en las dos puntas:

1. **Log del agente** (servicio): debería aparecer `✅ Ciclo completado y enviado OK` cada 5 min,
   sin `❌` en ninguno de los módulos habilitados.
2. **Log del server**: al recibir un reporte válido, loguea `✅ Reporte guardado: <hospital_id>` y
   la request HTTP devuelve `201 Created`. Un `401` ahí es casi siempre token/hospital_id que no
   coinciden (ver Paso 3).
3. **Dashboard**: entrar al detalle del hospital nuevo y confirmar que cada pestaña tiene datos
   frescos — Infraestructura (físico/virtual), Software (Mirth/SSL/DICOM/CHECKDB), y si aplica,
   KPIs de negocio.
4. Si algo vía Elastic no aparece (RIS/PACS/usuarios, DICOM, CHECKDB) aunque el resto del agente
   funcione bien, son módulos independientes entre sí — revisar primero si Logstash está
   escribiendo al índice (Kibana Dev Tools) antes de sospechar del agente.
