"""Pruebas para la ETAPA 6A: TestCoin Beta Comunitaria & Programa Founders.
Verifica:
1. Publicación gratuita en modo beta (BETA_GRATUITA=true) sin cobrar créditos ni dinero.
2. Preservación estricta de saldos de créditos existentes.
3. Liquidación con Puntos Founder independientes para campañas beta.
4. Compatibilidad con campañas estándar anteriores (800 créditos y 1 boleto).
5. Sistema de reputación permanente transaccional y métricas de confiabilidad.
6. Test for Test (T4T) voluntario sin coacción ni requisitos previos.
7. Handlers de Telegram integrados (/subirapp, /creditos, /t4t).
"""

import copy
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import db
import bot
from inscripcion_simulada import FirestoreSimulado


class Etapa6ABetaFoundersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dev_id = "100"
        self.tester_id = "200"
        self.beta_app_id = "app_beta_100"
        self.legacy_app_id = "app_legacy_100"
        self.dt_now = datetime(2026, 10, 15, 12, 0, 0)

        # 14 fechas válidas para completar una campaña
        self.fechas_14 = [f"2026-10-{i:02d}" for i in range(1, 15)]
        registros_14 = {
            f: {
                "tipo": "checkin",
                "id_mensaje": f"msg_{f}",
                "estado_entrega": "puntual",
                "captura": f"cap_{f}.png"
            } for f in self.fechas_14
        }

        self.storage = {
            "usuarios.json": {
                self.dev_id: {
                    "nombre": "Carlos Dev",
                    "rol": "desarrollador",
                    "creditos": 0,
                    "puntos_founder": 0,
                    "boletos_rifa": 0,
                    "reputacion": {
                        "campanas_completadas": 0,
                        "dias_totales_cumplidos": 0,
                        "faltas_injustificadas": 0,
                        "confiabilidad_pct": 100.0,
                        "nivel_confiabilidad": "Inicial"
                    },
                    "apps_testeando": []
                },
                self.tester_id: {
                    "nombre": "Ana Tester",
                    "rol": "tester",
                    "creditos": 350,
                    "puntos_founder": 50,
                    "boletos_rifa": 2,
                    "reputacion": {
                        "campanas_completadas": 0,
                        "dias_totales_cumplidos": 0,
                        "faltas_injustificadas": 0,
                        "confiabilidad_pct": 100.0,
                        "nivel_confiabilidad": "Inicial"
                    },
                    "apps_testeando": [self.beta_app_id, self.legacy_app_id]
                },
                "dev_maria": {
                    "nombre": "Maria Dev",
                    "rol": "desarrollador",
                    "creditos": 1500,
                    "puntos_founder": 0,
                    "boletos_rifa": 3,
                    "apps_testeando": []
                }
            },
            "apps.json": {
                self.beta_app_id: {
                    "dev_id": self.dev_id,
                    "descripcion": "App Beta Gratuita",
                    "link": "https://play.google.com/store/apps/details?id=com.carlos.beta",
                    "estado": "en_prueba",
                    "es_beta": True,
                    "tipo_campana": "beta_gratuita",
                    "puntos_founder_recompensa": 100,
                    "min_testers": 15,
                    "max_testers": 15,
                    "testers": [self.tester_id],
                    "pruebas": {
                        self.tester_id: {
                            "tester_nombre": "Ana Tester",
                            "estado": "instalada",
                            "fecha_inicio": "2026-10-01",
                            "dias_completados": 14,
                            "registros": copy.deepcopy(registros_14),
                            "posibles_faltas": {}
                        }
                    }
                },
                self.legacy_app_id: {
                    "dev_id": "dev_maria",
                    "descripcion": "App Estandar Pagada",
                    "link": "https://play.google.com/store/apps/details?id=com.maria.legacy",
                    "estado": "en_prueba",
                    "es_beta": False,
                    "tipo_campana": "estandar",
                    "min_testers": 15,
                    "max_testers": 15,
                    "testers": [self.tester_id],
                    "pruebas": {
                        self.tester_id: {
                            "tester_nombre": "Ana Tester",
                            "estado": "instalada",
                            "fecha_inicio": "2026-10-01",
                            "dias_completados": 14,
                            "registros": copy.deepcopy(registros_14),
                            "posibles_faltas": {}
                        }
                    }
                }
            },
            "recompensas.json": {},
            "auditoria_cierres.json": {},
            "solicitudes_liquidaciones.json": {},
            "solicitudes_cierres.json": {},
            "apelaciones_revisiones.json": {}
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    # --- 1. MODO BETA GRATUITO: PUBLICACIÓN SIN CRÉDITOS ---
    def test_publicar_beta_gratuita_con_cero_creditos(self):
        """1. En BETA_GRATUITA=true, un desarrollador con 0 créditos puede publicar gratis."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            self.assertTrue(db.es_beta_gratuita())
            app_id = "app_nueva_beta"
            res = self.firebase.ejecutar(
                db.publicar_campana,
                self.dev_id,
                "Carlos Dev",
                "https://play.google.com/store/apps/details?id=com.carlos.nueva",
                "Mi App Beta 1",
                app_id
            )
            self.assertEqual(res["resultado"], "creada")
            self.assertTrue(res["es_beta"])
            self.assertEqual(res["costo_creditos"], 0)

            # Verificar que no se le cobró créditos al usuario y conserva 0
            user = self.storage["usuarios.json"][self.dev_id]
            self.assertEqual(user["creditos"], 0)
            self.assertEqual(user["boletos_rifa"], 0)
            self.assertEqual(user["reputacion"]["campanas_publicadas"], 1)

            # Verificar datos de la campaña creada
            app = self.storage["apps.json"][app_id]
            self.assertTrue(app["es_beta"])
            self.assertEqual(app["tipo_campana"], "beta_gratuita")
            self.assertEqual(app["min_testers"], 15)
            self.assertEqual(app["max_testers"], 15)
            self.assertEqual(app["puntos_founder_recompensa"], 100)

    # --- 2. PRESERVACIÓN ESTRICTA DE SALDOS EXISTENTES ---
    def test_publicar_beta_gratuita_preserva_creditos_existentes(self):
        """2. La beta gratuita nunca descuenta ni sobrescribe créditos existentes del dev."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            app_id = "app_maria_beta"
            res = self.firebase.ejecutar(
                db.publicar_campana,
                "dev_maria",
                "Maria Dev",
                "https://play.google.com/store/apps/details?id=com.maria.beta",
                "App Beta Maria",
                app_id
            )
            self.assertEqual(res["resultado"], "creada")
            # El saldo original de 1500 créditos y 3 boletos debe permanecer intacto
            user = self.storage["usuarios.json"]["dev_maria"]
            self.assertEqual(user["creditos"], 1500)
            self.assertEqual(user["boletos_rifa"], 3)

    # --- 3. REGLA ESTÁNDAR CUANDO BETA ESTÁ DESACTIVADA ---
    def test_publicar_modo_estandar_requiere_800_creditos(self):
        """3. Con BETA_GRATUITA=false, se mantiene la regla original de requerir 800 créditos."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "false"}):
            self.assertFalse(db.es_beta_gratuita())
            # Carlos Dev tiene 0 créditos -> debe ser rechazado
            with self.assertRaises(db.PublicacionRechazada):
                self.firebase.ejecutar(
                    db.publicar_campana,
                    self.dev_id,
                    "Carlos Dev",
                    "https://play.google.com/store/apps/details?id=com.carlos.fail",
                    "App Rechazada",
                    "app_fail"
                )

    # --- 4. PROGRAMA FOUNDER: LIQUIDACIÓN DE CAMPAÑA BETA ---
    def test_liquidar_campana_beta_otorga_puntos_founder_sin_creditos_tradicionales(self):
        """4. Liquidar una campaña beta otorga Puntos Founder (+100) sin alterar créditos ni boletos."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.beta_app_id,
            self.tester_id,
            actor_id="admin_test",
            now_dt=self.dt_now
        )
        self.assertTrue(res["exito"])
        self.assertEqual(res["resultado"], "liquidada")
        self.assertTrue(res["es_beta"])
        self.assertEqual(res["puntos_founder"], 100)
        self.assertEqual(res["importe"], 0)  # No entrega créditos tradicionales
        self.assertEqual(res["boletos"], 0)  # No entrega boletos de rifa

        # Verificar saldo del tester:
        user = self.storage["usuarios.json"][self.tester_id]
        self.assertEqual(user["puntos_founder"], 150)  # 50 iniciales + 100 founder
        self.assertEqual(user["creditos"], 350)  # Exactamente los 350 iniciales, no 350+800
        self.assertEqual(user["boletos_rifa"], 2)  # Exactamente 2 iniciales

        # Movimiento inmutable en recompensas.json
        id_mov = db.id_movimiento_recompensa(self.beta_app_id, self.tester_id)
        self.assertIn(id_mov, self.storage["recompensas.json"])
        mov = self.storage["recompensas.json"][id_mov]
        self.assertTrue(mov["es_beta"])
        self.assertEqual(mov["tipo_recompensa"], "founder")
        self.assertEqual(mov["puntos_founder"], 100)
        self.assertEqual(mov["importe"], 0)
        self.assertEqual(mov["boletos"], 0)

    # --- 5. COMPATIBILIDAD CON CAMPAÑAS ANTERIORES / ESTÁNDAR ---
    def test_liquidar_campana_estandar_mantiene_800_creditos_y_un_boleto(self):
        """5. Liquidar una campaña estándar anterior entrega 800 créditos y 1 boleto, sin puntos founder."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.legacy_app_id,
            self.tester_id,
            actor_id="admin_test",
            now_dt=self.dt_now
        )
        self.assertTrue(res["exito"])
        self.assertFalse(res["es_beta"])
        self.assertEqual(res["importe"], 800)
        self.assertEqual(res["boletos"], 1)
        self.assertEqual(res["puntos_founder"], 0)

        user = self.storage["usuarios.json"][self.tester_id]
        self.assertEqual(user["creditos"], 1150)  # 350 + 800
        self.assertEqual(user["boletos_rifa"], 3)  # 2 + 1
        self.assertEqual(user["puntos_founder"], 50)  # Sin alterar los 50 previos

    # --- 6. REPUTACIÓN PERMANENTE ---
    def test_reputacion_permanente_se_actualiza_con_auditoria(self):
        """6. Al liquidar se consolida la reputación permanente: confiabilidad, nivel y estadísticas."""
        self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.beta_app_id,
            self.tester_id,
            actor_id="admin_test",
            now_dt=self.dt_now
        )
        user = self.storage["usuarios.json"][self.tester_id]
        rep = user["reputacion"]
        self.assertEqual(rep["campanas_completadas"], 1)
        self.assertEqual(rep["dias_totales_cumplidos"], 14)
        self.assertEqual(rep["faltas_injustificadas"], 0)
        self.assertEqual(rep["confiabilidad_pct"], 100.0)
        self.assertEqual(rep["nivel_confiabilidad"], "Excelente")

        # Función de formateo
        txt_rep = db.formatear_reputacion(user)
        self.assertIn("Excelente", txt_rep)
        self.assertIn("`100.0%` confiabilidad", txt_rep)
        self.assertIn("Campañas completadas: `1`", txt_rep)

        # Consulta estructurada
        info_rep = db.obtener_reputacion_tester(self.tester_id, cliente=self.firebase)
        self.assertTrue(info_rep["encontrado"])
        self.assertEqual(info_rep["nivel_confiabilidad"], "Excelente")

    # --- 7. TEST FOR TEST (T4T) VOLUNTARIO ---
    def test_t4t_desarrollador_puede_postular_a_otra_app_pero_no_a_la_suya(self):
        """7. T4T voluntario: un dev puede testear apps de colegas pero no su propia app."""
        # Carlos Dev intenta postular a su propia app -> Rechazado
        with self.assertRaises(db.InscripcionRechazada):
            self.firebase.ejecutar(db.inscribir_tester, self.dev_id, self.beta_app_id)

        # Carlos Dev se postula a la app de Maria Dev (T4T voluntario)
        self.storage["apps.json"][self.legacy_app_id]["estado"] = "reclutando"
        self.storage["apps.json"][self.legacy_app_id]["testers"] = []
        res = self.firebase.ejecutar(db.inscribir_tester, self.dev_id, self.legacy_app_id)
        self.assertIn(self.dev_id, self.storage["apps.json"][self.legacy_app_id]["testers"])
        self.assertIn(self.legacy_app_id, self.storage["usuarios.json"][self.dev_id]["apps_testeando"])

    # --- 8. T4T: PUBLICAR SIN HABER TESTEADO ANTES ---
    def test_t4t_desarrollador_publica_sin_obligacion_de_testear_previamente(self):
        """8. Publicar es completamente libre y no exige haber probado apps antes."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            nuevo_dev_id = "dev_virgen"
            self.storage["usuarios.json"][nuevo_dev_id] = {
                "nombre": "Pedro Dev",
                "creditos": 0,
                "puntos_founder": 0,
                "apps_testeando": []  # Cero apps testeadas
            }
            res = self.firebase.ejecutar(
                db.publicar_campana,
                nuevo_dev_id,
                "Pedro Dev",
                "https://play.google.com/store/apps/details?id=com.pedro.app",
                "App de Pedro",
                "app_pedro_1"
            )
            self.assertEqual(res["resultado"], "creada")

    # --- 9. INTEGRACIÓN DE BOT: /subirapp CON BETA GRATUITA Y 0 CRÉDITOS ---
    async def test_bot_subirapp_en_beta_gratuita_con_cero_creditos(self):
        """9. Handler /subirapp permite el flujo completo a un dev con 0 créditos si BETA_GRATUITA=true."""
        with patch.dict(os.environ, {"BETA_GRATUITA": "true"}):
            update = Mock()
            update.message = AsyncMock()
            update.message.from_user.id = int(self.dev_id)
            update.message.from_user.first_name = "Carlos Dev"

            context = Mock()
            context.args = []
            context.user_data = {}

            fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional))
            with patch.object(db, "_firebase_initialized", True), \
                 patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
                 patch.object(db, "_firebase_configurado", return_value=True), \
                 patch.dict("sys.modules", {"firebase_admin": fake_module}):

                # Paso 1: /subirapp
                paso1 = await bot.subirapp_start(update, context)
                self.assertEqual(paso1, bot.SUBIR_LINK)
                self.assertIn("Fase Beta", update.message.reply_text.call_args[0][0])

                # Paso 2: Link
                update.message.text = "https://play.google.com/store/apps/details?id=com.carlos.botbeta"
                paso2 = await bot.subirapp_link(update, context)
                self.assertEqual(paso2, bot.SUBIR_DESCRIPCION)

                # Paso 3: Descripción y publicación
                update.message.text = "Mi app en Fase Beta Founders"
                paso3 = await bot.subirapp_descripcion(update, context)
                self.assertEqual(paso3, bot.ConversationHandler.END)

                # Verificar mensaje final
                texto_confirmacion = update.message.reply_text.call_args[0][0]
                self.assertIn("BETA FOUNDERS", texto_confirmacion)
                self.assertIn("0 créditos", texto_confirmacion)

    # --- 10. INTEGRACIÓN DE BOT: /t4t Y /creditos ---
    async def test_bot_handlers_t4t_y_miscreditos(self):
        """10. Handlers /t4t y /creditos muestran Puntos Founder y reputación permanente."""
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.tester_id)
        update.message.from_user.first_name = "Ana Tester"
        context = Mock()

        # 1. /t4t
        await bot.t4t_info(update, context)
        t4t_msg = update.message.reply_text.call_args[0][0]
        self.assertIn("Test for Test (T4T)", t4t_msg)
        self.assertIn("Puntos Founder", t4t_msg)

        # 2. /creditos
        with patch.object(bot, "cargar_archivo", return_value=self.storage["usuarios.json"]):
            await bot.miscreditos(update, context)
            cred_msg = update.message.reply_text.call_args[0][0]
            self.assertIn("Puntos Founder", cred_msg)
            self.assertIn("Reputación Permanente", cred_msg)
            self.assertIn("Créditos tradicionales", cred_msg)


if __name__ == "__main__":
    unittest.main()
