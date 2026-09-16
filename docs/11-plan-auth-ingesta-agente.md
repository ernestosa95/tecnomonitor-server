# Plan — autenticación del agente por token, escalonada por versión

**Estado: ✅ implementado — 2026-09-11.** Resuelve [S2](04-seguridad.md#s2) de forma
escalonada para no cortar ingesta de los ~80 hospitales que hoy reportan sin autenticación.
Código en `main.py`/`database.py`/`dashboard_app/core.py`/`routers/hospitales_metadata.py`,
verificado local con `TestClient` contra los 4 casos del §7.3. **Falta**: correr la
migración (`Accesorios/agregar_token_ingesta_hospitales.py`) en producción y coordinar con
quien arma el agente nuevo el rollout real hospital por hospital.

## 1. Decisión de diseño (confirmada en la conversación)

- Token único por hospital, generado desde el panel de admin al dar de alta el hospital
  (o mediante un botón "regenerar" para los hospitales que ya existen).
- El agente lo manda en el header `Authorization: Bearer <token>` de cada
  `POST /v1/hospital-status` (no dentro del JSON).
- El servidor identifica al hospital **por el token** (no al revés): busca qué hospital
  tiene ese token, y si el `hospital_id` que viene en `envelope` no coincide con el que
  resolvió el token, rechaza — así un token filtrado nunca puede inyectar datos a nombre
  de otro hospital.
- El token se guarda **hasheado** (SHA-256, no bcrypt — bcrypt es deliberadamente lento
  para frenar fuerza bruta de contraseñas *elegidas por una persona*; acá el token ya es
  aleatorio de sobra, y este hash se calcula en *cada* reporte que entra de *cada*
  hospital, así que la lentitud de bcrypt sería puro costo de CPU sin beneficio real).
- **Gate por versión, no por fecha**: el corte no es "aceptamos sin token durante un
  tiempo y después cortamos" — es "los payloads con `schema_version` viejo (`3.0` a
  `4.3`) siguen sin pedir nada, los que declaren `schema_version: "4.5"` **exigen** el
  token". Cada hospital migra solo, el día que se le actualiza el agente — no hace falta
  coordinar una fecha de corte global para 80 hospitales.

## 2. Modelo de datos

Agregar a `HospitalMetadata` (`database.py`):

- `ingest_token_hash` — string, el hash SHA-256 del token (nunca el token en texto
  plano). `nullable=True` (los hospitales que todavía no migraron no tienen uno).
- Índice único sobre esa columna: tiene que ser único entre todos los hospitales (dos
  hospitales no pueden compartir hash) y necesita búsqueda rápida, porque se consulta en
  **cada** reporte entrante de **cada** hospital.

**Migración**: igual que con el índice de `software_monitoring` en la Fase 1 —
`Base.metadata.create_all()` no agrega columnas a una tabla que ya existe. Hace falta un
script nuevo en `Accesorios/` (`ALTER TABLE hospitales_metadata ADD COLUMN
ingest_token_hash ...` + `CREATE UNIQUE INDEX ...`), corrido en producción después de
desplegar el código, mismo patrón que ya usamos.

No hace falta una tabla aparte: es una relación 1 a 1 (un hospital, un token vigente a la
vez — regenerar reemplaza el anterior, no acumula históricos). Si en el futuro hiciera
falta rotación con solapamiento (token viejo y nuevo válidos un tiempo) ahí sí conviene
una tabla propia, pero no es el caso hoy.

## 3. Generación del token

Un helper nuevo, mismo criterio que `core.generar_password_temporal()` (que ya generamos
en la Fase 1): aleatorio con `secrets`, alfabeto sin caracteres ambiguos (sin `0`/`O`/`1`/`l`),
24-32 caracteres. Vive en `dashboard_app/core.py` junto al otro generador, porque es el
mismo tipo de utilidad (generar un secreto aleatorio) y ya es el lugar compartido por 2+
routers.

**Cuándo se genera:**
1. **Automático al crear un hospital** (`POST /api/hospitales-metadata`, en
   `routers/hospitales_metadata.py`) — todo hospital nuevo nace con su token.
2. **Bajo demanda para los que ya existen**: un endpoint nuevo
   `POST /api/hospitales-metadata/{hid}/regenerar-token` (mismo router), que genera uno
   nuevo y pisa el hash guardado. Es lo que vas a usar para migrar los 80 hospitales de a
   uno, a medida que les actualizás el agente — no hace falta un backfill masivo de una
   sola vez.

**Cómo se ve el token una vez**: igual que con `password_temporal` al crear un usuario —
la respuesta de esos dos endpoints devuelve el token en texto plano **una sola vez**, en
el momento de generarlo. Después de eso, el servidor solo tiene el hash — si se pierde,
no se puede "recuperar", solo regenerar (lo cual invalida el anterior).

## 4. Cambios en el endpoint de ingesta (`main.py`)

Hoy la lógica es:

```
schema_version = envelope.get("schema_version")
si schema_version está en [3.0, 4.0, 4.1, 4.2, 4.3]:
    usar el payload tal cual
si no:
    asumir que es formato legacy V2 y transformarlo
```

Pasa a ser (mismo lugar, antes de decidir si transformar o no):

```
schema_version = envelope.get("schema_version")

si schema_version está en el set VIEJO [3.0, 4.0, 4.1, 4.2, 4.3]:
    (sin cambios) usar el payload tal cual, sin pedir token

si schema_version == "4.5" (el set NUEVO):
    leer el header Authorization: Bearer <token>
    si falta el header -> rechazar (401, mensaje genérico)
    calcular sha256(token) y buscar el hospital que tiene ese hash
    si no hay ningún hospital con ese hash -> rechazar (401, mensaje genérico)
    si el hospital_id del payload no coincide con el que resolvió el token -> rechazar (401)
    -> token válido, usar el payload tal cual (mismo camino que hoy)

si no (versión desconocida, ninguno de los dos sets):
    ver §5 -- esto es lo que hoy se trata como "asumir V2", y es justo el
    punto frágil que ya señalamos en el contrato de ingesta.
```

**Dónde poner el rechazo**: como `HTTPException(status_code=401, detail="No autorizado")`
o similar -- genérico a propósito, mismo criterio de "rechazo sin data leakage" que ya
tiene el bloque de excepciones de este endpoint (no le decimos al que llama *por qué*
falló, para no ayudar a alguien a adivinar tokens ajenos por descarte).

**Nota de implementación** (para cuando se ejecute, no ahora): el chequeo de header +
token puede resolverse con una dependencia de FastAPI (`Depends(...)`) que lee el header,
pero la decisión de "hace falta o no" depende de un campo *adentro* del body
(`schema_version`), así que la rama completa conviene resolverla dentro del propio
`recibir_reporte`, no como un dependency puro -- FastAPI no tiene forma limpia de hacer
una dependencia condicional al contenido del body sin parsearlo primero.

## 5. El problema de la lista cerrada de versiones (hardening relacionado, a decidir)

Hoy "no está en la lista de versiones nativas" se interpreta como "es formato legacy V2,
transformalo". Eso ya era frágil antes de este cambio (documentado en
[10-contrato-ingesta-agente.md §2](10-contrato-ingesta-agente.md#2--la-trampa-más-importante-schema_version)),
y con esto se vuelve más importante todavía: si en algún momento sale una versión `"4.6"`
y alguien se olvida de sumarla al set nuevo, esos reportes **no se van a rechazar
limpiamente por falta de token** -- van a caer en la rama de "legacy V2" y probablemente
se corrompan de forma silenciosa, exactamente el escenario que ya veníamos advirtiendo.

No es necesario resolver esto en el mismo cambio, pero dejo la opción sobre la mesa para
que decidas: en vez de "todo lo que no reconozco es V2", podría ser "V2 es *solo* cuando
`schema_version` viene vacío o directamente no está la clave `envelope`" -- y cualquier
otro string desconocido (ej. un typo, o una versión futura no registrada) se rechaza con
un error claro en vez de asumir que es el formato viejo. Es un cambio chico y bastante
más seguro a largo plazo, pero toca la misma función y agrega una tercera rama -- lo dejo
como punto a decidir, no lo doy por incluido en el alcance de este plan salvo que lo
confirmes.

## 6. Qué se actualizó en la documentación (✅ hecho)

- **[10-contrato-ingesta-agente.md §2bis](10-contrato-ingesta-agente.md#2bis--autenticación-por-token--a-partir-de-schema_version-45-vigente)**:
  sección de autenticación (header, formato, qué pasa si falta/es inválido), marcada
  *vigente* a partir de `schema_version 4.5` -- las versiones viejas siguen documentadas
  tal cual están.
- **[04-seguridad.md#s2](04-seguridad.md#s2)**: estado pasado a ✅ RESUELTO (parcial,
  escalonado) — sigue habiendo hospitales sin migrar hasta que actualicen el agente.
- **[07-plan-de-accion.md](07-plan-de-accion.md)** Fase 2, ítem 2.1: marcado ✅, Fase 2
  pasa de pausada a en curso.

## 7. Orden de ejecución sugerido

Este endpoint recibe tráfico real de ~80 hospitales constantemente -- cualquier error acá
tiene impacto inmediato en producción. Orden seguido:

1. ✅ Columna nueva + script de migración (`Accesorios/agregar_token_ingesta_hospitales.py`)
   -- sin tocar `main.py` todavía, cero riesgo. **Falta correrlo en producción.**
2. ✅ Helper de generación de token en `core.py` (`generar_ingest_token`,
   `hash_ingest_token`) + los dos endpoints del panel (`crear_hospital_metadata` genera uno
   automático, `POST /api/hospitales-metadata/{hid}/regenerar-token` para los que ya
   existen).
3. ✅ El cambio en `main.py` (el gate por versión, `_validar_token_ingesta`). Probados los
   4 casos con `TestClient`: versión vieja sin token (sigue funcionando igual que antes),
   versión `4.5` sin token (rechaza 401), versión `4.5` con token de otro hospital (rechaza
   401), versión `4.5` con token correcto (acepta 201) -- mismo nivel de prueba que el resto
   de los refactors.
4. ✅ Contrato de ingesta actualizado de "pendiente" a "vigente"
   ([10-contrato-ingesta-agente.md §2bis](10-contrato-ingesta-agente.md#2bis--autenticación-por-token--a-partir-de-schema_version-45-vigente)).
   **Falta**: coordinar con quien arme el agente nuevo el rollout real hospital por
   hospital (generar el token, configurarlo en el agente, confirmar el primer reporte en
   `4.5` antes de dar por migrado a ese hospital).

## 8. Qué queda explícitamente fuera de este plan

- Rate limiting del endpoint de ingesta (es un hallazgo aparte, ver
  [04-seguridad.md#s2](04-seguridad.md#s2) y [S5](04-seguridad.md#s5) sobre límite de
  tamaño de payload) -- se puede sumar en el mismo trabajo o después, es independiente
  del token.
- Rotación automática o expiración de tokens -- hoy el token no vence, solo se puede
  regenerar a mano. Si en algún momento hace falta expiración, es un campo más
  (`token_generado_en`) y una función criterio, pero no está pedido ahora.
- El endurecimiento de la detección de V2 legacy (§5) -- señalado, no incluido salvo que
  lo pidas.
