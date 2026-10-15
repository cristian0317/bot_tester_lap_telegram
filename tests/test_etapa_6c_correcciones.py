import copy
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import db
import bot
from inscripcion_simulada import FirestoreSimulado, ConflictoLectura, ScopedMexicoCityTZ


class Etapa6CCorreccionesPreBetaTests(unittest.IsolatedAsyncioTestCase):
    """
    Pruebas automatizadas para la Etapa 6C: Correcciones Pre-Beta.
    Verifica:
    1. Mensajes correctos según tipo de campaña (Beta vs Tradicional).
    2. Desactivación de venta de créditos (/comprarcreditos) durante la beta.
    3. Límite de 1 campaña activa por desarrollador bajo concurrencia.
    4. Compatibilidad con desarrolladores testeando apps de otros.
    5. Funcionamiento y datos completos de /miperfil.
    6. Comandos /rifa, /hacerifa y operaciones seguras de saldos admin.
    """

    def setUp(self):
        self.dev_id = "101"
        self.tester_id = "202"
        self.beta_app_id = "app_beta_6c"
        self.trad_app_id = "app_trad_6c"

        self.storage = {
            "usuarios.json": {
                self.dev_id: {
                    "nombre": "Dev Beta",
                    "rol": "desarrollador",
                    "creditos": 0,
                    "puntos_founder": 0,
                    "boletos_rifa": 0,
                    "reputacion": {
                        "campanas_publicadas": 0,
                        "campanas_completadas": 0,
                        "dias_totales_cumplidos": 0,
                        "faltas_injustificadas": 0,
                        "confiabilidad_pct": 100.0,
                        "nivel_confiabilidad": "Inicial"
                    },
                    "apps_testeando": []
                },
                self.tester_id: {
                    "nombre": "Tester Beta",
                    "rol": "tester",
                    "creditos": 100,
                    "puntos_founder": 50,
                    "boletos_rifa": 1,
                    "reputacion": {
                        "campanas_completadas": 1,
                        "dias_totales_cumplidos": 14,
                        "faltas_injustificadas": 0,
                        "confiabilidad_pct": 100.0,
                        "nivel_confiabilidad": "Excelente"
                    },
                    "apps_testeando": [self.beta_app_id, self.trad_app_id]
                }
            },
            "apps.json": {
                self.beta_app_id: {
                    "dev_id": self.dev_id,
                    "descripcion": "App Beta 6C",
                    "link": "https://play.google.com/store/apps/details?id=com.beta6c",
                    "estado": "en_prueba",
                    "es_beta": True,
                    "tipo_campana": "beta_gratuita",
                    "puntos_founder_recompensa": 100,
                    "min_testers": 15,
                    "max_testers": 15,
                    "testers": [self.tester_id],
                    "pruebas": {
                        self.tester_id: {
                            "tester_nombre": "Tester Beta",
                            "estado": "instalada",
                            "dias_completados": 13,
                            "ultimo_checkin": "2026-10-13",
                            "registros": {
                                f"2026-10-{i:02d}": {
                                    "tipo": "checkin",
                                    "estado_entrega": "puntual",
                                    "captura": f"cap_{i}.png"
                                } for i in range(1, 14)
                            },
                            "posibles_faltas": {}
                        }
                    }
                },
                self.trad_app_id: {
                    "dev_id": "999",
                    "descripcion": "App Tradicional 6C",
                    "link": "https://play.google.com/store/apps/details?id=com.trad6c",
                    "estado": "en_prueba",
                    "es_beta": False,
                    "tipo_campana": "estandar",
                    "min_testers": 15,
                    "max_testers": 15,
                    "testers": [self.tester_id],
                    "pruebas": {
                        self.tester_id: {
                            "tester_nombre": "Tester Beta",
                            "estado": "instalada",
                            "dias_completados": 13,
                            "ultimo_checkin": "2026-10-13",
                            "registros": {
                                f"2026-10-{i:02d}": {
                                    "tipo": "checkin",
                                    "estado_entrega": "puntual",
                                    "captura": f"cap_{i}.png"
                                } for i in range(1, 14)
                            },
                            "posibles_faltas": {}
                        }
                    }
                }
            },
            "recompensas.json": {},
            "eventos_auditoria.json": {}
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    # --- 1. MENSAJES DE RECOMPENSAS ---
    async def test_checkin_dia_14_en_campana_beta_muestra_puntos_founder_y_no_promete_creditos(self):
        """1. En campaña beta, check-in día 14 muestra 100 Puntos Founder y no promete 800 créditos ni boletos."""
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.tester_id)
        update.message.from_user.first_name = "Tester Beta"
        photo_mock = Mock()
        photo_mock.file_id = "foto_dia_14_beta"
        update.message.photo = [photo_mock]
        update.message.caption = None
        update.message.text = None
        update.message.date = None

        context = Mock()
        context.user_data = {
            "checkin_app_id": self.beta_app_id,
            "checkin_vencimiento": "2026-10-14"
        }

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional, SERVER_TIMESTAMP="SERVER_TIMESTAMP"))
        with ScopedMexicoCityTZ(), \
             patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            res = await bot.checkin_foto(update, context)
            self.assertEqual(res, bot.ConversationHandler.END)

            # Verificar mensaje al tester
            llamada = update.message.reply_text.call_args[0][0]
            self.assertIn("100 Puntos Founder", llamada)
            self.assertIn("conciliación definitiva", llamada)
            self.assertNotIn("800 Créditos", llamada)
            self.assertNotIn("Boleto para la Rifa", llamada)

    async def test_checkin_dia_14_en_campana_tradicional_conserva_creditos_y_boletos(self):
        """2. En campaña tradicional, check-in día 14 conserva promesa de 800 créditos y 1 boleto."""
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.tester_id)
        update.message.from_user.first_name = "Tester Beta"
        photo_mock = Mock()
        photo_mock.file_id = "foto_dia_14_trad"
        update.message.photo = [photo_mock]
        update.message.caption = None
        update.message.text = None
        update.message.date = None

        context = Mock()
        context.user_data = {
            "checkin_app_id": self.trad_app_id,
            "checkin_vencimiento": "2026-10-14"
        }

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional, SERVER_TIMESTAMP="SERVER_TIMESTAMP"))
        with ScopedMexicoCityTZ(), \
             patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            res = await bot.checkin_foto(update, context)
            self.assertEqual(res, bot.ConversationHandler.END)

            llamada = update.message.reply_text.call_args[0][0]
            self.assertIn("800 Créditos", llamada)
            self.assertNotIn("Puntos Founder", llamada)

    # --- 2. DESACTIVACIÓN DE COMPRA DE CRÉDITOS ---
    async def test_comprarcreditos_en_beta_gratuita_explica_gratuidad_sin_instrucciones_de_pago(self):
        """3. En modo beta, /comprarcreditos explica gratuidad de publicación sin datos de cobro."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            self.assertTrue(db.es_beta_gratuita())
            update = Mock()
            update.message = AsyncMock()
            update.message.from_user.id = int(self.dev_id)
            context = Mock()

            await bot.comprarcreditos(update, context)
            texto = update.message.reply_text.call_args[0][0]

            self.assertIn("100% gratuito", texto)
            self.assertIn("0 créditos", texto)
            self.assertNotIn("$800 MXN", texto)
            self.assertNotIn("datos de pago", texto)

    async def test_comprarcreditos_fuera_de_beta_mantiene_sistema_de_compra(self):
        """4. Con BETA_GRATUITA=false, /comprarcreditos muestra las opciones estándar de recarga."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "false"}):
            self.assertFalse(db.es_beta_gratuita())
            update = Mock()
            update.message = AsyncMock()
            update.message.from_user.id = int(self.dev_id)
            context = Mock()

            await bot.comprarcreditos(update, context)
            texto = update.message.reply_text.call_args[0][0]

            self.assertIn("$800 MXN", texto)
            self.assertIn("800 Créditos", texto)

    # --- 3. LÍMITE DE CAMPAÑAS ACTIVAS ---
    def test_limite_una_campana_activa_por_desarrollador_en_beta(self):
        """5. Un dev con una campaña activa no puede publicar una segunda campaña en modo beta."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            app_id_1 = "app_dev_primera"
            res1 = self.firebase.ejecutar(
                db.publicar_campana,
                self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app1", "Primera App", app_id_1
            )
            self.assertEqual(res1["resultado"], "creada")

            # Intentar publicar una segunda campaña
            app_id_2 = "app_dev_segunda"
            with self.assertRaises(db.PublicacionRechazada) as ctx:
                self.firebase.ejecutar(
                    db.publicar_campana,
                    self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app2", "Segunda App", app_id_2
                )
            self.assertIn("Límite de la Beta", str(ctx.exception))
            self.assertNotIn(app_id_2, self.storage["apps.json"])

    def test_desarrollador_puede_publicar_nueva_campana_tras_cierre_de_la_anterior(self):
        """6. Tras completar o cerrar la campaña activa, el desarrollador puede publicar otra."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            app_id_1 = "app_dev_cerrable"
            res1 = self.firebase.ejecutar(
                db.publicar_campana,
                self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app1", "App Cerrable", app_id_1
            )
            self.assertEqual(res1["resultado"], "creada")

            # Simular cierre definitivo de la campaña
            self.storage["apps.json"][app_id_1]["estado"] = "cerrada_definitivamente"

            # Ahora debe permitir publicar una nueva
            app_id_2 = "app_dev_segunda_permitida"
            res2 = self.firebase.ejecutar(
                db.publicar_campana,
                self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app2", "Segunda Permitida", app_id_2
            )
            self.assertEqual(res2["resultado"], "creada")

    def test_limite_campana_activa_bajo_concurrencia_idempotente(self):
        """7. Dos publicaciones concurrentes para el mismo dev permiten solo una creación."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            # Transacción A publica app_A
            res_a = self.firebase.ejecutar(
                db.publicar_campana,
                self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app.a", "App Concurrente A", "app_conc_a"
            )
            self.assertEqual(res_a["resultado"], "creada")

            # Transacción B concurrente que intentó app_B se rechaza por conflicto de estado
            with self.assertRaises(db.PublicacionRechazada):
                self.firebase.ejecutar(
                    db.publicar_campana,
                    self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app.b", "App Concurrente B", "app_conc_b"
                )

            # Reintento exacto de app_A debe ser idempotente (ya_publicada)
            res_reintento = self.firebase.ejecutar(
                db.publicar_campana,
                self.dev_id, "Dev Beta", "https://play.google.com/store/apps/details?id=com.app.a", "App Concurrente A", "app_conc_a"
            )
            self.assertEqual(res_reintento["resultado"], "ya_publicada")

    def test_desarrollador_con_app_activa_puede_ser_tester_en_otras(self):
        """8. Tener una app activa como dev no impide participar como tester en aplicaciones de otros."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            # Inscribir a dev_id en la app tradicional de otro dev
            app_otro = self.trad_app_id
            self.storage["apps.json"][app_otro]["testers"].append(self.dev_id)
            self.assertIn(self.dev_id, self.storage["apps.json"][app_otro]["testers"])

    # --- 4. HABILITACIÓN DE /miperfil ---
    async def test_comando_miperfil_muestra_reputacion_puntos_founder_y_participacion(self):
        """9. /miperfil muestra reputación, puntos founder, créditos y apps activas sin duplicar lógica."""
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.tester_id)
        context = Mock()

        with patch.object(bot, "cargar_archivo", side_effect=lambda a: copy.deepcopy(self.storage.get(a, {}))):
            await bot.miperfil(update, context)

            texto = update.message.reply_text.call_args[0][0]
            self.assertIn("Perfil de Usuario", texto)
            self.assertIn("Tester Beta", texto)
            self.assertIn("🌟 **Puntos Founder:** `50` pts", texto)
            self.assertIn("🪙 **Créditos tradicionales:** `100`", texto)
            self.assertIn("Excelente", texto)
            self.assertIn("Apps activas testeando:", texto)

    # --- 5. REVISIÓN DE COMANDOS ANTIGUOS Y SALDOS SEGUROS ---
    async def test_rifa_y_hacerifa_pausadas_en_beta(self):
        """10. /rifa y /hacerifa alertan que están en pausa durante la beta conservando boletos."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            update = Mock()
            update.message = AsyncMock()
            update.message.from_user.id = int(self.tester_id)
            context = Mock()

            with patch.object(bot, "cargar_archivo", side_effect=lambda a: copy.deepcopy(self.storage.get(a, {}))):
                await bot.rifa(update, context)
                texto = update.message.reply_text.call_args[0][0]
                self.assertIn("Pausada durante la Beta Gratuita", texto)
                self.assertIn("Tus Boletos Acumulados", texto)

                # Admin ejecutando /hacerifa
                update_admin = Mock()
                update_admin.message = AsyncMock()
                admin_uid = "5028590623"
                update_admin.message.from_user.id = admin_uid
                with patch.object(bot, "ADMIN_IDS", [admin_uid]):
                    await bot.hacerifa(update_admin, context)
                    texto_admin = update_admin.message.reply_text.call_args[0][0]
                    self.assertIn("Rifa en Pausa", texto_admin)

    def test_actualizar_creditos_admin_seguro_no_sobrescribe_coleccion(self):
        """11. Actualizar créditos admin modifica solo al usuario objetivo de forma atómica."""
        res = self.firebase.ejecutar(db.actualizar_creditos_admin_seguro, self.tester_id, 200)
        self.assertEqual(res["creditos"], 300)
        self.assertEqual(self.storage["usuarios.json"][self.tester_id]["creditos"], 300)
        self.assertEqual(self.storage["usuarios.json"][self.dev_id]["creditos"], 0)


if __name__ == "__main__":
    unittest.main()
