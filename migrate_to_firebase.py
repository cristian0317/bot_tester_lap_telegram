import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

from db import is_firebase_active, _inicializar_firebase, USUARIOS_FILE, APPS_FILE

def migrar():
    logger.info("🚀 Iniciando proceso de migración de JSON a Firebase Firestore...")
    
    if not is_firebase_active():
        logger.error("❌ Firebase no está configurado. Por favor, especifica FIREBASE_CREDENTIALS o coloca firebase_key.json.")
        return

    db = _inicializar_firebase()

    # 1. Migrar Usuarios
    if os.path.exists(USUARIOS_FILE):
        try:
            with open(USUARIOS_FILE, "r", encoding="utf-8") as f:
                usuarios = json.load(f)
            
            logger.info(f"📦 Migrando {len(usuarios)} usuarios a la colección 'usuarios'...")
            batch = db.batch()
            count = 0
            for user_id, user_data in usuarios.items():
                doc_ref = db.collection("usuarios").document(str(user_id))
                batch.set(doc_ref, user_data, merge=True)
                count += 1
                if count % 400 == 0:
                    batch.commit()
                    batch = db.batch()
            
            batch.commit()
            logger.info("✅ Usuarios migrados con éxito a Firebase.")
        except Exception as e:
            logger.error(f"❌ Error al migrar usuarios: {e}")
    else:
        logger.info("ℹ️ No se encontró el archivo usuarios.json local.")

    # 2. Migrar Apps
    if os.path.exists(APPS_FILE):
        try:
            with open(APPS_FILE, "r", encoding="utf-8") as f:
                apps = json.load(f)
            
            logger.info(f"📱 Migrando {len(apps)} apps a la colección 'apps'...")
            batch = db.batch()
            count = 0
            for app_id, app_data in apps.items():
                doc_ref = db.collection("apps").document(str(app_id))
                batch.set(doc_ref, app_data, merge=True)
                count += 1
                if count % 400 == 0:
                    batch.commit()
                    batch = db.batch()
            
            batch.commit()
            logger.info("✅ Aplicaciones migradas con éxito a Firebase.")
        except Exception as e:
            logger.error(f"❌ Error al migrar aplicaciones: {e}")
    else:
        logger.info("ℹ️ No se encontró el archivo apps.json local.")

    logger.info("🎉 ¡Migración completada exitosamente!")

if __name__ == "__main__":
    migrar()
