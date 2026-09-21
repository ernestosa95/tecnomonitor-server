# Plan — redes DICOM (periférico / caché / central) en el mapa y las listas

**Estado — 2026-09-18: diagramado, NO ejecutado.** Solo análisis y plan; no se tocó código ni
base. Las decisiones de la sección 8 ya están cerradas; los puntos abiertos están en la 9.

**Prototipo navegable** (datos de ejemplo, sin backend; abrir el archivo en el navegador):
[`prototipos/prototipo_redes_dicom.html`](prototipos/prototipo_redes_dicom.html). Muestra el
mapa con selector de red, la lista con filtro, el detalle del hospital con la clasificación de
reglas, el modal del ABM y la tarjeta "Redes" con "destinos sin asignar".

## 1. Objetivo

Hoy cada hospital se monitorea como un nodo aislado. Algunos pertenecen a una **red DICOM**
donde se interconectan. Se quiere:

1. Registrar a mano, por hospital, su **categoría** dentro de la red y con qué otros hospitales
   se conecta.
2. **Mostrar la red de forma sencilla en el mapa.**
3. **Buscar/filtrar el conjunto de hospitales por la red** a la que pertenecen.

Categorías iniciales (pueden sumarse más a futuro):

| Rol | Qué es |
|---|---|
| `periferico` | Hospital muy chico, que solo visualiza: consulta imágenes de otro nodo de la red. |
| `cache` | Tiene un mini-PACS; con reglas de autoenrute envía las imágenes al nodo central. |
| `central` | Nodo central de la red. Puede conectarse con otros centrales. |

Solo algunos hospitales pertenecen a una red; **muchos son nodos aislados y no cambian en
nada.**

## 2. Qué hay hoy (revisado 2026-09-18)

- **El AE title del destino ya llega, sin tocar el agente.** En una regla de autoenrute el
  nombre del nodo de destino (`to_nickname`, tabla `DICOMCLIENT` de ExtensaPACS) **es el AE
  title del PACS de destino**, y `to_hostname` es su nombre descriptivo. Lo confirman las 43
  reglas de los 5 hospitales del extracto de 10 días (2026-09-08 → 09-18):

  | Hospital | Destino (`to_nickname` = AE title) | `to_hostname` | Reglas |
  |---|---|---|---|
  | H45 | `SIPROSAPAD` | PENTALOGIC PACS | 22 (todas) |
  | H44 | `SIPROSAHCST` | PENTALOGIC PACS | 8 |
  | H44 | `hcszswfmFIR` | PACS PROV | 8 |
  | HUPC | `CORD-PACS-BK` | ARHCORDPCS2V | 1 |
  | P03 | `ENTP_P03` | entelai192 | 1 |
  | P12 | `SynapseDicomSCP` | PACS LOCAL Synapse | 3 |

- **Matiz clave: un mismo PACS central puede tener un AE title por cada hospital que le
  envía.** H44 y H45 envían al mismo "PENTALOGIC PACS", pero con `SIPROSAHCST` y
  `SIPROSAPAD`. Un hospital, entonces, necesita registrar **varios identificadores**, y el
  cruce se hace por AE title **o** por hostname.
- **Hay destinos que no son hospitales de la red**: `SynapseDicomSCP` (PACS local del propio
  hospital), `ENTP_P03` (sistema de IA), `hcszswfmFIR` (PACS provincial). Una regla puede ser
  "de la red", "propia/local" o "externa", y hay que poder distinguirlas.
- El AE title **no es clave global** (puede haber genéricos repetidos), así que el cruce se
  acota a los miembros de la red y el enlace manual sigue siendo la fuente de verdad.
- **En `index_beta.html` hay un solo mapa**: el panel "Cobertura Geográfica" del dashboard
  (`#map-dashboard-container`, al lado de la lista de hospitales, con su propio filtro de estado
  y el botón de recorrido). Lo arma `initMapaDashboard`/`renderizarMarcadoresDash` en
  `static/script.js` con `GET /api/mapa-data` (`routers/resumen_red.py`) y la variable global
  `mapData`. `initMapa`/`renderizarMarcadores` (`#map-container`) pertenecían a la plantilla
  legacy `index.html` (retirada el 2026-09-21); si siguen en `script.js` son código muerto (REQ-02) y **no se tocan**.
- `/api/mapa-data` hace **una consulta por hospital** (~80) para saber si está online. Se
  aprovecha esta entrega para resolverlo con una sola consulta agrupada.
- `HospitalDTO` (`routers/hospitales_metadata.py`): el `PUT` pisa todos los campos del
  hospital. **No se le agrega la red**: un `script.js` desactualizado en caché la borraría.
- Los hospitales sin latitud/longitud no aparecen en el mapa (filtro existente); solo en
  listas.
- **La plantilla viva es `index_beta.html`**, no `index.html` (ver `docs/README.md`).

## 3. Modelo de datos

Solo **tablas nuevas**: `Base.metadata.create_all()` las crea al arrancar, sin script de
migración y sin tocar `hospitales_metadata`.

### `redes_dicom`
`id`, `nombre` (único), `descripcion`, `color`, `activa`, `created_at`.

### `red_dicom_miembros`
`hospital_id` (**clave primaria** → un hospital pertenece a una sola red; relajar esto a futuro
es cambiar la PK), `red_id`, `rol`, `updated_at`, `updated_by`.
Si un hospital no tiene fila, es un nodo aislado.

### `red_dicom_enlaces`
Grafo dirigido dentro de una red: `id`, `red_id`, `origen_hospital_id`, `destino_hospital_id`,
`tipo`, `notas`, `activo`. `UniqueConstraint(origen, destino, tipo)`.

| `tipo` | Sentido |
|---|---|
| `envia` | El origen **envía** imágenes al destino por autoenrute (caché → central; también central → central). |
| `consulta` | El origen **consulta/visualiza** desde el destino (periférico → cualquier nodo de la red). |

- Admite **varios destinos** por hospital (ej. un central de respaldo).
- **Sin jerarquía**: no hay niveles ni validación de árbol. Los centrales se conectan entre sí
  libremente; una conexión en ambos sentidos son dos filas (la UI ofrece "en ambos sentidos").
- Validaciones de servidor: origen y destino existen, **son miembros de la misma red**,
  origen ≠ destino, sin duplicado.

### `hospital_nodos_dicom` — "PACS de este hospital"
Los nombres con los que **otros equipos ven a este hospital**: `id`, `hospital_id`, `ae_title`
(el nombre de nodo que usan las reglas de autoenrute, `to_nickname`), `hostname` (nullable,
`to_hostname`), `descripcion`.

- **Varios por hospital**: un central registra un AE title por cada hospital que le envía
  (`SIPROSAPAD`, `SIPROSAHCST`, ...) y/o su hostname ("PENTALOGIC PACS"); un hospital con PACS
  de respaldo registra ambos.
- **Comparación normalizada**: sin distinguir mayúsculas ni espacios de más
  (`SIPROSAPAD` = `siprosapad`); se cruza por `ae_title` o, si no, por `hostname`.
- **Unicidad**: no es global, pero **dentro de una misma red un identificador no puede
  repetirse en dos hospitales** (el servidor lo rechaza al guardar): si no, el cruce sería
  ambiguo.
- También sirve para marcar las reglas **propias/locales** (destino = un PACS del mismo
  hospital, ej. `SynapseDicomSCP` en P12).

### Catálogo de roles: en código, no en la base
Una constante (`ROLES_RED_DICOM`) con, por rol: etiqueta, glifo del mapa y **tipos de enlace
permitidos**. Sumar una categoría nueva = una entrada más, sin migración.

| Rol | Tipos de enlace de salida permitidos (propuesta) |
|---|---|
| `periferico` | `consulta` |
| `cache` | `envia` (y `consulta`) |
| `central` | `envia`, `consulta` (hacia otros centrales) |

La matriz avisa (no bloquea duro) cuando un enlace no la respeta; la única validación dura es
la de la red común.

## 4. Carga manual y verificación de reglas (interfaz)

### Carga
- **Modal del hospital** (`index_beta.html`, `modal-hospital`): sección "Red DICOM" con red,
  rol, **"PACS de este hospital"** (lista de AE titles/hostnames), y "envía / consulta a"
  (selector de miembros de la misma red, con tipo por destino). **"Recibe de" / "consultado
  por" se muestran solos**, invertidos, solo lectura.
- **Tarjeta "Redes"** en configuración: alta/edición/baja de redes (nombre, color) y listado de
  miembros por red.
- **Destinos sin asignar** (ayuda de carga): lista de los destinos observados en las reglas
  (`to_nickname` + `to_hostname` + cuántas reglas y de qué hospitales) que todavía no
  pertenecen a ningún hospital, con un selector "asignar a hospital / marcar como externo".
  Es el mismo patrón "Adoptar" del panel de Mirth: evita tipear AE titles a ciegas.

### Verificación: "¿qué reglas de este hospital son parte de la red?"
Parado en un hospital que pertenece a una red, se clasifica **cada regla** por su destino:

| Clase | Criterio | Qué muestra |
|---|---|---|
| `en_red` | destino = identificador de **otro miembro de la misma red** | hospital destino, y si hay un enlace `envia` declarado hacia él |
| `propia` | destino = un identificador **de este mismo hospital** | "PACS local" |
| `otra_red` | destino = identificador de un hospital de **otra red** o que **no es miembro** de esta | aviso "fuera de la red" con el hospital |
| `externa` | destino **no está registrado** en ningún hospital | "sin asignar" (ej. sistema de IA, PACS provincial) |

- Resumen en el encabezado: **"22 de 22 reglas van a la red"** / **"8 de 16 reglas van a la
  red"** (caso H44, con dos destinos).
- **Validación cruzada** de los enlaces manuales contra las reglas:
  - hay reglas hacia un miembro sin enlace declarado → **sugerir el enlace** ("Adoptar");
  - hay un enlace `envia` declarado sin ninguna regla que lo respalde → **advertencia**
    ("enlace sin reglas"). Los enlaces `consulta` no se validan: no hay reglas de autoenrute.
- Se muestra **dentro de la pestaña Software** del detalle del hospital (decisión 2026-09-19;
  las pestañas actuales son Infraestructura, Software, KPIs Uso e Integraciones): la tarjeta
  "Red DICOM" va **entre las secciones existentes (SSL, Mirth, Elasticsearch) y "Autoenrute
  DICOM"**, y cada **tarjeta de regla** existente (`mirth-pill`) gana una **insignia** con su
  clasificación. Se engancha en `renderizarSoftware()` (`static/script.js`, hoy arma la sección
  DICOM con `data.dicom_routing`).
- **Solo Admin e Ingeniería** ven la tarjeta y la insignia; un usuario `Cliente` con la pestaña
  Software habilitada ve las tarjetas de autoenrute exactamente como hoy. El servidor no le envía
  los datos de red (no alcanza con ocultarlos en la interfaz).
- Los datos salen de la última fila de cada regla en `software_monitoring`
  (`app_name='dicom_routing'`, `extra_data.to_nickname`/`to_hostname`); no requiere agente
  nuevo ni tabla adicional.

### Endpoints nuevos
**Todos limitados a `Admin` e `Ingenieria`**, incluida la lectura de las reglas clasificadas:
no usa `require_hospital_access("software")` (ese permiso también lo pueden tener usuarios
`Cliente` con la pestaña Software habilitada).

| Método | Ruta | Uso |
|---|---|---|
| GET/POST/PUT/DELETE | `/api/redes[/{id}]` | ABM de redes (borrar una red con miembros: rechazado, o "quitar todos" explícito, solo Admin) |
| GET | `/api/hospitales/{hid}/red` | Red, rol, enlaces de salida y de entrada, PACS del hospital |
| PUT | `/api/hospitales/{hid}/red` | Reemplaza red, rol, enlaces de salida y PACS en una sola transacción |
| DELETE | `/api/hospitales/{hid}/red` | Saca al hospital de la red (borra sus enlaces y su membresía) |
| GET | `/api/hospitales/{hid}/red/reglas` | Reglas del hospital clasificadas (`en_red`/`propia`/`otra_red`/`externa`) + sugerencias y advertencias |
| GET | `/api/redes/destinos-sin-asignar` | Destinos observados que no pertenecen a ningún hospital |
| GET | `/api/redes/{id}/mapa` | Miembros con coordenadas, estado y enlaces con salud (sección 5) |

- Sacar a un hospital de una red o cambiarlo de red **borra sus enlaces** (avisando antes en la
  UI): no pueden quedar enlaces entre redes distintas.

## 5. Mapa

### Zoom y agrupación
Sin *clusters*: se usan los controles de zoom y arrastre que ya trae Leaflet (decisión
2026-09-19). Las zonas con hospitales muy cercanos (ej. Tucumán) se resuelven acercando el mapa.

### Selector de red (lista y mapa a la vez)
Un desplegable **Red: todas / sin red / cada red** en la barra de la **lista** de hospitales,
junto al buscador y el filtro de estado. Filtra la lista y gobierna el mapa: con una red elegida,
sus hospitales se resaltan, el resto se atenúa (no se oculta) y el mapa encuadra la red. Debajo de
la barra aparece una **franja de resumen** de la red (miembros, salud de los envíos y los
miembros sin coordenadas). El recorrido automático respeta el filtro (como ya hace con
Online/Offline). El panel de mapa conserva su propio filtro de estado y el botón de recorrido.

### Cómo se dibuja
- **Color = estado** (online/offline, sin cambios). **Forma/tamaño = rol** (central grande,
  caché mediano, periférico chico): la categoría no depende solo del color. Leyenda visible.
- **Líneas solo con una red elegida** o al hacer clic en un miembro (con 80 hospitales, dibujar
  todo ensucia el mapa). Flecha origen → destino; **`consulta` en punteado**; un par en ambos
  sentidos se dibuja como una sola línea con doble flecha.
- **Salud del enlace** (fase 6): color de la línea = peor estado de las reglas clasificadas
  `en_red` hacia ese destino (verde/amarillo/rojo, reusando `alerts_engine.evaluar_cola`, con
  caché de ~60 s). Gris = enlace declarado sin reglas observadas.
- **Popup** del hospital: red, rol, "envía a", "recibe de", resumen de reglas.
- `/api/mapa-data` gana `red_id`, `red_nombre`, `rol` por hospital (campos nuevos, un
  `script.js` viejo los ignora) y pasa a **una sola consulta** para el estado online.

## 6. Búsqueda y filtro por red

- Filtro **"Red"** en la lista de hospitales del dashboard (sección 5) e **insignia Red · Rol
  bajo el ID** de cada fila. Es el mismo filtro que gobierna el mapa.
- **No se toca el directorio de hospitales del ABM** (Configuración › Hospitales): columnas y
  filtro quedan como están.
- Fuera de alcance: `cliente.html` (vista externa), igual que con el mapa de Mirth.

## 7. Agente: sin cambios

Se descartó el cambio de agente que figuraba en la primera versión de este plan (agregar un
campo `aetitle`): `to_nickname` **es** el AE title y `to_hostname` el nombre descriptivo, y ya
viajan en el payload por ambos caminos (SQL directo y Elastic, mismo contrato). No hay bump de
versión, no hay `build.bat` y no depende de Logstash.

## 8. Decisiones cerradas (2026-09-18)

1. **Una red por hospital**, por ahora (la PK de `red_dicom_miembros` lo impone; relajarlo es
   un cambio contenido).
2. **Sin jerarquía**, pero **varios centrales pueden conectarse entre sí**.
3. **El periférico se conecta con otros nodos de la red**, siempre en modo `consulta`; puede
   consultar de más de un nodo.
4. **Más categorías a futuro**: por eso el rol vive en un catálogo en código.
5. **Cada hospital registra el nombre de su PACS** (AE title, y hostname como respaldo), y con
   eso se clasifican, parado en un hospital, las reglas que pertenecen a la red.
6. **La tarjeta "Red DICOM" va dentro de la pestaña Software** del detalle del hospital.
7. **Permisos**: carga, lectura de la clasificación y tarjeta solo para `Admin` e
   `Ingenieria`; `Cliente` no ve nada de esto. `cliente.html` queda fuera.
8. **Mapa sin *clusters***: alcanza con el zoom y el arrastre de Leaflet.
9. **Estados vacíos, offline o sin datos**: alcanza con el código de color existente
   (no se diseñan pantallas aparte).
10. **Todo va en `index_beta.html`, tocándolo lo menos posible**: la lógica y los estilos viven
    en archivos nuevos y la plantilla solo recibe puntos de anclaje (sección 11).

## 9. Puntos abiertos

- **Matriz de enlaces permitidos por rol** (sección 3): la de arriba es una propuesta. Se deja
  no bloqueante hasta confirmarla.
- **Datos iniciales**: falta la lista real de redes, miembros, roles y enlaces, y el PACS
  (AE titles/hostnames) de cada hospital. Para levantar los destinos reales de todos los
  hospitales de una vez, correr en la base de producción:
  ```sql
  SELECT hospital_id,
         json_extract(extra_data,'$.to_nickname') AS ae_title_destino,
         json_extract(extra_data,'$.to_hostname') AS hostname_destino,
         COUNT(DISTINCT component_id) AS reglas
  FROM software_monitoring
  WHERE app_name='dicom_routing' AND timestamp >= datetime('now','-2 days')
  GROUP BY 1,2,3 ORDER BY 1,2;
  ```
- **¿Los AE titles por remitente son la norma?** En SIPROSA (H44/H45) sí. Si en otras redes
  el central usa un único AE title, alcanza con uno.
- **Nombre de las redes** y colores (los define el usuario al cargarlas).
- **Textos de la interfaz** ("Caché", "Periférico", "Central", "Adoptar enlace", "Destinos sin
  asignar", "Red DICOM"): el usuario los confirma antes de implementar.
- **Diferidos a propósito** (2026-09-19): comportamiento con redes de 15+ miembros (etiquetas
  que se pisan) — "lo vemos llegado el momento"; vista de celular — se revisa "de manera
  general más adelante".
- **Alertas por red** (fuera de alcance): si cae un central, hoy todos sus cachés alertan
  autoenrute a la vez. Un incidente agregado por red sería la siguiente iteración natural.

## 10. Fases

| # | Fase | Depende de | Entrega valor sola |
|---|---|---|---|
| 1 | **Reunir los datos reales**: redes, roles, enlaces y PACS de cada hospital (con la consulta de la sección 9); definir el CSV de importación | — | — |
| 2 | Servidor: 4 tablas, catálogo de roles, endpoints ABM, modal "Red DICOM" (con "PACS de este hospital") y tarjeta "Redes" | 1 | Sí: carga y consulta |
| 3 | **Clasificación de reglas por red**: `/api/hospitales/{hid}/red/reglas`, tarjeta "Red DICOM" en el detalle del hospital e insignia por regla | 2 | **Sí: es lo que se pidió para verificar cada hospital** |
| 4 | Mapa: `/api/mapa-data` con red/rol y consulta única, `/api/redes/{id}/mapa`, selector, glifos por rol, líneas, leyenda (ambos mapas) | 2 | **Sí: es el resultado visible en el mapa** |
| 5 | Listas: filtro "Red" y columna Red/Rol | 2 | Sí |
| 6 | Validación cruzada (sugerir enlaces / enlaces sin reglas), "destinos sin asignar" y salud del enlace en el mapa | 3 y 4 | Sí |

Las fases 3, 4 y 5 son independientes entre sí una vez hecha la 2. **Ninguna toca el agente.**

**Estado de la revisión de interfaz (2026-09-19)**: el prototipo fue aprobado en lo general y
se ajustó con estas decisiones (tarjeta dentro de Software con vista por rol, zoom de mapa). Falta
la confirmación de los textos antes de pasar a implementación.

## 11. Integración en `index_beta.html` — cambios mínimos (decisión 2026-09-19)

**Principio**: la plantilla viva es `index_beta.html` (ruta `/beta`; `index.html` era legacy y se
retiró el 2026-09-21). Recibe solo **puntos de anclaje**; la lógica y los estilos van en **archivos nuevos**, como
se hizo con el mapa de Mirth (`mapa_integraciones.js/.css`).

**Archivos nuevos**: `static/redes_dicom.js` (módulo `window.RedesDicom`),
`static/redes_dicom.css`, `routers/redes_dicom.py`.

| Pieza | Dónde | Cambio en `index_beta.html` | Cambio en `script.js` |
|---|---|---|---|
| Carga del módulo | `<head>` y final del `<body>` | `<link>` del CSS (junto al de `mapa_integraciones`) y `<script src="/static/redes_dicom.js">` **después de leaflet.js** | — |
| Selector de red | `.hosp-list-toolbar` del dashboard | `<select id="filter-red">` (1 línea; el módulo llena las opciones) | `aplicarFiltros()`: sumar `cumpleRed` a la condición y llamar `RedesDicom.decorarLista()` |
| Insignia bajo el ID y franja de resumen | filas de `#dashboard-body` | — (las inserta el módulo) | — (sale del hook anterior: `aplicarFiltros` ya corre tras cada render de la tabla) |
| Mapa: formas por rol, enlaces, leyenda, atenuado | `#map-dashboard-container` | — | `renderizarMarcadoresDash()`: `marker._hid = h.id` y, al final, `RedesDicom.dibujarMapa(mapDashInstance, mapDashMarkers, mapData)`. El módulo agrega su propia capa Leaflet |
| Recorrido automático respeta la red | `siguienteDestinoDash` | — | 1 línea: filtrar con `RedesDicom.cumple(h.id)` |
| Tarjeta "Red DICOM" + insignia por regla | pestaña Software | — | `data-regla="${regla.id_rule}"` en la tarjeta de regla (1 atributo) y `RedesDicom.enSoftware(currentHospitalId)` tras pintar (1 línea) |
| Sección en el modal del hospital | `#modal-hospital` | `<div id="hosp-red-dicom"></div>` tras `#bloque-datos-manuales` | `abrirModalHospital()` y `editarHospital()`: `RedesDicom.modalAbrir(id)`; `guardarHospital()`: `await RedesDicom.modalGuardar(id)` dentro del `res.ok` |
| Pantalla "Redes" | Configuración | 1 botón `conf-tab` (`switchConfTab('tab-redes', this); RedesDicom.cargarConfig()`) y `<div id="tab-redes" class="conf-tab-panel">` | — |

- **Total estimado**: ~5 líneas en `index_beta.html` y ~9 sentencias de una línea en `script.js`.
  No cambian: la estructura de las tablas, las columnas, los filtros existentes, `HospitalDTO`
  ni las otras pestañas.
- **Datos**: `/api/mapa-data` suma `red_id`, `red_nombre` y `rol` por hospital (los consume el
  módulo a través de `mapData`).
- **Rol `Cliente`**: el módulo no se activa (no hace pedidos) y los endpoints devuelven 403.
- **Idempotencia**: `refresco-vivo.js` y los re-render vuelven a pintar la pestaña Software y la
  lista; el módulo borra lo que insertó antes de volver a insertarlo.
- **Orden de carga**: `leaflet.js` se incluye después de `script.js`; el módulo solo usa `L`
  dentro de `dibujarMapa`, nunca al cargarse.
- **Lo que difiere del prototipo**: nada estructural; el prototipo ya reproduce el layout de la
  lista + panel de mapa del dashboard y las tarjetas de regla de Software.

## 12. Despliegue (a comunicar al ejecutar)

- Tablas nuevas: **sin script de migración** (las crea `create_all` al arrancar).
- Copiar **juntos** `database.py` y los archivos que lo usan (lección de la etapa 2 del
  baseline DICOM: un `database.py` viejo tumba los endpoints nuevos).
- Servidor: `routers/redes_dicom.py` (nuevo), `dashboard.py` (registro), `resumen_red.py`
  (`/api/mapa-data`), `static/redes_dicom.js` y `.css` (nuevos), `static/script.js` y
  `templates/index_beta.html` (solo los puntos de anclaje de la sección 11).
- Agente: **sin cambios**.

## 13. Riesgos

1. **AE titles genéricos repetidos** → mitigado: el cruce se acota a los miembros de la misma
   red y un identificador no puede repetirse dentro de una red; el enlace manual manda.
   **Un central con un AE title por remitente** → se registran varios por hospital.
2. **Enlaces huérfanos** al mover un hospital de red → se borran al cambiarlo (con aviso).
3. **Mapa recargado** → líneas solo con red elegida o clic, y atenuado del resto.
4. **Coordenadas faltantes** → los miembros sin lat/long no aparecen en el mapa; el panel de la
   red debe listarlos aparte para que no parezca que faltan.
5. **Costo de la salud del enlace** → se calcula por hospital origen con caché corto y solo
   para la red elegida.
6. **Nombres mal tipeados** (`SIPROSAPAD` vs `SIPROSA-PAD`) → la regla queda `externa`. La lista
   de "destinos sin asignar" muestra el nombre exacto que llega, para asignarlo sin tipear.
