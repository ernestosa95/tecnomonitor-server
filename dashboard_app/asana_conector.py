import asana
from asana.rest import ApiException
import logging
from datetime import datetime
import requests
import os
from dotenv import load_dotenv

load_dotenv()

ASANA_ACCESS_TOKEN = os.environ.get("ASANA_ACCESS_TOKEN")
WORKSPACE_GID      = os.environ.get("WORKSPACE_GID")
MAIN_PROJECT_GID   = os.environ.get("MAIN_PROJECT_GID")
RESPONSABLE_GID    = os.environ.get("RESPONSABLE_GID")
FOLLOWERS_GIDS     = os.environ.get("FOLLOWERS_GIDS", "").split(",")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("asana-conector")

# ==========================================
# 🛡️ CIRCUIT BREAKER: Bandera global de estado
# ==========================================
ASANA_ENABLED = bool(ASANA_ACCESS_TOKEN)

if not ASANA_ENABLED:
    logger.warning("⚠️ ASANA_ACCESS_TOKEN no encontrado. La integración con Asana está deshabilitada.")


def _obtener_icono(nivel):
    """Devuelve el icono de semáforo según el nivel de alerta."""
    if nivel == "CRITICAL": return "🔴"
    if nivel == "WARNING": return "🟠"
    if nivel == "NOTICE": return "🟡"
    if nivel == "OK": return "🟢"
    return "🚨"

def crear_tarea_alerta(hospital_id, tipo, nivel, mensaje_detalle, hospital_project_gid=None, extra_followers=None):
    """Crea una tarea nueva con el nivel correspondiente en Asana."""
    # 🛡️ Cortocircuito si la integración está deshabilitada
    if not ASANA_ENABLED:
        return None

    configuration = asana.Configuration()
    configuration.access_token = ASANA_ACCESS_TOKEN
    api_client = asana.ApiClient(configuration)
    tasks_api_instance = asana.TasksApi(api_client)
    
    titulo = f"{_obtener_icono(nivel)} {hospital_id} | {tipo}"
    ahora = datetime.now().strftime('%H:%M:%S')
    
    notas = f"""INCIDENTE DETECTADO - TECNOMONITOR V3
    
        🏥 Hospital: {hospital_id}
        ⚠️ Tipo: {tipo}
        🕒 Hora Detección: {ahora}
        📊 Nivel: {nivel}
        📝 Detalle: {mensaje_detalle}

        Asignada automáticamente por TecnoMonitor."""
    
    # Configurar proyectos de destino (Global + Específico del Hospital)
    proyectos_destino = [str(MAIN_PROJECT_GID).strip()]
    if hospital_project_gid:
        clean_gid = str(hospital_project_gid).strip() # Limpiamos espacios basura
        # Aseguramos que sea mayor a 5 caracteres Y que contenga ÚNICAMENTE números
        if len(clean_gid) > 5 and clean_gid.isdigit() and clean_gid != str(MAIN_PROJECT_GID).strip():
            proyectos_destino.append(clean_gid)
        elif clean_gid and not clean_gid.isdigit():
            logger.error(f"⚠️ Asana Project ID inválido para {hospital_id}: '{clean_gid}'. Se ignorará y solo se usará el Main Project.")

    # ========================================================
    # SEGUIDORES: Usar únicamente los configurados en la interfaz
    # ========================================================
    followers_finales = set()
    
    if extra_followers:
        for f in extra_followers:
            if f:
                followers_finales.add(str(f).strip())
                
    # Filtrar valores vacíos para evitar errores de la API de Asana
    followers_finales = list(filter(None, followers_finales))

    body = {
        'data': {
            'workspace': WORKSPACE_GID,
            'name': titulo,
            'notes': notas,
            'projects': proyectos_destino,
            'assignee': RESPONSABLE_GID,
            'followers': followers_finales
        }
    }
    
    try:
        result = tasks_api_instance.create_task(body, {})
        # Extracción segura del GID de la tarea según la respuesta de la librería
        task_gid = result.get('gid') if isinstance(result, dict) else (getattr(result, 'gid', None) or getattr(getattr(result, 'data', None), 'gid', None))
        
        if task_gid:
            logger.info(f"📡 Asana: Tarea creada ID: {task_gid} ({nivel}).")
            return task_gid
    except Exception as e:
        logger.error(f"❌ Error al crear en Asana: {e}")
    return None
    

def actualizar_tarea_asana(task_gid, hospital_id, tipo, nivel, mensaje_detalle, reabrir=False):
    """Actualiza título, comenta y opcionalmente reabre una tarea existente."""
    # 🛡️ Cortocircuito
    if not ASANA_ENABLED or not task_gid: 
        return

    configuration = asana.Configuration()
    configuration.access_token = ASANA_ACCESS_TOKEN
    api_client = asana.ApiClient(configuration)
    tasks_api = asana.TasksApi(api_client)
    stories_api = asana.StoriesApi(api_client)

    titulo_nuevo = f"{_obtener_icono(nivel)} {hospital_id} | {tipo}"
    ahora = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    try:
        # 1. Actualizar título y estado (reabrir si es necesario)
        data_update = {'name': titulo_nuevo}
        if reabrir:
            data_update['completed'] = False
            texto_comentario = f"⚠️ INCIDENTE REABIERTO ({ahora})\nNivel: {nivel}\nDetalle: {mensaje_detalle}"
        else:
            texto_comentario = f"🔄 ACTUALIZACIÓN DE ESTADO ({ahora})\nNuevo Nivel: {nivel}\nDetalle: {mensaje_detalle}"

        tasks_api.update_task({'data': data_update}, task_gid, {})
        
        # 2. Agregar comentario con los nuevos datos
        stories_api.create_story_for_task({"data": {"text": texto_comentario}}, task_gid, {})
        logger.info(f"🔄 Asana: Tarea {task_gid} actualizada a {nivel}.")

    except Exception as e:
        logger.error(f"❌ Error al actualizar tarea {task_gid}: {e}")

def cerrar_tarea_asana(task_gid, hospital_id, tipo, fecha_fin):
    """Marca la tarea como normalizada (Verde) y la completa."""
    # 🛡️ Cortocircuito
    if not ASANA_ENABLED or not task_gid: 
        return

    configuration = asana.Configuration()
    configuration.access_token = ASANA_ACCESS_TOKEN
    api_client = asana.ApiClient(configuration)
    stories_api = asana.StoriesApi(api_client)
    tasks_api = asana.TasksApi(api_client)

    titulo_verde = f"{_obtener_icono('OK')} {hospital_id} | {tipo} Normalizado"

    try:
        stories_api.create_story_for_task({
            "data": { "text": f"✅ Valores normalizados a las {fecha_fin.strftime('%H:%M:%S')}. Cerrando ticket automáticamente." }
        }, task_gid, {})
        
        tasks_api.update_task({'data': {'completed': True, 'name': titulo_verde}}, task_gid, {})
        logger.info(f"✅ Asana: Tarea {task_gid} completada exitosamente.")
        
    except Exception as e:
        logger.error(f"❌ Error al cerrar tarea {task_gid}: {e}")

def adjuntar_pdf_a_tarea(task_gid, pdf_bytes, filename):
    """Sube un archivo PDF en memoria a una tarea de Asana."""
    # 🛡️ Cortocircuito
    if not ASANA_ENABLED or not task_gid: 
        return None
    
    url = f"https://app.asana.com/api/1.0/tasks/{task_gid}/attachments"
    headers = {
        "Authorization": f"Bearer {ASANA_ACCESS_TOKEN}"
    }
    
    # Empaquetamos los bytes del PDF para que Asana lo reconozca como archivo
    files = {
        "file": (filename, pdf_bytes, "application/pdf")
    }
    
    try:
        response = requests.post(url, headers=headers, files=files)
        if response.status_code == 200:
            logger.info(f"✅ Asana: PDF adjuntado con éxito a la tarea {task_gid}.")
            # Construimos la URL directa a la tarea para dársela al usuario
            return f"https://app.asana.com/0/0/{task_gid}/f"
        else:
            logger.error(f"❌ Error al adjuntar en Asana: {response.text}")
            return None
    except Exception as e:
        logger.error(f"❌ Excepción al adjuntar en Asana: {e}")
        return None

def notificar_solicitud_acceso(email, nombre, apellido, motivo):
    """Crea una tarea en Asana para la solicitud de un nuevo usuario."""
    # 🛡️ Cortocircuito
    if not ASANA_ENABLED:
        logger.error("No se pudo enviar solicitud de acceso: Asana deshabilitado.")
        return False

    configuration = asana.Configuration()
    configuration.access_token = ASANA_ACCESS_TOKEN
    api_client = asana.ApiClient(configuration)
    tasks_api_instance = asana.TasksApi(api_client)
    
    titulo = f"👤 SOLICITUD ACCESO | {nombre} {apellido}"
    ahora = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    
    notas = f"""SOLICITUD DE NUEVO USUARIO - TECNOMONITOR
    
📧 Email: {email}
👤 Nombre: {nombre} {apellido}
🕒 Fecha: {ahora}
📝 Motivo: {motivo}

Solicitud enviada desde el portal de acceso de TecnoMonitor."""
    
    # Eliminamos el campo 'followers' para evitar el error de formato
    body = {
        'data': {
            'workspace': WORKSPACE_GID,
            'name': titulo,
            'notes': notas,
            'projects': [str(MAIN_PROJECT_GID)],
            'assignee': RESPONSABLE_GID # Ernesto Ridel
        }
    }
    
    try:
        tasks_api_instance.create_task(body, {})
        logger.info(f"✅ Asana: Solicitud de acceso enviada para {email}.")
        return True
    except Exception as e:
        logger.error(f"❌ Error al enviar solicitud a Asana: {e}")
        return False

def verificar_conexion_asana():
    """Valida que el token no solo exista, sino que autentique. Detecta el token expirado."""
    if not ASANA_ENABLED:
        logger.warning("⚠️ Asana deshabilitado: no hay ASANA_ACCESS_TOKEN en el entorno.")
        return False
    try:
        configuration = asana.Configuration()
        configuration.access_token = ASANA_ACCESS_TOKEN
        api_client = asana.ApiClient(configuration)
        users_api = asana.UsersApi(api_client)
        me = users_api.get_user("me", {"opt_fields": "name"})
        nombre = me.get("name") if isinstance(me, dict) else getattr(me, "name", "?")
        logger.info(f"✅ Asana operativo. Autenticado como: {nombre}")
        return True
    except Exception as e:
        logger.error(f"❌ Asana NO responde (token inválido/expirado o sin salida a api.asana.com): {repr(e)}")
        return False

