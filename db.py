import os
import json
import logging
import copy
import hashlib

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
                          user_id, app_id, captura, fecha, server_timestamp):
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
    prueba = app.get("pruebas", {}).get(user_id)
    if movimiento.exists:
        return {"resultado": "ya_pagada"}
    if prueba and prueba.get("estado") == "completada":
        raise ErrorRecompensa("Prueba antigua completada sin movimiento: requiere conciliación. No se acreditó otra recompensa.")
    if not prueba or prueba.get("estado") != "instalada":
        raise ErrorRecompensa("La prueba no es elegible: falta una instalación activa.")
    if prueba.get("ultimo_checkin") == fecha:
        return {"resultado": "ya_registrado"}
    dias = prueba.get("dias_completados", 1) + 1
    prueba.update(dias_completados=dias, ultimo_checkin=fecha, ultima_captura=captura)
    completa = dias >= 14
    if completa:
        prueba["estado"] = "completada"
    transaction.update(app_ref, {"pruebas": app["pruebas"]})
    if completa:
        transaction.update(user_ref, {
            "creditos": usuario.get("creditos", 0) + 800,
            "boletos_rifa": usuario.get("boletos_rifa", 0) + 1,
        })
        transaction.create(movimiento_ref, {
            "usuario": user_id, "campana": app_id, "importe": 800,
            "boletos": 1, "fecha": server_timestamp,
        })
    return {"resultado": "completada" if completa else "checkin", "dias": dias,
            "descripcion": app.get("descripcion"), "dev_id": app.get("dev_id")}


def registrar_checkin(user_id, app_id, captura, fecha):
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
        )
    except ErrorRecompensa:
        raise
    except Exception as exc:
        raise ErrorRecompensa("No se pudo confirmar el guardado. Reintentá: un movimiento ya guardado no se paga nuevamente.") from exc

class InscripcionRechazada(Exception):
    """La inscripción no cumple las reglas; no se debe escribir nada."""


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
    estado = app.get("estado")
    if estado == "lista" and len(testers) >= 12:
        raise InscripcionRechazada("❌ La campaña está llena: ya tiene 12 testers activos.")
    if estado != "reclutando":
        raise InscripcionRechazada("❌ La campaña no está disponible para postulaciones. Solo se aceptan mientras está reclutando.")
    if len(testers) >= 12:
        raise InscripcionRechazada("❌ La campaña está llena: ya tiene 12 testers activos.")
    testers.append(user_id)
    membresias.append(app_id)
    completa = len(testers) == 12
    cambios_app = {"testers": testers, "min_testers": 12, "max_testers": 12}
    if completa:
        cambios_app["estado"] = "lista"
    return cambios_app, {"apps_testeando": membresias}, {
        "total": len(testers), "completa": completa, "dev_id": app.get("dev_id"),
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
