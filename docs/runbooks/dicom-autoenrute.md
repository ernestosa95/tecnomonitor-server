# Protocolo de Atención: Falla de Autoenrute DICOM (`dicom-autoenrute`)

### Introducción al funcionamiento del autoenrute DICOM

Las reglas de autoenrute operan de forma automática al momento en que se almacena un
estudio DICOM en el PACS de origen (Suitestensa). El sistema toma dicho estudio y lo
reenvía con una cadencia programada hacia otro PACS de destino. Este reenvío es
configurable: si se deja vacío el campo del nodo de origen, el sistema toma **todos** los
equipos (como tomógrafos, mamógrafos, ecógrafos o sistemas del hospital) que envían sus
imágenes al PACS de origen y Suitestensa se encarga de reenviarlas según la programación
establecida hacia el PACS de destino. Asimismo, es posible configurar reglas específicas
para enviar determinadas sintaxis o protocolos DICOM (por ejemplo, restringir mamografías
exclusivamente a la modalidad `DICOM MG` excluyendo `DOC` o `SR` para asegurar la
compatibilidad con el PACS de destino).

---

### 1. Síntomas comunes

* **Pérdida de conectividad con el PACS de destino:** Las reglas de autoenrutado encolan
  los estudios porque el PACS de destino no se encuentra disponible debido a caídas de
  red, falta de conectividad física/lógica o interrupciones en el servicio remoto.

* **Falta de autorización de nodos (AETitle no registrado):** El PACS de destino rechaza
  las transmisiones o las retiene en cola porque no tiene configurado ni autorizado al
  PACS local (Suitestensa) como un nodo emisor válido habilitado para enviarle estudios.

* **Baja lógica de reglas o equipos obsoletos:** El autoenrute se bloquea o acumula colas
  retenidas porque la regla de enrutamiento o el equipo de diagnóstico por imágenes
  asociado fue dado de baja o removido del diagrama operativo del hospital (tanto a nivel
  de PACS de destino como en el sistema del hospital), manteniendo activas tareas
  pendientes que ya no tienen validez ni necesidad operativa.

* **Estudios retenidos en colas locales:** Las imágenes quedan acumuladas en las consolas
  de adquisición o en el motor de salida local sin poder completar el flujo de
  transferencia programado.

---

### 2. Tablas de la base de datos relacionadas

En la arquitectura de bases de datos de Suitestensa (`ExtensaPACS`), la configuración
lógica y el estado transaccional de las reglas de reenvío automático y enrutamiento DICOM
se gestionan principalmente en las siguientes tablas del motor SQL:

1. **`[ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGRULES]`**
   * **Propósito:** Contiene la configuración general y la definición de todos los nodos y
     reglas de autoenrute configurados en el sistema.
   * **Campos clave:** `[IDRULE]` (identificador único de la regla), `[FROMNODE]` (nodo de
     origen), `[TONODE]` (nodo de destino), `[BUFFER]`, `[ACTIVE]` (estado
     activo/inactivo), `[SCHEDULE]`, `[DURATION]`, `[MAXTHREAD]` y `[MAXTOP]`.

2. **`[ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGQUEUE]`**
   * **Propósito:** Registra la cola transaccional activa de los estudios pendientes de
     enrutamiento y su estado actual de reintento.
   * **Campos clave:** `[IDQUEUE]`, `[IDRULE]`, `[FROMNODE]`, `[TONODE]`, `[STATE]`,
     `[TRYCOUNT]`, `[LASTPOSTINGTRY]` y `[LASTUPDATE_DT]`.

3. **`[ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGDELIVERED]`**
   * **Propósito:** Almacena el historial y registro de los estudios e instancias que ya
     han sido enrutados y entregados con éxito hacia el PACS de destino.
   * **Campos clave:** `[IDQUEUE]`, `[IDRULE]`, `[FROMNODE]`, `[TONODE]`, `[INSTANCE_KEY]`
     y `[DELIVERDATE]`.

---

### 3. Diagnóstico rápido de reglas de autoenrute y conectividad

Ante una alerta generada por el motor de autoenrute DICOM (`dicom_autoenrute.py`), ejecuta
los siguientes pasos operativos:

1. **Inspección de la regla en la base de datos:**
   * Consulta la tabla de reglas para verificar la vigencia y el estado operativo del
     enrutamiento:

     ```sql
     SELECT TOP (1000) [IDRULE], [FROMNODE], [TONODE], [BUFFER], [ACTIVE], [SCHEDULE],
            [DURATION], [GUID], [CREATEDON]
     FROM [ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGRULES]
     ```

   * Valida que los campos `FROMNODE`, `TONODE` y el estado `ACTIVE` correspondan al flujo
     esperado entre el equipo de origen y el PACS de destino.

2. **Verificación de la cola retenida:**
   * Revisa el volumen de instancias pendientes en la cola de enrutamiento:

     ```sql
     SELECT TOP (1000) [IDQUEUE], [IDRULE], [FROMNODE], [TONODE], [STATE], [TRYCOUNT],
            [LASTPOSTINGTRY]
     FROM [ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGQUEUE]
     WHERE [IDRULE] = <ID_DE_LA_REGLA>
     ```

3. **Verificación de recursos y servicios del servidor:**
   * Comprueba que los servicios críticos de integración y transferencia
     (`SLBrokerService`, `SLPacsService`, `SLPacsManagerService`) estén en ejecución.
   * Verifica que el consumo de memoria RAM y el almacenamiento en los discos físicos del
     servidor PACS central no superen el umbral crítico del 90%.

4. **Validación de parámetros DICOM (AETitle, IP y Puerto):**
   * Ingresa a la interfaz de **Administración → Gestión de red** para revalidar que la
     dirección IP, el puerto (por defecto `1104` para el servidor PACS) y el Application
     Entity Title (`AETitle`) de la modalidad o del PACS de destino sean exactos.
   * Asegúrate de que el nodo emisor local (Suitestensa) esté correctamente configurado
     como entidad autorizada en el destino.

5. **Comprobación del diagrama operativo (Baja lógica):**
   * Revisa si la regla de autoenrute o el equipo de diagnóstico asociado fue dado de baja
     recientemente del diagrama del hospital o de la institución, lo cual genera tareas
     encoladas obsoletas que deben purgarse.

---

### 4. Mitigación y acciones correctivas

1. Corregir los parámetros de red, puertos o AE Titles erróneos en la configuración del
   nodo dentro de Suitestensa.
2. Registrar los nodos faltantes y solicitar la autorización en la contraparte para
   restablecer la confianza mutua de transferencia DICOM.
3. Ejecutar los scripts de base de datos correspondientes para vaciar o purgar las colas
   de enrutamiento retenidas en `DICOMAUTOROUTINGQUEUE` debido a la caída de conectividad.
4. Reiniciar los servicios críticos de transferencia y el programador de tareas para
   reanudar el flujo normal de envío.
5. Dar de baja lógica aquellas reglas o equipos obsoletos que ya no formen parte del
   diagrama del hospital para evitar falsas alarmas persistentes en Asana.
6. Ajustar la configuración de sintaxis o protocolos en la regla (por ejemplo, filtrar solo
   modalidades `MG` excluyendo `DOC`/`SR`) si el PACS de destino rechaza objetos
   incompatibles.

---

### 5. Cuándo escalar

Derivar el incidente al equipo general de ingeniería antes de derivar a soporte de Nivel 2
si, tras los reinicios y la revalidación de nodos, persisten los bloqueos de transmisión,
fallas físicas en los equipos o saturación persistente en las colas de red.

**Señal adicional para escalar de entrada, sin esperar el ciclo completo de diagnóstico**:
si el ticket de Asana ya se reabrió varias veces sobre la misma ruta en pocos días (el
comentario de reapertura indica "van N veces"), es una ruta con un problema de fondo que
no se resolvió — no tiene sentido repetir el diagnóstico básico cada vez, conviene
escalar directo.

---

### 6. Cierre y Plantilla de Notificación a Referentes

Para documentar formalmente el proceso ante el referente de sistemas o de diagnóstico por
imágenes del hospital, se establece el envío de la siguiente plantilla de detalle una vez
regularizada la situación:

```markdown
**Asunto:** Informe de resolución de incidente - Autoenrutamiento DICOM ([Nombre de la Institución])
**Fecha:** [YYYY-MM-DD]
**Ticket Asana asociado:** [Enlace al ticket]

Estimado/a [Nombre del Referente],

Nos dirigimos a usted para informarle sobre la regularización de las reglas de
autoenrutamiento DICOM que presentaban retención de estudios hacia el PACS de destino.

A continuación se detalla el diagnóstico y las acciones correctivas aplicadas sobre las
reglas afectadas:

| ID Regla (`IDRULE`) | Nodo Origen (`FROMNODE`) | Nodo Destino (`TONODE`) | Motivo de la Falla / Retención | Acción Correctiva Aplicada |
| :--- | :--- | :--- | :--- | :--- |
| [Ej: 6] | [Ej: Tomógrafo 01] | [Ej: PACS Provincial] | [Ej: Falta de autorización de AETitle / Conectividad de red caída] | [Ej: Actualización de AETitle en nodo y purga de cola SQL] |
| [Ej: 12] | [Ej: Mamógrafo Mamomat] | [Ej: PACS Externo] | [Ej: Regla obsoleta / Equipo dado de baja del diagrama operativo] | [Ej: Baja lógica de regla en `DICOMAUTOROUTINGRULES` y limpieza de cola] |

**Verificación y Cierre:**
- Se ejecutaron pruebas de flujo enviando un estudio piloto, confirmando la recepción
  exitosa en el destino.
- Se validó el drenaje completo de la cola en `DICOMAUTOROUTINGQUEUE` por debajo del
  umbral configurado.
- La alerta generada en Asana se ha cerrado automáticamente.

Quedamos a disposición por cualquier consulta adicional.

Atentamente,
**Equipo de Soporte Técnico — Tecnoimagen S.A.**
```
