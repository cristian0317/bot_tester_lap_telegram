from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
import json
import os
import random
import logging
import uuid
from datetime import datetime
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

# Configuración de Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Token del Bot de Telegram (Obtenido de variable de entorno)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TOKEN", ""))

# Filtro de seguridad para ocultar el Token de Telegram y bloquear peticiones HTTP en logs
class TokenMaskingFilter(logging.Filter):
    def filter(self, record):
        msg = str(record.msg)
        if "api.telegram.org/bot" in msg or (TOKEN and TOKEN in msg):
            if "HTTP Request:" in msg or "httpx" in record.name or "httpcore" in record.name:
                return False
            if TOKEN and TOKEN in msg:
                record.msg = msg.replace(TOKEN, "[TOKEN_OCULTO]")
        return True

# Aplicar filtro de seguridad global a todos los loggers y handlers
token_filter = TokenMaskingFilter()
logging.getLogger().addFilter(token_filter)
for handler in logging.getLogger().handlers:
    handler.addFilter(token_filter)

for log_name in ("httpx", "httpcore", "telegram", "telegram.ext", "urllib3"):
    l = logging.getLogger(log_name)
    l.setLevel(logging.WARNING)
    l.addFilter(token_filter)

logger = logging.getLogger(__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra en bot.log cualquier excepción no controlada que ocurra en el bot."""
    logger.error("Excepción ocurrida al procesar la actualización:", exc_info=context.error)
    if isinstance(context.error, db.ConflictoGuardado) and update and update.effective_message:
        await update.effective_message.reply_text(str(context.error))

# Estados de las conversaciones
REGISTRO_TIPO = 1
SUBIR_LINK = 3
SUBIR_DESCRIPCION = 4
CONFIRMAR_FOTO = 10
CHECKIN_FOTO = 11

# Rutas de los archivos JSON de almacenamiento
USUARIOS_FILE = "usuarios.json"
APPS_FILE = "apps.json"
TESTERS_POR_CAMPANA = 15

# IDs de administradores con permisos (Cargados desde variable de entorno ADMIN_IDS)
_admin_ids_env = os.getenv("ADMIN_IDS", "7967828114")
ADMIN_IDS = [x.strip() for x in _admin_ids_env.split(",") if x.strip()]

# --- TECLADOS PERMANENTES (BOTONES AMIGABLES SIN TECLEAR) ---
def obtener_teclado_principal(user_id=None):
    """Devuelve el menú de botones principales para el teclado de Telegram."""
    botones = [
        ["🚀 Subir App", "📱 Ver Apps"],
        ["📸 Confirmar Instalación", "📝 Check-in Diario"],
        ["📊 Mis Créditos", "👥 Referidos", "🎟️ Rifa Mensual"],
        ["📢 Mis Campañas", "📲 Mis Apps en Prueba"]
    ]
    if user_id and str(user_id) in ADMIN_IDS:
        botones.append(["🛠️ Panel Admin"])
    
    return ReplyKeyboardMarkup(botones, resize_keyboard=True)

# --- FUNCIONES AUXILIARES DE ALMACENAMIENTO (FIREBASE / JSON) ---
def cargar_archivo(archivo):
    """Carga un archivo o colección. Si está configurado Firebase, lee de Firestore; de lo contrario, del JSON local."""
    if archivo == USUARIOS_FILE:
        return db.cargar_usuarios()
    elif archivo == APPS_FILE:
        return db.cargar_apps()
    else:
        return db._cargar_json_local(archivo)

def guardar_archivo(archivo, datos):
    """Guarda datos en Firebase (Firestore) y/o en el JSON local de respaldo."""
    if archivo == USUARIOS_FILE:
        db.guardar_usuarios(datos)
    elif archivo == APPS_FILE:
        db.guardar_apps(datos)
    else:
        db._guardar_json_local(archivo, datos)

# --- INICIO Y REGISTRO DE USUARIO CON DEEP-LINK DE REFERIDOS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start: Da la bienvenida y muestra los botones de selección de rol."""
    if context.args and context.args[0].startswith("ref_"):
        referrer_id = context.args[0].replace("ref_", "")
        context.user_data["referrer_id"] = referrer_id

    keyboard = [
        ["👨‍💻 Soy Desarrollador"],
        ["📱 Soy Tester"],
        ["👤 Ambos"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 ¡Bienvenido a **TestCoin**!\n\n"
        "La plataforma que conecta desarrolladores con testers reales para pruebas de Google Play Store.\n\n"
        "🎁 ¡Al registrarte recibirás **200 créditos de bienvenida**!\n\n"
        "Seleccioná una opción con los botones de abajo:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return REGISTRO_TIPO

async def seleccion_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa la selección de rol y activa el menú principal de botones permanentes."""
    texto = update.message.text
    nombre = update.message.from_user.first_name
    user_id = str(update.message.from_user.id)

    usuarios = cargar_archivo(USUARIOS_FILE)

    if "Desarrollador" in texto:
        rol = "desarrollador"
    elif "Tester" in texto:
        rol = "tester"
    elif "Ambos" in texto:
        rol = "ambos"
    else:
        return REGISTRO_TIPO

    referrer_id = context.user_data.get("referrer_id")

    if user_id not in usuarios:
        boletos_iniciales = 0
        if referrer_id and referrer_id in usuarios and referrer_id != user_id:
            boletos_iniciales += 1
            usuarios[referrer_id]["boletos_rifa"] = usuarios[referrer_id].get("boletos_rifa", 0) + 1
            if "referidos" not in usuarios[referrer_id]:
                usuarios[referrer_id]["referidos"] = []
            usuarios[referrer_id]["referidos"].append(user_id)

            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        f"🎉 **¡Un nuevo amigo se registró con tu enlace!**\n\n"
                        f"👤 **Nuevo usuario:** {nombre}\n"
                        f"🎟️ **¡Ganaste 1 Boleto Extra para la Rifa Mensual!**\n"
                        f"Boletos actuales: `{usuarios[referrer_id]['boletos_rifa']}`"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        usuarios[user_id] = {
            "nombre": nombre,
            "rol": rol,
            "creditos": 200,
            "boletos_rifa": boletos_iniciales,
            "advertencias": 0,
            "referido_por": referrer_id if referrer_id in usuarios else None,
            "referidos": [],
            "apps_testeando": []
        }
    else:
        usuarios[user_id]["rol"] = rol

    guardar_archivo(USUARIOS_FILE, usuarios)

    respuesta = (
        f"🎉 ¡Registro completado como **{rol.capitalize()}**, {nombre}!\n\n"
        f"🪙 **Saldo actual:** {usuarios[user_id]['creditos']} créditos de bienvenida.\n"
        f"🎟️ **Boletos para la rifa mensual:** {usuarios[user_id].get('boletos_rifa', 0)}\n\n"
        "👇 **¡Usá los botones de abajo para navegar sin escribir comandos!**"
    )

    await update.message.reply_text(respuesta, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
    return ConversationHandler.END

# --- SUBIR APP (BOTÓN "🚀 Subir App" O COMANDO /subirapp) ---
async def subirapp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicio del flujo para publicar una app."""
    user_id = str(update.message.from_user.id)

    # 1. Verificar disponibilidad de Firestore ANTES de otorgar recargas admin o pedir datos
    if not db._firebase_configurado():
        await update.message.reply_text(
            "❌ La publicación de campañas no está disponible en modo local: los archivos JSON no permiten descontar créditos y publicar de forma atómica. Contactá al administrador.",
            reply_markup=obtener_teclado_principal(user_id)
        )
        return ConversationHandler.END

    # 2. Soporte para reiniciar explícitamente con /subirapp nueva
    if context.args and "nueva" in context.args:
        context.user_data.pop("pub_app_id", None)
        context.user_data.pop("app_link", None)
        context.user_data.pop("app_descripcion", None)
        context.user_data.pop("pub_estado_incierto", None)

    # 3. Recuperar solicitud pendiente ANTES de validación de saldo o recarga admin
    pub_app_id = context.user_data.get("pub_app_id")
    app_link = context.user_data.get("app_link")
    app_desc = context.user_data.get("app_descripcion")
    es_incierto = context.user_data.get("pub_estado_incierto", False)

    if pub_app_id and (es_incierto or (app_link and app_desc)):
        nombre = update.message.from_user.first_name
        try:
            resultado = db.publicar_campana(user_id, nombre, app_link, app_desc, pub_app_id)
        except db.PublicacionRechazada as exc:
            context.user_data.pop("pub_app_id", None)
            context.user_data.pop("app_link", None)
            context.user_data.pop("app_descripcion", None)
            context.user_data.pop("pub_estado_incierto", None)
            await update.message.reply_text(str(exc), reply_markup=obtener_teclado_principal(user_id))
            return ConversationHandler.END
        except db.ErrorPersistenciaPublicacion as exc:
            context.user_data["pub_estado_incierto"] = True
            await update.message.reply_text(
                f"{exc}\n\nℹ️ **Estado incierto:** Tu solicitud conserva el identificador `{pub_app_id}` y sus datos. Podés reintentar con `/subirapp` o iniciar otra con `/subirapp nueva` (mientras la sesión permanezca en memoria).",
                reply_markup=obtener_teclado_principal(user_id)
            )
            return ConversationHandler.END

        try:
            await update.message.reply_text(
                f"🎉 **¡CAMPAÑA REGISTRADA EN LISTA DE ESPERA!**\n\n"
                f"📱 **Link:** {app_link}\n"
                f"📝 **Descripción:** {app_desc}\n"
                f"🟡 **Estado:** `Reclutando Testers` (0/12 mínimos requeridos)\n"
                f"🪙 **Créditos descontados:** 800\n"
                f"🎟️ **¡Ganaste 1 boleto extra para la Rifa Mensual!**\n\n"
                f"📌 **¿Qué sigue?**\n"
                f"Tu app aparecerá en **📱 Ver Apps** como 🟡 *En lista de espera*. Cuando se unan **12 testers**, la app cambiará a 🟢 *Lista para Iniciar* y podrás dar el banderazo con `/iniciarprueba {pub_app_id}`.\n\n"
                f"🆔 **ID de tu campaña:** `{pub_app_id}`",
                reply_markup=obtener_teclado_principal(user_id)
            )
            context.user_data.pop("pub_app_id", None)
            context.user_data.pop("app_link", None)
            context.user_data.pop("app_descripcion", None)
            context.user_data.pop("pub_estado_incierto", None)
        except Exception as msg_exc:
            context.user_data["pub_estado_incierto"] = True
            logger.warning(f"Error al enviar confirmación por Telegram tras commit exitoso: {msg_exc}")
            raise msg_exc

        return ConversationHandler.END

    if pub_app_id and app_link and not app_desc:
        await update.message.reply_text(
            f"ℹ️ **Solicitud pendiente en curso** (`{pub_app_id}`).\n"
            f"🔗 **Link guardado:** {app_link}\n\n"
            f"Paso 2/2 — Escribí una descripción breve de tu app:",
            parse_mode="Markdown"
        )
        return SUBIR_DESCRIPCION

    # 4. Solicitud nueva: Validar registro y saldo/recarga admin
    usuarios = cargar_archivo(USUARIOS_FILE)

    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá `/start` para registrarte primero.", parse_mode="Markdown")
        return ConversationHandler.END

    usuario = usuarios[user_id]
    creditos = usuario.get("creditos", 0)

    # Si es admin, otorgar créditos de prueba automáticamente
    if creditos < 800:
        if user_id in ADMIN_IDS:
            usuario["creditos"] += 800
            guardar_archivo(USUARIOS_FILE, usuarios)
            await update.message.reply_text("🎁 **[Modo Admin]** Te hemos otorgado +800 créditos de prueba automáticamente.")
            creditos = usuario["creditos"]
        else:
            await update.message.reply_text(
                f"⚠️ **Créditos insuficientes**\n\n"
                f"Publicar una campaña requiere 🪙 **800 créditos** (Tu saldo actual: **{creditos} créditos**).\n\n"
                f"¿Cómo obtener créditos?\n\n"
                f"1️⃣ **Trabajar en la plataforma (Gratis):**\n"
                f"   Ganá créditos testeando aplicaciones tocando el botón **📱 Ver Apps**.\n\n"
                f"2️⃣ **Comprar créditos con dinero:**\n"
                f"   Recargá 800 créditos por **$800 MXN** usando `/comprarcreditos`.\n\n"
                f"💡 Una vez que tengas 800 créditos, tocá el botón **🚀 Subir App** de nuevo.",
                reply_markup=obtener_teclado_principal(user_id),
                parse_mode="Markdown"
            )
            return ConversationHandler.END

    import uuid
    context.user_data["pub_app_id"] = f"app_{user_id}_{uuid.uuid4().hex[:8]}"

    await update.message.reply_text(
        "📲 **Publicar nueva campaña en TestCoin**\n\n"
        "Paso 1/2 — Enviá el **link o URL** de tu aplicación en Google Play Store:\n"
        "(Ejemplo: `https://play.google.com/store/apps/details?id=com.tuapp`)",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return SUBIR_LINK

async def subirapp_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda el link de la app y solicita la descripción."""
    link_enviado = update.message.text.strip()

    if not (link_enviado.startswith("http://") or link_enviado.startswith("https://") or "play.google.com" in link_enviado):
        await update.message.reply_text(
            "⚠️ Por favor enviá un enlace válido de Google Play Store.\n"
            "Ejemplo: `https://play.google.com/store/apps/details?id=com.miempresa.miapp`",
            parse_mode="Markdown"
        )
        return SUBIR_LINK

    context.user_data["app_link"] = link_enviado
    await update.message.reply_text(
        "✅ **Link recibido y guardado con éxito.**\n\n"
        "Paso 2/2 — Escribí una descripción breve de tu app (qué hace, qué características deben probar los testers):",
        parse_mode="Markdown"
    )
    return SUBIR_DESCRIPCION

async def subirapp_descripcion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza el registro de la app en Lista de Espera, descuenta 800 créditos y restablece botones de forma atómica."""
    descripcion_enviada = update.message.text.strip()
    user_id = str(update.message.from_user.id)
    nombre = update.message.from_user.first_name

    app_id = context.user_data.get("pub_app_id")
    app_link = context.user_data.get("app_link")

    if not app_id:
        await update.message.reply_text(
            "❌ La sesión de publicación expiró o no se pudo recuperar. Usá el comando /subirapp para iniciar una nueva solicitud.",
            reply_markup=obtener_teclado_principal(user_id)
        )
        return ConversationHandler.END

    if not app_link:
        await update.message.reply_text(
            "❌ Falta el enlace de la aplicación. Usá /subirapp para iniciar la publicación.",
            reply_markup=obtener_teclado_principal(user_id)
        )
        return ConversationHandler.END

    context.user_data["app_descripcion"] = descripcion_enviada

    try:
        resultado = db.publicar_campana(user_id, nombre, app_link, descripcion_enviada, app_id)
    except db.PublicacionRechazada as exc:
        context.user_data.pop("pub_app_id", None)
        context.user_data.pop("app_link", None)
        context.user_data.pop("app_descripcion", None)
        context.user_data.pop("pub_estado_incierto", None)
        await update.message.reply_text(str(exc), reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END
    except db.ErrorPersistenciaPublicacion as exc:
        context.user_data["pub_estado_incierto"] = True
        await update.message.reply_text(
            f"{exc}\n\nℹ️ **Estado incierto:** Tu solicitud conserva el identificador `{app_id}` y sus datos. Podés reintentar con `/subirapp` o iniciar otra con `/subirapp nueva` (mientras la sesión permanezca en memoria).",
            reply_markup=obtener_teclado_principal(user_id)
        )
        return ConversationHandler.END

    try:
        await update.message.reply_text(
            f"🎉 **¡CAMPAÑA REGISTRADA EN LISTA DE ESPERA!**\n\n"
            f"📱 **Link:** {app_link}\n"
            f"📝 **Descripción:** {descripcion_enviada}\n"
            f"🟡 **Estado:** `Reclutando Testers` (0/12 mínimos requeridos)\n"
            f"🪙 **Créditos descontados:** 800\n"
            f"🎟️ **¡Ganaste 1 boleto extra para la Rifa Mensual!**\n\n"
            f"📌 **¿Qué sigue?**\n"
            f"Tu app aparecerá en **📱 Ver Apps** como 🟡 *En lista de espera*. Cuando se unan **12 testers**, la app cambiará a 🟢 *Lista para Iniciar* y podrás dar el banderazo con `/iniciarprueba {app_id}`.\n\n"
            f"🆔 **ID de tu campaña:** `{app_id}`",
            reply_markup=obtener_teclado_principal(user_id)
        )
        context.user_data.pop("pub_app_id", None)
        context.user_data.pop("app_link", None)
        context.user_data.pop("app_descripcion", None)
        context.user_data.pop("pub_estado_incierto", None)
    except Exception as msg_exc:
        context.user_data["pub_estado_incierto"] = True
        logger.warning(f"Error al enviar confirmación por Telegram tras commit exitoso: {msg_exc}")
        raise msg_exc

    return ConversationHandler.END

# --- VER APPS (BOTÓN "📱 Ver Apps") ---
async def verapps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra campañas en reclutamiento con plazas activas disponibles."""
    user_id = str(update.message.from_user.id)
    apps = cargar_archivo(APPS_FILE)

    apps_disponibles = []
    for app_id, datos in apps.items():
        if datos.get("estado") != "reclutando":
            continue
        max_t = datos.get("max_testers")
        if max_t is None:
            max_t = datos.get("min_testers") or 12
        if len(datos.get("testers", [])) < max_t:
            apps_disponibles.append((app_id, datos))

    if not apps_disponibles:
        await update.message.reply_text(
            "📱 **Catálogo de Apps**\n\n"
            "Por el momento no hay aplicaciones buscando testers.\n"
            "¡Volvé a consultar más tarde!",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return

    mensaje = "📱 **Apps disponibles para Testear**\n\n"
    mensaje += "Cada campaña reúne sus participantes. Al completarlos, el desarrollador podrá iniciar la prueba conjunta de 14 días:\n\n"

    for app_id, datos in apps_disponibles:
        num_testers = len(datos.get("testers", []))
        max_t = datos.get("max_testers")
        if max_t is None:
            max_t = datos.get("min_testers") or 12
        quedan = max_t - num_testers
        indicador = f"🟡 **Reclutando:** `{num_testers}/{max_t}` inscritos (*Quedan {quedan} plazas*)"

        mensaje += (
            f"🔹 **{datos.get('descripcion', 'Sin descripción')}**\n"
            f"🔗 **Link:** {datos.get('link')}\n"
            f"📊 **Estado:** {indicador}\n"
            f"🆔 **ID:** `{app_id}`\n"
            f"👉 Para unirte escribí: `/postular {app_id}`\n\n"
        )

    await update.message.reply_text(mensaje, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

# --- POSTULACIÓN DE TESTERS ---
async def postular(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inscribe a un usuario en las 12 plazas activas de una campaña."""
    user_id = str(update.message.from_user.id)
    teclado = obtener_teclado_principal(user_id)

    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "⚠️ Uso: /postular <app_id>\nPodés consultar el ID en 📱 Ver Apps.",
            reply_markup=teclado
        )
        return

    app_id = context.args[0]
    try:
        resultado = db.inscribir_tester(user_id, app_id)
    except db.InscripcionRechazada as exc:
        await update.message.reply_text(str(exc), reply_markup=teclado)
        return
    except db.ErrorPersistenciaInscripcion as exc:
        logger.warning("No se pudo confirmar el guardado de la inscripción.")
        await update.message.reply_text(str(exc), reply_markup=teclado)
        return

    completa = resultado["completa"]
    min_t = resultado.get("min_testers", 15)
    max_t = resultado.get("max_testers", min_t)
    mensaje = (
        f"✅ Postulación aceptada en la campaña {app_id}.\n"
        f"👥 Testers inscritos: {resultado['total']}/{max_t}.\n"
    )
    if completa:
        mensaje += f"La campaña ya reunió los {min_t} participantes necesarios y está lista para iniciar.\n"
    mensaje += "Esperá el aviso de inicio del desarrollador antes de confirmar la instalación."
    await update.message.reply_text(mensaje, reply_markup=teclado)

    if completa:
        try:
            await context.bot.send_message(
                chat_id=resultado["dev_id"],
                text=(
                    f"✅ La campaña {app_id} ya reunió los {min_t} testers necesarios.\n"
                    f"La prueba todavía no ha iniciado. Podés iniciarla con /iniciarprueba {app_id}."
                )
            )
        except Exception:
            logger.warning(f"No se pudo notificar al desarrollador que su campaña reunió los {min_t} testers.")

# --- INICIAR PRUEBA (DESARROLLADOR) ---
async def iniciarprueba(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite al desarrollador iniciar la prueba de 14 días cuando ya juntó los 15 testers requeridos."""
    user_id = str(update.message.from_user.id)

    if not context.args:
        await update.message.reply_text(
            "⚠️ Especificá el ID de la campaña a iniciar.\n"
            "Ejemplo: `/iniciarprueba app_12345_1`\n\n"
            "Podés ver tus campañas tocando **📢 Mis Campañas**.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return

    app_id = context.args[0]
    apps = cargar_archivo(APPS_FILE)

    if app_id not in apps:
        await update.message.reply_text("❌ La campaña especificada no existe.", reply_markup=obtener_teclado_principal(user_id))
        return

    app = apps[app_id]

    if app.get("dev_id") != user_id:
        await update.message.reply_text("❌ Solo el desarrollador creador de esta campaña puede iniciar la prueba.", reply_markup=obtener_teclado_principal(user_id))
        return

    num_testers = len(app.get("testers", []))
    min_testers = app.get("min_testers", 12)

    if num_testers < min_testers:
        await update.message.reply_text(
            f"🟡 **Aún no juntás los {min_testers} testers mínimos**\n\n"
            f"Actualmente tenés `{num_testers}/{min_testers}` testers inscritos.\n"
            f"Esperá a que la lista de espera se llene para iniciar de forma pareja.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return

    if app.get("estado") == "en_prueba":
        await update.message.reply_text("🚀 Esta prueba ya se encuentra activa.", reply_markup=obtener_teclado_principal(user_id))
        return

    app["estado"] = "en_prueba"
    now_tz = db.parse_dt_mexico(None)
    fecha_hoy = now_tz.strftime("%Y-%m-%d")
    app["fecha_lanzamiento"] = fecha_hoy
    app["timestamp_lanzamiento"] = now_tz.isoformat()
    fecha_v1 = db.calcular_vencimiento_primera_entrega(app["timestamp_lanzamiento"], fecha_hoy)
    guardar_archivo(APPS_FILE, apps)

    await update.message.reply_text(
        f"🚀 **¡PRUEBA INICIADA EXITOSAMENTE!**\n\n"
        f"📱 **App:** {app.get('descripcion')}\n"
        f"👥 **Testers participantes:** `{num_testers}`\n"
        f"📅 **Fecha de lanzamiento:** `{fecha_hoy}`\n"
        f"⏰ **Vencimiento 1ª entrega (instalación):** `{fecha_v1}` a las **17:00:00** (America/Mexico_City)\n\n"
        f"📢 Se les notificó automáticamente a todos los testers para que descarguen la app e instalen de forma pareja.",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

    for tester_id in app.get("testers", []):
        try:
            await context.bot.send_message(
                chat_id=tester_id,
                text=(
                    f"🚀 **¡LA PRUEBA DE LA APP HA INICIADO HOY!** 🟢\n\n"
                    f"El desarrollador dio el banderazo de salida para la app **{app.get('descripcion')}**.\n\n"
                    f"📌 **Pasos a seguir hoy:**\n"
                    f"1. Descargá e instalá la app desde Google Play Store: {app.get('link')}\n"
                    f"2. Sacale una captura de pantalla donde se vea instalada.\n"
                    f"3. Enviá tu captura tocando el botón **📸 Confirmar Instalación**.\n\n"
                    f"⏰ **HORARIO DE CORTE:** La 1ª entrega (instalación) vence el **{fecha_v1} a las 5:00 PM** (America/Mexico_City)."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

# --- CONFIRMACIÓN DE INSTALACIÓN ---
def validar_confirmacion(user_id, app_id, usuarios, apps):
    """Devuelve el motivo de rechazo sin modificar los datos consultados."""
    if user_id not in usuarios:
        return "❌ No estás registrado. Usá /start para registrarte primero."
    if app_id not in apps:
        return "❌ La campaña indicada no existe."
    app = apps[app_id]
    if app.get("estado") != "en_prueba":
        return "❌ La campaña no está en prueba. Esperá a que el desarrollador la inicie."
    en_testers = user_id in {str(tester_id) for tester_id in app.get("testers", [])}
    en_usuario = app_id in usuarios[user_id].get("apps_testeando", [])
    if en_testers != en_usuario:
        return "❌ Tu inscripción es inconsistente entre la campaña y tu cuenta. Contactá al administrador; no se modificaron los datos."
    if not en_testers:
        return "❌ No estás inscrito en esta campaña."
    pruebas = app.get("pruebas", {})
    if user_id in pruebas:
        if pruebas[user_id].get("estado") == "completada":
            return "✅ Ya completaste esta prueba. Tu progreso se conserva y no se puede reiniciar la instalación."
        return "ℹ️ Ya existe una prueba en curso para esta campaña. Tu progreso se conserva; usá /checkin para continuar."
    return None


async def confirmar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el proceso para recibir la captura de pantalla de instalación."""
    user_id = str(update.message.from_user.id)
    context.user_data.pop("confirmar_app_id", None)
    context.user_data.pop("confirmar_vencimiento", None)
    usuarios = cargar_archivo(USUARIOS_FILE)

    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start para registrarte primero.")
        return ConversationHandler.END

    usuario = usuarios[user_id]
    apps_testeando = usuario.get("apps_testeando", [])

    if not context.args and not apps_testeando:
        await update.message.reply_text(
            "❌ No estás postulado en ninguna app. Postulate primero en **📱 Ver Apps**.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    app_id = context.args[0] if context.args else apps_testeando[-1]
    apps = cargar_archivo(APPS_FILE)
    rechazo = validar_confirmacion(user_id, app_id, usuarios, apps)
    if rechazo:
        await update.message.reply_text(rechazo, reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END
    context.user_data["confirmar_app_id"] = app_id
    app_info = apps[app_id]
    fn_v1 = getattr(db, "calcular_vencimiento_primera_entrega", None)
    if callable(fn_v1):
        fecha_v1 = fn_v1(app_info.get("timestamp_lanzamiento"), app_info.get("fecha_lanzamiento") or datetime.now().strftime("%Y-%m-%d"))
    else:
        fecha_v1 = app_info.get("fecha_lanzamiento") or datetime.now().strftime("%Y-%m-%d")
    context.user_data["confirmar_vencimiento"] = fecha_v1

    await update.message.reply_text(
        f"📸 **Confirmar Instalación de la App**\n\n"
        f"📱 **App:** {app_info.get('descripcion', app_id)}\n"
        f"📅 **Vencimiento 1ª entrega:** `{fecha_v1}` a las **5:00 PM** (America/Mexico_City)\n\n"
        f"Por favor, enviá una **captura de pantalla** donde se vea la aplicación instalada en tu teléfono.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return CONFIRMAR_FOTO

async def confirmar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la captura de pantalla de instalación."""
    user_id = str(update.message.from_user.id)
    nombre = update.message.from_user.first_name
    app_id = context.user_data.pop("confirmar_app_id", None)
    fecha_venc = context.user_data.pop("confirmar_vencimiento", None)

    if not app_id:
        await update.message.reply_text("❌ Ocurrió un error.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    foto_file_id = update.message.photo[-1].file_id
    fn_msg_id = getattr(db, "_obtener_id_mensaje", None)
    id_mensaje = fn_msg_id(update, user_id) if callable(fn_msg_id) else f"msg_{user_id}_default"
    telegram_dt = getattr(update.message, "date", None) if update and hasattr(update, "message") else None
    fecha_hoy = db.parse_dt_mexico(telegram_dt).strftime("%Y-%m-%d") if hasattr(db, "parse_dt_mexico") else datetime.now().strftime("%Y-%m-%d")

    try:
        resultado = db.confirmar_instalacion(user_id, app_id, foto_file_id, fecha_hoy, nombre,
                                              id_mensaje=id_mensaje, telegram_timestamp=telegram_dt, fecha_vencimiento=fecha_venc)
    except (db.ConfirmacionRechazada, db.ErrorPersistenciaConfirmacion) as exc:
        await update.message.reply_text(str(exc), reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    res_tipo = resultado.get("resultado")
    if res_tipo in ("ya_completada", "ya_instalada"):
        await update.message.reply_text(
            resultado.get("mensaje", "ℹ️ Tu progreso ya se encuentra registrado."),
            reply_markup=obtener_teclado_principal(user_id)
        )
        return ConversationHandler.END

    app_desc = resultado.get("descripcion", app_id)
    est_ent = resultado.get("estado_entrega", "puntual")
    est_txt = "✅ Puntual" if est_ent == "puntual" else "⚠️ Tardía"

    await update.message.reply_text(
        f"✅ **¡Instalación confirmada exitosamente!**\n\n"
        f"📱 **App:** {app_desc}\n"
        f"📅 **Vencimiento satisfecho:** `{resultado.get('fecha_vencimiento')}`\n"
        f"⏱️ **Evaluación de entrega:** `{est_txt}`\n"
        f"⏳ **Días activos requeridos:** 14 días\n"
        f"📊 **Días completados (puntuales):** `{1 if est_ent == 'puntual' else 0}/14`\n\n"
        f"💡 Usá el botón **📝 Check-in Diario** todos los días antes de las 5:00 PM.",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

    dev_id = resultado.get("dev_id")
    if dev_id:
        try:
            await context.bot.send_message(
                chat_id=dev_id,
                text=(
                    f"🔔 **¡Nuevo tester activado!**\n\n"
                    f"👤 Tester **{nombre}** ha confirmed la instalación de tu app:\n"
                    f"📝 **App:** {app_desc}\n"
                    f"📅 **Fecha de inicio:** `{fecha_hoy}`"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    return ConversationHandler.END

# --- CHECK-IN DIARIO ---
async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia la recolección de captura diaria permitiendo elegir entrega tardía u abierta si existen ambas."""
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)

    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start para registrarte primero.")
        return ConversationHandler.END

    usuario = usuarios[user_id]
    apps_testeando = usuario.get("apps_testeando", [])

    if not apps_testeando:
        await update.message.reply_text(
            "❌ No estás probando ninguna aplicación. Tocá el botón **📱 Ver Apps** para unirte.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    app_id = context.args[0] if context.args else apps_testeando[-1]
    apps = cargar_archivo(APPS_FILE)
    if app_id not in apps:
        await update.message.reply_text("❌ La app no fue encontrada.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    app = apps[app_id]
    prueba = app.get("pruebas", {}).get(user_id, {})

    if prueba.get("estado") == "completada":
        context.user_data.pop("checkin_app_id", None)
        await update.message.reply_text("✅ Ya completaste esta prueba. No necesitás realizar más check-ins; tu progreso se conserva.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    if not prueba or prueba.get("estado") != "instalada":
        await update.message.reply_text("⚠️ Primero debes enviar tu captura inicial tocando el botón **📸 Confirmar Instalación**.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
        return ConversationHandler.END

    fn_parse = getattr(db, "parse_dt_mexico", None)
    now_dt = fn_parse(None) if callable(fn_parse) else datetime.now()
    from datetime import datetime as _real_datetime, timedelta
    if hasattr(now_dt, "hour") and now_dt.hour >= 17:
        fecha_actual = (now_dt.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    elif hasattr(now_dt, "date"):
        fecha_actual = now_dt.date().strftime("%Y-%m-%d")
    else:
        fecha_actual = now_dt.strftime("%Y-%m-%d")

    fecha_anterior = (_real_datetime.strptime(fecha_actual, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")

    registros = prueba.get("registros", {})
    fn_v1 = getattr(db, "calcular_vencimiento_primera_entrega", None)
    if callable(fn_v1):
        fecha_v1 = fn_v1(app.get("timestamp_lanzamiento"), app.get("fecha_lanzamiento") or now_dt.strftime("%Y-%m-%d"))
    else:
        fecha_v1 = app.get("fecha_lanzamiento") or now_dt.strftime("%Y-%m-%d")

    d_v1 = _real_datetime.strptime(fecha_v1, "%Y-%m-%d").date()
    d_ant = _real_datetime.strptime(fecha_anterior, "%Y-%m-%d").date()
    tiene_anterior = fecha_anterior in registros
    tiene_actual = fecha_actual in registros

    context.user_data["checkin_app_id"] = app_id

    if (d_ant >= d_v1) and not tiene_anterior and not tiene_actual:
        context.user_data["opcion_fecha_anterior"] = fecha_anterior
        context.user_data["opcion_fecha_actual"] = fecha_actual
        keyboard = [
            [f"⚠️ Evidencia tardía ({fecha_anterior})"],
            [f"✅ Entrega de hoy ({fecha_actual})"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            f"📝 **Seleccioná la entrega a completar para {app.get('descripcion')}:**\n\n"
            f"1️⃣ **Evidencia tardía:** Fecha `{fecha_anterior}` (vencimiento anterior no entregado)\n"
            f"2️⃣ **Entrega actualmente abierta:** Fecha `{fecha_actual}` (vence hoy a las 5:00 PM)",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return CHECKIN_FOTO

    context.user_data["checkin_vencimiento"] = fecha_actual
    await update.message.reply_text(
        f"📸 **Check-in Diario de Prueba**\n\n"
        f"📱 **App:** {app.get('descripcion')}\n"
        f"📅 **Vencimiento que completás:** `{fecha_actual}` (17:00:00 America/Mexico_City)\n"
        f"📊 **Progreso puntual verificado:** `{prueba.get('dias_completados', 1)}/14` días.\n\n"
        f"Por favor, enviá una captura de pantalla del día de hoy mostrando la aplicación instalada en tu teléfono.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return CHECKIN_FOTO

async def checkin_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la captura diaria o procesa la selección de fecha."""
    user_id = str(update.message.from_user.id)
    nombre = update.message.from_user.first_name
    app_id = context.user_data.get("checkin_app_id")

    msg_text = getattr(update.message, "text", None) if update and hasattr(update, "message") else None
    if msg_text:
        texto = msg_text.strip()
        fecha_ant = context.user_data.get("opcion_fecha_anterior")
        fecha_act = context.user_data.get("opcion_fecha_actual")
        if fecha_ant and fecha_ant in texto:
            context.user_data["checkin_vencimiento"] = fecha_ant
            await update.message.reply_text(f"✅ Seleccionaste entregar evidencia tardía para `{fecha_ant}`.\nPor favor enviá la captura de pantalla ahora:", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
            return CHECKIN_FOTO
        elif fecha_act and fecha_act in texto:
            context.user_data["checkin_vencimiento"] = fecha_act
            await update.message.reply_text(f"✅ Seleccionaste entregar evidencia para la fecha abierta `{fecha_act}`.\nPor favor enviá la captura de pantalla ahora:", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
            return CHECKIN_FOTO

    photos = getattr(update.message, "photo", None) if update and hasattr(update, "message") else None
    if not app_id or not photos:
        await update.message.reply_text("❌ Por favor enviá una captura de pantalla de la aplicación.", reply_markup=obtener_teclado_principal(user_id))
        return CHECKIN_FOTO

    fecha_venc = context.user_data.pop("checkin_vencimiento", None)
    foto_file_id = update.message.photo[-1].file_id
    fn_msg_id = getattr(db, "_obtener_id_mensaje", None)
    id_mensaje = fn_msg_id(update, user_id) if callable(fn_msg_id) else f"msg_{user_id}_default"
    telegram_dt = getattr(update.message, "date", None) if update and hasattr(update, "message") else None
    fn_parse = getattr(db, "parse_dt_mexico", None)
    fecha_hoy = fn_parse(telegram_dt).strftime("%Y-%m-%d") if callable(fn_parse) else datetime.now().strftime("%Y-%m-%d")

    try:
        resultado = db.registrar_checkin(user_id, app_id, foto_file_id, fecha_hoy,
                                          id_mensaje=id_mensaje, telegram_timestamp=telegram_dt, fecha_vencimiento=fecha_venc)
    except db.ErrorRecompensa as exc:
        await update.message.reply_text(str(exc), reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    res_tipo = resultado.get("resultado")
    if res_tipo == "duplicado":
        await update.message.reply_text("⚠️ Esta fotografía/mensaje ya fue procesada anteriormente.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    if res_tipo == "actualizado":
        estado_ent = resultado.get("estado_entrega", "puntual")
        await update.message.reply_text(
            f"ℹ️ **Captura actualizada para la fecha {resultado.get('fecha_vencimiento')}**\n\n"
            f"📌 Estado registrado: `{estado_ent.capitalize()}`\n"
            f"Se guardó la nueva foto en versiones anteriores sin alterar el contador ni la puntualidad original.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if res_tipo in ("ya_pagada", "ya_registrado"):
        mensaje = ("Esta prueba ya tiene su recompensa registrada. No se vuelve a acreditar."
                   if res_tipo == "ya_pagada" else "Ya registraste el check-in de hoy.")
        await update.message.reply_text(mensaje, reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    app = resultado
    dias_actuales = resultado["dias"]
    estado_ent = resultado.get("estado_entrega", "puntual")

    if res_tipo == "completada":
        await update.message.reply_text(
            f"🎉 **¡FELICITACIONES! HAS COMPLETADO LOS 14 DÍAS DE PRUEBA** 🏆\n\n"
            f"📱 **App:** {app.get('descripcion')}\n"
            f"🪙 **Recompensa Otorgada:** +800 Créditos\n"
            f"🎟️ **Premio Extra:** +1 Boleto para la Rifa Mensual\n\n"
            f"✅ *Liquidación Auditada (5D):* Recompensa verificada y acreditada transaccionalmente.\n\n"
            f"¡Muchas gracias por contribuir a la comunidad TestCoin!",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )

        dev_id = app.get("dev_id")
        if dev_id:
            try:
                await context.bot.send_message(
                    chat_id=dev_id,
                    text=(
                        f"🏆 **¡Un tester ha completado exitosamente los 14 días!**\n\n"
                        f"👤 Tester: **{nombre}**\n"
                        f"📝 App: {app.get('descripcion')}\n"
                        f"✅ 14 días de prueba cumplidos satisfactoriamente."
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    else:
        estado_icon = "✅ Puntual" if estado_ent == "puntual" else "⚠️ Tardío"
        await update.message.reply_text(
            f"✅ **¡Check-in diario registrado exitosamente!**\n\n"
            f"📱 **App:** {app.get('descripcion')}\n"
            f"📅 **Vencimiento satisfecho:** `{resultado.get('fecha_vencimiento')}`\n"
            f"⏱️ **Evaluación:** `{estado_icon}`\n"
            f"📊 **Progreso puntual verificado:** `{dias_actuales}/14` días.\n\n"
            f"💡 ¡Faltan sólo {14 - dias_actuales} días para obtener tus 800 créditos!",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )

    return ConversationHandler.END

# --- REFERIDOS ---
async def referidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el enlace de referido."""
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)

    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start.")
        return

    bot_username = (await context.bot.get_me()).username
    link_referido = f"https://t.me/{bot_username}?start=ref_{user_id}"

    mis_referidos = usuarios[user_id].get("referidos", [])
    boletos = usuarios[user_id].get("boletos_rifa", 0)

    await update.message.reply_text(
        f"👥 **Programa de Referidos — TestCoin**\n\n"
        f"¡Invitá amigos a la plataforma y ganá boletos extra para la Rifa Mensual!\n\n"
        f"🔗 **Tu enlace de referido único:**\n`{link_referido}`\n\n"
        f"📊 **Tus Estadísticas:**\n"
        f"• Amigos invitados: `{len(mis_referidos)}` usuarios\n"
        f"• Boletos de rifa acumulados: `{boletos}` boletos\n\n"
        f"🎁 Por cada amigo que se registre con tu enlace, ¡ambos ganan **+1 boleto extra** para el sorteo mensual!",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

# --- RIFA MENSUAL ---
async def rifa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la información de la Rifa Mensual."""
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)

    boletos_usuario = usuarios.get(user_id, {}).get("boletos_rifa", 0)
    total_boletos = sum(u.get("boletos_rifa", 0) for u in usuarios.values())
    probabilidad = (boletos_usuario / total_boletos * 100) if total_boletos > 0 else 0

    await update.message.reply_text(
        f"🎟️ **Rifa Mensual TestCoin**\n\n"
        f"Todos los usuarios activos participan automáticamente en el sorteo mensual.\n\n"
        f"📊 **Tus Boletos:** `{boletos_usuario}` boletos\n"
        f"🌐 **Total de boletos en el bombo:** `{total_boletos}` boletos\n"
        f"🎲 **Tu probabilidad actual:** `{probabilidad:.1f}%`\n\n"
        f"💡 **¿Cómo conseguir más boletos?**\n"
        f"• Publicá una campaña (+1 boleto)\n"
        f"• Completá los 14 días de prueba (+1 boleto)\n"
        f"• Invitá amigos tocando el botón **👥 Referidos** (+1 boleto por cada uno)",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def hacerifa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return

    usuarios = cargar_archivo(USUARIOS_FILE)
    bombo = []
    for uid, datos in usuarios.items():
        boletos = datos.get("boletos_rifa", 0)
        bombo.extend([uid] * boletos)

    if not bombo:
        await update.message.reply_text("❌ No hay boletos registrados para realizar la rifa.")
        return

    ganador_id = random.choice(bombo)
    ganador = usuarios[ganador_id]

    await update.message.reply_text(
        f"🎉 **¡RESULTADOS DE LA RIFA MENSUAL TESTCOIN!** 🏆\n\n"
        f"🎟️ **Total de boletos participantes:** `{len(bombo)}`\n"
        f"👑 **¡GANADOR DEL SORTEO!**\n"
        f"👤 **Nombre:** {ganador.get('nombre')}\n"
        f"🆔 **ID:** `{ganador_id}`\n"
        f"🎟️ **Boletos acumulados:** `{ganador.get('boletos_rifa')}` boletos",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

    try:
        await context.bot.send_message(
            chat_id=ganador_id,
            text=(
                f"🎉 **¡FELICITACIONES! HAS GANADO LA RIFA MENSUAL** 🏆\n\n"
                f"Un administrador te contactará en breve para entregarte tu premio."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

# --- CONSULTA DE SALDO Y COMANDOS DE CONSULTA ---
async def miscreditos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)
    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start.")
        return
    usuario = usuarios[user_id]
    await update.message.reply_text(
        f"📊 **Estado de tu Cuenta — TestCoin**\n\n"
        f"👤 **Usuario:** {usuario.get('nombre')}\n"
        f"🏷️ **Rol:** {usuario.get('rol', 'Sin definir').capitalize()}\n"
        f"🪙 **Créditos acumulados:** {usuario.get('creditos', 0)}\n"
        f"🎟️ **Boletos para Rifa Mensual:** {usuario.get('boletos_rifa', 0)}\n"
        f"⚠️ **Advertencias recibidas:** {usuario.get('advertencias', 0)}",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def misapps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)
    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start.")
        return
    usuario = usuarios[user_id]
    apps_testeando_ids = usuario.get("apps_testeando", [])
    if not apps_testeando_ids:
        await update.message.reply_text("📱 No estás probando ninguna aplicación. Tocá **📱 Ver Apps**.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
        return
    apps = cargar_archivo(APPS_FILE)
    mensaje = "📱 **Apps que estás probando:**\n\n"
    for app_id in apps_testeando_ids:
        if app_id in apps:
            app = apps[app_id]
            prueba = app.get("pruebas", {}).get(user_id, {})
            estado_app = app.get("estado", "reclutando")
            estado_prueba = prueba.get("estado", "Pendiente de captura")
            fecha_inicio = prueba.get("fecha_inicio", "Sin iniciar")
            dias = prueba.get("dias_completados", 0)
            min_t = app.get("min_testers", 12)
            registros = prueba.get("registros", {})
            requiere_conc = prueba.get("requiere_conciliacion", False)

            dias_puntuales = sum(1 for r in registros.values() if r.get("estado_entrega") == "puntual")
            if requiere_conc:
                dias_txt = f"Avance previo sin conciliar: `{dias}/14` (Requiere conciliación en 5D)"
            elif registros:
                dias_txt = f"Entregas puntualidad verificada: `{dias_puntuales}/14`"
            else:
                dias_txt = f"Progreso: `{dias}/14` días"

            if estado_app == "reclutando":
                indicador = f"🟡 En lista de espera (esperando {min_t} testers)"
            elif estado_app == "lista":
                indicador = "🟢 Lista para iniciar (esperando lanzamiento del Dev)"
            else:
                indicador = f"🚀 En prueba activa ({dias_txt})"
            mensaje += (
                f"🔹 **{app.get('descripcion', 'Sin descripción')}**\n"
                f"🔗 Link: {app.get('link')}\n"
                f"📊 Estado campaña: {indicador}\n"
                f"📌 Tu estado: `{estado_prueba}`\n"
                f"📅 Fecha inicio: `{fecha_inicio}`\n"
                f"🆔 ID: `{app_id}`\n\n"
            )
    await update.message.reply_text(mensaje, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")


async def settimezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite al usuario consultar o configurar su zona horaria preferida (por defecto America/Mexico_City)."""
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)
    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start.")
        return

    if context.args:
        tz_str = context.args[0]
        try:
            db.parse_dt_mexico(None)
            usuarios[user_id]["timezone"] = tz_str
            guardar_archivo(USUARIOS_FILE, usuarios)
            await update.message.reply_text(f"✅ Zona horaria configurada exitosamente: `{tz_str}`", parse_mode="Markdown")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error al configurar zona horaria: {exc}")
    else:
        user_tz = usuarios[user_id].get("timezone", "America/Mexico_City")
        await update.message.reply_text(
            f"🌍 **Tu zona horaria configurada:** `{user_tz}`\n\n"
            f"Para cambiarla usá: `/settimezone America/Mexico_City`",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )


async def ejecutar_avisos_1500(context: ContextTypes.DEFAULT_TYPE):
    """Envía recordatorios persistentes a las 15:00 (America/Mexico_City) revalidando la entrega y la campaña."""
    apps = cargar_archivo(APPS_FILE)
    now_dt = db.parse_dt_mexico(None)
    from datetime import timedelta
    for app_id, app in apps.items():
        if app.get("estado") != "en_prueba":
            continue
        fecha_v1 = db.calcular_vencimiento_primera_entrega(app.get("timestamp_lanzamiento"), app.get("fecha_lanzamiento"))
        if now_dt.hour >= 17:
            cur_venc = (now_dt.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            cur_venc = now_dt.date().strftime("%Y-%m-%d")

        for tester_id in app.get("testers", []):
            res = db.reclamar_aviso(app_id, tester_id, cur_venc, "recordatorio_1500")
            if res.get("elegible"):
                token = res.get("token_reclamacion")
                try:
                    await context.bot.send_message(
                        chat_id=tester_id,
                        text=(
                            f"⏰ **RECORDATORIO TESTCOIN (15:00)** 📸\n\n"
                            f"App: **{app.get('descripcion')}**\n"
                            f"Tu entrega para la fecha `{cur_venc}` vence hoy a las **5:00 PM** (America/Mexico_City).\n"
                            f"Por favor enviá tu captura de pantalla a tiempo."
                        ),
                        parse_mode="Markdown"
                    )
                    db.confirmar_aviso_enviado(app_id, tester_id, cur_venc, "recordatorio_1500", token_reclamacion=token)
                except Exception as exc:
                    logger.warning(f"Error al enviar aviso 15:00 a tester {tester_id}: {exc}")


async def ejecutar_avisos_1630(context: ContextTypes.DEFAULT_TYPE):
    """Envía recordatorios persistentes a las 16:30 (America/Mexico_City) revalidando la entrega y la campaña."""
    apps = cargar_archivo(APPS_FILE)
    now_dt = db.parse_dt_mexico(None)
    from datetime import timedelta
    for app_id, app in apps.items():
        if app.get("estado") != "en_prueba":
            continue
        fecha_v1 = db.calcular_vencimiento_primera_entrega(app.get("timestamp_lanzamiento"), app.get("fecha_lanzamiento"))
        if now_dt.hour >= 17:
            cur_venc = (now_dt.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            cur_venc = now_dt.date().strftime("%Y-%m-%d")

        for tester_id in app.get("testers", []):
            res = db.reclamar_aviso(app_id, tester_id, cur_venc, "recordatorio_1630")
            if res.get("elegible"):
                token = res.get("token_reclamacion")
                try:
                    await context.bot.send_message(
                        chat_id=tester_id,
                        text=(
                            f"🚨 **¡ÚLTIMO AVISO! RECORDATORIO (16:30)** ⚠️\n\n"
                            f"App: **{app.get('descripcion')}**\n"
                            f"Solo quedan 30 minutos para el corte de las **5:00 PM** (`{cur_venc}`).\n"
                            f"¡Enviá tu captura ahora!"
                        ),
                        parse_mode="Markdown"
                    )
                    db.confirmar_aviso_enviado(app_id, tester_id, cur_venc, "recordatorio_1630", token_reclamacion=token)
                except Exception as exc:
                    logger.warning(f"Error al enviar aviso 16:30 a tester {tester_id}: {exc}")


async def ejecutar_corte_1700(context: ContextTypes.DEFAULT_TYPE):
    """Procesa la detección de posibles faltas de vencimientos omitidos."""
    nuevas_faltas = db.marcar_posibles_faltas()
    logger.info(f"Corte 17:00 procesado. Nuevas posibles faltas pendientes de revisión: {nuevas_faltas}")

async def miscampanas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    apps = cargar_archivo(APPS_FILE)
    mis_apps = [(app_id, datos) for app_id, datos in apps.items() if datos.get("dev_id") == user_id]
    if not mis_apps:
        await update.message.reply_text("📢 Aún no tenés campañas creadas. Publicá una tocando **🚀 Subir App**.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
        return
    mensaje = "📢 **Tus Campañas Publicadas:**\n\n"
    for app_id, datos in mis_apps:
        num_testers = len(datos.get("testers", []))
        min_testers = datos.get("min_testers", 12)
        max_testers = datos.get("max_testers", min_testers)
        estado = datos.get("estado", "reclutando")
        pruebas_activas = len([p for p in datos.get("pruebas", {}).values() if p.get("estado") in ["instalada", "completada"]])

        if estado == "reclutando":
            estado_txt = f"🟡 **En lista de espera** (`{num_testers}/{min_testers}` mín.)"
            accion = f"💡 *Se notificará cuando se unan los {min_testers} testers.*"
        elif estado == "lista":
            estado_txt = f"🟢 **¡LISTA PARA INICIAR!** (`{num_testers}/{max_testers}` testers)"
            accion = f"👉 **¡Lanzala ahora!** Escribí: `/iniciarprueba {app_id}`"
        else:
            estado_txt = f"🚀 **En prueba activa** (`{pruebas_activas}` testers activos)"
            accion = f"📅 Lanzada el `{datos.get('fecha_lanzamiento', 'Reciente')}`"

        mensaje += (
            f"📱 **{datos.get('descripcion', 'Sin descripción')}**\n"
            f"🔗 Link: {datos.get('link')}\n"
            f"📊 Estado: {estado_txt}\n"
            f"{accion}\n"
            f"🆔 ID: `{app_id}`\n\n"
        )
    await update.message.reply_text(mensaje, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

async def comprarcreditos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    await update.message.reply_text(
        "💳 **Recarga de Créditos — TestCoin**\n\n"
        "Si sos desarrollador y no querés probar apps de otros usuarios, podés comprar créditos directamente:\n\n"
        "📦 **Paquete Campaña Completa:**\n"
        "• 🪙 **800 Créditos** (Cubre 1 campaña con 15 testers por 14 días)\n"
        "• 💰 **Precio:** $800 MXN\n\n"
        "📲 **¿Cómo recargar?**\n"
        "1. Realizá la solicitud enviando un mensaje al administrador.\n"
        "2. Te proporcionaremos los datos de pago para activar tus créditos al instante.\n\n"
        "📩 Contacto Soporte/Admin: `@AdminTestCoin`",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    await update.message.reply_text(
        "🛠️ **Panel de Administración — TestCoin**\n\n"
        "• `/darcreditos <user_id> <cantidad>`\n"
        "• `/regalarcampana <user_id>`\n"
        "• `/ajustardias <app_id> <tester_id> <dias>`\n"
        "• `/hacerifa`\n"
        "• `/advertir <tester_id> <motivo>`\n"
        "• `/sancionar`\n"
        "• `/stats`",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def darcreditos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso correcto: `/darcreditos <user_id> <cantidad>`", parse_mode="Markdown")
        return
    target_user_id = context.args[0]
    try:
        cantidad = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ La cantidad debe ser un número entero.")
        return
    usuarios = cargar_archivo(USUARIOS_FILE)
    if target_user_id not in usuarios:
        await update.message.reply_text(f"❌ El usuario `{target_user_id}` no existe.", parse_mode="Markdown")
        return
    usuarios[target_user_id]["creditos"] = usuarios[target_user_id].get("creditos", 0) + cantidad
    guardar_archivo(USUARIOS_FILE, usuarios)
    await update.message.reply_text(f"✅ +{cantidad} Créditos otorgados a {usuarios[target_user_id]['nombre']}.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

async def regalarcampana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("⚠️ Uso correcto: `/regalarcampana <user_id>`", parse_mode="Markdown")
        return
    target_user_id = context.args[0]
    usuarios = cargar_archivo(USUARIOS_FILE)
    if target_user_id not in usuarios:
        await update.message.reply_text(f"❌ El usuario `{target_user_id}` no existe.", parse_mode="Markdown")
        return
    usuarios[target_user_id]["creditos"] = usuarios[target_user_id].get("creditos", 0) + 800
    guardar_archivo(USUARIOS_FILE, usuarios)
    await update.message.reply_text(f"🎁 Se le han acreditado 800 créditos de regalo a {usuarios[target_user_id]['nombre']}.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    usuarios = cargar_archivo(USUARIOS_FILE)
    apps = cargar_archivo(APPS_FILE)
    await update.message.reply_text(
        f"📊 **Estadísticas de TestCoin**\n\n"
        f"👥 **Usuarios Registrados:** {len(usuarios)}\n"
        f"📱 **Campañas Creadas:** {len(apps)}\n"
        f"🟡 **En Lista de Espera:** {len([a for a in apps.values() if a.get('estado') == 'reclutando'])}\n"
        f"🟢 **Listas para Iniciar:** {len([a for a in apps.values() if a.get('estado') == 'lista'])}\n"
        f"🚀 **En Prueba Activa:** {len([a for a in apps.values() if a.get('estado') == 'en_prueba'])}",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def ajustardias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    await update.message.reply_text(
        "🛑 **El comando /ajustardias ha sido deshabilitado.**\n\n"
        "Para garantizar la transparencia, trazabilidad y auditoría de TestCoin, "
        "ya no es posible alterar el contador de días numéricos de forma directa ni arbitraria.\n\n"
        "👉 **Flujo de corrección por fecha:**\n"
        "Por favor, utilizá `/casos` para evaluar y corregir fechas específicas de tus testers "
        "indicando el motivo y registrando la auditoría correspondiente.",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def advertirtester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ **Uso del comando:** `/advertir <tester_user_id> <motivo>`", parse_mode="Markdown")
        return

    target_tester_id = context.args[0]
    motivo = " ".join(context.args[1:])
    usuarios = cargar_archivo(USUARIOS_FILE)
    if target_tester_id not in usuarios:
        await update.message.reply_text(f"❌ El tester con ID `{target_tester_id}` no está registrado.", parse_mode="Markdown")
        return

    usuarios[target_tester_id]["advertencias"] = usuarios[target_tester_id].get("advertencias", 0) + 1
    total_adv = usuarios[target_tester_id]["advertencias"]
    guardar_archivo(USUARIOS_FILE, usuarios)

    await update.message.reply_text(
        f"⚠️ **¡Advertencia enviada al tester!**\n\n"
        f"👤 **Tester:** {usuarios[target_tester_id]['nombre']} (`{target_tester_id}`)\n"
        f"📝 **Motivo:** {motivo}\n"
        f"🚨 **Total de advertencias:** {total_adv}",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

async def sancionartester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "🚨 **Panel de Selección de Sanciones — Admin**\n\n"
            "1️⃣ `/sancionar <tester_id> creditos <cantidad>`\n"
            "2️⃣ `/sancionar <tester_id> boletos <cantidad>`\n"
            "3️⃣ `/sancionar <tester_id> expulsar <app_id>`\n"
            "4️⃣ `/sancionar <tester_id> resetear`",
            parse_mode="Markdown"
        )
        return

    target_tester_id = context.args[0]
    tipo_sancion = context.args[1].lower()

    if tipo_sancion == "expulsar":
        if len(context.args) < 3:
            await update.message.reply_text("⚠️ Especificá el ID de la app: `/sancionar <tester_id> expulsar <app_id>`", parse_mode="Markdown")
            return
        app_id = context.args[2]
        try:
            res_exp = db.expulsar_tester(target_tester_id, app_id)
        except db.ExpulsionRechazada as exc:
            await update.message.reply_text(str(exc), reply_markup=obtener_teclado_principal(user_id))
            return
        except db.ErrorPersistenciaExpulsion as exc:
            await update.message.reply_text(str(exc), reply_markup=obtener_teclado_principal(user_id))
            return

        tester_nombre = res_exp.get("tester_nombre", target_tester_id)
        if res_exp["resultado"] == "ya_expulsado":
            msg_admin = f"ℹ️ El tester `{target_tester_id}` ya no figuraba inscrito en `{app_id}`. No se realizaron cambios."
        else:
            inconsistente_txt = " (se reparó una inscripción inconsistente)" if res_exp.get("inconsistente") else ""
            msg_admin = f"🚨 Tester {tester_nombre} (`{target_tester_id}`) fue expulsado exitosamente de `{app_id}`{inconsistente_txt}."

        await update.message.reply_text(msg_admin, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
        return

    usuarios = cargar_archivo(USUARIOS_FILE)
    if target_tester_id not in usuarios:
        await update.message.reply_text(f"❌ El tester `{target_tester_id}` no está registrado.", parse_mode="Markdown")
        return

    tester = usuarios[target_tester_id]

    if tipo_sancion == "creditos":
        if len(context.args) < 3:
            await update.message.reply_text("⚠️ Especificá la cantidad.", parse_mode="Markdown")
            return
        try:
            cant = int(context.args[2])
        except ValueError:
            await update.message.reply_text("❌ La cantidad debe ser un número entero.")
            return
        tester["creditos"] = max(0, tester.get("creditos", 0) - cant)
        guardar_archivo(USUARIOS_FILE, usuarios)
        msg_admin = f"🚨 Descuento de 🪙 `{cant}` créditos a {tester['nombre']}."

    elif tipo_sancion == "boletos":
        if len(context.args) < 3:
            await update.message.reply_text("⚠️ Especificá la cantidad.", parse_mode="Markdown")
            return
        try:
            cant = int(context.args[2])
        except ValueError:
            await update.message.reply_text("❌ La cantidad debe ser un número entero.")
            return
        tester["boletos_rifa"] = max(0, tester.get("boletos_rifa", 0) - cant)
        guardar_archivo(USUARIOS_FILE, usuarios)
        msg_admin = f"🚨 Descuento de 🎟️ `{cant}` boletos a {tester['nombre']}."

    elif tipo_sancion == "resetear":
        tester["creditos"] = 0
        tester["boletos_rifa"] = 0
        guardar_archivo(USUARIOS_FILE, usuarios)
        msg_admin = f"🚨 Cuenta de {tester['nombre']} reseteada."
    else:
        await update.message.reply_text("❌ Tipo de sanción no reconocido.", parse_mode="Markdown")
        return

    await update.message.reply_text(msg_admin, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

# --- FLUJOS DE CASOS, APELACIONES Y AUDITORÍA (5C.2) ---
async def _mostrar_menu_casos_app(target, app_id, app, user_id, context=None):
    pruebas = app.get("pruebas") or {}
    teclas = []
    total_fechas = 0
    idx_counter = 0

    items_list = []
    for tester_id, prueba in pruebas.items():
        posibles = prueba.get("posibles_faltas") or {}
        for f_venc, f_entry in posibles.items():
            total_fechas += 1
            estado = f_entry.get("estado_resolucion", f_entry.get("estado", "pendiente_revision"))
            emoji = "🟢" if estado == "cumplida_por_revision" else ("🟡" if estado == "justificada" else ("🔴" if estado == "injustificada" else "⏳"))
            item_data = {
                "app_id": app_id,
                "tester_id": str(tester_id),
                "fecha": f_venc,
                "estado": estado,
                "version_revision": prueba.get("version_revision", 0),
                "dias_completados": prueba.get("dias_completados", 0)
            }
            if context and hasattr(context, "user_data"):
                context.user_data[f"caso_idx_{idx_counter}"] = item_data
            items_list.append((idx_counter, f"{emoji} {f_venc} | Tester {str(tester_id)[:6]} | {estado}"))
            teclas.append([InlineKeyboardButton(
                f"{emoji} {f_venc} | Tester {str(tester_id)[:6]} | {estado}",
                callback_data=f"c_cas_sel:{idx_counter}"
            )])
            idx_counter += 1

    if total_fechas == 0:
        texto = f"ℹ️ La campaña `{app_id}` no tiene fechas con faltas o revisiones registradas."
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(texto, parse_mode="Markdown")
        else:
            await target.reply_text(texto, parse_mode="Markdown")
        return

    texto = (
        f"📋 **Casos de la Campaña: {app.get('descripcion', app_id)}**\n"
        f"Seleccioná una fecha para revisar:"
    )
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")
    else:
        await target.reply_text(texto, reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")


async def casos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    args = context.args or []
    apps = cargar_archivo(APPS_FILE)

    if args:
        app_id = args[0]
        if app_id not in apps:
            await update.message.reply_text("❌ Campaña no encontrada.")
            return
        app = apps[app_id]
        if str(app.get("dev_id")) != user_id and user_id not in ADMIN_IDS:
            await update.message.reply_text("❌ No tenés permisos para gestionar los casos de esta campaña.")
            return
        await _mostrar_menu_casos_app(update.message, app_id, app, user_id, context=context)
    else:
        campanas = [aid for aid, a in apps.items() if str(a.get("dev_id")) == user_id or user_id in ADMIN_IDS]
        if not campanas:
            await update.message.reply_text("ℹ️ No tenés campañas activas como desarrollador.")
            return
        teclas = []
        for aid in campanas:
            teclas.append([InlineKeyboardButton(f"📱 {apps[aid].get('descripcion', aid)}", callback_data=f"c_app:{aid}")])
        await update.message.reply_text("📋 **Seleccioná una campaña para revisar sus casos:**", reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")


async def miscasos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    casos_list = db.obtener_casos_tester(user_id)
    if not casos_list:
        await update.message.reply_text("ℹ️ No tenés casos ni revisiones registradas en tus pruebas.")
        return

    ap_idx_counter = 0
    for c in casos_list:
        app_id = c["app_id"]
        app_nombre = c["app_nombre"]
        activa = c.get("activa", True)
        post_exp = c.get("post_expulsion", False)
        estado_str = "Activa" if activa else "Expulsado / Inactiva"

        texto = f"📱 **Campaña:** {app_nombre} (`{app_id}`)\n"
        texto += f"📌 **Estado:** `{estado_str}`\n"
        if not activa and post_exp:
            texto += "⚠️ *Nota: Información histórica consultada desde auditoría. Las capturas detalladas no se conservan tras la expulsión.*\n"
        texto += "--------------------------------------\n"

        fechas = c.get("fechas") or {}
        if not fechas:
            texto += "ℹ️ No hay fechas registradas para esta campaña.\n"
            await update.message.reply_text(texto, parse_mode="Markdown")
            continue

        teclas = []
        for f_venc, f_info in fechas.items():
            est = f_info.get("estado_resolucion", "pendiente")
            motivo = f_info.get("motivo_resolucion", "Sin motivo")
            id_evt = f_info.get("id_evento_resolucion")
            apel = f_info.get("apelacion")

            texto += f"• **Fecha:** `{f_venc}`\n"
            texto += f"  - **Estado:** `{est}`\n"
            if motivo:
                texto += f"  - **Motivo Dev:** {motivo}\n"

            if apel:
                texto += f"  - **Apelación:** `{apel.get('estado_apelacion')}`\n"
                if apel.get("motivo_admin"):
                    texto += f"    *Resolución Admin:* {apel.get('motivo_admin')}\n"
            else:
                texto += "  - **Apelación:** Ninguna\n"
                if f_info.get("apelable") and id_evt:
                    context.user_data[f"apelar_idx_{ap_idx_counter}"] = {
                        "app_id": app_id,
                        "fecha": f_venc,
                        "id_evento": id_evt,
                        "estado_resolucion": est
                    }
                    teclas.append([InlineKeyboardButton(f"⚖️ Apelar {f_venc}", callback_data=f"c_ap_sel:{ap_idx_counter}")])
                    ap_idx_counter += 1
            texto += "\n"

        markup = InlineKeyboardMarkup(teclas) if teclas else None
        await update.message.reply_text(texto, reply_markup=markup, parse_mode="Markdown")


async def apelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ **Uso del comando:** `/apelar <app_id> <fecha_YYYY-MM-DD>`\n\n"
            "También podés usar `/miscasos` y presionar el botón **⚖️ Apelar** correspondiente.",
            parse_mode="Markdown"
        )
        return
    app_id, fecha = args[0], args[1]
    casos_list = db.obtener_casos_tester(user_id)
    target_caso = None
    for c in casos_list:
        if c["app_id"] == app_id and fecha in c.get("fechas", {}):
            target_caso = c["fechas"][fecha]
            break

    if not target_caso:
        await update.message.reply_text("❌ No se encontró una resolución registrada para esa fecha en esa campaña.")
        return
    if not target_caso.get("apelable") or not target_caso.get("id_evento_resolucion"):
        await update.message.reply_text(f"❌ La fecha `{fecha}` no se encuentra en un estado apelable o no tiene evento de resolución.", parse_mode="Markdown")
        return
    if target_caso.get("apelacion") and target_caso["apelacion"].get("estado_apelacion") == "pendiente":
        await update.message.reply_text("ℹ️ Ya tenés una apelación pendiente de resolución para esta fecha.")
        return

    context.user_data["estado_espera"] = "esperando_justificacion_apelacion"
    context.user_data["temp_apelacion"] = {
        "app_id": app_id,
        "fecha": fecha,
        "id_evento": target_caso["id_evento_resolucion"],
        "estado_resolucion": target_caso.get("estado_resolucion")
    }
    await update.message.reply_text(
        f"📝 **Ingresá la justificación de tu apelación para la fecha {fecha}:**\n"
        f"(Explicá detalladamente el motivo; mínimo 5 caracteres)",
        parse_mode="Markdown"
    )


async def apelaciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    pendientes = db.obtener_apelaciones_pendientes()
    if not pendientes:
        await update.message.reply_text("✅ No hay apelaciones pendientes de revisión.")
        return

    await update.message.reply_text(f"⚖️ **Panel de Apelaciones Pendientes ({len(pendientes)})**", parse_mode="Markdown")
    for idx, ap in enumerate(pendientes):
        context.user_data[f"apel_idx_{idx}"] = ap
        context.bot_data[f"apel_idx_{idx}"] = ap
        id_ap = ap.get("id_apelacion")
        app_id = ap.get("app_id")
        t_id = ap.get("user_id")
        f_v = ap.get("fecha_vencimiento")
        est_ap = ap.get("estado_resolucion_apelado")
        just = ap.get("justificacion_tester", "")

        texto = (
            f"📱 **App:** `{app_id}` | 👤 **Tester:** `{t_id}`\n"
            f"📅 **Fecha:** `{f_v}`\n"
            f"⚙️ **Resolución Apelada:** `{est_ap}`\n"
            f"📝 **Justificación Tester:** {just}\n"
        )

# --- SUB-ETAPA 5D: CONCILIACIÓN DEFINITIVA Y CIERRE DE CAMPAÑA ---
async def _mostrar_conciliacion_campana(target, app_id, app, user_id, context=None):
    resumen = db.obtener_resumen_conciliacion_campana(app_id)
    if not resumen.get("encontrada"):
        msg = f"❌ No se pudo obtener el resumen de la campaña `{app_id}`."
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(msg, parse_mode="Markdown")
        else:
            await target.reply_text(msg, parse_mode="Markdown")
        return

    desc = resumen.get("descripcion", app_id)
    est_op = resumen.get("estado_operativo", "en_prueba")
    est_conc = resumen.get("estado_conciliacion", "pendiente")
    total_ins = resumen.get("total_testers_inscritos", 0)
    total_comp = resumen.get("total_completados", 0)
    total_liq = resumen.get("total_liquidadas", 0)
    total_pend = resumen.get("total_recompensas_pendientes", 0)
    faltas = len(resumen.get("con_faltas_pendientes", []))
    apelaciones = len(resumen.get("con_apelaciones_pendientes", []))
    legacy = len(resumen.get("requieren_conciliacion_legacy", []))
    puede_cerrarse = resumen.get("puede_cerrarse", False)
    f_cierre = resumen.get("fecha_cierre_definitivo")

    texto = (
        f"📊 **Conciliación Definitiva — {desc}**\n"
        f"--------------------------------------\n"
        f"📱 **ID:** `{app_id}`\n"
        f"⚙️ **Estado Operativo:** `{est_op}`\n"
        f"🔒 **Estado Conciliación:** `{est_conc}`\n"
        f"👥 **Testers Inscritos:** `{total_ins}/15`\n"
        f"🏆 **Cumplieron 14 días:** `{total_comp}`\n"
        f"🪙 **Recompensas Liquidadas:** `{total_liq}` (+800 / +1🎟️)\n"
        f"⏳ **Recompensas Pendientes:** `{total_pend}`\n"
    )

    if faltas > 0:
        texto += f"⚠️ **Faltas sin resolver:** `{faltas}` (Revisar en `/casos {app_id}`)\n"
    if apelaciones > 0:
        texto += f"⚖️ **Apelaciones pendientes:** `{apelaciones}` (Revisar en `/apelaciones`)\n"
    if legacy > 0:
        texto += f"📜 **Casos legacy pendientes:** `{legacy}`\n"

    if est_conc == "cerrada_definitivamente":
        texto += f"\n🏁 **Campaña cerrada definitivamente el:** `{f_cierre}`\n"
    elif puede_cerrarse:
        texto += "\n✅ **Campaña solvente y lista para cierre definitivo.**\n"
    else:
        texto += "\n🛑 **Campaña bloqueada para cierre.** Deben resolverse faltas, apelaciones y liquidar recompensas pendientes antes de cerrar.\n"

    teclas = []
    if total_pend > 0 and est_conc != "cerrada_definitivamente":
        teclas.append([InlineKeyboardButton(f"🪙 Liquidar {total_pend} Recompensa(s) Pendientes", callback_data=f"c_cnc_liq:{app_id}")])

    if puede_cerrarse and est_conc != "cerrada_definitivamente":
        teclas.append([InlineKeyboardButton("🏁 Cerrar Campaña Definitivamente", callback_data=f"c_cnc_close:{app_id}")])

    teclas.append([InlineKeyboardButton("🔄 Actualizar", callback_data=f"c_cnc_refresh:{app_id}")])
    teclas.append([InlineKeyboardButton("🔙 Volver a lista", callback_data="c_cnc_list")])

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")
    else:
        await target.reply_text(texto, reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")


async def conciliacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    apps = cargar_archivo(APPS_FILE)
    args = context.args or []
    es_admin = user_id in ADMIN_IDS

    if args:
        app_id = args[0].strip()
        if app_id not in apps:
            await update.message.reply_text(f"❌ Campaña `{app_id}` no encontrada.", parse_mode="Markdown")
            return
        app = apps[app_id]
        if not es_admin and str(app.get("dev_id")) != user_id:
            await update.message.reply_text("❌ No tenés permisos para ver la conciliación de esta campaña.", parse_mode="Markdown")
            return
        await _mostrar_conciliacion_campana(update.message, app_id, app, user_id, context=context)
        return

    if es_admin:
        campanas = [aid for aid, a in apps.items() if a.get("estado") in ["en_prueba", "finalizada", "cerrada", "cerrada_definitivamente"]]
    else:
        campanas = [aid for aid, a in apps.items() if str(a.get("dev_id")) == user_id and a.get("estado") in ["en_prueba", "finalizada", "cerrada", "cerrada_definitivamente"]]

    if not campanas:
        await update.message.reply_text(
            "ℹ️ No tenés campañas en prueba, finalizadas o en proceso de conciliación actualmente.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return

    teclas = []
    for aid in campanas:
        a_data = apps[aid]
        st_icon = "🟢" if a_data.get("estado") == "en_prueba" else "🏁"
        desc = a_data.get("descripcion", aid)
        teclas.append([InlineKeyboardButton(f"{st_icon} {desc[:25]} ({aid})", callback_data=f"c_cnc_app:{aid}")])

    await update.message.reply_text(
        "📊 **Panel de Conciliación Definitiva y Cierre (5D)**\n\n"
        "Seleccioná una campaña para consultar su estado, liquidar recompensas pendientes o realizar el cierre definitivo:",
        reply_markup=InlineKeyboardMarkup(teclas),
        parse_mode="Markdown"
    )


async def manejar_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)

    # 1. Dev / Admin: Selección de Campaña en /casos
    if data.startswith("c_app:"):
        app_id = data.split(":", 1)[1]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await query.edit_message_text("❌ Campaña no encontrada.")
            return
        app = apps[app_id]
        if str(app.get("dev_id")) != user_id and user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos para esta campaña.")
            return
        await _mostrar_menu_casos_app(query, app_id, app, user_id, context=context)
        return

    # 2. Dev / Admin: Selección de Caso/Fecha
    if data.startswith("c_cas_sel:"):
        idx = int(data.split(":", 1)[1])
        item = context.user_data.get(f"caso_idx_{idx}")
        if not item:
            await query.edit_message_text("⚠️ Sesión expirada. Volvé a ingresar con /casos.")
            return
        app_id = item["app_id"]
        tester_id = item["tester_id"]
        fecha = item["fecha"]
        estado = item["estado"]
        version = item.get("version_revision", 0)

        teclas = [
            [InlineKeyboardButton("🟢 Cumplida por Revisión", callback_data=f"c_cas_dec:c:{idx}")],
            [InlineKeyboardButton("🟡 Justificada (Sin día)", callback_data=f"c_cas_dec:j:{idx}")],
            [InlineKeyboardButton("🔴 Injustificada (Falta)", callback_data=f"c_cas_dec:i:{idx}")],
            [InlineKeyboardButton("🔙 Volver", callback_data=f"c_app:{app_id}")]
        ]
        await query.edit_message_text(
            f"📋 **Revisión de Caso**\n"
            f"--------------------------------------\n"
            f"📱 **Campaña:** `{app_id}`\n"
            f"👤 **Tester:** `{tester_id}`\n"
            f"📅 **Fecha:** `{fecha}`\n"
            f"⚙️ **Estado actual:** `{estado}` (v{version})\n\n"
            f"Seleccioná la nueva resolución propuesta:",
            reply_markup=InlineKeyboardMarkup(teclas),
            parse_mode="Markdown"
        )
        return

    # 3. Dev / Admin: Selección de Resolución para un Caso
    if data.startswith("c_cas_dec:"):
        parts = data.split(":")
        st_code = parts[1]
        idx = int(parts[2])
        item = context.user_data.get(f"caso_idx_{idx}")
        if not item:
            await query.edit_message_text("⚠️ Sesión expirada. Volvé a ingresar con /casos.")
            return

        map_st = {
            "c": "cumplida_por_revision",
            "j": "justificada",
            "i": "injustificada"
        }
        nuevo_st = map_st.get(st_code, "justificada")

        dias_actuales = item.get("dias_completados", 0)
        estado_previo = item.get("estado")
        if nuevo_st == "cumplida_por_revision" and estado_previo != "cumplida_por_revision":
            dias_proyectados = dias_actuales + 1
        elif nuevo_st != "cumplida_por_revision" and estado_previo == "cumplida_por_revision":
            dias_proyectados = max(0, dias_actuales - 1)
        else:
            dias_proyectados = dias_actuales

        context.user_data["estado_espera"] = "esperando_motivo_revision"
        context.user_data["temp_revision"] = {
            "app_id": item["app_id"],
            "tester_id": item["tester_id"],
            "fecha": item["fecha"],
            "nuevo_estado": nuevo_st,
            "estado_actual": item["estado"],
            "version_esperada": item.get("version_revision", 0),
            "dias_actuales": dias_actuales,
            "dias_proyectados": dias_proyectados
        }

        await query.message.reply_text(
            f"📝 **Resolución seleccionada:** `{nuevo_st}`\n\n"
            f"Por favor, escribí el motivo obligatorio de esta resolución (mínimo 5 caracteres):",
            parse_mode="Markdown"
        )
        return

    # 4. Dev / Admin: Confirmación de Resolución de Caso
    if data.startswith("c_cas_conf:"):
        sol_id = data.split(":", 1)[1]
        pending = context.user_data.get(f"pending_rev_{sol_id}")
        if not pending:
            await query.edit_message_text("⚠️ La sesión de confirmación expiró o el bot se reinició. Por favor, volvé a iniciar desde /casos.")
            return

        app_id = pending["app_id"]
        tester_id = pending["tester_id"]
        fecha = pending["fecha"]
        nuevo_st = pending["nuevo_estado"]
        motivo = pending["motivo"]
        version_esperada = pending["version_esperada"]

        res = db.revisar_falta_obligacion(
            actor_id=user_id,
            app_id=app_id,
            user_id=tester_id,
            fecha_vencimiento=fecha,
            nuevo_estado=nuevo_st,
            motivo=motivo,
            id_solicitud=sol_id,
            version_revision_esperada=version_esperada
        )

        context.user_data.pop(f"pending_rev_{sol_id}", None)

        if res.get("exito"):
            await query.edit_message_text(
                f"✅ **¡Resolución guardada y auditada con éxito!**\n\n"
                f"📱 **Campaña:** `{app_id}`\n"
                f"👤 **Tester:** `{tester_id}`\n"
                f"📅 **Fecha:** `{fecha}`\n"
                f"⚙️ **Nuevo Estado:** `{res.get('estado_resolucion')}`\n"
                f"📊 **Días Verificados:** `{res.get('dias_completados')}`\n"
                f"🆔 **Evento Auditoría:** `{res.get('id_evento')}`",
                parse_mode="Markdown"
            )
        elif res.get("motivo") == "conflicto_modificacion_concurrente":
            await query.edit_message_text(
                "⚠️ **CONFLICTO DE CONCURRENCIA**\n\n"
                "El estado o versión de la prueba cambió mientras preparabas la decisión.\n"
                "Por favor revisá el estado actualizado desde /casos antes de decidir.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"❌ **No se pudo guardar la resolución:** `{res.get('motivo')}`",
                parse_mode="Markdown"
            )
        return

    if data.startswith("c_cas_canc:"):
        sol_id = data.split(":", 1)[1]
        context.user_data.pop(f"pending_rev_{sol_id}", None)
        await query.edit_message_text("❌ Operación cancelada. No se modificaron los registros.")
        return

    # 5. Tester: Selección de Apelación en /miscasos
    if data.startswith("c_ap_sel:"):
        idx = int(data.split(":", 1)[1])
        item = context.user_data.get(f"apelar_idx_{idx}")
        if not item:
            await query.edit_message_text("⚠️ Sesión expirada. Volvé a ingresar con /miscasos.")
            return

        context.user_data["estado_espera"] = "esperando_justificacion_apelacion"
        context.user_data["temp_apelacion"] = item
        await query.message.reply_text(
            f"📝 **Apelar Fecha: {item['fecha']} ({item['app_id']})**\n\n"
            f"Por favor, ingresá la justificación de tu apelación (mínimo 5 caracteres):",
            parse_mode="Markdown"
        )
        return

    # 6. Tester: Confirmación de Apelación
    if data.startswith("c_ap_conf:"):
        sol_id = data.split(":", 1)[1]
        pending = context.user_data.get(f"pending_apel_{sol_id}")
        if not pending:
            await query.edit_message_text("⚠️ La sesión de confirmación expiró o el bot se reinició. Volvé a iniciar desde /miscasos.")
            return

        res = db.crear_apelacion(
            actor_id=user_id,
            app_id=pending["app_id"],
            user_id=user_id,
            fecha_vencimiento=pending["fecha"],
            id_evento_resolucion_apelada=pending["id_evento"],
            justificacion=pending["justificacion"],
            id_solicitud=sol_id
        )

        context.user_data.pop(f"pending_apel_{sol_id}", None)

        if res.get("exito"):
            await query.edit_message_text(
                "✅ **¡Apelación registrada con éxito!**\n\n"
                "Un administrador revisará tu justificación y emitirá una resolución con auditoría.\n"
                "Podés consultar el estado de tu apelación en `/miscasos`.",
                parse_mode="Markdown"
            )
        elif res.get("motivo") == "apelacion_ya_pendiente":
            await query.edit_message_text("ℹ️ Ya tenés una apelación pendiente de resolución para esta fecha.")
        else:
            await query.edit_message_text(f"❌ No se pudo registrar la apelación: `{res.get('motivo')}`")
        return

    if data.startswith("c_ap_canc:"):
        sol_id = data.split(":", 1)[1]
        context.user_data.pop(f"pending_apel_{sol_id}", None)
        await query.edit_message_text("❌ Apelación cancelada.")
        return

    # 7. Admin: Selección de Apelación en /apelaciones
    if data.startswith("c_adm_sel:"):
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos de Administrador.")
            return
        idx = int(data.split(":", 1)[1])
        ap = context.user_data.get(f"apel_idx_{idx}") or context.bot_data.get(f"apel_idx_{idx}")
        if not ap:
            await query.edit_message_text("⚠️ Sesión expirada. Cargá nuevamente /apelaciones.")
            return

        id_ap = ap.get("id_apelacion")
        teclas = [
            [InlineKeyboardButton("🟢 Aceptar (Cumplida por Revisión)", callback_data=f"c_adm_dec:acc_c:{idx}")],
            [InlineKeyboardButton("🟡 Aceptar (Justificada)", callback_data=f"c_adm_dec:acc_j:{idx}")],
            [InlineKeyboardButton("🔴 Rechazar Apelación", callback_data=f"c_adm_dec:rej:{idx}")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="c_adm_canc_gen")]
        ]
        await query.edit_message_text(
            f"⚖️ **Resolución de Apelación: {id_ap}**\n\n"
            f"📱 **App:** `{ap.get('app_id')}` | 👤 **Tester:** `{ap.get('user_id')}`\n"
            f"📅 **Fecha:** `{ap.get('fecha_vencimiento')}`\n"
            f"⚙️ **Resolución Apelada:** `{ap.get('estado_resolucion_apelado')}`\n"
            f"📝 **Justificación Tester:** {ap.get('justificacion_tester')}\n\n"
            f"Seleccioná la decisión a aplicar:",
            reply_markup=InlineKeyboardMarkup(teclas),
            parse_mode="Markdown"
        )
        return

    # 8. Admin: Decisión de Apelación
    if data.startswith("c_adm_dec:"):
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos de Administrador.")
            return
        parts = data.split(":")
        dec_code = parts[1]
        idx = int(parts[2])
        ap = context.user_data.get(f"apel_idx_{idx}") or context.bot_data.get(f"apel_idx_{idx}")
        if not ap:
            await query.edit_message_text("⚠️ Sesión expirada. Cargá nuevamente /apelaciones.")
            return

        decision = "aceptada" if dec_code.startswith("acc") else "rechazada"
        nueva_res = "cumplida_por_revision" if dec_code == "acc_c" else ("justificada" if dec_code == "acc_j" else None)

        context.user_data["estado_espera"] = "esperando_motivo_admin_apelacion"
        context.user_data["temp_res_apel"] = {
            "id_apelacion": ap.get("id_apelacion"),
            "app_id": ap.get("app_id"),
            "decision": decision,
            "nueva_resolucion": nueva_res
        }

        await query.message.reply_text(
            f"📝 **Decisión:** `{decision.upper()}`\n"
            f"Por favor, ingresá el motivo obligatorio de la resolución del Admin (mínimo 5 caracteres):",
            parse_mode="Markdown"
        )
        return

    # 9. Admin: Confirmación de Resolución de Apelación
    if data.startswith("c_adm_conf:"):
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos de Administrador.")
            return
        sol_id = data.split(":", 1)[1]
        pending = context.user_data.get(f"pending_res_apel_{sol_id}")
        if not pending:
            await query.edit_message_text("⚠️ La sesión de confirmación expiró o el bot se reinició. Cargá nuevamente /apelaciones.")
            return

        res = db.resolver_apelacion(
            admin_id=user_id,
            app_id=pending["app_id"],
            id_apelacion=pending["id_apelacion"],
            decision=pending["decision"],
            nueva_resolucion=pending["nueva_resolucion"],
            motivo_admin=pending["motivo_admin"],
            id_solicitud=sol_id
        )

        context.user_data.pop(f"pending_res_apel_{sol_id}", None)

        if res.get("exito"):
            await query.edit_message_text(
                f"✅ **¡Resolución de apelación registrada con éxito!**\n\n"
                f"⚖️ **ID Apelación:** `{res.get('id_apelacion')}`\n"
                f"🎯 **Decisión:** `{res.get('decision')}`\n"
                f"🔄 **Resolución Resultante:** `{res.get('nueva_resolucion')}`\n"
                f"🆔 **Evento Admin:** `{res.get('id_evento_admin')}`",
                parse_mode="Markdown"
            )
        elif res.get("motivo") == "resolucion_apelada_modificada":
            await query.edit_message_text(
                "⚠️ **RESOLUCIÓN MODIFICADA PREVIAMENTE**\n\n"
                "La resolución de esta fecha cambió antes de guardar su decisión.\n"
                "Cargue nuevamente /apelaciones para revisar el estado vigente.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"❌ Error al registrar resolución: `{res.get('motivo')}`")
        return

    if data.startswith("c_adm_canc:"):
        sol_id = data.split(":", 1)[1]
        context.user_data.pop(f"pending_res_apel_{sol_id}", None)
        await query.edit_message_text("❌ Resolución de apelación cancelada.")
        return

    if data == "c_adm_canc_gen":
        await query.edit_message_text("❌ Operación cancelada.")
        return

    # 10. Conciliación y Cierre de Campaña (5D)
    if data == "c_cnc_list":
        apps = cargar_archivo(APPS_FILE)
        es_admin = user_id in ADMIN_IDS
        if es_admin:
            campanas = [aid for aid, a in apps.items() if a.get("estado") in ["en_prueba", "finalizada", "cerrada", "cerrada_definitivamente"]]
        else:
            campanas = [aid for aid, a in apps.items() if str(a.get("dev_id")) == user_id and a.get("estado") in ["en_prueba", "finalizada", "cerrada", "cerrada_definitivamente"]]

        if not campanas:
            await query.edit_message_text("ℹ️ No hay campañas disponibles para conciliación.")
            return

        teclas = []
        for aid in campanas:
            a_data = apps[aid]
            st_icon = "🟢" if a_data.get("estado") == "en_prueba" else "🏁"
            desc = a_data.get("descripcion", aid)
            teclas.append([InlineKeyboardButton(f"{st_icon} {desc[:25]} ({aid})", callback_data=f"c_cnc_app:{aid}")])

        await query.edit_message_text(
            "📊 **Panel de Conciliación Definitiva y Cierre (5D)**\n\n"
            "Seleccioná una campaña:",
            reply_markup=InlineKeyboardMarkup(teclas),
            parse_mode="Markdown"
        )
        return

    if data.startswith("c_cnc_app:"):
        app_id = data.split(":", 1)[1]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await query.edit_message_text("❌ Campaña no encontrada.")
            return
        app = apps[app_id]
        if str(app.get("dev_id")) != user_id and user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos para esta campaña.")
            return
        await _mostrar_conciliacion_campana(query, app_id, app, user_id, context=context)
        return

    if data.startswith("c_cnc_refresh:"):
        app_id = data.split(":", 1)[1]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await query.edit_message_text("❌ Campaña no encontrada.")
            return
        await _mostrar_conciliacion_campana(query, app_id, apps[app_id], user_id, context=context)
        return

    if data.startswith("c_cnc_liq:"):
        app_id = data.split(":", 1)[1]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await query.edit_message_text("❌ Campaña no encontrada.")
            return
        app = apps[app_id]
        if str(app.get("dev_id")) != user_id and user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos para liquidar recompensas en esta campaña.")
            return

        res = db.liquidar_campana_pendientes(app_id, actor_id=user_id)
        if res.get("exito"):
            msg_res = (
                f"🪙 **Liquidación de Recompensas Ejecutada (5D)**\n\n"
                f"📱 Campaña: `{app_id}`\n"
                f"✅ Liquidados con éxito: `{res.get('liquidados_exitosos', 0)}` testers (+800 créditos y +1🎟️ c/u)\n"
                f"⚠️ Errores / Bloqueados: `{res.get('errores', 0)}`"
            )
        else:
            msg_res = f"❌ Error al procesar liquidaciones: `{res.get('motivo')}`"

        teclas = [[InlineKeyboardButton("🔙 Volver a Conciliación", callback_data=f"c_cnc_app:{app_id}")]]
        await query.edit_message_text(msg_res, reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")
        return

    if data.startswith("c_cnc_close:"):
        app_id = data.split(":", 1)[1]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await query.edit_message_text("❌ Campaña no encontrada.")
            return
        app = apps[app_id]
        if str(app.get("dev_id")) != user_id and user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos para cerrar esta campaña.")
            return

        teclas = [
            [InlineKeyboardButton("✅ Sí, Confirmar Cierre Definitivo", callback_data=f"c_cnc_close_conf:{app_id}")],
            [InlineKeyboardButton("❌ Cancelar", callback_data=f"c_cnc_app:{app_id}")]
        ]
        await query.edit_message_text(
            f"⚠️ **CONFIRMAR CIERRE DEFINITIVO DE CAMPAÑA**\n\n"
            f"📱 Campaña: `{app.get('descripcion', app_id)}` (`{app_id}`)\n\n"
            f"Esta acción cerrará definitivamente la campaña en TestCoin. Se registrará un evento inmutable de auditoría.\n"
            f"¿Desea proceder?",
            reply_markup=InlineKeyboardMarkup(teclas),
            parse_mode="Markdown"
        )
        return

    if data.startswith("c_cnc_close_conf:"):
        app_id = data.split(":", 1)[1]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await query.edit_message_text("❌ Campaña no encontrada.")
            return
        app = apps[app_id]
        if str(app.get("dev_id")) != user_id and user_id not in ADMIN_IDS:
            await query.edit_message_text("❌ No tenés permisos para cerrar esta campaña.")
            return

        res = db.cerrar_campana_definitiva(app_id, actor_id=user_id)
        if res.get("exito"):
            msg_close = (
                f"🏁 **¡CAMPAÑA CERRADA DEFINITIVAMENTE!**\n\n"
                f"📱 Campaña: `{app_id}`\n"
                f"📅 Fecha de Cierre: `{res.get('fecha_cierre_definitivo')}`\n"
                f"🆔 Auditoría de Cierre: `{res.get('id_evento_cierre')}`\n"
                f"👤 Cerrada por: `{res.get('actor_cierre')}`"
            )
            teclas = [[InlineKeyboardButton("🔙 Ver Resumen", callback_data=f"c_cnc_app:{app_id}")]]
        else:
            motivo = res.get("motivo")
            detalles = res.get("detalles", "")
            msg_close = (
                f"🛑 **No se pudo cerrar la campaña:** `{motivo}`\n\n"
                f"⚠️ La campaña tiene operaciones pendientes que deben resolverse antes de cerrarla.\n"
                f"Detalles: `{detalles}`"
            )
            teclas = [[InlineKeyboardButton("🔙 Volver a Conciliación", callback_data=f"c_cnc_app:{app_id}")]]

        await query.edit_message_text(msg_close, reply_markup=InlineKeyboardMarkup(teclas), parse_mode="Markdown")
        return



# --- MANEJADOR DE TEXTOS Y BOTONES GENÉRICOS ---
async def mensaje_texto_generico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Atiende mensajes de texto que coincidan con los botones o detecte enlaces enviando la respuesta con botones."""
    texto = update.message.text.strip()
    user_id = str(update.message.from_user.id)

    estado_espera = context.user_data.get("estado_espera")

    if estado_espera == "esperando_motivo_revision":
        if len(texto) < 5:
            await update.message.reply_text("❌ El motivo debe tener al menos 5 caracteres. Por favor, escribilo nuevamente:")
            return
        temp = context.user_data.pop("temp_revision", {})
        context.user_data["estado_espera"] = None
        app_id = temp.get("app_id")
        tester_id = temp.get("tester_id")
        fecha = temp.get("fecha")
        nuevo_estado = temp.get("nuevo_estado")
        version_esperada = temp.get("version_esperada", 0)
        dias_actuales = temp.get("dias_actuales", 0)
        dias_proyectados = temp.get("dias_proyectados", dias_actuales)

        diff = dias_proyectados - dias_actuales
        diff_str = f"+{diff} día" if diff > 0 else (f"{diff} días" if diff < 0 else "Sin cambio")

        id_solicitud = f"rev_{app_id}_{tester_id}_{fecha}_{uuid.uuid4().hex[:8]}"
        context.user_data[f"pending_rev_{id_solicitud}"] = {
            "id_solicitud": id_solicitud,
            "app_id": app_id,
            "tester_id": tester_id,
            "fecha": fecha,
            "nuevo_estado": nuevo_estado,
            "motivo": texto,
            "version_esperada": version_esperada
        }

        teclado = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirmar y Guardar", callback_data=f"c_cas_conf:{id_solicitud}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"c_cas_canc:{id_solicitud}")
            ]
        ])

        await update.message.reply_text(
            f"⚠️ **CONFIRMACIÓN DE RESOLUCIÓN DE CASO**\n"
            f"--------------------------------------\n"
            f"📱 **Campaña:** `{app_id}`\n"
            f"👤 **Tester:** `{tester_id}`\n"
            f"📅 **Fecha:** `{fecha}`\n"
            f"⚙️ **Estado Actual:** `{temp.get('estado_actual', 'pendiente')}` (v{version_esperada})\n"
            f"🎯 **Resolución Propuesta:** `{nuevo_estado}`\n"
            f"📝 **Motivo:** {texto}\n"
            f"📊 **Efecto en Días Verificados:** `{dias_actuales}` ➔ `{dias_proyectados}` ({diff_str})\n\n"
            f"¿Desea confirmar esta resolución?",
            reply_markup=teclado,
            parse_mode="Markdown"
        )
        return

    if estado_espera == "esperando_justificacion_apelacion":
        if len(texto) < 5:
            await update.message.reply_text("❌ La justificación debe tener al menos 5 caracteres. Por favor, escribila nuevamente:")
            return
        temp = context.user_data.pop("temp_apelacion", {})
        context.user_data["estado_espera"] = None
        app_id = temp.get("app_id")
        fecha = temp.get("fecha")
        id_evento = temp.get("id_evento")
        estado_resolucion = temp.get("estado_resolucion")

        id_solicitud = f"apel_{app_id}_{user_id}_{id_evento[:8] if id_evento else 'evt'}_{uuid.uuid4().hex[:6]}"
        context.user_data[f"pending_apel_{id_solicitud}"] = {
            "id_solicitud": id_solicitud,
            "app_id": app_id,
            "user_id": user_id,
            "fecha": fecha,
            "id_evento": id_evento,
            "justificacion": texto
        }

        teclado = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirmar y Enviar", callback_data=f"c_ap_conf:{id_solicitud}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"c_ap_canc:{id_solicitud}")
            ]
        ])

        await update.message.reply_text(
            f"⚠️ **CONFIRMACIÓN DE APELACIÓN**\n"
            f"--------------------------------------\n"
            f"📱 **Campaña:** `{app_id}`\n"
            f"📅 **Fecha:** `{fecha}`\n"
            f"⚖️ **Resolución Apelada:** `{estado_resolucion}`\n"
            f"📝 **Tu Justificación:** {texto}\n\n"
            f"ℹ️ *Nota: La presentación de la apelación conserva la resolución original y no cancela sus consecuencias mientras esté en revisión por el Admin.*",
            reply_markup=teclado,
            parse_mode="Markdown"
        )
        return

    if estado_espera == "esperando_motivo_admin_apelacion":
        if len(texto) < 5:
            await update.message.reply_text("❌ El motivo del Admin debe tener al menos 5 caracteres. Por favor, escribilo nuevamente:")
            return
        temp = context.user_data.pop("temp_res_apel", {})
        context.user_data["estado_espera"] = None
        id_apelacion = temp.get("id_apelacion")
        app_id = temp.get("app_id")
        decision = temp.get("decision")
        nueva_resolucion = temp.get("nueva_resolucion")

        id_solicitud = f"res_apel_{id_apelacion[:10]}_{uuid.uuid4().hex[:6]}"
        context.user_data[f"pending_res_apel_{id_solicitud}"] = {
            "id_solicitud": id_solicitud,
            "id_apelacion": id_apelacion,
            "app_id": app_id,
            "decision": decision,
            "nueva_resolucion": nueva_resolucion,
            "motivo_admin": texto
        }

        teclado = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirmar Decisión Admin", callback_data=f"c_admin_apel_conf:{id_solicitud}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"c_admin_apel_canc:{id_solicitud}")
            ]
        ])

        nueva_res_str = f"`{nueva_resolucion}`" if nueva_resolucion else "Conservar resolución original"
        await update.message.reply_text(
            f"⚠️ **CONFIRMACIÓN DE DECISIÓN DE APELACIÓN (ADMIN)**\n"
            f"--------------------------------------\n"
            f"⚖️ **ID Apelación:** `{id_apelacion}`\n"
            f"🎯 **Decisión:** `{decision.upper()}`\n"
            f"🔄 **Nueva Resolución:** {nueva_res_str}\n"
            f"📝 **Motivo Admin:** {texto}\n\n"
            f"¿Desea registrar esta resolución?",
            reply_markup=teclado,
            parse_mode="Markdown"
        )
        return

    if "http://" in texto or "https://" in texto or "play.google.com" in texto:
        await update.message.reply_text(
            "📌 **¡Hola! Detectamos que enviaste un enlace de Play Store.**\n\n"
            "Para publicar tu aplicación en la Lista de Espera de TestCoin, por favor tocá el botón **🚀 Subir App**.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "🤖 **Menú Principal TestCoin**\n\n"
            "Por favor usá los botones de abajo para navegar:",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )

# --- MAIN: CONFIGURACIÓN DEL BOT ---
async def post_init(application: Application):
    """Configura los comandos sugeridos en Telegram dividiendo comandos públicos y de admin."""
    comandos_publicos = [
        BotCommand("start", "Iniciar bot"),
        BotCommand("verapps", "Ver apps disponibles"),
        BotCommand("postular", "Inscribirme como tester: /postular <app_id>"),
        BotCommand("miscreditos", "Ver mis créditos"),
        BotCommand("referidos", "Sistema de referidos"),
        BotCommand("rifa", "Rifa mensual"),
        BotCommand("miscampanas", "Ver mis campañas"),
        BotCommand("misapps", "Ver apps en prueba"),
        BotCommand("miscasos", "Ver mis casos y resoluciones"),
        BotCommand("casos", "Revisar casos de mi campaña"),
        BotCommand("apelar", "Apelar una resolución"),
    ]
    
    # 1. Menú público predeterminado para todos los usuarios
    await application.bot.set_my_commands(comandos_publicos)

    # 2. Menú exclusivo para Administradores
    comandos_admin = comandos_publicos + [
        BotCommand("admin", "Panel de Administración"),
        BotCommand("darcreditos", "Otorgar créditos a un usuario"),
        BotCommand("stats", "Estadísticas del bot"),
        BotCommand("sancionar", "Sancionar usuario"),
        BotCommand("advertir", "Advertir usuario"),
        BotCommand("hacerifa", "Realizar sorteo de la rifa"),
        BotCommand("apelaciones", "Panel de apelaciones pendientes"),
    ]
    
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                comandos_admin,
                scope=BotCommandScopeChat(chat_id=int(admin_id))
            )
        except Exception as e:
            print(f"⚠️ No se pudo registrar menú personalizado para el Admin {admin_id}: {e}")

# --- RUTINAS DEL PLANIFICADOR Y RECUPERACIÓN DE AVISOS Y CORTES ---
async def procesar_avisos_y_recuperacion(bot_context, clock=None, now_dt=None):
    """Procesa avisos programados y realiza recuperación de tareas pendientes o abandonadas.

    Acepta un proveedor de reloj sustituible ('clock') que devuelve la fecha/hora actual progresivamente.
    Obtiene la hora actual para cada reclamación y la vuelve a verificar inmediatamente antes de enviar por Telegram.
    Si ya se alcanzó o superó el corte (17:00 America/Mexico_City), no inicia el envío y conserva la fecha/identidad original.
    """
    if clock is None and now_dt is not None:
        clock = now_dt

    if clock is None:
        clock_fn = lambda: db.parse_dt_mexico(None)
    elif callable(clock):
        clock_fn = clock
    else:
        init_val = clock
        clock_fn = lambda: init_val

    try:
        now_dt = clock_fn()
    except RuntimeError as e:
        logger.error(f"Error al obtener zona horaria para procesar avisos: {e}")
        return {"avisos_enviados": 0, "faltas_marcadas": 0}

    fecha_hoy = now_dt.strftime("%Y-%m-%d")
    t_1500 = datetime.strptime("15:00:00", "%H:%M:%S").time()
    t_1630 = datetime.strptime("16:30:00", "%H:%M:%S").time()
    t_1700 = datetime.strptime("17:00:00", "%H:%M:%S").time()

    apps_data = cargar_archivo(APPS_FILE)
    avisos_enviados = 0

    if apps_data:
        for app_id, app in apps_data.items():
            if app.get("estado") != "en_prueba":
                continue

            testers = app.get("testers", [])

            for user_id in testers:
                user_id_str = str(user_id)

                curr_dt = clock_fn()
                curr_time = curr_dt.time()

                if curr_time >= t_1700:
                    logger.info(f"Corte alcanzado ({curr_dt}) al iterar tester {user_id_str}. Se omiten envíos posteriores.")
                    continue

                franjas_elegibles = []
                if t_1500 <= curr_time < t_1700:
                    franjas_elegibles.append("1500")
                if t_1630 <= curr_time < t_1700:
                    franjas_elegibles.append("1630")

                for tipo_aviso in franjas_elegibles:
                    dt_reclamo = clock_fn()
                    if dt_reclamo.time() >= t_1700:
                        break

                    rec = db.reclamar_aviso(app_id, user_id_str, fecha_hoy, tipo_aviso, now_dt=dt_reclamo)
                    if not rec.get("elegible"):
                        continue

                    token = rec.get("token_reclamacion")
                    if not token:
                        continue

                    dt_previo_envio = clock_fn()
                    if dt_previo_envio.time() >= t_1700:
                        logger.info(f"Corte alcanzado ({dt_previo_envio}) inmediatamente antes de send_message. No se envía aviso {tipo_aviso} a {user_id_str}.")
                        break

                    if not bot_context or not hasattr(bot_context, "bot") or bot_context.bot is None:
                        logger.warning(f"bot_context.bot no disponible. No se envía ni confirma aviso {tipo_aviso} para {user_id_str}.")
                        continue

                    msg = (
                        f"⏰ *Recordatorio de Check-in*\n\n"
                        f"App: *{app.get('descripcion', app_id)}*\n"
                        f"Fecha de entrega: *{fecha_hoy}*\n"
                        f"Por favor envía tu captura de pantalla antes de las 17:00 America/Mexico_City."
                    )

                    try:
                        await bot_context.bot.send_message(chat_id=int(user_id_str), text=msg, parse_mode="Markdown")
                    except Exception as exc:
                        logger.warning(f"Error al enviar aviso por Telegram a usuario {user_id_str}: {exc}")
                        continue

                    db.confirmar_aviso_enviado(app_id, user_id_str, fecha_hoy, tipo_aviso, token_reclamacion=token)
                    avisos_enviados += 1

    dt_final = clock_fn()
    faltas_marcadas = db.marcar_posibles_faltas(dt_final)
    return {"avisos_enviados": avisos_enviados, "faltas_marcadas": faltas_marcadas}


async def ejecutar_recuperacion_inicio(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rutina de recuperación ejecutada al iniciar el bot para procesar avisos e interrupciones."""
    logger.info("🔄 Ejecutando rutina de recuperación de inicio...")
    res = await procesar_avisos_y_recuperacion(context)
    logger.info(f"✅ Recuperación de inicio completada: {res['avisos_enviados']} avisos enviados, {res['faltas_marcadas']} faltas registradas.")


async def ejecutar_avisos_1500(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback diario a las 15:00 CDT."""
    now_dt = db.parse_dt_mexico(None)
    await procesar_avisos_y_recuperacion(context, now_dt=now_dt)


async def ejecutar_avisos_1630(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback diario a las 16:30 CDT."""
    now_dt = db.parse_dt_mexico(None)
    await procesar_avisos_y_recuperacion(context, now_dt=now_dt)


async def ejecutar_corte_1700(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback diario a las 17:00:01 CDT para marcar faltas."""
    now_dt = db.parse_dt_mexico(None)
    db.marcar_posibles_faltas(now_dt)


async def ejecutar_procesamiento_periodico(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesamiento periódico cada 5 minutos de avisos abandonados o pendientes."""
    now_dt = db.parse_dt_mexico(None)
    await procesar_avisos_y_recuperacion(context, now_dt=now_dt)


def main():
    """Inicializa el bot de Telegram con soporte 100% de Botones Teclado."""
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv_start = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTRO_TIPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, seleccion_tipo)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    conv_subirapp = ConversationHandler(
        entry_points=[
            CommandHandler("subirapp", subirapp_start),
            MessageHandler(filters.Regex("^🚀 Subir App$"), subirapp_start)
        ],
        states={
            SUBIR_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, subirapp_link)],
            SUBIR_DESCRIPCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, subirapp_descripcion)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    conv_confirmar = ConversationHandler(
        entry_points=[
            CommandHandler("confirmar", confirmar_start),
            MessageHandler(filters.Regex("^📸 Confirmar Instalación$"), confirmar_start)
        ],
        states={
            CONFIRMAR_FOTO: [MessageHandler(filters.PHOTO, confirmar_foto)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    conv_checkin = ConversationHandler(
        entry_points=[
            CommandHandler("checkin", checkin_start),
            MessageHandler(filters.Regex("^📝 Check-in Diario$"), checkin_start)
        ],
        states={
            CHECKIN_FOTO: [
                MessageHandler(filters.PHOTO, checkin_foto),
                MessageHandler(filters.TEXT & ~filters.COMMAND, checkin_foto)
            ],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(conv_start)
    app.add_handler(conv_subirapp)
    app.add_handler(conv_confirmar)
    app.add_handler(conv_checkin)

    # Handlers para Comandos y Botones Directos
    app.add_handler(CommandHandler("verapps", verapps))
    app.add_handler(MessageHandler(filters.Regex("^📱 Ver Apps$"), verapps))
    app.add_handler(CommandHandler("postular", postular))

    app.add_handler(CommandHandler("miscreditos", miscreditos))
    app.add_handler(CommandHandler("creditos", miscreditos))
    app.add_handler(MessageHandler(filters.Regex("^📊 Mis Créditos$"), miscreditos))

    app.add_handler(CommandHandler("referidos", referidos))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referidos$"), referidos))

    app.add_handler(CommandHandler("rifa", rifa))
    app.add_handler(MessageHandler(filters.Regex("^🎟️ Rifa Mensual$"), rifa))

    app.add_handler(CommandHandler("miscampanas", miscampanas))
    app.add_handler(MessageHandler(filters.Regex("^📢 Mis Campañas$"), miscampanas))

    app.add_handler(CommandHandler("misapps", misapps))
    app.add_handler(MessageHandler(filters.Regex("^📲 Mis Apps en Prueba$"), misapps))

    app.add_handler(CommandHandler("settimezone", settimezone))

    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(MessageHandler(filters.Regex("^🛠️ Panel Admin$"), admin))

    # Otros comandos de administración y utilidades
    app.add_handler(CommandHandler("iniciarprueba", iniciarprueba))
    app.add_handler(CommandHandler("comprarcreditos", comprarcreditos))
    app.add_handler(CommandHandler("hacerifa", hacerifa))
    app.add_handler(CommandHandler("ajustardias", ajustardias))
    app.add_handler(CommandHandler("advertir", advertirtester))
    app.add_handler(CommandHandler("sancionar", sancionartester))
    app.add_handler(CommandHandler("darcreditos", darcreditos))
    app.add_handler(CommandHandler("regalarcampana", regalarcampana))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("casos", casos))
    app.add_handler(CommandHandler("miscasos", miscasos))
    app.add_handler(CommandHandler("apelar", apelar))
    app.add_handler(CommandHandler("apelaciones", apelaciones))
    app.add_handler(CommandHandler("conciliacion", conciliacion))
    app.add_handler(CallbackQueryHandler(manejar_callback_query))

    # Captura genérica de texto
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_texto_generico))

    # Configuración de tareas programadas (Avisos de 15:00, 16:30 y Corte 17:00 America/Mexico_City)
    if app.job_queue:
        import datetime as dt_module
        tz_cdmx = db.get_mexico_city_tz()
        app.job_queue.run_once(ejecutar_recuperacion_inicio, when=1)
        app.job_queue.run_daily(ejecutar_avisos_1500, time=dt_module.time(15, 0, 0, tzinfo=tz_cdmx))
        app.job_queue.run_daily(ejecutar_avisos_1630, time=dt_module.time(16, 30, 0, tzinfo=tz_cdmx))
        app.job_queue.run_daily(ejecutar_corte_1700, time=dt_module.time(17, 0, 1, tzinfo=tz_cdmx))
        app.job_queue.run_repeating(ejecutar_procesamiento_periodico, interval=300, first=10)
    else:
        logger.error("⚠️ CRÍTICO: JobQueue no está disponible. Los avisos programados y el corte de las 17:00 (America/Mexico_City) están DESHABILITADOS. El bot NO está listo para piloto.")
        print("⚠️ CRÍTICO: JobQueue no disponible. Avisos y tareas automáticas deshabilitados. Bot NO apto para piloto.")

    # Manejador global de errores
    app.add_error_handler(error_handler)

    listen_mode = os.getenv("LISTEN_MODE", "polling").lower()
    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")

    if listen_mode == "webhook" and webhook_url:
        url_path = f"telegram/{TOKEN}"
        full_webhook_url = f"{webhook_url.rstrip('/')}/{url_path}"
        logger.info(f"Modo Webhook activado: {full_webhook_url}")
        app.run_webhook(
            listen=host,
            port=port,
            url_path=url_path,
            webhook_url=full_webhook_url
        )
    else:
        logger.info("TestCoin Bot corriendo en Modo Polling...")
        app.run_polling()

if __name__ == "__main__":
    main()
