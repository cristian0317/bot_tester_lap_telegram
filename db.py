import os
import json
import logging

logger = logging.getLogger(__name__)

# Rutas de fallback local
USUARIOS_FILE = "usuarios.json"
APPS_FILE = "apps.json"

_firebase_initialized = False
_db = None

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
            return usuarios
        except Exception as e:
            logger.error(f"Error al leer usuarios de Firebase: {e}")
            return _cargar_json_local(USUARIOS_FILE)
    return _cargar_json_local(USUARIOS_FILE)

def guardar_usuarios(datos):
    """Guarda/actualiza todos los usuarios en Firebase y en el backup local."""
    _guardar_json_local(USUARIOS_FILE, datos)
    db = _inicializar_firebase()
    if _firebase_initialized and db:
        try:
            batch = db.batch()
            for user_id, user_data in datos.items():
                doc_ref = db.collection("usuarios").document(str(user_id))
                batch.set(doc_ref, user_data, merge=True)
            batch.commit()
        except Exception as e:
            logger.error(f"Error al guardar usuarios en Firebase: {e}")

def guardar_usuario(user_id, user_data):
    """Guarda/actualiza un usuario individual en Firebase y localmente."""
    usuarios = _cargar_json_local(USUARIOS_FILE)
    usuarios[str(user_id)] = user_data
    _guardar_json_local(USUARIOS_FILE, usuarios)

    db = _inicializar_firebase()
    if _firebase_initialized and db:
        try:
            db.collection("usuarios").document(str(user_id)).set(user_data, merge=True)
        except Exception as e:
            logger.error(f"Error al guardar usuario {user_id} en Firebase: {e}")

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
            return apps
        except Exception as e:
            logger.error(f"Error al leer apps de Firebase: {e}")
            return _cargar_json_local(APPS_FILE)
    return _cargar_json_local(APPS_FILE)

def guardar_apps(datos):
    """Guarda/actualiza todas las apps en Firebase y en el backup local."""
    _guardar_json_local(APPS_FILE, datos)
    db = _inicializar_firebase()
    if _firebase_initialized and db:
        try:
            batch = db.batch()
            for app_id, app_data in datos.items():
                doc_ref = db.collection("apps").document(str(app_id))
                batch.set(doc_ref, app_data, merge=True)
            batch.commit()
        except Exception as e:
            logger.error(f"Error al guardar apps en Firebase: {e}")

def guardar_app(app_id, app_data):
    """Guarda/actualiza una app individual en Firebase y localmente."""
    apps = _cargar_json_local(APPS_FILE)
    apps[str(app_id)] = app_data
    _guardar_json_local(APPS_FILE, apps)

    db = _inicializar_firebase()
    if _firebase_initialized and db:
        try:
            db.collection("apps").document(str(app_id)).set(app_data, merge=True)
        except Exception as e:
            logger.error(f"Error al guardar app {app_id} en Firebase: {e}")
