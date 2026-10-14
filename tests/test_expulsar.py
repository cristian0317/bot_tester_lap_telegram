import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import db
from inscripcion_simulada import FirestoreSimulado


class ExpulsionTests(unittest.TestCase):
    def setUp(self):
        self.tester_id = "42"
        self.app_id = "app_demo_1"
        self.storage = {
            "usuarios.json": {
                self.tester_id: {
                    "nombre": "Tester Juan",
                    "creditos": 500,
                    "boletos_rifa": 1,
                    "apps_testeando": [self.app_id]
                },
                "43": {
                    "nombre": "Tester Maria",
                    "creditos": 300,
                    "boletos_rifa": 0,
                    "apps_testeando": [self.app_id]
                }
            },
            "apps.json": {
                self.app_id: {
                    "dev_id": "99",
                    "descripcion": "App Demo",
                    "estado": "en_prueba",
                    "testers": [self.tester_id, "43"],
                    "min_testers": 12,
                    "max_testers": 12,
                    "pruebas": {
                        self.tester_id: {"estado": "instalada", "dias_completados": 5},
                        "43": {"estado": "instalada", "dias_completados": 8}
                    }
                }
            }
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    def test_full_expulsion_removes_tester_from_both_docs_and_pruebas(self):
        """1. Expulsión completa: remueve al tester de testers, apps_testeando y pruebas."""
        res = self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        self.assertEqual(res["resultado"], "expulsado")
        self.assertEqual(res["tester_nombre"], "Tester Juan")

        user = self.storage["usuarios.json"][self.tester_id]
        app = self.storage["apps.json"][self.app_id]

        self.assertNotIn(self.app_id, user["apps_testeando"])
        self.assertNotIn(self.tester_id, app["testers"])
        self.assertNotIn(self.tester_id, app["pruebas"])

        # Verificar que los demás usuarios y sus pruebas permanezcan intactos
        self.assertIn("43", app["testers"])
        self.assertIn("43", app["pruebas"])
        self.assertEqual(app["pruebas"]["43"]["dias_completados"], 8)

    def test_real_pruebas_map_replacement_with_multiple_testers(self):
        """2. Eliminación real de pruebas: el reemplazo del mapa pruebas solo elimina al tester expulsado."""
        self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        pruebas = self.storage["apps.json"][self.app_id]["pruebas"]
        self.assertNotIn(self.tester_id, pruebas)
        self.assertIn("43", pruebas)
        self.assertEqual(len(pruebas), 1)

    def test_cleanup_residual_duplicates(self):
        """3. Limpieza de duplicados residuales antiguos en listas."""
        self.storage["apps.json"][self.app_id]["testers"] = [self.tester_id, "43", self.tester_id]
        self.storage["usuarios.json"][self.tester_id]["apps_testeando"] = [self.app_id, "otra_app", self.app_id]

        self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        app = self.storage["apps.json"][self.app_id]
        user = self.storage["usuarios.json"][self.tester_id]

        self.assertEqual(app["testers"], ["43"])
        self.assertEqual(user["apps_testeando"], ["otra_app"])

    def test_idempotent_repetition_returns_already_expelled(self):
        """4. Repetición idempotente: expulsar a un tester ya expulsado devuelve ya_expulsado con 0 cambios."""
        self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        res_repeat = self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)

        self.assertEqual(res_repeat["resultado"], "ya_expulsado")
        self.assertEqual(self.storage["usuarios.json"][self.tester_id]["apps_testeando"], [])
        self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id]["testers"])

    def test_inconsistent_enrolment_repaired_in_both_directions(self):
        """5. Inscripción inconsistente: repara restos si solo figuraba en un sentido."""
        # Figuraba solo en apps.json
        self.storage["usuarios.json"][self.tester_id]["apps_testeando"] = []
        res = self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)

        self.assertEqual(res["resultado"], "expulsado")
        self.assertTrue(res["inconsistente"])
        self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id]["testers"])

    def test_inexistent_documents_rejected_without_writes(self):
        """6. Documentos inexistentes rechazados con ExpulsionRechazada."""
        with self.assertRaises(db.ExpulsionRechazada):
            self.firebase.ejecutar(db.expulsar_tester, "999", self.app_id)

        with self.assertRaises(db.ExpulsionRechazada):
            self.firebase.ejecutar(db.expulsar_tester, self.tester_id, "app_inexistente")

    def test_local_mode_rejected_without_json_writes(self):
        """7. Modo local sin Firestore rechaza expulsión con ErrorPersistenciaExpulsion."""
        with patch.object(db, "_firebase_configurado", return_value=False):
            with self.assertRaises(db.ErrorPersistenciaExpulsion):
                db.expulsar_tester(self.tester_id, self.app_id)

    def test_commit_failure_prevents_partial_changes(self):
        """8. Fallo de commit (fail_commit) no aplica escrituras parciales."""
        self.firebase.fail_commit = True
        with self.assertRaises(db.ErrorPersistenciaExpulsion):
            self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)

        self.assertIn(self.app_id, self.storage["usuarios.json"][self.tester_id]["apps_testeando"])
        self.assertIn(self.tester_id, self.storage["apps.json"][self.app_id]["testers"])

    def test_campaign_state_lista_reverts_to_reclutando_when_below_twelve(self):
        """9. Campaña 'lista' vuelve a 'reclutando' si queda con menos de 12 testers."""
        self.storage["apps.json"][self.app_id]["estado"] = "lista"
        self.storage["apps.json"][self.app_id]["testers"] = [str(100 + i) for i in range(12)]
        self.storage["apps.json"][self.app_id]["testers"][0] = self.tester_id

        self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        app = self.storage["apps.json"][self.app_id]
        self.assertEqual(len(app["testers"]), 11)
        self.assertEqual(app["estado"], "reclutando")

    def test_campaign_state_en_prueba_remains_active(self):
        """10. Campaña 'en_prueba' conserva su estado activo tras expulsión."""
        self.storage["apps.json"][self.app_id]["estado"] = "en_prueba"
        self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        self.assertEqual(self.storage["apps.json"][self.app_id]["estado"], "en_prueba")

    def test_credits_and_rewards_preserved(self):
        """11. Saldo de créditos, boletos y recompensas se mantienen intactos."""
        cred_before = self.storage["usuarios.json"][self.tester_id]["creditos"]
        boletos_before = self.storage["usuarios.json"][self.tester_id]["boletos_rifa"]

        self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)

        self.assertEqual(self.storage["usuarios.json"][self.tester_id]["creditos"], cred_before)
        self.assertEqual(self.storage["usuarios.json"][self.tester_id]["boletos_rifa"], boletos_before)


class ExpulsionHandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.admin_id = "7967828114"
        self.regular_id = "10"
        self.tester_id = "42"
        self.app_id = "app_demo_1"
        self.storage = {
            "usuarios.json": {
                self.admin_id: {"nombre": "Admin User", "creditos": 0},
                self.regular_id: {"nombre": "Regular User", "creditos": 0},
                self.tester_id: {"nombre": "Tester Juan", "creditos": 500, "apps_testeando": [self.app_id]}
            },
            "apps.json": {
                self.app_id: {
                    "dev_id": "99",
                    "descripcion": "App Demo",
                    "estado": "reclutando",
                    "testers": [self.tester_id],
                    "min_testers": 12,
                    "pruebas": {self.tester_id: {"estado": "instalada"}}
                }
            }
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    async def test_non_admin_user_rejected_by_handler(self):
        """12. Usuario no administrador en sancionartester es ignorado sin ejecutar la expulsión."""
        from bot import sancionartester
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.regular_id)

        context = Mock()
        context.args = [self.tester_id, "expulsar", self.app_id]

        await sancionartester(update, context)
        update.message.reply_text.assert_not_called()
        self.assertIn(self.app_id, self.storage["usuarios.json"][self.tester_id]["apps_testeando"])

    async def test_lost_ack_on_handler_retry(self):
        """13. Respuesta Telegram fallida tras commit: el reintento del handler recupera respuesta de ya_expulsado."""
        from bot import sancionartester
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.admin_id)

        context = Mock()
        context.args = [self.tester_id, "expulsar", self.app_id]

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            # Primera invocación
            await sancionartester(update, context)
            self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id]["testers"])

            # Segunda invocación (reintento del handler)
            update.message.reply_text.reset_mock()
            await sancionartester(update, context)
            update.message.reply_text.assert_called_once()
            self.assertIn("no se realizaron cambios", update.message.reply_text.call_args[0][0].lower())

    async def test_concurrent_conflict_triggers_retry(self):
        """14. Conflicto concurrente sobre documento leído provoca ConflictoLectura y reintento en simulador."""
        def intercalar_escritura():
            self.storage["apps.json"][self.app_id]["testers"].append("999")

        self.firebase.before_commit = intercalar_escritura

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            res = db.expulsar_tester(self.tester_id, self.app_id)
            self.assertEqual(res["resultado"], "expulsado")
            self.assertEqual(self.firebase.attempts, 2)
            self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id]["testers"])
            self.assertIn("999", self.storage["apps.json"][self.app_id]["testers"])


if __name__ == "__main__":
    unittest.main()
