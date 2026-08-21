from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import json
import os
import random
import logging
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
logger = logging.getLogger(__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra en bot.log cualquier excepción no controlada que ocurra en el bot."""
    logger.error("Excepción ocurrida al procesar la actualización:", exc_info=context.error)

# Token del Bot de Telegram (Obtenido de variable de entorno o fallback)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TOKEN", "8145425963:AAEsR5z-k0FyWXh-QOlMFQ-p_eqXl22TZHA"))

# Estados de las conversaciones
REGISTRO_TIPO = 1
SUBIR_LINK = 3
SUBIR_DESCRIPCION = 4
CONFIRMAR_FOTO = 10
CHECKIN_FOTO = 11

# Rutas de los archivos JSON de almacenamiento
USUARIOS_FILE = "usuarios.json"
APPS_FILE = "apps.json"

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
    """Finaliza el registro de la app en Lista de Espera, descuenta 800 créditos y restablece botones."""
    descripcion_enviada = update.message.text.strip()
    context.user_data["app_descripcion"] = descripcion_enviada
    user_id = str(update.message.from_user.id)
    nombre = update.message.from_user.first_name

    usuarios = cargar_archivo(USUARIOS_FILE)
    usuario = usuarios.get(user_id, {})

    if usuario.get("creditos", 0) >= 800:
        usuario["creditos"] -= 800
        usuario["boletos_rifa"] = usuario.get("boletos_rifa", 0) + 1
        guardar_archivo(USUARIOS_FILE, usuarios)
    else:
        await update.message.reply_text("❌ Ocurrió un error con tus créditos. Operación cancelada.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    apps = cargar_archivo(APPS_FILE)
    app_id = f"app_{user_id}_{len(apps)+1}"
    app_link = context.user_data["app_link"]

    apps[app_id] = {
        "dev_id": user_id,
        "dev_nombre": nombre,
        "link": app_link,
        "descripcion": descripcion_enviada,
        "estado": "reclutando",
        "testers": [],
        "min_testers": 12,
        "max_testers": 15,
        "dias_requeridos": 14,
        "pruebas": {}
    }
    guardar_archivo(APPS_FILE, apps)

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
    return ConversationHandler.END

# --- VER APPS (BOTÓN "📱 Ver Apps") ---
async def verapps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la lista de aplicaciones en espera o reclutando testers."""
    user_id = str(update.message.from_user.id)
    apps = cargar_archivo(APPS_FILE)

    apps_disponibles = [
        (app_id, datos) for app_id, datos in apps.items()
        if datos.get("estado") in ["reclutando", "lista"] and len(datos.get("testers", [])) < datos.get("max_testers", 15)
    ]

    if not apps_disponibles:
        await update.message.reply_text(
            "📱 **Catálogo de Apps**\n\n"
            "Por el momento no hay aplicaciones buscando testers.\n"
            "¡Volvé a consultar más tarde!",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return

    mensaje = "📱 **Lista de Espera de Apps para Testear**\n\n"
    mensaje += "Súmate como tester a las campañas. Se necesitan al menos 12 testers para iniciar la prueba conjunta de 14 días:\n\n"

    for app_id, datos in apps_disponibles:
        num_testers = len(datos.get("testers", []))
        min_testers = datos.get("min_testers", 12)
        max_testers = datos.get("max_testers", 15)
        estado = datos.get("estado", "reclutando")

        if estado == "reclutando":
            indicador = f"🟡 **Reclutando:** `{num_testers}/{min_testers}` mínimos (*Faltan {min_testers - num_testers} testers*)"
        else:
            indicador = f"🟢 **¡Lista para Iniciar!** `{num_testers}/{max_testers}` testers inscritos"

        mensaje += (
            f"🔹 **{datos.get('descripcion', 'Sin descripción')}**\n"
            f"🔗 **Link:** {datos.get('link')}\n"
            f"📊 **Estado:** {indicador}\n"
            f"🆔 **ID:** `{app_id}`\n"
            f"👉 Para unirte escribí: `/postular {app_id}`\n\n"
        )

    await update.message.reply_text(mensaje, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

# --- INICIAR PRUEBA (DESARROLLADOR) ---
async def iniciarprueba(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permite al desarrollador iniciar la prueba de 14 días cuando ya juntó los 12 testers requeridos."""
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
            f"🟡 **Aún no juntás los 12 testers mínimos**\n\n"
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
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    app["fecha_lanzamiento"] = fecha_hoy
    guardar_archivo(APPS_FILE, apps)

    await update.message.reply_text(
        f"🚀 **¡PRUEBA INICIADA EXITOSAMENTE!**\n\n"
        f"📱 **App:** {app.get('descripcion')}\n"
        f"👥 **Testers participantes:** `{num_testers}`\n"
        f"📅 **Fecha de lanzamiento:** `{fecha_hoy}`\n\n"
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
                    f"⏰ **HORARIO DE CORTE:** Recordá enviar tus capturas diarias antes de las **5:00 PM**."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

# --- CONFIRMACIÓN DE INSTALACIÓN ---
async def confirmar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el proceso para recibir la captura de pantalla de instalación."""
    user_id = str(update.message.from_user.id)
    usuarios = cargar_archivo(USUARIOS_FILE)

    if user_id not in usuarios:
        await update.message.reply_text("❌ No estás registrado. Usá /start para registrarte primero.")
        return ConversationHandler.END

    usuario = usuarios[user_id]
    apps_testeando = usuario.get("apps_testeando", [])

    if not apps_testeando:
        await update.message.reply_text(
            "❌ No estás postulado en ninguna app. Postulate primero en **📱 Ver Apps**.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if context.args:
        app_id = context.args[0]
        if app_id not in apps_testeando:
            await update.message.reply_text(f"❌ No estás postulado en la app `{app_id}`.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
            return ConversationHandler.END
    else:
        app_id = apps_testeando[-1]

    context.user_data["confirmar_app_id"] = app_id
    apps = cargar_archivo(APPS_FILE)
    app_info = apps.get(app_id, {})

    await update.message.reply_text(
        f"📸 **Confirmar Instalación de la App**\n\n"
        f"📱 **App:** {app_info.get('descripcion', app_id)}\n\n"
        f"Por favor, enviá una **captura de pantalla** donde se vea la aplicación instalada en tu teléfono.\n"
        f"⏰ *Recordá enviarla antes de las 5:00 PM.*",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return CONFIRMAR_FOTO

async def confirmar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la captura de pantalla de instalación."""
    user_id = str(update.message.from_user.id)
    nombre = update.message.from_user.first_name
    app_id = context.user_data.get("confirmar_app_id")

    if not app_id:
        await update.message.reply_text("❌ Ocurrió un error.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    foto_file_id = update.message.photo[-1].file_id
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")

    apps = cargar_archivo(APPS_FILE)
    if app_id not in apps:
        await update.message.reply_text("❌ La app especificada no fue encontrada.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    app = apps[app_id]
    if "pruebas" not in app:
        app["pruebas"] = {}

    app["pruebas"][user_id] = {
        "tester_nombre": nombre,
        "estado": "instalada",
        "fecha_inicio": fecha_hoy,
        "captura_inicial": foto_file_id,
        "dias_completados": 1,
        "ultimo_checkin": fecha_hoy
    }
    guardar_archivo(APPS_FILE, apps)

    await update.message.reply_text(
        f"✅ **¡Instalación confirmada exitosamente!**\n\n"
        f"📱 **App:** {app.get('descripcion')}\n"
        f"📅 **Fecha de Inicio:** `{fecha_hoy}`\n"
        f"⏳ **Días activos requeridos:** 14 días\n"
        f"📊 **Días completados:** 1/14\n\n"
        f"💡 Usá el botón **📝 Check-in Diario** todos los días antes de las 5:00 PM.",
        reply_markup=obtener_teclado_principal(user_id),
        parse_mode="Markdown"
    )

    dev_id = app.get("dev_id")
    if dev_id:
        try:
            await context.bot.send_message(
                chat_id=dev_id,
                text=(
                    f"🔔 **¡Nuevo tester activado!**\n\n"
                    f"👤 Tester **{nombre}** ha confirmed la instalación de tu app:\n"
                    f"📝 **App:** {app.get('descripcion')}\n"
                    f"📅 **Fecha de inicio:** `{fecha_hoy}`"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    return ConversationHandler.END

# --- CHECK-IN DIARIO ---
async def checkin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia la recolección de captura diaria."""
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

    if context.args:
        app_id = context.args[0]
    else:
        app_id = apps_testeando[-1]

    apps = cargar_archivo(APPS_FILE)
    if app_id not in apps:
        await update.message.reply_text("❌ La app no fue encontrada.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    app = apps[app_id]
    prueba = app.get("pruebas", {}).get(user_id, {})

    if not prueba or prueba.get("estado") != "instalada":
        await update.message.reply_text("⚠️ Primero debes enviar tu captura inicial tocando el botón **📸 Confirmar Instalación**.", reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")
        return ConversationHandler.END

    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    if prueba.get("ultimo_checkin") == fecha_hoy:
        await update.message.reply_text(
            f"ℹ️ **¡Ya realizaste tu check-in diario de hoy!**\n\n"
            f"📊 Progreso actual: `{prueba.get('dias_completados', 1)}/14` días completados.\n"
            f"Volvé mañana antes de las **5:00 PM** para la siguiente captura.",
            reply_markup=obtener_teclado_principal(user_id),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data["checkin_app_id"] = app_id
    await update.message.reply_text(
        f"📸 **Check-in Diario de Prueba**\n\n"
        f"📱 **App:** {app.get('descripcion')}\n"
        f"📊 **Progreso actual:** `{prueba.get('dias_completados', 1)}/14` días.\n\n"
        f"Por favor, enviá una captura de pantalla del día de hoy mostrando la aplicación instalada en tu teléfono.\n"
        f"⏰ *Límite recomendado de recepción: antes de las 5:00 PM.*",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return CHECKIN_FOTO

async def checkin_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la captura diaria."""
    user_id = str(update.message.from_user.id)
    nombre = update.message.from_user.first_name
    app_id = context.user_data.get("checkin_app_id")

    if not app_id:
        await update.message.reply_text("❌ Ocurrió un error.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    foto_file_id = update.message.photo[-1].file_id
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")

    apps = cargar_archivo(APPS_FILE)
    app = apps.get(app_id, {})
    prueba = app.get("pruebas", {}).get(user_id, {})

    if not prueba:
        await update.message.reply_text("❌ Ocurrió un error con tu prueba.", reply_markup=obtener_teclado_principal(user_id))
        return ConversationHandler.END

    dias_actuales = prueba.get("dias_completados", 1) + 1
    prueba["dias_completados"] = dias_actuales
    prueba["ultimo_checkin"] = fecha_hoy
    prueba["ultima_captura"] = foto_file_id
    guardar_archivo(APPS_FILE, apps)

    if dias_actuales >= 14 and prueba.get("estado") != "completada":
        prueba["estado"] = "completada"
        guardar_archivo(APPS_FILE, apps)

        usuarios = cargar_archivo(USUARIOS_FILE)
        if user_id in usuarios:
            usuarios[user_id]["creditos"] = usuarios[user_id].get("creditos", 0) + 800
            usuarios[user_id]["boletos_rifa"] = usuarios[user_id].get("boletos_rifa", 0) + 1
            guardar_archivo(USUARIOS_FILE, usuarios)

        await update.message.reply_text(
            f"🎉 **¡FELICITACIONES! HAS COMPLETADO LOS 14 DÍAS DE PRUEBA** 🏆\n\n"
            f"📱 **App:** {app.get('descripcion')}\n"
            f"🪙 **Recompensa Otorgada:** +800 Créditos\n"
            f"🎟️ **Premio Extra:** +1 Boleto para la Rifa Mensual\n\n"
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
        await update.message.reply_text(
            f"✅ **¡Check-in diario registrado exitosamente!**\n\n"
            f"📱 **App:** {app.get('descripcion')}\n"
            f"📊 **Progreso actual:** `{dias_actuales}/14` días completados.\n"
            f"📅 **Fecha:** `{fecha_hoy}`\n\n"
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
            if estado_app == "reclutando":
                indicador = "🟡 En lista de espera (esperando 12 testers)"
            elif estado_app == "lista":
                indicador = "🟢 Lista para iniciar (esperando lanzamiento del Dev)"
            else:
                indicador = f"🚀 En prueba activa ({dias}/14 días)"
            mensaje += (
                f"🔹 **{app.get('descripcion', 'Sin descripción')}**\n"
                f"🔗 Link: {app.get('link')}\n"
                f"📊 Estado campaña: {indicador}\n"
                f"📌 Tu estado: `{estado_prueba}`\n"
                f"📅 Fecha inicio: `{fecha_inicio}`\n"
                f"🆔 ID: `{app_id}`\n\n"
            )
    await update.message.reply_text(mensaje, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

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
        max_testers = datos.get("max_testers", 15)
        estado = datos.get("estado", "reclutando")
        pruebas_activas = len([p for p in datos.get("pruebas", {}).values() if p.get("estado") in ["instalada", "completada"]])

        if estado == "reclutando":
            estado_txt = f"🟡 **En lista de espera** (`{num_testers}/{min_testers}` mín.)"
            accion = f"💡 *Se notificará cuando se unan los 12 testers.*"
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

    if len(context.args) < 3:
        await update.message.reply_text("⚠️ **Uso del comando:** `/ajustardias <app_id> <tester_user_id> <dias_completados>`", parse_mode="Markdown")
        return

    app_id = context.args[0]
    tester_id = context.args[1]
    try:
        nuevos_dias = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ El número de días debe ser un entero.")
        return

    apps = cargar_archivo(APPS_FILE)
    if app_id not in apps:
        await update.message.reply_text("❌ La campaña no fue encontrada.")
        return

    app = apps[app_id]
    if app.get("dev_id") != user_id and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tenés permisos para ajustar los días de esta campaña.")
        return

    pruebas = app.get("pruebas", {})
    if tester_id not in pruebas:
        await update.message.reply_text(f"❌ El tester `{tester_id}` no ha iniciado la prueba en esta app.", parse_mode="Markdown")
        return

    pruebas[tester_id]["dias_completados"] = nuevos_dias
    guardar_archivo(APPS_FILE, apps)

    await update.message.reply_text(
        f"🔄 **¡Días de prueba ajustados con éxito!**\n\n"
        f"📱 **App:** {app.get('descripcion')}\n"
        f"👤 **Tester ID:** `{tester_id}`\n"
        f"📊 **Nuevo contador:** `{nuevos_dias}/14` días completados.",
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

    elif tipo_sancion == "expulsar":
        if len(context.args) < 3:
            await update.message.reply_text("⚠️ Especificá el ID de la app.", parse_mode="Markdown")
            return
        app_id = context.args[2]
        apps = cargar_archivo(APPS_FILE)
        if app_id not in apps:
            await update.message.reply_text(f"❌ La app `{app_id}` no existe.", parse_mode="Markdown")
            return
        app = apps[app_id]
        if target_tester_id in app.get("testers", []):
            app["testers"].remove(target_tester_id)
        if "pruebas" in app and target_tester_id in app["pruebas"]:
            del app["pruebas"][target_tester_id]
        if len(app["testers"]) < app.get("min_testers", 12) and app.get("estado") == "lista":
            app["estado"] = "reclutando"
        guardar_archivo(APPS_FILE, apps)
        if app_id in tester.get("apps_testeando", []):
            tester["apps_testeando"].remove(app_id)
            guardar_archivo(USUARIOS_FILE, usuarios)
        msg_admin = f"🚨 Tester {tester['nombre']} fue expulsado de `{app_id}`."

    elif tipo_sancion == "resetear":
        tester["creditos"] = 0
        tester["boletos_rifa"] = 0
        guardar_archivo(USUARIOS_FILE, usuarios)
        msg_admin = f"🚨 Cuenta de {tester['nombre']} reseteada."
    else:
        await update.message.reply_text("❌ Tipo de sanción no reconocido.", parse_mode="Markdown")
        return

    await update.message.reply_text(msg_admin, reply_markup=obtener_teclado_principal(user_id), parse_mode="Markdown")

# --- MANEJADOR DE TEXTOS Y BOTONES GENÉRICOS ---
async def mensaje_texto_generico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Atiende mensajes de texto que coincidan con los botones o detecte enlaces enviando la respuesta con botones."""
    texto = update.message.text.strip()
    user_id = str(update.message.from_user.id)

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
        BotCommand("miscreditos", "Ver mis créditos"),
        BotCommand("referidos", "Sistema de referidos"),
        BotCommand("rifa", "Rifa mensual"),
        BotCommand("miscampanas", "Ver mis campañas"),
        BotCommand("misapps", "Ver apps en prueba"),
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
    ]
    
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                comandos_admin,
                scope=BotCommandScopeChat(chat_id=int(admin_id))
            )
        except Exception as e:
            print(f"⚠️ No se pudo registrar menú personalizado para el Admin {admin_id}: {e}")

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
            CHECKIN_FOTO: [MessageHandler(filters.PHOTO, checkin_foto)],
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

    # Captura genérica de texto
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_texto_generico))

    # Manejador global de errores
    app.add_error_handler(error_handler)

    listen_mode = os.getenv("LISTEN_MODE", "polling").lower()
    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")

    if listen_mode == "webhook" and webhook_url:
        url_path = f"telegram/{TOKEN}"
        full_webhook_url = f"{webhook_url.rstrip('/')}/{url_path}"
        print(f"🚀 Iniciando TestCoin Bot en Modo Webhook ({host}:{port})...")
        logger.info(f"Modo Webhook activado: {full_webhook_url}")
        app.run_webhook(
            listen=host,
            port=port,
            url_path=url_path,
            webhook_url=full_webhook_url
        )
    else:
        print("✅ TestCoin Bot corriendo en Modo Polling...")
        app.run_polling()

if __name__ == "__main__":
    main()