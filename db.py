import os
import json
import logging
import copy
import hashlib
from datetime import datetime, timezone, timedelta
import uuid


def get_mexico_city_tz():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo("America/Mexico_City")
    except (ImportError, Exception) as exc:
        raise RuntimeError(f"No se pudo cargar la zona horaria 'America/Mexico_City': {exc}. Operaciones de calendario bloqueadas.")



def parse_dt_mexico(dt_val):
    if dt_val is None:
        return datetime.now(get_mexico_city_tz())
    if isinstance(dt_val, str):
        try:
            dt = datetime.fromisoformat(dt_val)
        except Exception:
            dt = datetime.now(get_mexico_city_tz())
    else:
        dt = dt_val
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_mexico_city_tz())
    return dt.astimezone(get_mexico_city_tz())


def obtener_admin_ids():
    admin_ids_env = os.getenv("ADMIN_IDS", "")
    if not admin_ids_env:
        return []
    return [x.strip() for x in admin_ids_env.split(",") if x.strip()]


def calcular_dias_cumplidos_validos(prueba):
    registros = prueba.get("registros", {})
    posibles_faltas = prueba.get("posibles_faltas", {})

    todas_fechas = set(registros.keys()) | set(posibles_faltas.keys())
    fechas_cumplidas = set()

    for fecha in todas_fechas:
        falta = posibles_faltas.get(fecha, {})
        estado_res = falta.get("estado_resolucion")

        if estado_res is not None:
            if estado_res == "cumplida_por_revision":
                fechas_cumplidas.add(fecha)
        else:
            reg = registros.get(fecha, {})
            if reg.get("estado_entrega") in ["puntual", "valido"]:
                fechas_cumplidas.add(fecha)

    return len(fechas_cumplidas)


def _preservar_progreso_legacy_si_corresponde(prueba, motivo_origen="revision_manual"):
    dias_base = prueba.get("dias_completados", 0)
    dias_legacy = prueba.get("dias_completados_legacy")

    if dias_legacy is not None:
        return

    historial_eventos_anterior = list(prueba.get("historial_eventos_ids") or [])
    dias_verificados_actuales = calcular_dias_cumplidos_validos(prueba)

    es_legacy_explicito = (
        prueba.get("es_progreso_legacy") is True or
        (len(historial_eventos_anterior) == 0 and dias_base > 0 and dias_base > dias_verificados_actuales)
    )

    if es_legacy_explicito:
        prueba["dias_completados_legacy"] = dias_base
        prueba["incertidumbre_progreso_legacy"] = True
        prueba["requiere_conciliacion_legacy"] = True
        prueba["requiere_conciliacion"] = True
        prueba["motivo_incertidumbre_legacy"] = motivo_origen or "progreso_antiguo_sin_historial_diario_verificado"


def _registrar_evidencia_post_resolucion(falta_entry, captura, id_mensaje, tg_dt, target_vencimiento):
    estado_entrega_msg = evaluar_puntualidad_entrega(tg_dt, target_vencimiento)
    fecha_tg_str = tg_dt.isoformat() if tg_dt else parse_dt_mexico(None).isoformat()
    nueva_evidencia = {
        "captura": captura,
        "id_mensaje": id_mensaje,
        "fecha_mensaje_telegram": fecha_tg_str,
        "timestamp_recepcion": parse_dt_mexico(None).isoformat(),
        "estado_entrega": estado_entrega_msg
    }

    evidencias_hist = list(falta_entry.get("evidencias_post_resolucion") or [])
    if not evidencias_hist and isinstance(falta_entry.get("evidencia_tardia_post_resolucion"), dict):
        evidencias_hist.append(falta_entry["evidencia_tardia_post_resolucion"])

    ya_existe = any(ev.get("id_mensaje") == id_mensaje for ev in evidencias_hist)
    if not ya_existe:
        evidencias_hist.append(nueva_evidencia)

    falta_entry["evidencias_post_resolucion"] = evidencias_hist
    falta_entry["evidencia_tardia_post_resolucion"] = nueva_evidencia
    falta_entry["requiere_nueva_revision"] = True
    return falta_entry


def calcular_vencimiento_primera_entrega(timestamp_lanzamiento, fecha_lanzamiento_str):
    if timestamp_lanzamiento:
        dt_lan = parse_dt_mexico(timestamp_lanzamiento)
    elif fecha_lanzamiento_str:
        try:
            d_lan = datetime.strptime(fecha_lanzamiento_str, "%Y-%m-%d").date()
            dt_lan = datetime(d_lan.year, d_lan.month, d_lan.day, 0, 0, 0, tzinfo=get_mexico_city_tz())
        except Exception:
            dt_lan = datetime.now(get_mexico_city_tz())
    else:
        dt_lan = datetime.now(get_mexico_city_tz())
    dt_venc = dt_lan + timedelta(days=1)
    return dt_venc.strftime("%Y-%m-%d")


def evaluar_puntualidad_entrega(telegram_dt, fecha_vencimiento_str):
    tg_dt = parse_dt_mexico(telegram_dt)
    venc_date = datetime.strptime(fecha_vencimiento_str, "%Y-%m-%d").date()
    cutoff_dt = datetime(venc_date.year, venc_date.month, venc_date.day, 17, 0, 0, tzinfo=get_mexico_city_tz())
    if tg_dt <= cutoff_dt:
        return "puntual"
    return "tardio"


def _obtener_id_mensaje(update, user_id):
    if not update:
        return f"msg_{user_id}_default"
    chat_obj = getattr(update, "effective_chat", None)
    msg_obj = getattr(update, "message", None)
    chat_id = getattr(chat_obj, "id", None) if chat_obj else None
    if not chat_id and msg_obj:
        chat_id = getattr(msg_obj, "chat_id", user_id)
    msg_id = getattr(msg_obj, "message_id", "1") if msg_obj else "1"
    return f"{chat_id or user_id}_{msg_id}"

logger = logging.getLogger(__name__)

# Rutas de fallback local
USUARIOS_FILE = "usuarios.json"
APPS_FILE = "apps.json"

_firebase_initialized = False
_db = None


class ErrorRecompensa(Exception):
    """Rechazo o resultado de persistencia no confirmado."""


class ConflictoGuardado(Exception):
    """Datos sin referencia fiable o modificados desde la lectura."""


class SnapshotColeccion(dict):
    def __init__(self, datos):
        super().__init__(datos)
        self.original = copy.deepcopy(datos)


def _firebase_configurado():
    return bool(_firebase_initialized or _db is not None
                or os.getenv("FIREBASE_CREDENTIALS") or os.getenv("FIREBASE_KEY_PATH")
                or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                or os.path.exists("firebase_key.json"))


def _guardar_con_revision(coleccion, datos):
    """CAS por documento: jamás publica snapshots obsoletos ni fallback JSON."""
    if not isinstance(datos, SnapshotColeccion):
        raise ConflictoGuardado("Guardado rechazado: falta una lectura fiable de Firestore.")
    if datos.original.keys() - datos.keys():
        raise ConflictoGuardado("La eliminación de documentos requiere una operación específica.")
    cambios = {k: copy.deepcopy(v) for k, v in datos.items() if k not in datos.original or v != datos.original[k]}
    if not cambios:
        return
    cliente = _inicializar_firebase()
    if cliente is None:
        raise ConflictoGuardado("Firestore no está disponible; no se guardaron datos locales.")
    from firebase_admin import firestore

    @firestore.transactional
    def guardar(transaction):
        refs = {k: cliente.collection(coleccion).document(str(k)) for k in cambios}
        for k, ref in refs.items():
            snap = ref.get(transaction=transaction)
            actual = snap.to_dict() if snap.exists else None
            if actual != datos.original.get(k):
                raise ConflictoGuardado("Los datos cambiaron. Repetí la operación para usar el saldo y progreso actuales.")
        for k, ref in refs.items():
            transaction.set(ref, cambios[k])

    guardar(cliente.transaction())


def id_movimiento_recompensa(app_id, user_id):
    clave = json.dumps([str(app_id), str(user_id)], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(clave.encode("utf-8")).hexdigest()


def _checkin_transaccional(transaction, user_ref, app_ref, movimiento_ref,
                          user_id, app_id, captura, fecha, server_timestamp,
                          id_mensaje=None, telegram_timestamp=None, fecha_vencimiento=None):
    usuario_snap = user_ref.get(transaction=transaction)
    app_snap = app_ref.get(transaction=transaction)
    movimiento = movimiento_ref.get(transaction=transaction)
    if not usuario_snap.exists or not app_snap.exists:
        raise ErrorRecompensa("Usuario no registrado o campaña inexistente.")
    usuario, app = usuario_snap.to_dict(), app_snap.to_dict()
    if app.get("estado") != "en_prueba":
        raise ErrorRecompensa("La campaña no está activa.")
    if user_id not in {str(uid) for uid in app.get("testers", [])} or app_id not in usuario.get("apps_testeando", []):
        raise ErrorRecompensa("No estás inscrito en ambos registros; contactá al administrador.")

    pruebas = app.get("pruebas", {})
    prueba = pruebas.get(user_id)
    if prueba and prueba.get("estado") == "completada":
        raise ErrorRecompensa("Prueba antigua completada sin movimiento: requiere conciliación. No se acreditó otra recompensa.")
    if not prueba or prueba.get("estado") != "instalada":
        raise ErrorRecompensa("La prueba no es elegible: falta una instalación activa.")

    target_vencimiento = fecha_vencimiento or fecha
    if telegram_timestamp:
        tg_dt = parse_dt_mexico(telegram_timestamp)
    elif target_vencimiento:
        try:
            d_v = datetime.strptime(target_vencimiento, "%Y-%m-%d").date()
            tg_dt = datetime(d_v.year, d_v.month, d_v.day, 12, 0, 0, tzinfo=get_mexico_city_tz())
        except Exception:
            tg_dt = parse_dt_mexico(None)
    else:
        tg_dt = parse_dt_mexico(None)

    _preservar_progreso_legacy_si_corresponde(prueba, "primer_checkin_post_legacy")

    registros = copy.deepcopy(prueba.get("registros") or {})
    es_legacy = not bool(prueba.get("registros"))
    if es_legacy:
        prueba["requiere_conciliacion"] = True

    posibles_faltas = copy.deepcopy(prueba.get("posibles_faltas") or {})

    if not id_mensaje:
        id_mensaje = f"msg_chk_{user_id}_{target_vencimiento}_{captura[:8] if isinstance(captura, str) and len(captura)>=8 else captura}"

    msg_ref = user_ref.client.collection("mensajes_procesados").document(str(id_mensaje))
    msg_snap = msg_ref.get(transaction=transaction)
    if msg_snap.exists:
        msg_data = msg_snap.to_dict()
        if (msg_data.get("app_id") == str(app_id) and
            msg_data.get("user_id") == str(user_id) and
            msg_data.get("fecha_vencimiento") == target_vencimiento):
            pass  # Es el mismo mensaje intentado para la misma entrega (idempotente)
        else:
            return {
                "resultado": "duplicado",
                "dias": prueba.get("dias_completados", 1),
                "descripcion": app.get("descripcion"),
                "dev_id": app.get("dev_id")
            }
    else:
        transaction.set(msg_ref, {
            "app_id": str(app_id),
            "user_id": str(user_id),
            "fecha_vencimiento": target_vencimiento,
            "timestamp": parse_dt_mexico(None).isoformat()
        })

    if target_vencimiento in registros:
        reg_existente = registros[target_vencimiento]
        if reg_existente.get("captura") == captura or reg_existente.get("id_mensaje") == id_mensaje:
            res_val = "ya_pagada" if movimiento.exists else ("duplicado" if reg_existente.get("id_mensaje") == id_mensaje and reg_existente.get("captura") != captura else "ya_registrado")
            return {
                "resultado": res_val,
                "dias": prueba.get("dias_completados", 1),
                "descripcion": app.get("descripcion"),
                "dev_id": app.get("dev_id")
            }
        versiones = list(reg_existente.get("versiones_anteriores") or [])
        if reg_existente.get("captura"):
            versiones.append(reg_existente["captura"])
        reg_existente["versiones_anteriores"] = versiones
        reg_existente["captura"] = captura
        reg_existente["timestamp_guardado"] = parse_dt_mexico(None).isoformat()
        registros[target_vencimiento] = reg_existente
        prueba["registros"] = registros
        prueba["ultima_captura"] = captura

        if target_vencimiento in posibles_faltas:
            falta_entry = posibles_faltas[target_vencimiento]
            if falta_entry.get("estado_resolucion") in ["cumplida_por_revision", "justificada", "injustificada"]:
                _registrar_evidencia_post_resolucion(falta_entry, captura, id_mensaje, tg_dt, target_vencimiento)
                posibles_faltas[target_vencimiento] = falta_entry
                prueba["posibles_faltas"] = posibles_faltas

        es_campana_beta = bool(app.get("es_beta") or app.get("tipo_campana") == "beta_gratuita")
        transaction.update(app_ref, {"pruebas": app["pruebas"]})
        return {
            "resultado": "actualizado",
            "dias": prueba.get("dias_completados", 1),
            "estado_entrega": reg_existente.get("estado_entrega"),
            "fecha_vencimiento": target_vencimiento,
            "descripcion": app.get("descripcion"),
            "dev_id": app.get("dev_id"),
            "es_beta": es_campana_beta
        }

    estado_entrega = evaluar_puntualidad_entrega(tg_dt, target_vencimiento)

    registros[target_vencimiento] = {
        "tipo": "checkin",
        "id_mensaje": id_mensaje,
        "fecha_mensaje_telegram": tg_dt.isoformat(),
        "timestamp_guardado": parse_dt_mexico(None).isoformat(),
        "estado_entrega": estado_entrega,
        "captura": captura,
        "versiones_anteriores": []
    }
    prueba["registros"] = registros

    if target_vencimiento in posibles_faltas:
        falta_entry = posibles_faltas[target_vencimiento]
        if falta_entry.get("estado_resolucion") in ["cumplida_por_revision", "justificada", "injustificada"]:
            _registrar_evidencia_post_resolucion(falta_entry, captura, id_mensaje, tg_dt, target_vencimiento)
        else:
            if estado_entrega == "puntual":
                falta_entry["estado"] = "resuelta_por_mensaje_a_tiempo"
            else:
                falta_entry["estado"] = "entrega_tardia_registrada"
        posibles_faltas[target_vencimiento] = falta_entry
        prueba["posibles_faltas"] = posibles_faltas

    dias_validos = calcular_dias_cumplidos_validos(prueba)
    dias = dias_validos
    prueba["dias_completados"] = dias
    prueba["ultimo_checkin"] = target_vencimiento
    prueba["ultima_captura"] = captura
    prueba["estado"] = "instalada"  # Conserva estado operativo para seguir entregando

    es_campana_beta = bool(app.get("es_beta") or app.get("tipo_campana") == "beta_gratuita")
    tiene_faltas_pendientes = any(f.get("estado") == "pendiente_revision" for f in posibles_faltas.values())
    tiene_entregas_tardias = any(r.get("estado_entrega") == "tardio" for r in registros.values())
    tiene_legacy = bool(prueba.get("requiere_conciliacion_legacy")) or bool(prueba.get("incertidumbre_progreso_legacy"))
    req_conciliacion = bool(prueba.get("requiere_conciliacion")) or tiene_legacy or tiene_faltas_pendientes or tiene_entregas_tardias or es_campana_beta
    if req_conciliacion:
        prueba["requiere_conciliacion"] = True

    pagar_recompensa = (dias >= 14) and (not req_conciliacion) and (not movimiento.exists)

    transaction.update(app_ref, {"pruebas": app["pruebas"]})

    if pagar_recompensa:
        transaction.update(user_ref, {
            "creditos": usuario.get("creditos", 0) + 800,
            "boletos_rifa": usuario.get("boletos_rifa", 0) + 1,
        })
        transaction.create(movimiento_ref, {
            "usuario": user_id, "campana": app_id, "importe": 800,
            "boletos": 1, "fecha": server_timestamp,
        })

    return {
        "resultado": "completada" if pagar_recompensa else "checkin",
        "dias": dias,
        "estado_entrega": estado_entrega,
        "fecha_vencimiento": target_vencimiento,
        "requiere_conciliacion": req_conciliacion,
        "descripcion": app.get("descripcion"),
        "dev_id": app.get("dev_id"),
        "es_beta": es_campana_beta
    }


def registrar_checkin(user_id, app_id, captura, fecha, id_mensaje=None, telegram_timestamp=None, fecha_vencimiento=None):
    """Progreso y recompensa en un único commit; nunca utiliza JSON."""
    try:
        if not _firebase_configurado():
            raise ErrorRecompensa("Check-in y recompensa no disponibles en modo local; no se modificaron los JSON.")
        cliente = _inicializar_firebase()
        if cliente is None or not _firebase_initialized:
            raise ErrorRecompensa("Firestore no disponible. No se pudo confirmar el guardado.")
        from firebase_admin import firestore
        return firestore.transactional(_checkin_transaccional)(
            cliente.transaction(), cliente.collection("usuarios").document(str(user_id)),
            cliente.collection("apps").document(str(app_id)),
            cliente.collection("recompensas").document(id_movimiento_recompensa(app_id, user_id)),
            str(user_id), str(app_id), captura, fecha, firestore.SERVER_TIMESTAMP,
            id_mensaje, telegram_timestamp, fecha_vencimiento
        )
    except ErrorRecompensa:
        raise
    except Exception as exc:
        raise ErrorRecompensa("No se pudo confirmar el guardado. Reintentá: un movimiento ya guardado no se paga nuevamente.") from exc

def es_beta_gratuita():
    """Indica si el sistema opera en modo Beta Gratuita Comunitaria & Founders (Etapa 6A)."""
    val = os.getenv("BETA_GRATUITA", "false")
    return str(val).strip().lower() in ("true", "1", "yes", "si", "t")


PUNTOS_FOUNDER_DEFECTO = 100


class PublicacionRechazada(Exception):
    """La publicación no cumple las reglas; no se debe escribir nada."""


class ErrorPersistenciaPublicacion(Exception):
    """No se pudo confirmar la publicación atómica."""


def _publicar_transaccional(transaction, user_ref, app_ref, user_id, app_id, dev_nombre, app_link, descripcion):
    app_snap = app_ref.get(transaction=transaction)
    if app_snap.exists:
        app_dict = app_snap.to_dict()
        if (app_dict.get("dev_id") == user_id and
            app_dict.get("link") == app_link and
            app_dict.get("descripcion") == descripcion):
            return {
                "resultado": "ya_publicada",
                "app_id": app_id,
                "descripcion": descripcion,
                "es_beta": app_dict.get("es_beta", False)
            }
        raise PublicacionRechazada("❌ Conflicto de publicación: el ID de campaña ya existe con otro contenido o propietario.")

    user_snap = user_ref.get(transaction=transaction)
    if not user_snap.exists:
        raise PublicacionRechazada("❌ No estás registrado. Usá /start antes de publicar tu aplicación.")

    usuario = user_snap.to_dict()
    creditos = usuario.get("creditos", 0)

    modo_beta = es_beta_gratuita()

    if not modo_beta:
        if creditos < 800:
            raise PublicacionRechazada(f"❌ Créditos insuficientes: tenés {creditos} de 800 requeridos para publicar.")

        transaction.update(user_ref, {
            "creditos": creditos - 800,
            "boletos_rifa": usuario.get("boletos_rifa", 0) + 1
        })
    else:
        # Durante la beta gratuita se permite como máximo 1 campaña activa por desarrollador
        # (en estado 'reclutando' o 'en_prueba').
        campana_activa_id = usuario.get("campana_activa_id")
        client = getattr(app_ref, "client", None) or getattr(user_ref, "client", None)
        if campana_activa_id and str(campana_activa_id) != str(app_id) and client:
            try:
                activa_doc_ref = client.collection("apps").document(str(campana_activa_id))
                act_snap = activa_doc_ref.get(transaction=transaction)
                if act_snap.exists:
                    act_data = act_snap.to_dict() or {}
                    if act_data.get("estado") in ["reclutando", "en_prueba"]:
                        desc_act = act_data.get("descripcion", campana_activa_id)
                        raise PublicacionRechazada(
                            f"❌ Límite de la Beta: ya tenés una campaña activa en curso ({desc_act}). "
                            f"Durante la beta gratuita se permite un máximo de 1 campaña activa por desarrollador."
                        )
            except PublicacionRechazada:
                raise
            except Exception:
                pass

        # En modo Beta Gratuita no se cobran los 800 créditos ni dinero.
        # Los créditos existentes no se eliminan ni se sobrescriben.
        reputacion = usuario.get("reputacion") or {}
        reputacion["campanas_publicadas"] = reputacion.get("campanas_publicadas", 0) + 1
        transaction.update(user_ref, {
            "reputacion": reputacion,
            "campana_activa_id": str(app_id)
        })

    nueva_campana = {
        "dev_id": user_id,
        "dev_nombre": dev_nombre,
        "link": app_link,
        "descripcion": descripcion,
        "estado": "reclutando",
        "testers": [],
        "min_testers": 15,
        "max_testers": 15,
        "dias_requeridos": 14,
        "pruebas": {},
        "es_beta": modo_beta,
        "tipo_campana": "beta_gratuita" if modo_beta else "estandar",
        "puntos_founder_recompensa": PUNTOS_FOUNDER_DEFECTO if modo_beta else 0
    }
    transaction.create(app_ref, nueva_campana)
    return {
        "resultado": "creada",
        "app_id": app_id,
        "descripcion": descripcion,
        "es_beta": modo_beta,
        "costo_creditos": 0 if modo_beta else 800
    }


def publicar_campana(user_id, dev_nombre, app_link, descripcion, app_id):
    if not app_id:
        raise ValueError("app_id es obligatorio para publicar_campana.")
    if not _firebase_configurado():
        raise ErrorPersistenciaPublicacion(
            "❌ La publicación de campañas no está disponible en modo local: los archivos JSON no permiten descontar créditos y publicar de forma atómica. Contactá al administrador."
        )
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        raise ErrorPersistenciaPublicacion("❌ Firestore no disponible. No se pudo confirmar el guardado.")

    from firebase_admin import firestore

    try:
        return firestore.transactional(_publicar_transaccional)(
            cliente.transaction(),
            cliente.collection("usuarios").document(str(user_id)),
            cliente.collection("apps").document(str(app_id)),
            str(user_id), str(app_id), dev_nombre, app_link, descripcion
        )
    except PublicacionRechazada:
        raise
    except Exception as exc:
        raise ErrorPersistenciaPublicacion(
            "❌ No se pudo confirmar el guardado de la publicación. Intentá nuevamente: si el guardado llegó a completarse, el reintento lo detectará sin descontar créditos de nuevo."
        ) from exc


class ExpulsionRechazada(Exception):
    """La expulsión no cumple las reglas; no se debe escribir nada."""


class ErrorPersistenciaExpulsion(Exception):
    """No se pudo confirmar la expulsión atómica."""


def _expulsar_en_transaccion(transaction, user_ref, app_ref, tester_id, app_id):
    user_snap = user_ref.get(transaction=transaction)
    app_snap = app_ref.get(transaction=transaction)

    if not user_snap.exists:
        raise ExpulsionRechazada("❌ El tester indicado no existe.")
    if not app_snap.exists:
        raise ExpulsionRechazada("❌ La campaña indicada no existe.")

    usuario, app = user_snap.to_dict(), app_snap.to_dict()

    testers = list(app.get("testers") or [])
    membresias = list(usuario.get("apps_testeando") or [])
    pruebas = copy.deepcopy(app.get("pruebas") or {})

    tester_id_str = str(tester_id)
    app_id_str = str(app_id)

    en_app = any(str(uid) == tester_id_str for uid in testers)
    en_usuario = any(str(aid) == app_id_str for aid in membresias)
    en_pruebas = tester_id_str in pruebas

    if not en_app and not en_usuario and not en_pruebas:
        return {"resultado": "ya_expulsado", "tester_id": tester_id_str, "app_id": app_id_str}

    nuevos_testers = [uid for uid in testers if str(uid) != tester_id_str]
    nuevas_membresias = [aid for aid in membresias if str(aid) != app_id_str]
    pruebas.pop(tester_id_str, None)

    cambios_app = {
        "testers": nuevos_testers,
        "pruebas": pruebas
    }

    min_t = app.get("min_testers")
    if min_t is None:
        min_t = 12
    # Si la campaña estaba "lista" y queda por debajo del mínimo de testers requeridos, vuelve a "reclutando"
    if app.get("estado") == "lista" and len(nuevos_testers) < min_t:
        cambios_app["estado"] = "reclutando"

    transaction.update(app_ref, cambios_app)
    transaction.update(user_ref, {"apps_testeando": nuevas_membresias})

    return {
        "resultado": "expulsado",
        "tester_id": tester_id_str,
        "app_id": app_id_str,
        "tester_nombre": usuario.get("nombre", tester_id_str),
        "inconsistente": en_app != en_usuario
    }


def expulsar_tester(tester_id, app_id):
    if not _firebase_configurado():
        raise ErrorPersistenciaExpulsion(
            "❌ La expulsión de testers no está disponible en modo local: los archivos JSON no permiten actualizar ambas colecciones de forma atómica. Contactá al administrador."
        )
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        raise ErrorPersistenciaExpulsion("❌ Firestore no disponible. No se pudo confirmar el guardado.")

    from firebase_admin import firestore
    try:
        return firestore.transactional(_expulsar_en_transaccion)(
            cliente.transaction(),
            cliente.collection("usuarios").document(str(tester_id)),
            cliente.collection("apps").document(str(app_id)),
            str(tester_id), str(app_id)
        )
    except ExpulsionRechazada:
        raise
    except Exception as exc:
        raise ErrorPersistenciaExpulsion(
            "❌ No se pudo confirmar el guardado de la expulsión. Intentá nuevamente: si el guardado llegó a completarse, el reintento lo detectará de forma segura."
        ) from exc


class ConfirmacionRechazada(Exception):
    """La confirmación de instalación no cumple las condiciones; no se debe escribir nada."""


class ErrorPersistenciaConfirmacion(Exception):
    """No se pudo confirmar la instalación de la app."""


def _confirmar_instalacion_transaccional(transaction, user_ref, app_ref, user_id, app_id, foto_file_id, fecha_hoy, nombre,
                                        id_mensaje=None, telegram_timestamp=None, fecha_vencimiento=None):
    user_snap = user_ref.get(transaction=transaction)
    app_snap = app_ref.get(transaction=transaction)

    if not user_snap.exists:
        raise ConfirmacionRechazada("❌ No estás registrado. Usá /start para registrarte primero.")
    if not app_snap.exists:
        raise ConfirmacionRechazada("❌ La campaña indicada no existe.")

    usuario = user_snap.to_dict()
    app = app_snap.to_dict()

    if app.get("estado") != "en_prueba":
        raise ConfirmacionRechazada("❌ La campaña no está en prueba. Esperá a que el desarrollador la inicie.")

    en_testers = user_id in {str(tester_id) for tester_id in app.get("testers", [])}
    en_usuario = app_id in usuario.get("apps_testeando", [])

    if en_testers != en_usuario:
        raise ConfirmacionRechazada("❌ Tu inscripción es inconsistente entre la campaña y tu cuenta. Contactá al administrador; no se modificaron los datos.")
    if not en_testers:
        raise ConfirmacionRechazada("❌ No estás inscrito en esta campaña.")

    pruebas = copy.deepcopy(app.get("pruebas") or {})
    if user_id in pruebas:
        prueba = pruebas[user_id]
        if prueba.get("estado") == "completada":
            return {
                "resultado": "ya_completada",
                "app": app,
                "mensaje": "✅ Ya completaste esta prueba. Tu progreso se conserva y no se puede reiniciar la instalación."
            }
        return {
            "resultado": "ya_instalada",
            "app": app,
            "mensaje": "ℹ️ Ya existe una prueba en curso para esta campaña. Tu progreso se conserva; usá /checkin para continuar."
        }

    if telegram_timestamp:
        tg_dt = parse_dt_mexico(telegram_timestamp)
    elif fecha_hoy:
        try:
            d_h = datetime.strptime(fecha_hoy, "%Y-%m-%d").date()
            tg_dt = datetime(d_h.year, d_h.month, d_h.day, 12, 0, 0, tzinfo=get_mexico_city_tz())
        except Exception:
            tg_dt = parse_dt_mexico(None)
    else:
        tg_dt = parse_dt_mexico(None)
    if not fecha_vencimiento:
        fecha_vencimiento = calcular_vencimiento_primera_entrega(app.get("timestamp_lanzamiento"), app.get("fecha_lanzamiento") or fecha_hoy)

    if not id_mensaje:
        id_mensaje = f"msg_inst_{user_id}_{fecha_vencimiento}_{foto_file_id[:8] if isinstance(foto_file_id, str) and len(foto_file_id)>=8 else foto_file_id}"

    msg_ref = user_ref.client.collection("mensajes_procesados").document(str(id_mensaje))
    msg_snap = msg_ref.get(transaction=transaction)
    if msg_snap.exists:
        msg_data = msg_snap.to_dict()
        if (msg_data.get("app_id") == str(app_id) and
            msg_data.get("user_id") == str(user_id) and
            msg_data.get("fecha_vencimiento") == fecha_vencimiento):
            return {
                "resultado": "ya_instalada",
                "app": app,
                "mensaje": "ℹ️ Ya existe una prueba en curso para esta campaña. Tu progreso se conserva; usá /checkin para continuar."
            }
        else:
            raise ConfirmacionRechazada("❌ Este mensaje ya fue utilizado en otra entrega.")
    else:
        transaction.set(msg_ref, {
            "app_id": str(app_id),
            "user_id": str(user_id),
            "fecha_vencimiento": fecha_vencimiento,
            "timestamp": parse_dt_mexico(None).isoformat()
        })

    estado_entrega = evaluar_puntualidad_entrega(tg_dt, fecha_vencimiento)

    nueva_prueba = {
        "tester_nombre": nombre,
        "estado": "instalada",
        "fecha_inicio": fecha_hoy,
        "captura_inicial": foto_file_id,
        "dias_completados": 1 if estado_entrega == "puntual" else 0,
        "ultimo_checkin": fecha_hoy,
        "registros": {
            fecha_vencimiento: {
                "tipo": "instalacion",
                "id_mensaje": id_mensaje,
                "fecha_mensaje_telegram": tg_dt.isoformat(),
                "timestamp_guardado": parse_dt_mexico(None).isoformat(),
                "estado_entrega": estado_entrega,
                "captura": foto_file_id,
                "versiones_anteriores": []
            }
        },
        "posibles_faltas": {}
    }

    pruebas[user_id] = nueva_prueba
    transaction.update(app_ref, {"pruebas": pruebas})

    return {
        "resultado": "creada",
        "app": app,
        "fecha_inicio": fecha_hoy,
        "fecha_vencimiento": fecha_vencimiento,
        "estado_entrega": estado_entrega,
        "descripcion": app.get("descripcion"),
        "dev_id": app.get("dev_id")
    }


def confirmar_instalacion(user_id, app_id, foto_file_id, fecha_hoy, nombre, id_mensaje=None, telegram_timestamp=None, fecha_vencimiento=None):
    """Confirmación atómica de instalación en Firestore, sin fallback ni escrituras JSON.

    Valida la campaña y las relaciones en una única transacción antes de registrar la prueba.
    """
    if not _firebase_configurado():
        raise ErrorPersistenciaConfirmacion(
            "❌ La confirmación de instalación no está disponible en modo local: los archivos JSON no permiten validar ni guardar la prueba de forma transaccional. Contactá al administrador."
        )
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        raise ErrorPersistenciaConfirmacion("❌ Firestore no disponible. No se pudo confirmar el guardado.")

    from firebase_admin import firestore
    try:
        ejecutar = firestore.transactional(_confirmar_instalacion_transaccional)
        return ejecutar(
            cliente.transaction(),
            cliente.collection("usuarios").document(str(user_id)),
            cliente.collection("apps").document(str(app_id)),
            str(user_id), str(app_id), foto_file_id, fecha_hoy, nombre,
            id_mensaje, telegram_timestamp, fecha_vencimiento
        )
    except ConfirmacionRechazada:
        raise
    except Exception as exc:
        raise ErrorPersistenciaConfirmacion(
            "❌ No se pudo confirmar el guardado de tu instalación. Intentá nuevamente: si el guardado llegó a completarse, el reintento lo detectará sin alterar tu progreso."
        ) from exc


class InscripcionRechazada(Exception):
    """La inscripción no cumple las rules; no se debe escribir nada."""


class ErrorPersistenciaInscripcion(Exception):
    """No se pudo confirmar una inscripción durable."""


def _preparar_inscripcion(user_id, app_id, usuario, app):
    """Valida snapshots y devuelve solo los campos de inscripción."""
    if usuario is None:
        raise InscripcionRechazada("❌ No estás registrado. Usá /start antes de postularte.")
    if app is None:
        raise InscripcionRechazada("❌ La campaña indicada no existe.")
    if str(app.get("dev_id")) == user_id:
        raise InscripcionRechazada("❌ No podés postularte para probar tu propia app.")
    testers = list(app.get("testers", []))
    membresias = list(usuario.get("apps_testeando", []))
    en_app = user_id in {str(uid) for uid in testers}
    en_usuario = app_id in membresias
    if en_app != en_usuario:
        raise InscripcionRechazada("❌ Inscripción inconsistente: figurás inscrito solo en uno de los registros. Contactá al administrador; no se modificaron los datos.")
    if en_app:
        raise InscripcionRechazada("ℹ️ Ya estás inscrito en esta campaña.")
    min_t = app.get("min_testers")
    max_t = app.get("max_testers")

    # Si faltan campos de cupo en campañas legacy anteriores a 5A,
    # se conservan los valores compatibles de 12 por defecto para datos antiguos.
    if min_t is None and max_t is None:
        min_t, max_t = 12, 12
    elif min_t is None:
        min_t = max_t
    elif max_t is None:
        max_t = min_t

    estado = app.get("estado")
    if estado == "lista" and len(testers) >= min_t:
        raise InscripcionRechazada(f"❌ La campaña está llena: ya tiene {min_t} testers activos.")
    if estado != "reclutando":
        raise InscripcionRechazada("❌ La campaña no está disponible para postulaciones. Solo se aceptan mientras está reclutando.")
    if len(testers) >= max_t:
        raise InscripcionRechazada(f"❌ La campaña está llena: ya tiene {max_t} testers activos.")

    testers.append(user_id)
    membresias.append(app_id)
    completa = len(testers) >= min_t
    cambios_app = {"testers": testers, "min_testers": min_t, "max_testers": max_t}
    if completa:
        cambios_app["estado"] = "lista"
    return cambios_app, {"apps_testeando": membresias}, {
        "total": len(testers),
        "min_testers": min_t,
        "max_testers": max_t,
        "completa": completa,
        "dev_id": app.get("dev_id"),
    }


def _inscribir_en_transaccion(transaction, user_ref, app_ref, user_id, app_id):
    """Callback reintentable: sin mensajes ni efectos fuera de la transacción."""
    user_snapshot = user_ref.get(transaction=transaction)
    app_snapshot = app_ref.get(transaction=transaction)
    cambios_app, cambios_usuario, resultado = _preparar_inscripcion(
        user_id, app_id,
        user_snapshot.to_dict() if user_snapshot.exists else None,
        app_snapshot.to_dict() if app_snapshot.exists else None,
    )
    transaction.update(app_ref, cambios_app)
    transaction.update(user_ref, cambios_usuario)
    return resultado


def inscribir_tester(user_id, app_id):
    """Inscripción atómica en Firestore, sin fallback ni escrituras JSON.

    Se rechaza el modo local porque sus dos archivos no son transaccionales.
    Si el resultado del commit es incierto, el reintento relee ambas relaciones.
    """
    try:
        configurado = (
            _firebase_initialized or _db is not None
            or bool(os.getenv("FIREBASE_CREDENTIALS"))
            or bool(os.getenv("FIREBASE_KEY_PATH"))
            or bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
            or os.path.exists("firebase_key.json")
        )
        if not configurado:
            raise ErrorPersistenciaInscripcion(
                "❌ Las inscripciones no están disponibles en modo local: los archivos JSON no permiten guardar ambos registros conjuntamente. Contactá al administrador."
            )
        cliente = _inicializar_firebase()
        if cliente is None or not _firebase_initialized:
            raise RuntimeError("Firestore configurado pero no disponible")
        from firebase_admin import firestore

        ejecutar = firestore.transactional(_inscribir_en_transaccion)
        return ejecutar(
            cliente.transaction(),
            cliente.collection("usuarios").document(str(user_id)),
            cliente.collection("apps").document(str(app_id)),
            str(user_id), str(app_id),
        )
    except (InscripcionRechazada, ErrorPersistenciaInscripcion):
        raise
    except Exception as exc:
        raise ErrorPersistenciaInscripcion(
            "❌ No se pudo confirmar el guardado de tu inscripción. Intentá nuevamente más tarde; si el guardado llegó a completarse, el reintento lo detectará sin duplicarlo."
        ) from exc


def _inicializar_firebase():
    global _firebase_initialized, _db
    if _firebase_initialized:
        return _db

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        cred = None
        firebase_env = os.getenv("FIREBASE_CREDENTIALS")
        key_path = os.getenv("FIREBASE_KEY_PATH", "firebase_key.json")

        if firebase_env:
            try:
                cred_dict = json.loads(firebase_env)
                cred = credentials.Certificate(cred_dict)
                logger.info("🔑 Inicializando Firebase con variable de entorno FIREBASE_CREDENTIALS.")
            except Exception as e:
                logger.error(f"❌ Error al parsear la variable FIREBASE_CREDENTIALS: {e}")

        if not cred and os.path.exists(key_path):
            try:
                cred = credentials.Certificate(key_path)
                logger.info(f"🔑 Inicializando Firebase con el archivo {key_path}.")
            except Exception as e:
                logger.error(f"❌ Error al cargar archivo {key_path}: {e}")

        if not cred and os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            gac_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if os.path.exists(gac_path):
                try:
                    cred = credentials.Certificate(gac_path)
                    logger.info(f"🔑 Inicializando Firebase con GOOGLE_APPLICATION_CREDENTIALS ({gac_path}).")
                except Exception as e:
                    logger.error(f"❌ Error al cargar GOOGLE_APPLICATION_CREDENTIALS: {e}")

        if cred:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            _db = firestore.client()
            _firebase_initialized = True
            logger.info("✅ Conexión con Firebase Cloud Firestore establecida con éxito.")
        else:
            logger.warning("⚠️ No se encontraron credenciales de Firebase. Operando en modo local (JSON).")

    except ImportError:
        logger.warning("⚠️ La librería 'firebase-admin' no está instalada. Operando en modo local (JSON).")
    except Exception as e:
        logger.error(f"❌ Error al conectar con Firebase: {e}. Operando en modo local (JSON).")

    return _db

def is_firebase_active():
    """Retorna True si la conexión con Firebase está activa."""
    _inicializar_firebase()
    return _firebase_initialized

# --- LECTURA Y ESCRITURA DE ARCHIVOS LOCALES (FALLBACK) ---
def _cargar_json_local(archivo):
    if os.path.exists(archivo):
        try:
            with open(archivo, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error leyendo {archivo}: {e}")
    return {}

def _guardar_json_local(archivo, datos):
    try:
        with open(archivo, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando {archivo}: {e}")

# --- OPERACIONES DE USUARIOS ---
def cargar_usuarios():
    """Carga todos los usuarios desde Firebase o desde usuarios.json."""
    db = _inicializar_firebase()
    if _firebase_initialized and db:
        try:
            docs = db.collection("usuarios").stream()
            usuarios = {}
            for doc in docs:
                usuarios[doc.id] = doc.to_dict()
            return SnapshotColeccion(usuarios)
        except Exception as e:
            logger.error(f"Error al leer usuarios de Firebase: {e}")
            return _cargar_json_local(USUARIOS_FILE)
    return _cargar_json_local(USUARIOS_FILE)

def guardar_usuarios(datos):
    if _firebase_configurado():
        _guardar_con_revision("usuarios", datos)
        return
    _guardar_json_local(USUARIOS_FILE, datos)

def guardar_usuario(user_id, user_data):
    if _firebase_configurado():
        raise ConflictoGuardado("Usa una lectura y guardado con revision; no se admiten documentos sin referencia.")
    datos = _cargar_json_local(USUARIOS_FILE)
    datos[str(user_id)] = user_data
    _guardar_json_local(USUARIOS_FILE, datos)

# --- OPERACIONES DE APPS ---
def cargar_apps():
    """Carga todas las aplicaciones desde Firebase o desde apps.json."""
    db = _inicializar_firebase()
    if _firebase_initialized and db:
        try:
            docs = db.collection("apps").stream()
            apps = {}
            for doc in docs:
                apps[doc.id] = doc.to_dict()
            return SnapshotColeccion(apps)
        except Exception as e:
            logger.error(f"Error al leer apps de Firebase: {e}")
            return _cargar_json_local(APPS_FILE)
    return _cargar_json_local(APPS_FILE)

def guardar_apps(datos):
    if _firebase_configurado():
        _guardar_con_revision("apps", datos)
        return
    _guardar_json_local(APPS_FILE, datos)

def guardar_app(app_id, app_data):
    if _firebase_configurado():
        raise ConflictoGuardado("Usa una lectura y guardado con revision; no se admiten documentos sin referencia.")
    datos = _cargar_json_local(APPS_FILE)
    datos[str(app_id)] = app_data
    _guardar_json_local(APPS_FILE, datos)


# --- MANEJO TRANSACCIONAL DE AVISOS PERSISTENTES Y POSIBLES FALTAS ---
def _reclamar_aviso_transaccional(transaction, aviso_ref, app_ref, user_id, app_id, fecha_vencimiento, tipo_aviso, now_dt):
    aviso_snap = aviso_ref.get(transaction=transaction)
    app_snap = app_ref.get(transaction=transaction)

    if not app_snap.exists:
        return {"elegible": False, "motivo": "campana_inexistente"}
    app = app_snap.to_dict()
    if app.get("estado") != "en_prueba":
        return {"elegible": False, "motivo": "campana_no_en_prueba"}
    if str(user_id) not in {str(t) for t in app.get("testers", [])}:
        return {"elegible": False, "motivo": "user_no_inscrito"}

    try:
        venc_date = datetime.strptime(fecha_vencimiento, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {"elegible": False, "motivo": "fecha_invalida"}

    cutoff_dt = datetime(venc_date.year, venc_date.month, venc_date.day, 17, 0, 0, tzinfo=get_mexico_city_tz())
    now_tz = parse_dt_mexico(now_dt)
    if now_tz > cutoff_dt:
        return {"elegible": False, "motivo": "vencimiento_expirado"}

    prueba = app.get("pruebas", {}).get(str(user_id))
    if not prueba:
        fecha_primera = calcular_vencimiento_primera_entrega(
            app.get("timestamp_lanzamiento"), app.get("fecha_lanzamiento")
        )
        if fecha_vencimiento != fecha_primera:
            return {"elegible": False, "motivo": "sin_prueba"}
    else:
        registros = prueba.get("registros", {})
        if fecha_vencimiento in registros:
            return {"elegible": False, "motivo": "entrega_ya_satisfecha"}

    aviso = aviso_snap.to_dict() if aviso_snap.exists else {}
    intentos = aviso.get("intentos", 0)
    estado = aviso.get("estado")
    ultimo_intento_str = aviso.get("ultimo_intento")

    if estado == "enviado":
        return {"elegible": False, "motivo": "ya_enviado"}
    if intentos >= 3:
        return {"elegible": False, "motivo": "limite_intentos_alcanzado"}

    now_tz = parse_dt_mexico(now_dt)
    if estado == "enviando" and ultimo_intento_str:
        try:
            dt_ultimo = parse_dt_mexico(ultimo_intento_str)
            if (now_tz - dt_ultimo).total_seconds() < 300:
                return {"elegible": False, "motivo": "en_proceso"}
        except Exception:
            pass

    token_reclamacion = str(uuid.uuid4())
    transaction.set(aviso_ref, {
        "app_id": str(app_id),
        "user_id": str(user_id),
        "fecha_vencimiento": fecha_vencimiento,
        "tipo": tipo_aviso,
        "estado": "enviando",
        "intentos": intentos + 1,
        "ultimo_intento": now_tz.isoformat(),
        "token_reclamacion": token_reclamacion
    })
    return {"elegible": True, "intentos": intentos + 1, "token_reclamacion": token_reclamacion}


def reclamar_aviso(app_id, user_id, fecha_vencimiento, tipo_aviso, now_dt=None):
    if not _firebase_configurado():
        return {"elegible": False, "motivo": "local_mode"}
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return {"elegible": False, "motivo": "firestore_no_disponible"}
    from firebase_admin import firestore
    aviso_id = f"{app_id}_{user_id}_{fecha_vencimiento}_{tipo_aviso}"
    aviso_ref = cliente.collection("avisos_programados").document(aviso_id)
    app_ref = cliente.collection("apps").document(str(app_id))
    if now_dt is None:
        now_dt = parse_dt_mexico(None)
    return firestore.transactional(_reclamar_aviso_transaccional)(
        cliente.transaction(), aviso_ref, app_ref, str(user_id), str(app_id), fecha_vencimiento, tipo_aviso, now_dt
    )


def confirmar_aviso_enviado(app_id, user_id, fecha_vencimiento, tipo_aviso, token_reclamacion=None):
    """NOTA DE DISEÑO: Si una llamada a la API de Telegram falla con resultado incierto, el aviso puede reintentarse.
    No es posible garantizar entrega estrictamente 'exactly once' ante fallos de conexión."""
    if not token_reclamacion or not str(token_reclamacion).strip():
        logger.warning(f"Intento de confirmar aviso {tipo_aviso} para user {user_id} sin token_reclamacion.")
        return False
    if not _firebase_configurado():
        return False
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return False
    from firebase_admin import firestore
    aviso_id = f"{app_id}_{user_id}_{fecha_vencimiento}_{tipo_aviso}"
    aviso_ref = cliente.collection("avisos_programados").document(aviso_id)
    now_iso = parse_dt_mexico(None).isoformat()

    res_container = {"exito": False}

    def _confirmar_tx(transaction):
        snap = aviso_ref.get(transaction=transaction)
        if not snap.exists:
            logger.warning(f"Intento de confirmar aviso inexistente {aviso_id}.")
            return
        datos = snap.to_dict()

        if datos.get("estado") == "enviado":
            if datos.get("token_reclamacion") == str(token_reclamacion):
                res_container["exito"] = True
                return
            else:
                logger.warning(f"Token de reclamación no coincide en aviso ya enviado {aviso_id}.")
                return

        if datos.get("estado") != "enviando":
            logger.warning(f"Estado de aviso no apto para confirmación: {datos.get('estado')}")
            return
        if datos.get("token_reclamacion") != str(token_reclamacion):
            logger.warning(f"Token de reclamación no coincide en aviso {aviso_id}.")
            return

        datos["estado"] = "enviado"
        datos["timestamp_envio"] = now_iso
        transaction.set(aviso_ref, datos)
        res_container["exito"] = True

    firestore.transactional(_confirmar_tx)(cliente.transaction())
    return res_container["exito"]


def _marcar_posibles_faltas_transaccional(transaction, app_ref, dt_actual):
    app_snap = app_ref.get(transaction=transaction)
    if not app_snap.exists:
        return 0
    app = app_snap.to_dict()
    if app.get("estado") != "en_prueba":
        return 0

    dt_now = parse_dt_mexico(dt_actual)
    fecha_lanzamiento = app.get("fecha_lanzamiento")
    timestamp_lanzamiento = app.get("timestamp_lanzamiento")

    if not fecha_lanzamiento and not timestamp_lanzamiento:
        return 0

    fecha_v1_str = calcular_vencimiento_primera_entrega(timestamp_lanzamiento, fecha_lanzamiento)
    d_v1 = datetime.strptime(fecha_v1_str, "%Y-%m-%d").date()

    pruebas = copy.deepcopy(app.get("pruebas") or {})
    nuevas_faltas = 0

    todos_testers = set(str(t) for t in app.get("testers", [])) | set(str(k) for k in pruebas.keys())

    for user_id in todos_testers:
        if user_id in pruebas:
            prueba = pruebas[user_id]
        else:
            prueba = {
                "tester_nombre": "Sin confirmación",
                "estado": "sin_instalacion",
                "posibles_faltas": {},
                "registros": {}
            }
            pruebas[user_id] = prueba

        registros = prueba.get("registros", {})
        posibles_faltas = copy.deepcopy(prueba.get("posibles_faltas") or {})

        cur_date = d_v1
        d_today = dt_now.date()

        while cur_date <= d_today:
            cur_date_str = cur_date.strftime("%Y-%m-%d")
            cutoff_dt = datetime(cur_date.year, cur_date.month, cur_date.day, 17, 0, 0, tzinfo=get_mexico_city_tz())

            if dt_now > cutoff_dt:
                if cur_date_str not in registros and cur_date_str not in posibles_faltas:
                    posibles_faltas[cur_date_str] = {
                        "fecha_vencimiento": cur_date_str,
                        "estado": "pendiente_revision",
                        "timestamp_corte": dt_now.isoformat(),
                        "motivo": "vencimiento_sin_entrega_en_corte"
                    }
                    nuevas_faltas += 1

            cur_date += timedelta(days=1)

        prueba["posibles_faltas"] = posibles_faltas

    if nuevas_faltas > 0:
        transaction.update(app_ref, {"pruebas": pruebas})

    return nuevas_faltas


def marcar_posibles_faltas(dt_actual=None):
    if not _firebase_configurado():
        return 0
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return 0
    from firebase_admin import firestore
    apps_stream = cliente.collection("apps").stream()
    total_faltas = 0
    for app_doc in apps_stream:
        app_ref = cliente.collection("apps").document(app_doc.id)
        faltas = firestore.transactional(_marcar_posibles_faltas_transaccional)(
            cliente.transaction(), app_ref, dt_actual
        )
        total_faltas += faltas
    return total_faltas


def _revisar_falta_transaccional(
    transaction,
    app_ref,
    audit_ref,
    sol_ref,
    actor_id,
    app_id,
    user_id,
    fecha_vencimiento,
    nuevo_estado,
    motivo,
    id_solicitud,
    version_revision_esperada,
    server_timestamp,
    now_dt=None
):
    if not id_solicitud or not str(id_solicitud).strip():
        return {"exito": False, "motivo": "id_solicitud_requerido"}

    app_snap = app_ref.get(transaction=transaction)
    sol_snap = sol_ref.get(transaction=transaction)

    if not app_snap.exists:
        return {"exito": False, "motivo": "campana_inexistente"}
    app = app_snap.to_dict()

    if app.get("estado") != "en_prueba":
        return {"exito": False, "motivo": "campana_no_en_prueba"}

    actor_id_str = str(actor_id)
    dev_id_str = str(app.get("dev_id", ""))
    admin_ids = obtener_admin_ids()
    is_dev = (actor_id_str == dev_id_str)
    is_admin = (actor_id_str in admin_ids)

    if not (is_dev or is_admin):
        return {"exito": False, "motivo": "permiso_denegado"}

    user_id_str = str(user_id)
    sol_id_str = str(id_solicitud).strip()

    # 1. Idempotencia independiente: Verificar sol_ref ANTES del chequeo de versión y prueba activa
    if sol_snap.exists:
        sol_dict = sol_snap.to_dict()
        mismo_contenido = (
            sol_dict.get("actor_id") == actor_id_str and
            sol_dict.get("app_id") == str(app_id) and
            sol_dict.get("user_id") == user_id_str and
            sol_dict.get("fecha_vencimiento") == fecha_vencimiento and
            sol_dict.get("nuevo_estado") == nuevo_estado and
            sol_dict.get("motivo") == motivo.strip()
        )
        if mismo_contenido:
            return sol_dict.get("resultado")
        else:
            return {"exito": False, "motivo": "id_solicitud_reutilizado_con_contenido_diferente"}

    # 2. Solicitud nueva: Exigir inscripción/prueba del tester
    testers_set = set(str(t) for t in app.get("testers", []))
    pruebas = copy.deepcopy(app.get("pruebas") or {})

    if user_id_str not in testers_set and user_id_str not in pruebas:
        return {"exito": False, "motivo": "user_no_inscrito"}

    prueba = pruebas.get(user_id_str)
    if not prueba:
        return {"exito": False, "motivo": "sin_prueba"}

    # 3. Solicitud nueva: Exigir versión esperada válida e igual (None no es permitido)
    if version_revision_esperada is None or not isinstance(version_revision_esperada, int):
        return {"exito": False, "motivo": "version_revision_esperada_requerida"}

    version_actual = prueba.get("version_revision", 0)
    if version_revision_esperada != version_actual:
        return {"exito": False, "motivo": "conflicto_modificacion_concurrente", "version_actual": version_actual}

    # 4. Validación de fecha
    try:
        d_venc = datetime.strptime(fecha_vencimiento, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return {"exito": False, "motivo": "fecha_invalida"}

    dt_now = parse_dt_mexico(now_dt)
    d_today = dt_now.date()

    if d_venc > d_today:
        return {"exito": False, "motivo": "fecha_futura"}

    fecha_v1_str = calcular_vencimiento_primera_entrega(
        app.get("timestamp_lanzamiento"), app.get("fecha_lanzamiento")
    )
    d_v1 = datetime.strptime(fecha_v1_str, "%Y-%m-%d").date()

    if d_venc < d_v1:
        return {"exito": False, "motivo": "fecha_anterior_a_obligaciones"}

    # 5. Validación de estado y motivo
    estados_permitidos = {"cumplida_por_revision", "justificada", "injustificada"}
    if nuevo_estado not in estados_permitidos:
        return {"exito": False, "motivo": "estado_no_permitido"}

    if not motivo or not isinstance(motivo, str) or len(motivo.strip()) < 5:
        return {"exito": False, "motivo": "motivo_invalido_o_corto"}

    # 6. Aplicar la revisión
    _preservar_progreso_legacy_si_corresponde(prueba, "revision_manual_post_legacy")

    posibles_faltas = copy.deepcopy(prueba.get("posibles_faltas") or {})
    falta_entry = copy.deepcopy(posibles_faltas.get(fecha_vencimiento) or {})
    estado_resolucion_anterior = falta_entry.get("estado_resolucion", falta_entry.get("estado", "pendiente_revision"))

    id_evento = str(audit_ref.id)
    falta_entry["fecha_vencimiento"] = fecha_vencimiento
    falta_entry["estado_resolucion"] = nuevo_estado
    falta_entry["estado"] = nuevo_estado
    falta_entry["motivo_resolucion"] = motivo.strip()
    falta_entry["actor_resolucion"] = actor_id_str
    falta_entry["id_evento_resolucion"] = id_evento
    falta_entry["requiere_nueva_revision"] = False
    posibles_faltas[fecha_vencimiento] = falta_entry
    prueba["posibles_faltas"] = posibles_faltas

    nueva_version = version_actual + 1
    prueba["version_revision"] = nueva_version

    historial_eventos = list(prueba.get("historial_eventos_ids") or [])
    historial_eventos.append(id_evento)
    prueba["historial_eventos_ids"] = historial_eventos

    dias_validos = calcular_dias_cumplidos_validos(prueba)
    prueba["dias_completados"] = dias_validos

    resultado_dict = {
        "exito": True,
        "app_id": str(app_id),
        "user_id": str(user_id),
        "fecha_vencimiento": fecha_vencimiento,
        "estado_resolucion": nuevo_estado,
        "dias_completados": dias_validos,
        "version_revision": nueva_version,
        "id_evento": id_evento,
        "id_solicitud": sol_id_str
    }

    sol_data = {
        "id_solicitud": sol_id_str,
        "solicitud_key": str(sol_ref.id),
        "actor_id": actor_id_str,
        "app_id": str(app_id),
        "user_id": user_id_str,
        "fecha_vencimiento": fecha_vencimiento,
        "nuevo_estado": nuevo_estado,
        "motivo": motivo.strip(),
        "resultado": resultado_dict,
        "fecha_registro": server_timestamp
    }

    audit_data = {
        "id_evento": id_evento,
        "id_solicitud": sol_id_str,
        "app_id": str(app_id),
        "user_id": user_id_str,
        "actor_id": actor_id_str,
        "rol_actor": "admin" if is_admin else "dev",
        "fecha_vencimiento": fecha_vencimiento,
        "estado_resolucion_anterior": estado_resolucion_anterior,
        "estado_resolucion_nuevo": nuevo_estado,
        "motivo": motivo.strip(),
        "version_revision": nueva_version,
        "fecha_auditoria": server_timestamp
    }

    pruebas[user_id_str] = prueba
    transaction.update(app_ref, {"pruebas": pruebas})
    transaction.create(audit_ref, audit_data)
    transaction.create(sol_ref, sol_data)

    return resultado_dict


def revisar_falta_obligacion(
    actor_id,
    app_id,
    user_id,
    fecha_vencimiento,
    nuevo_estado,
    motivo,
    id_solicitud,
    version_revision_esperada=None,
    now_dt=None
):
    if not _firebase_configurado():
        return {"exito": False, "motivo": "local_mode"}
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return {"exito": False, "motivo": "firestore_no_disponible"}
    from firebase_admin import firestore
    app_ref = cliente.collection("apps").document(str(app_id))
    id_evento = str(uuid.uuid4())
    audit_ref = cliente.collection("auditoria_revisiones").document(id_evento)
    sol_doc_id = f"{app_id}_{user_id}_{id_solicitud}"
    sol_ref = cliente.collection("solicitudes_revisiones").document(sol_doc_id)
    return firestore.transactional(_revisar_falta_transaccional)(
        cliente.transaction(),
        app_ref,
        audit_ref,
        sol_ref,
        actor_id,
        app_id,
        user_id,
        fecha_vencimiento,
        nuevo_estado,
        motivo,
        id_solicitud,
        version_revision_esperada,
        firestore.SERVER_TIMESTAMP,
        now_dt
    )


# --- MANEJO TRANSACCIONAL DE APELACIONES Y CONSULTA DE CASOS (5C.2) ---
def _crear_apelacion_transaccional(
    transaction,
    app_ref,
    sol_ref,
    apel_ref,
    actor_id,
    app_id,
    user_id,
    fecha_vencimiento,
    id_evento_resolucion_apelada,
    justificacion,
    id_solicitud,
    server_timestamp
):
    actor_id_str = str(actor_id)
    user_id_str = str(user_id)
    sol_id_str = str(id_solicitud).strip()
    id_evt_apelado_str = str(id_evento_resolucion_apelada).strip()

    # 1. Comprobación de propiedad: El usuario solo puede apelar sus propios casos
    if actor_id_str != user_id_str:
        return {"exito": False, "motivo": "permiso_denegado"}

    # 2. Idempotencia independiente
    sol_snap = sol_ref.get(transaction=transaction)
    if sol_snap.exists:
        sol_dict = sol_snap.to_dict()
        mismo_contenido = (
            sol_dict.get("actor_id") == actor_id_str and
            sol_dict.get("app_id") == str(app_id) and
            sol_dict.get("user_id") == user_id_str and
            sol_dict.get("fecha_vencimiento") == fecha_vencimiento and
            sol_dict.get("id_evento_resolucion_apelada") == id_evt_apelado_str and
            sol_dict.get("justificacion") == justificacion.strip()
        )
        if mismo_contenido:
            return sol_dict.get("resultado")
        else:
            return {"exito": False, "motivo": "id_solicitud_reutilizado_con_contenido_diferente"}

    # 3. Validación de justificación obligatoria
    if not justificacion or not isinstance(justificacion, str) or len(justificacion.strip()) < 5:
        return {"exito": False, "motivo": "justificacion_invalida_o_corta"}

    # 4. Comprobar que no exista apelación pendiente previa
    apel_snap = apel_ref.get(transaction=transaction)
    if apel_snap.exists:
        apel_dict = apel_snap.to_dict()
        if apel_dict.get("estado_apelacion") == "pendiente":
            return {"exito": False, "motivo": "apelacion_ya_pendiente"}
        else:
            return {"exito": False, "motivo": "apelacion_ya_resuelta"}

    # 5. Obtener resolución apelada (en prueba activa o en auditoría histórica si fue expulsado)
    app_snap = app_ref.get(transaction=transaction)
    if not app_snap.exists:
        return {"exito": False, "motivo": "campana_inexistente"}

    app = app_snap.to_dict()
    pruebas = copy.deepcopy(app.get("pruebas") or {})
    prueba = pruebas.get(user_id_str)

    if prueba is not None:
        posibles_faltas = prueba.get("posibles_faltas") or {}
        falta_entry = posibles_faltas.get(fecha_vencimiento)
        if not falta_entry:
            return {"exito": False, "motivo": "fecha_sin_resolucion"}
        estado_resolucion_apelado = falta_entry.get("estado_resolucion", falta_entry.get("estado"))
        id_evt_actual = falta_entry.get("id_evento_resolucion")
        if id_evt_actual and id_evt_actual != id_evt_apelado_str:
            return {"exito": False, "motivo": "evento_resolucion_no_coincide"}
    else:
        # Tester post-expulsión: buscar en auditoría
        audit_ev_ref = app_ref.client.collection("auditoria_revisiones").document(id_evt_apelado_str)
        audit_snap = audit_ev_ref.get(transaction=transaction)
        if not audit_snap.exists:
            return {"exito": False, "motivo": "resolucion_no_encontrada"}
        audit_dict = audit_snap.to_dict()
        if (audit_dict.get("app_id") != str(app_id) or
            audit_dict.get("user_id") != user_id_str or
            audit_dict.get("fecha_vencimiento") != fecha_vencimiento):
            return {"exito": False, "motivo": "resolucion_no_coincide"}
        estado_resolucion_apelado = audit_dict.get("estado_resolucion_nuevo")

    # Estados apelables: justificada, injustificada, cumplida_por_revision
    estados_apelables = {"injustificada", "justificada", "cumplida_por_revision"}
    if estado_resolucion_apelado not in estados_apelables:
        return {"exito": False, "motivo": "estado_no_apelable"}

    # 6. Preparar documentos de apelación y solicitud
    resultado_dict = {
        "exito": True,
        "id_apelacion": str(apel_ref.id),
        "app_id": str(app_id),
        "user_id": user_id_str,
        "fecha_vencimiento": fecha_vencimiento,
        "id_evento_resolucion_apelada": id_evt_apelado_str,
        "estado_resolucion_apelado": estado_resolucion_apelado,
        "estado_apelacion": "pendiente",
        "id_solicitud": sol_id_str
    }

    apel_data = {
        "id_apelacion": str(apel_ref.id),
        "app_id": str(app_id),
        "user_id": user_id_str,
        "fecha_vencimiento": fecha_vencimiento,
        "id_evento_resolucion_apelada": id_evt_apelado_str,
        "estado_resolucion_apelado": estado_resolucion_apelado,
        "justificacion_tester": justificacion.strip(),
        "estado_apelacion": "pendiente",
        "motivo_admin": None,
        "admin_id": None,
        "nueva_resolucion": None,
        "fecha_creacion": server_timestamp,
        "fecha_resolucion": None,
        "id_solicitud_creacion": sol_id_str
    }

    sol_data = {
        "id_solicitud": sol_id_str,
        "solicitud_key": str(sol_ref.id),
        "actor_id": actor_id_str,
        "app_id": str(app_id),
        "user_id": user_id_str,
        "fecha_vencimiento": fecha_vencimiento,
        "id_evento_resolucion_apelada": id_evt_apelado_str,
        "justificacion": justificacion.strip(),
        "resultado": resultado_dict,
        "fecha_registro": server_timestamp
    }

    transaction.create(apel_ref, apel_data)
    transaction.create(sol_ref, sol_data)

    return resultado_dict


def crear_apelacion(
    actor_id,
    app_id,
    user_id,
    fecha_vencimiento,
    id_evento_resolucion_apelada,
    justificacion,
    id_solicitud
):
    if not _firebase_configurado():
        return {"exito": False, "motivo": "local_mode"}
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return {"exito": False, "motivo": "firestore_no_disponible"}
    from firebase_admin import firestore
    app_ref = cliente.collection("apps").document(str(app_id))
    apel_doc_id = f"{app_id}_{user_id}_{id_evento_resolucion_apelada}"
    apel_ref = cliente.collection("apelaciones_revisiones").document(apel_doc_id)
    sol_doc_id = f"{app_id}_{user_id}_{id_solicitud}"
    sol_ref = cliente.collection("solicitudes_revisiones").document(sol_doc_id)
    return firestore.transactional(_crear_apelacion_transaccional)(
        cliente.transaction(),
        app_ref,
        sol_ref,
        apel_ref,
        actor_id,
        app_id,
        user_id,
        fecha_vencimiento,
        id_evento_resolucion_apelada,
        justificacion,
        id_solicitud,
        firestore.SERVER_TIMESTAMP
    )


def _resolver_apelacion_transaccional(
    transaction,
    app_ref,
    apel_ref,
    audit_ref,
    sol_ref,
    admin_id,
    id_apelacion,
    decision,
    nueva_resolucion,
    motivo_admin,
    id_solicitud,
    version_revision_esperada,
    server_timestamp,
    now_dt
):
    admin_id_str = str(admin_id)
    admin_ids = obtener_admin_ids()

    # 1. Permisos: Solo administrador
    if admin_id_str not in admin_ids:
        return {"exito": False, "motivo": "permiso_denegado"}

    # 2. Idempotencia independiente
    sol_id_str = str(id_solicitud).strip()
    sol_snap = sol_ref.get(transaction=transaction)
    if sol_snap.exists:
        sol_dict = sol_snap.to_dict()
        mismo_contenido = (
            sol_dict.get("admin_id") == admin_id_str and
            sol_dict.get("id_apelacion") == str(id_apelacion) and
            sol_dict.get("decision") == decision and
            sol_dict.get("nueva_resolucion") == (nueva_resolucion if decision == "aceptada" else None) and
            sol_dict.get("motivo_admin") == motivo_admin.strip()
        )
        if mismo_contenido:
            return sol_dict.get("resultado")
        else:
            return {"exito": False, "motivo": "id_solicitud_reutilizado_con_contenido_diferente"}

    # 3. Validación de decisión y motivo
    if decision not in {"aceptada", "rechazada"}:
        return {"exito": False, "motivo": "decision_invalida"}

    if not motivo_admin or not isinstance(motivo_admin, str) or len(motivo_admin.strip()) < 5:
        return {"exito": False, "motivo": "motivo_invalido_o_corto"}

    if decision == "aceptada":
        if nueva_resolucion not in {"cumplida_por_revision", "justificada"}:
            return {"exito": False, "motivo": "nueva_resolucion_invalida"}

    # 4. Lectura y validación de la apelación
    apel_snap = apel_ref.get(transaction=transaction)
    if not apel_snap.exists:
        return {"exito": False, "motivo": "apelacion_no_encontrada"}

    apel_dict = apel_snap.to_dict()
    if apel_dict.get("estado_apelacion") != "pendiente":
        return {"exito": False, "motivo": "apelacion_no_pendiente", "estado_actual": apel_dict.get("estado_apelacion")}

    app_id = apel_dict.get("app_id")
    user_id_str = str(apel_dict.get("user_id"))
    fecha_vencimiento = apel_dict.get("fecha_vencimiento")
    id_evento_resolucion_apelada = str(apel_dict.get("id_evento_resolucion_apelada"))
    estado_resolucion_apelado = apel_dict.get("estado_resolucion_apelado")

    # 5. Lectura de la campaña y control de concurrencia
    app_snap = app_ref.get(transaction=transaction)
    app = app_snap.to_dict() if app_snap.exists else {}
    pruebas = copy.deepcopy(app.get("pruebas") or {})
    prueba = pruebas.get(user_id_str)

    id_evento_admin = str(audit_ref.id)
    dias_validos = None
    nueva_version = None

    if prueba is not None:
        # Prueba activa
        posibles_faltas = copy.deepcopy(prueba.get("posibles_faltas") or {})
        falta_entry = copy.deepcopy(posibles_faltas.get(fecha_vencimiento) or {})
        id_evt_actual = falta_entry.get("id_evento_resolucion")
        estado_actual = falta_entry.get("estado_resolucion", falta_entry.get("estado"))

        # Si la resolución de esa fecha cambió antes de que el admin resuelva:
        if id_evt_actual and id_evt_actual != id_evento_resolucion_apelada:
            return {"exito": False, "motivo": "resolucion_apelada_modificada"}
        if estado_actual != estado_resolucion_apelado:
            return {"exito": False, "motivo": "resolucion_apelada_modificada"}

        # Chequeo de concurrencia en la prueba completa:
        version_actual = prueba.get("version_revision", 0)
        if version_revision_esperada is not None and version_revision_esperada != version_actual:
            return {"exito": False, "motivo": "conflicto_modificacion_concurrente", "version_actual": version_actual}

        if decision == "aceptada":
            _preservar_progreso_legacy_si_corresponde(prueba, "resolucion_apelacion")
            falta_entry["fecha_vencimiento"] = fecha_vencimiento
            falta_entry["estado_resolucion"] = nueva_resolucion
            falta_entry["estado"] = nueva_resolucion
            falta_entry["motivo_resolucion"] = f"Apelación aceptada: {motivo_admin.strip()}"
            falta_entry["actor_resolucion"] = admin_id_str
            falta_entry["id_evento_resolucion"] = id_evento_admin
            falta_entry["requiere_nueva_revision"] = False
            posibles_faltas[fecha_vencimiento] = falta_entry
            prueba["posibles_faltas"] = posibles_faltas

            dias_validos = calcular_dias_cumplidos_validos(prueba)
            prueba["dias_completados"] = dias_validos

            nueva_version = version_actual + 1
            prueba["version_revision"] = nueva_version

            historial_eventos = list(prueba.get("historial_eventos_ids") or [])
            historial_eventos.append(id_evento_admin)
            prueba["historial_eventos_ids"] = historial_eventos

            pruebas[user_id_str] = prueba
            transaction.update(app_ref, {"pruebas": pruebas})
        else:
            # Rechazada: no se modifica la prueba en apps (mantiene resolución y días)
            dias_validos = prueba.get("dias_completados", 0)
            nueva_version = version_actual
    else:
        # Post-expulsión: NO se recrea la prueba en app["pruebas"]
        pass

    # 6. Actualizar documento de apelación
    transaction.update(apel_ref, {
        "estado_apelacion": decision,
        "motivo_admin": motivo_admin.strip(),
        "admin_id": admin_id_str,
        "nueva_resolucion": nueva_resolucion if decision == "aceptada" else None,
        "id_evento_resolucion_admin": id_evento_admin,
        "fecha_resolucion": server_timestamp,
        "id_solicitud_resolucion": sol_id_str
    })

    # 7. Registrar auditoría con SERVER_TIMESTAMP
    audit_data = {
        "id_evento": id_evento_admin,
        "id_solicitud": sol_id_str,
        "id_apelacion": str(apel_ref.id),
        "app_id": str(app_id),
        "user_id": user_id_str,
        "actor_id": admin_id_str,
        "rol_actor": "admin",
        "tipo_evento": "resolucion_apelacion",
        "fecha_vencimiento": fecha_vencimiento,
        "estado_resolucion_anterior": estado_resolucion_apelado,
        "estado_resolucion_nuevo": nueva_resolucion if decision == "aceptada" else estado_resolucion_apelado,
        "decision_apelacion": decision,
        "motivo": motivo_admin.strip(),
        "post_expulsion": (prueba is None),
        "fecha_auditoria": server_timestamp
    }
    transaction.create(audit_ref, audit_data)

    # 8. Guardar solicitud procesada
    resultado_dict = {
        "exito": True,
        "id_apelacion": str(apel_ref.id),
        "app_id": str(app_id),
        "user_id": user_id_str,
        "fecha_vencimiento": fecha_vencimiento,
        "decision": decision,
        "nueva_resolucion": nueva_resolucion if decision == "aceptada" else estado_resolucion_apelado,
        "dias_completados": dias_validos,
        "version_revision": nueva_version,
        "id_evento_admin": id_evento_admin,
        "post_expulsion": (prueba is None)
    }

    sol_data = {
        "id_solicitud": sol_id_str,
        "solicitud_key": str(sol_ref.id),
        "admin_id": admin_id_str,
        "id_apelacion": str(apel_ref.id),
        "decision": decision,
        "nueva_resolucion": nueva_resolucion if decision == "aceptada" else None,
        "motivo_admin": motivo_admin.strip(),
        "resultado": resultado_dict,
        "fecha_registro": server_timestamp
    }
    transaction.create(sol_ref, sol_data)

    return resultado_dict


def resolver_apelacion(
    admin_id,
    app_id,
    id_apelacion,
    decision,
    nueva_resolucion,
    motivo_admin,
    id_solicitud,
    version_revision_esperada=None,
    now_dt=None
):
    if not _firebase_configurado():
        return {"exito": False, "motivo": "local_mode"}
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return {"exito": False, "motivo": "firestore_no_disponible"}
    from firebase_admin import firestore
    app_ref = cliente.collection("apps").document(str(app_id))
    apel_ref = cliente.collection("apelaciones_revisiones").document(str(id_apelacion))
    id_evento = str(uuid.uuid4())
    audit_ref = cliente.collection("auditoria_revisiones").document(id_evento)
    sol_doc_id = f"{app_id}_{id_solicitud}"
    sol_ref = cliente.collection("solicitudes_revisiones").document(sol_doc_id)
    return firestore.transactional(_resolver_apelacion_transaccional)(
        cliente.transaction(),
        app_ref,
        apel_ref,
        audit_ref,
        sol_ref,
        admin_id,
        id_apelacion,
        decision,
        nueva_resolucion,
        motivo_admin,
        id_solicitud,
        version_revision_esperada,
        firestore.SERVER_TIMESTAMP,
        now_dt
    )


def obtener_casos_tester(user_id):
    """Obtiene los casos del usuario para /miscasos.

    Soporta pruebas activas y casos post-expulsión consultados desde auditoría.
    """
    cliente = _inicializar_firebase()
    user_id_str = str(user_id)
    casos_por_app = {}

    apps = cargar_apps()
    for app_id, app_data in apps.items():
        pruebas = app_data.get("pruebas") or {}
        if user_id_str in pruebas:
            prueba = pruebas[user_id_str]
            fechas_casos = {}
            posibles_faltas = prueba.get("posibles_faltas") or {}
            for f_venc, f_entry in posibles_faltas.items():
                estado = f_entry.get("estado_resolucion", f_entry.get("estado", "pendiente_revision"))
                id_evt = f_entry.get("id_evento_resolucion")
                fechas_casos[f_venc] = {
                    "fecha_vencimiento": f_venc,
                    "estado_resolucion": estado,
                    "motivo_resolucion": f_entry.get("motivo_resolucion", ""),
                    "id_evento_resolucion": id_evt,
                    "apelable": estado in {"injustificada", "justificada", "cumplida_por_revision"} and bool(id_evt),
                    "apelacion": None
                }
            casos_por_app[app_id] = {
                "app_id": app_id,
                "app_nombre": app_data.get("descripcion", app_id),
                "activa": True,
                "dias_completados": prueba.get("dias_completados", 0),
                "fechas": fechas_casos
            }

    if _firebase_initialized and cliente:
        try:
            audit_docs = cliente.collection("auditoria_revisiones").stream()
            for doc in audit_docs:
                adata = doc.to_dict()
                if str(adata.get("user_id")) == user_id_str:
                    a_app_id = str(adata.get("app_id"))
                    f_venc = adata.get("fecha_vencimiento")
                    id_evt = str(doc.id)
                    if a_app_id not in casos_por_app:
                        app_data = apps.get(a_app_id, {})
                        casos_por_app[a_app_id] = {
                            "app_id": a_app_id,
                            "app_nombre": app_data.get("descripcion", a_app_id),
                            "activa": False,
                            "dias_completados": 0,
                            "post_expulsion": True,
                            "fechas": {}
                        }
                    app_caso = casos_por_app[a_app_id]
                    if f_venc not in app_caso["fechas"]:
                        app_caso["fechas"][f_venc] = {
                            "fecha_vencimiento": f_venc,
                            "estado_resolucion": adata.get("estado_resolucion_nuevo"),
                            "motivo_resolucion": adata.get("motivo", ""),
                            "id_evento_resolucion": id_evt,
                            "apelable": adata.get("estado_resolucion_nuevo") in {"injustificada", "justificada", "cumplida_por_revision"},
                            "apelacion": None
                        }
                    else:
                        if not app_caso["fechas"][f_venc].get("id_evento_resolucion"):
                            app_caso["fechas"][f_venc]["id_evento_resolucion"] = id_evt
                            app_caso["fechas"][f_venc]["apelable"] = True
        except Exception as e:
            logger.warning(f"Error al leer auditoria_revisiones para tester {user_id}: {e}")

    if _firebase_initialized and cliente:
        try:
            apel_docs = cliente.collection("apelaciones_revisiones").stream()
            for doc in apel_docs:
                ap_data = doc.to_dict()
                if str(ap_data.get("user_id")) == user_id_str:
                    a_app_id = str(ap_data.get("app_id"))
                    f_venc = ap_data.get("fecha_vencimiento")
                    if a_app_id in casos_por_app and f_venc in casos_por_app[a_app_id]["fechas"]:
                        casos_por_app[a_app_id]["fechas"][f_venc]["apelacion"] = ap_data
        except Exception as e:
            logger.warning(f"Error al leer apelaciones_revisiones para tester {user_id}: {e}")

    return list(casos_por_app.values())


def obtener_apelaciones_pendientes():
    """Obtiene la lista de apelaciones en estado 'pendiente' para el panel de Admin."""
    cliente = _inicializar_firebase()
    if not _firebase_initialized or not cliente:
        return []
    try:
        docs = cliente.collection("apelaciones_revisiones").stream()
        pendientes = []
        for doc in docs:
            data = doc.to_dict()
            if data.get("estado_apelacion") == "pendiente":
                data["id_apelacion"] = doc.id
                pendientes.append(data)
        return pendientes
    except Exception as e:
        logger.warning(f"Error al leer apelaciones pendientes: {e}")
        return []


def deshabilitar_ajuste_arbitrario_dias():
    """Garantía de 5C: Deshabilita cualquier ajuste manual directo del contador de días."""
    raise NotImplementedError(
        "El ajuste arbitrario de días ha sido deshabilitado. Usá revisar_falta_obligacion() por fecha con auditoría."
    )


# --- CONCILIACIÓN DEFINITIVA, RECOMPENSAS Y CIERRE DE CAMPAÑAS (5D) ---

def verificar_cumplimiento_tester(app_id, user_id, cliente=None):
    """
    Verifica el cumplimiento individual de un tester para una campaña según reglas de 5D.
    Comprueba:
    1. Que la campaña exista y haya iniciado.
    2. Que el usuario pertenezca a la campaña y no esté expulsado.
    3. Que la prueba esté activa ('instalada' o 'completada').
    4. Que no existan faltas con estado 'pendiente_revision' o 'requiere_nueva_revision'.
    5. Que no existan apelaciones en estado 'pendiente'.
    6. Que no existan marcas de incertidumbre o conciliación legacy sin solventar.
    7. Que cuente con al menos 14 días válidos auditados (calcular_dias_cumplidos_validos).
    8. Que no haya sido liquidado anteriormente.
    """
    if cliente is None and _firebase_configurado():
        cliente = _inicializar_firebase()

    app = None
    if cliente and _firebase_initialized:
        app_doc = cliente.collection("apps").document(str(app_id)).get()
        if app_doc.exists:
            app = app_doc.to_dict()
    else:
        apps = _cargar_json_local(APPS_FILE)
        app = apps.get(str(app_id))

    if not app:
        return {"elegible": False, "motivo": "campana_inexistente", "dias_validos": 0}

    estado_app = app.get("estado")
    if estado_app not in ["en_prueba", "finalizada", "cerrada", "cerrada_definitivamente"]:
        return {"elegible": False, "motivo": "campana_no_iniciada", "dias_validos": 0}

    testers = [str(t) for t in app.get("testers", [])]
    user_id_str = str(user_id)
    if user_id_str not in testers:
        return {"elegible": False, "motivo": "tester_no_inscrito", "dias_validos": 0}

    pruebas = app.get("pruebas") or {}
    prueba = pruebas.get(user_id_str)
    if not prueba:
        return {"elegible": False, "motivo": "sin_prueba", "dias_validos": 0}

    if prueba.get("estado") == "expulsada":
        return {"elegible": False, "motivo": "tester_expulsado", "dias_validos": 0}

    if prueba.get("estado") not in ["instalada", "completada"]:
        return {"elegible": False, "motivo": "prueba_no_activa", "dias_validos": 0}

    posibles_faltas = prueba.get("posibles_faltas") or {}
    faltas_pendientes = [
        f_v for f_v, f in posibles_faltas.items()
        if f.get("estado") == "pendiente_revision" or f.get("requiere_nueva_revision") is True
    ]
    if faltas_pendientes:
        return {
            "elegible": False,
            "motivo": "faltas_pendientes_revision",
            "dias_validos": calcular_dias_cumplidos_validos(prueba),
            "faltas_pendientes": faltas_pendientes
        }

    apelaciones_pendientes = []
    if cliente and _firebase_initialized:
        try:
            for falta in posibles_faltas.values():
                id_evt = falta.get("id_evento_resolucion")
                if id_evt:
                    apel_id = f"{app_id}_{user_id}_{id_evt}"
                    ap_doc = cliente.collection("apelaciones_revisiones").document(apel_id).get()
                    if ap_doc.exists and ap_doc.to_dict().get("estado_apelacion") == "pendiente":
                        apelaciones_pendientes.append(apel_id)
        except Exception as e:
            logger.warning(f"Error al verificar apelaciones de {user_id}: {e}")
    if apelaciones_pendientes:
        return {
            "elegible": False,
            "motivo": "apelacion_pendiente",
            "dias_validos": calcular_dias_cumplidos_validos(prueba),
            "apelaciones_pendientes": apelaciones_pendientes
        }

    if bool(prueba.get("requiere_conciliacion_legacy")) or bool(prueba.get("incertidumbre_progreso_legacy")):
        return {
            "elegible": False,
            "motivo": "requiere_conciliacion_legacy",
            "dias_validos": calcular_dias_cumplidos_validos(prueba)
        }

    dias_validos = calcular_dias_cumplidos_validos(prueba)
    if dias_validos < 14:
        return {
            "elegible": False,
            "motivo": "dias_insuficientes",
            "dias_validos": dias_validos
        }

    id_mov = id_movimiento_recompensa(app_id, user_id)
    ya_liquidada = False
    if prueba.get("recompensa_liquidada") is True:
        ya_liquidada = True
    elif cliente and _firebase_initialized:
        mov_doc = cliente.collection("recompensas").document(id_mov).get()
        if mov_doc.exists:
            ya_liquidada = True
    else:
        recompensas = _cargar_json_local("recompensas.json")
        if id_mov in recompensas:
            ya_liquidada = True

    if ya_liquidada:
        return {
            "elegible": False,
            "ya_liquidada": True,
            "motivo": "ya_liquidada",
            "dias_validos": dias_validos,
            "id_movimiento": id_mov
        }

    return {
        "elegible": True,
        "motivo": "cumple_requisitos",
        "dias_validos": dias_validos,
        "id_movimiento": id_mov
    }


def _liquidar_recompensa_transaccional(
    transaction,
    user_ref,
    app_ref,
    movimiento_ref,
    sol_ref,
    user_id,
    app_id,
    actor_id,
    server_timestamp,
    now_dt,
    id_solicitud=None
):
    user_id_str = str(user_id)
    app_id_str = str(app_id)
    actor_id_str = str(actor_id) if actor_id else "sistema"
    sol_id_str = str(id_solicitud).strip() if id_solicitud else None

    # --- 1. LECTURAS TRANSACCIONALES (Todas antes de cualquier escritura) ---
    user_snap = user_ref.get(transaction=transaction)
    app_snap = app_ref.get(transaction=transaction)
    mov_snap = movimiento_ref.get(transaction=transaction)
    sol_snap = sol_ref.get(transaction=transaction) if sol_ref else None

    if not user_snap.exists or not app_snap.exists:
        return {"exito": False, "motivo": "usuario_o_campana_inexistente"}

    usuario = user_snap.to_dict()
    app = app_snap.to_dict()
    pruebas = copy.deepcopy(app.get("pruebas") or {})
    prueba = pruebas.get(user_id_str)

    posibles_faltas = copy.deepcopy(prueba.get("posibles_faltas") or {}) if prueba else {}
    apel_snaps = []
    if app_ref.client and prueba:
        for falta in posibles_faltas.values():
            id_evt = falta.get("id_evento_resolucion")
            if id_evt:
                ap_ref = app_ref.client.collection("apelaciones_revisiones").document(f"{app_id}_{user_id}_{id_evt}")
                apel_snaps.append(ap_ref.get(transaction=transaction))

    # --- 2. EVALUACIÓN Y VALIDACIONES ---
    if sol_snap and sol_snap.exists:
        sol_data = sol_snap.to_dict()
        mismo_contenido = (
            sol_data.get("user_id") == user_id_str and
            sol_data.get("app_id") == app_id_str
        )
        if mismo_contenido:
            return sol_data.get("resultado")
        else:
            return {"exito": False, "motivo": "id_solicitud_reutilizado_con_contenido_diferente"}

    if mov_snap.exists or (prueba and prueba.get("recompensa_liquidada") is True):
        dias_audit = prueba.get("dias_completados", 14) if prueba else 14
        es_beta = bool(app.get("es_beta") or app.get("tipo_campana") == "beta_gratuita")
        puntos_f = int(app.get("puntos_founder_recompensa", PUNTOS_FOUNDER_DEFECTO)) if es_beta else 0
        return {
            "exito": True,
            "resultado": "ya_liquidada",
            "motivo": "ya_pagada",
            "app_id": app_id_str,
            "user_id": user_id_str,
            "es_beta": es_beta,
            "importe": 0 if es_beta else 800,
            "boletos": 0 if es_beta else 1,
            "puntos_founder": puntos_f,
            "dias_auditados": dias_audit,
            "id_movimiento": str(movimiento_ref.id)
        }

    if not prueba:
        return {"exito": False, "motivo": "sin_prueba"}

    if prueba.get("estado") == "expulsada":
        return {"exito": False, "motivo": "tester_expulsado"}

    if prueba.get("estado") not in ["instalada", "completada"]:
        return {"exito": False, "motivo": "prueba_no_activa"}

    testers = [str(t) for t in app.get("testers", [])]
    if user_id_str not in testers:
        return {"exito": False, "motivo": "user_no_inscrito"}

    if app.get("estado") not in ["en_prueba", "finalizada", "cerrada", "cerrada_definitivamente"]:
        return {"exito": False, "motivo": "campana_no_activa"}

    faltas_pendientes = [
        f_v for f_v, f in posibles_faltas.items()
        if f.get("estado") == "pendiente_revision" or f.get("requiere_nueva_revision") is True
    ]
    if faltas_pendientes:
        return {"exito": False, "motivo": "faltas_pendientes_revision", "faltas": faltas_pendientes}

    for ap_snap in apel_snaps:
        if ap_snap.exists and ap_snap.to_dict().get("estado_apelacion") == "pendiente":
            return {"exito": False, "motivo": "apelacion_pendiente"}

    if bool(prueba.get("requiere_conciliacion_legacy")) or bool(prueba.get("incertidumbre_progreso_legacy")):
        return {"exito": False, "motivo": "requiere_conciliacion_legacy"}

    dias_validos = calcular_dias_cumplidos_validos(prueba)
    if dias_validos < 14:
        return {"exito": False, "motivo": "dias_insuficientes", "dias_validos": dias_validos}

    # --- 3. ESCRITURAS TRANSACCIONALES ATÓMICAS ---
    dt_now = parse_dt_mexico(now_dt)
    fecha_iso = dt_now.isoformat()

    es_campana_beta = bool(app.get("es_beta") or app.get("tipo_campana") == "beta_gratuita")
    puntos_founder = int(app.get("puntos_founder_recompensa", PUNTOS_FOUNDER_DEFECTO)) if es_campana_beta else 0
    importe_creditos = 0 if es_campana_beta else 800
    boletos_otorgados = 0 if es_campana_beta else 1

    # Actualización y consolidación de Reputación Permanente
    reputacion = copy.deepcopy(usuario.get("reputacion") or {})
    campanas_completadas = reputacion.get("campanas_completadas", 0) + 1
    dias_totales = reputacion.get("dias_totales_cumplidos", 0) + dias_validos

    faltas_injust_actual = 0
    for f in posibles_faltas.values():
        if f.get("estado_resolucion") == "injustificada" or f.get("estado") == "injustificada":
            faltas_injust_actual += 1
    faltas_injust_total = reputacion.get("faltas_injustificadas", 0) + faltas_injust_actual

    total_evaluado = campanas_completadas * 14
    cumplimiento_ratio = max(0.0, min(1.0, (total_evaluado - faltas_injust_total) / total_evaluado)) if total_evaluado > 0 else 1.0
    confiabilidad_pct = round(cumplimiento_ratio * 100, 1)

    if confiabilidad_pct >= 95.0:
        nivel_confiabilidad = "Excelente"
    elif confiabilidad_pct >= 80.0:
        nivel_confiabilidad = "Alta"
    elif confiabilidad_pct >= 60.0:
        nivel_confiabilidad = "Media"
    else:
        nivel_confiabilidad = "Baja"

    reputacion.update({
        "campanas_completadas": campanas_completadas,
        "dias_totales_cumplidos": dias_totales,
        "faltas_injustificadas": faltas_injust_total,
        "confiabilidad_pct": confiabilidad_pct,
        "nivel_confiabilidad": nivel_confiabilidad,
        "ultima_actualizacion": fecha_iso
    })

    user_updates = {
        "reputacion": reputacion
    }
    if es_campana_beta:
        # En campañas beta se otorgan Puntos Founder independientes sin alterar créditos ni boletos
        user_updates["puntos_founder"] = usuario.get("puntos_founder", 0) + puntos_founder
    else:
        # En campañas estándar se conservan exactamente los 800 créditos y 1 boleto
        user_updates["creditos"] = usuario.get("creditos", 0) + 800
        user_updates["boletos_rifa"] = usuario.get("boletos_rifa", 0) + 1

    transaction.update(user_ref, user_updates)

    recompensa_doc = {
        "usuario": user_id_str,
        "campana": app_id_str,
        "es_beta": es_campana_beta,
        "tipo_recompensa": "founder" if es_campana_beta else "estandar",
        "importe": importe_creditos,
        "boletos": boletos_otorgados,
        "puntos_founder": puntos_founder,
        "fecha": server_timestamp,
        "estado": "liquidada",
        "motivo": "cumplimiento_14_dias",
        "dias_auditados": dias_validos,
        "actor_liquidacion": actor_id_str,
        "fecha_iso": fecha_iso,
        "id_movimiento": str(movimiento_ref.id)
    }
    transaction.create(movimiento_ref, recompensa_doc)

    prueba["recompensa_liquidada"] = True
    prueba["fecha_liquidacion"] = fecha_iso
    prueba["estado"] = "completada"
    prueba["requiere_conciliacion"] = False
    prueba["dias_completados"] = dias_validos
    pruebas[user_id_str] = prueba
    transaction.update(app_ref, {"pruebas": pruebas})

    resultado = {
        "exito": True,
        "resultado": "liquidada",
        "app_id": app_id_str,
        "user_id": user_id_str,
        "es_beta": es_campana_beta,
        "importe": importe_creditos,
        "boletos": boletos_otorgados,
        "puntos_founder": puntos_founder,
        "dias_auditados": dias_validos,
        "fecha_liquidacion": fecha_iso,
        "id_movimiento": str(movimiento_ref.id)
    }

    if sol_ref:
        transaction.create(sol_ref, {
            "id_solicitud": sol_id_str,
            "user_id": user_id_str,
            "app_id": app_id_str,
            "actor_id": actor_id_str,
            "resultado": resultado,
            "fecha_registro": server_timestamp
        })

    return resultado


def liquidar_recompensa_tester(app_id, user_id, actor_id=None, now_dt=None, id_solicitud=None):
    """
    Realiza la liquidación transaccional e idempotente de 800 créditos y 1 boleto de rifa a un tester elegible.
    """
    if not _firebase_configurado():
        return {"exito": False, "motivo": "modo_local_no_disponible"}
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return {"exito": False, "motivo": "firestore_no_disponible"}

    from firebase_admin import firestore
    user_ref = cliente.collection("usuarios").document(str(user_id))
    app_ref = cliente.collection("apps").document(str(app_id))
    id_mov = id_movimiento_recompensa(app_id, user_id)
    movimiento_ref = cliente.collection("recompensas").document(id_mov)
    sol_ref = None
    if id_solicitud:
        sol_ref = cliente.collection("solicitudes_liquidaciones").document(f"{app_id}_{user_id}_{id_solicitud}")

    return firestore.transactional(_liquidar_recompensa_transaccional)(
        cliente.transaction(),
        user_ref,
        app_ref,
        movimiento_ref,
        sol_ref,
        user_id,
        app_id,
        actor_id,
        firestore.SERVER_TIMESTAMP,
        now_dt,
        id_solicitud
    )


def obtener_resumen_conciliacion_campana(app_id, cliente=None):
    """
    Construye un resumen exhaustivo de conciliación y cierre para una campaña.
    """
    if cliente is None and _firebase_configurado():
        cliente = _inicializar_firebase()

    app = None
    if cliente and _firebase_initialized:
        app_doc = cliente.collection("apps").document(str(app_id)).get()
        if app_doc.exists:
            app = app_doc.to_dict()
    else:
        apps = _cargar_json_local(APPS_FILE)
        app = apps.get(str(app_id))

    if not app:
        return {"encontrada": False, "motivo": "campana_inexistente"}

    testers_lista = [str(t) for t in app.get("testers", [])]
    pruebas = app.get("pruebas") or {}
    todos_testers = set(testers_lista) | set(str(k) for k in pruebas.keys())

    completados = []
    liquidados = []
    pendientes_liquidacion = []
    con_faltas_pendientes = []
    con_apelaciones_pendientes = []
    expulsados = []
    requieren_conciliacion_legacy = []
    en_progreso = []

    for t_id in todos_testers:
        eval_t = verificar_cumplimiento_tester(app_id, t_id, cliente=cliente)
        p = pruebas.get(t_id, {})
        dias = eval_t.get("dias_validos", 0)

        if p.get("estado") == "expulsada":
            expulsados.append({"tester_id": t_id, "dias": dias})
            continue

        if eval_t.get("ya_liquidada"):
            liquidados.append({"tester_id": t_id, "dias": dias})
            completados.append(t_id)
        elif eval_t.get("elegible"):
            completados.append(t_id)
            pendientes_liquidacion.append({"tester_id": t_id, "dias": dias})
        else:
            motivo = eval_t.get("motivo")
            if motivo == "faltas_pendientes_revision":
                con_faltas_pendientes.append({"tester_id": t_id, "dias": dias, "faltas": eval_t.get("faltas_pendientes")})
            elif motivo == "apelacion_pendiente":
                con_apelaciones_pendientes.append({"tester_id": t_id, "dias": dias, "apelaciones": eval_t.get("apelaciones_pendientes")})
            elif motivo == "requiere_conciliacion_legacy":
                requieren_conciliacion_legacy.append({"tester_id": t_id, "dias": dias})
            elif motivo == "dias_insuficientes":
                en_progreso.append({"tester_id": t_id, "dias": dias})
            else:
                en_progreso.append({"tester_id": t_id, "dias": dias, "motivo": motivo})

    puede_cerrarse = (
        len(pendientes_liquidacion) == 0 and
        len(con_faltas_pendientes) == 0 and
        len(con_apelaciones_pendientes) == 0 and
        len(requieren_conciliacion_legacy) == 0
    )

    estado_conciliacion = (
        "cerrada_definitivamente" if app.get("estado") in ["cerrada", "cerrada_definitivamente"] or app.get("estado_conciliacion") == "cerrada_definitivamente"
        else ("lista_para_cierre" if puede_cerrarse else "bloqueada_por_pendientes")
    )

    return {
        "encontrada": True,
        "app_id": str(app_id),
        "descripcion": app.get("descripcion", ""),
        "dev_id": str(app.get("dev_id", "")),
        "estado_operativo": app.get("estado", "reclutando"),
        "estado_conciliacion": estado_conciliacion,
        "puede_cerrarse": puede_cerrarse,
        "fecha_lanzamiento": app.get("fecha_lanzamiento") or app.get("timestamp_lanzamiento"),
        "fecha_cierre_definitivo": app.get("fecha_cierre_definitivo"),
        "total_testers_inscritos": len(testers_lista),
        "testers_completados": completados,
        "total_completados": len(completados),
        "recompensas_liquidadas": liquidados,
        "total_liquidadas": len(liquidados),
        "recompensas_pendientes": pendientes_liquidacion,
        "total_recompensas_pendientes": len(pendientes_liquidacion),
        "con_faltas_pendientes": con_faltas_pendientes,
        "con_apelaciones_pendientes": con_apelaciones_pendientes,
        "requieren_conciliacion_legacy": requieren_conciliacion_legacy,
        "expulsados": expulsados,
        "en_progreso": en_progreso,
        "es_beta": bool(app.get("es_beta") or app.get("tipo_campana") == "beta_gratuita")
    }


def liquidar_campana_pendientes(app_id, actor_id=None, now_dt=None):
    """
    Ejecuta la liquidación de todos los testers con 14 días válidos y elegibles en una campaña.
    """
    resumen = obtener_resumen_conciliacion_campana(app_id)
    if not resumen.get("encontrada"):
        return {"exito": False, "motivo": "campana_inexistente"}

    pendientes = resumen.get("recompensas_pendientes", [])
    resultados = []
    exitosos = 0
    errores = 0

    for item in pendientes:
        t_id = item["tester_id"]
        res_liq = liquidar_recompensa_tester(app_id, t_id, actor_id=actor_id, now_dt=now_dt)
        resultados.append({"tester_id": t_id, "resultado": res_liq})
        if res_liq.get("exito"):
            exitosos += 1
        else:
            errores += 1

    return {
        "exito": True,
        "app_id": str(app_id),
        "total_pendientes_procesados": len(pendientes),
        "liquidados_exitosos": exitosos,
        "errores": errores,
        "detalles": resultados
    }


def _cerrar_campana_transaccional(
    transaction,
    app_ref,
    audit_close_ref,
    sol_ref,
    app_id,
    actor_id,
    motivo,
    server_timestamp,
    now_dt,
    id_solicitud=None
):
    actor_id_str = str(actor_id)
    app_id_str = str(app_id)
    admin_ids = obtener_admin_ids()

    # 1. Lecturas
    app_snap = app_ref.get(transaction=transaction)
    sol_snap = sol_ref.get(transaction=transaction) if sol_ref else None

    if not app_snap.exists:
        return {"exito": False, "motivo": "campana_inexistente"}

    app = app_snap.to_dict()

    if sol_snap and sol_snap.exists:
        sol_data = sol_snap.to_dict()
        if sol_data.get("app_id") == app_id_str:
            return sol_data.get("resultado")
        else:
            return {"exito": False, "motivo": "id_solicitud_reutilizado_con_contenido_diferente"}

    is_dev = str(app.get("dev_id")) == actor_id_str
    is_admin = actor_id_str in admin_ids
    if not (is_dev or is_admin):
        return {"exito": False, "motivo": "permiso_denegado"}

    if app.get("estado") in ["cerrada", "cerrada_definitivamente"] or app.get("estado_conciliacion") == "cerrada_definitivamente":
        return {
            "exito": True,
            "resultado": "ya_cerrada",
            "app_id": app_id_str,
            "fecha_cierre_definitivo": app.get("fecha_cierre_definitivo")
        }

    pruebas = copy.deepcopy(app.get("pruebas") or {})
    mov_snaps = {}
    apel_snaps = {}
    if app_ref.client:
        for t_id, p in pruebas.items():
            if p.get("estado") != "expulsada":
                id_mov = id_movimiento_recompensa(app_id, t_id)
                m_ref = app_ref.client.collection("recompensas").document(id_mov)
                mov_snaps[t_id] = m_ref.get(transaction=transaction)

            posibles = p.get("posibles_faltas") or {}
            for f in posibles.values():
                id_evt = f.get("id_evento_resolucion")
                if id_evt:
                    ap_ref = app_ref.client.collection("apelaciones_revisiones").document(f"{app_id}_{t_id}_{id_evt}")
                    apel_snaps[f"{t_id}_{id_evt}"] = ap_ref.get(transaction=transaction)

    # 2. Validaciones de conciliación
    recompensas_pendientes = []
    faltas_pendientes = []
    apelaciones_pendientes = []
    legacy_pendientes = []

    for t_id, p in pruebas.items():
        if p.get("estado") == "expulsada":
            continue

        posibles = p.get("posibles_faltas") or {}
        for f_v, f in posibles.items():
            if f.get("estado") == "pendiente_revision" or f.get("requiere_nueva_revision") is True:
                faltas_pendientes.append(f"{t_id}:{f_v}")

        for f in posibles.values():
            id_evt = f.get("id_evento_resolucion")
            if id_evt:
                ap_snap = apel_snaps.get(f"{t_id}_{id_evt}")
                if ap_snap and ap_snap.exists and ap_snap.to_dict().get("estado_apelacion") == "pendiente":
                    apelaciones_pendientes.append(f"{t_id}:{id_evt}")

        if bool(p.get("requiere_conciliacion_legacy")) or bool(p.get("incertidumbre_progreso_legacy")):
            legacy_pendientes.append(t_id)

        dias_val = calcular_dias_cumplidos_validos(p)
        if dias_val >= 14:
            m_snap = mov_snaps.get(t_id)
            ya_pagada = (m_snap and m_snap.exists) or (p.get("recompensa_liquidada") is True)
            if not ya_pagada:
                recompensas_pendientes.append(t_id)

    if apelaciones_pendientes:
        return {"exito": False, "motivo": "apelaciones_pendientes", "detalles": apelaciones_pendientes}

    if faltas_pendientes:
        return {"exito": False, "motivo": "faltas_pendientes", "detalles": faltas_pendientes}

    if legacy_pendientes:
        return {"exito": False, "motivo": "casos_requieren_conciliacion", "detalles": legacy_pendientes}

    if recompensas_pendientes:
        return {"exito": False, "motivo": "recompensas_pendientes", "detalles": recompensas_pendientes}

    # 3. Aplicar cierre
    dt_now = parse_dt_mexico(now_dt)
    fecha_cierre_iso = dt_now.isoformat()

    motivo_str = motivo.strip() if motivo and str(motivo).strip() else "cierre_regular_conciliado"

    transaction.update(app_ref, {
        "estado": "cerrada",
        "estado_conciliacion": "cerrada_definitivamente",
        "fecha_cierre_definitivo": fecha_cierre_iso,
        "actor_cierre": actor_id_str,
        "motivo_cierre": motivo_str
    })

    id_audit_close = str(audit_close_ref.id)
    transaction.create(audit_close_ref, {
        "id_evento_cierre": id_audit_close,
        "app_id": app_id_str,
        "actor_id": actor_id_str,
        "rol_actor": "admin" if is_admin else "dev",
        "fecha_cierre": fecha_cierre_iso,
        "motivo": motivo_str,
        "fecha_registro": server_timestamp
    })

    resultado = {
        "exito": True,
        "resultado": "cerrada_definitivamente",
        "app_id": app_id_str,
        "fecha_cierre_definitivo": fecha_cierre_iso,
        "actor_cierre": actor_id_str,
        "id_evento_cierre": id_audit_close
    }

    if sol_ref:
        transaction.create(sol_ref, {
            "id_solicitud": str(sol_ref.id),
            "app_id": app_id_str,
            "actor_id": actor_id_str,
            "resultado": resultado,
            "fecha_registro": server_timestamp
        })

    return resultado


def cerrar_campana_definitiva(app_id, actor_id, motivo=None, now_dt=None, id_solicitud=None):
    """
    Cierra definitivamente una campaña tras validar que no existan recompensas pendientes,
    faltas sin resolver ni apelaciones abiertas.
    """
    if not _firebase_configurado():
        return {"exito": False, "motivo": "modo_local_no_disponible"}
    cliente = _inicializar_firebase()
    if cliente is None or not _firebase_initialized:
        return {"exito": False, "motivo": "firestore_no_disponible"}

    from firebase_admin import firestore
    app_ref = cliente.collection("apps").document(str(app_id))
    id_audit = f"cierre_{app_id}_{parse_dt_mexico(now_dt).strftime('%Y%m%d%H%M%S')}"
    audit_close_ref = cliente.collection("auditoria_cierres").document(id_audit)
    sol_ref = None
    if id_solicitud:
        sol_ref = cliente.collection("solicitudes_cierres").document(f"{app_id}_{id_solicitud}")

    return firestore.transactional(_cerrar_campana_transaccional)(
        cliente.transaction(),
        app_ref,
        audit_close_ref,
        sol_ref,
        app_id,
        actor_id,
        motivo,
        firestore.SERVER_TIMESTAMP,
        now_dt,
        id_solicitud
    )


# --- REPUTACIÓN PERMANENTE Y PROGRAMA FOUNDER (ETAPA 6A) ---

def obtener_reputacion_tester(user_id, cliente=None):
    """
    Recupera los datos consolidados de reputación permanente y puntos founder de un usuario.
    """
    if cliente is None and _firebase_configurado():
        cliente = _inicializar_firebase()

    user_data = None
    if cliente and _firebase_initialized:
        doc = cliente.collection("usuarios").document(str(user_id)).get()
        if doc.exists:
            user_data = doc.to_dict()
    else:
        usuarios = _cargar_json_local(USUARIOS_FILE)
        user_data = usuarios.get(str(user_id))

    if not user_data:
        return {
            "encontrado": False,
            "puntos_founder": 0,
            "nivel_confiabilidad": "Nuevo",
            "confiabilidad_pct": 100.0,
            "campanas_completadas": 0,
            "dias_totales_cumplidos": 0,
            "faltas_injustificadas": 0
        }

    rep = user_data.get("reputacion") or {}
    return {
        "encontrado": True,
        "usuario": str(user_id),
        "nombre": user_data.get("nombre", ""),
        "puntos_founder": user_data.get("puntos_founder", 0),
        "nivel_confiabilidad": rep.get("nivel_confiabilidad", "Inicial"),
        "confiabilidad_pct": rep.get("confiabilidad_pct", 100.0),
        "campanas_completadas": rep.get("campanas_completadas", 0),
        "dias_totales_cumplidos": rep.get("dias_totales_cumplidos", 0),
        "faltas_injustificadas": rep.get("faltas_injustificadas", 0),
        "ultima_actualizacion": rep.get("ultima_actualizacion")
    }


def formatear_reputacion(usuario):
    """
    Genera el formato legible de reputación permanente para visualización en el bot.
    """
    rep = (usuario or {}).get("reputacion") or {}
    nivel = rep.get("nivel_confiabilidad", "Inicial")
    conf_pct = rep.get("confiabilidad_pct", 100.0)
    completadas = rep.get("campanas_completadas", 0)
    dias = rep.get("dias_totales_cumplidos", 0)
    faltas = rep.get("faltas_injustificadas", 0)

    estrellas = "⭐⭐⭐⭐⭐" if nivel == "Excelente" else (
        "⭐⭐⭐⭐" if nivel == "Alta" else (
            "⭐⭐⭐" if nivel == "Media" else "⭐"
        )
    ) if completadas > 0 else "🌱 (Nuevo)"

    return (
        f"{estrellas} **{nivel}** (`{conf_pct}%` confiabilidad)\n"
        f"🏆 Campañas completadas: `{completadas}` | 📅 Días cumplidos: `{dias}` | ⚠️ Faltas: `{faltas}`"
    )


def _actualizar_creditos_admin_transaccional(transaction, user_ref, delta_creditos):
    user_snap = user_ref.get(transaction=transaction)
    if not user_snap.exists:
        return None
    data = user_snap.to_dict() or {}
    nuevo_saldo = max(0, int(data.get("creditos", 0)) + delta_creditos)
    transaction.update(user_ref, {"creditos": nuevo_saldo})
    data["creditos"] = nuevo_saldo
    return data


def actualizar_creditos_admin_seguro(target_user_id, delta_creditos):
    """
    Actualiza de forma atómica y segura los créditos de un único usuario.
    Evita sobrescribir colecciones completas para mitigar riesgos de concurrencia.
    """
    target_id_str = str(target_user_id)
    cliente = _inicializar_firebase() if _firebase_configurado() else None
    if cliente and _firebase_initialized:
        from firebase_admin import firestore
        user_ref = cliente.collection("usuarios").document(target_id_str)
        return firestore.transactional(_actualizar_creditos_admin_transaccional)(
            cliente.transaction(), user_ref, delta_creditos
        )
    else:
        usuarios = _cargar_json_local(USUARIOS_FILE)
        if target_id_str not in usuarios:
            return None
        usuarios[target_id_str]["creditos"] = max(0, int(usuarios[target_id_str].get("creditos", 0)) + delta_creditos)
        _guardar_json_local(USUARIOS_FILE, usuarios)
        return usuarios[target_id_str]


def _sancionar_usuario_admin_transaccional(transaction, user_ref, tipo_sancion, cantidad):
    user_snap = user_ref.get(transaction=transaction)
    if not user_snap.exists:
        return None
    data = user_snap.to_dict() or {}
    if tipo_sancion == "creditos":
        data["creditos"] = max(0, int(data.get("creditos", 0)) - cantidad)
        transaction.update(user_ref, {"creditos": data["creditos"]})
    elif tipo_sancion == "boletos":
        data["boletos_rifa"] = max(0, int(data.get("boletos_rifa", 0)) - cantidad)
        transaction.update(user_ref, {"boletos_rifa": data["boletos_rifa"]})
    elif tipo_sancion == "resetear":
        data["creditos"] = 0
        data["boletos_rifa"] = 0
        transaction.update(user_ref, {"creditos": 0, "boletos_rifa": 0})
    return data


def sancionar_usuario_admin_seguro(target_user_id, tipo_sancion, cantidad=0):
    """
    Aplica sanciones numéricas de forma atómica sobre un único documento de usuario.
    """
    target_id_str = str(target_user_id)
    cliente = _inicializar_firebase() if _firebase_configurado() else None
    if cliente and _firebase_initialized:
        from firebase_admin import firestore
        user_ref = cliente.collection("usuarios").document(target_id_str)
        return firestore.transactional(_sancionar_usuario_admin_transaccional)(
            cliente.transaction(), user_ref, tipo_sancion, cantidad
        )
    else:
        usuarios = _cargar_json_local(USUARIOS_FILE)
        if target_id_str not in usuarios:
            return None
        user = usuarios[target_id_str]
        if tipo_sancion == "creditos":
            user["creditos"] = max(0, int(user.get("creditos", 0)) - cantidad)
        elif tipo_sancion == "boletos":
            user["boletos_rifa"] = max(0, int(user.get("boletos_rifa", 0)) - cantidad)
        elif tipo_sancion == "resetear":
            user["creditos"] = 0
            user["boletos_rifa"] = 0
        _guardar_json_local(USUARIOS_FILE, usuarios)
        return user


