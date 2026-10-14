import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import db
from inscripcion_simulada import FirestoreSimulado, ConflictoLectura


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.user_id = "42"
        self.app_id = "app_42_test1"
        self.storage = {
            "usuarios.json": {
                self.user_id: {
                    "nombre": "Juan Dev",
                    "creditos": 1000,
                    "boletos_rifa": 2,
                    "apps_testeando": []
                }
            },
            "apps.json": {}
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    def test_valid_publication_reduces_credits_adds_ticket_and_creates_app(self):
        """1. Publicación válida: descuenta 800 créditos, otorga 1 boleto y crea app reclutando."""
        res = self.firebase.ejecutar(
            db.publicar_campana,
            self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "Mi App", self.app_id
        )
        self.assertEqual(res["resultado"], "creada")
        user = self.storage["usuarios.json"][self.user_id]
        self.assertEqual(user["creditos"], 200)
        self.assertEqual(user["boletos_rifa"], 3)
        app = self.storage["apps.json"][self.app_id]
        self.assertEqual(app["dev_id"], self.user_id)
        self.assertEqual(app["estado"], "reclutando")
        self.assertEqual(app["min_testers"], 15)
        self.assertEqual(app["max_testers"], 15)
        self.assertEqual(app["testers"], [])
        self.assertEqual(app["pruebas"], {})

    def test_unregistered_user_rejected(self):
        """2. Usuario inexistente es rechazado con PublicacionRechazada sin escrituras."""
        with self.assertRaises(db.PublicacionRechazada):
            self.firebase.ejecutar(
                db.publicar_campana,
                "99", "Fantasma", "https://play.google.com/store/apps/details?id=com.app", "App", "app_99_1"
            )
        self.assertNotIn("app_99_1", self.storage["apps.json"])

    def test_insufficient_credits_rejected_without_changes(self):
        """3. Créditos insuficientes (<800) rechazados con PublicacionRechazada sin cambios."""
        self.storage["usuarios.json"][self.user_id]["creditos"] = 500
        with self.assertRaises(db.PublicacionRechazada):
            self.firebase.ejecutar(
                db.publicar_campana,
                self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "App", self.app_id
            )
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 500)
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["boletos_rifa"], 2)
        self.assertNotIn(self.app_id, self.storage["apps.json"])

    def test_retry_same_request_returns_already_published_without_double_charge(self):
        """4. Reintento de la misma solicitud con saldo ya descontado reconoce la app sin volver a cobrar."""
        self.firebase.ejecutar(
            db.publicar_campana,
            self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "Mi App", self.app_id
        )
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 200)

        # Reintento idéntico
        res_retry = self.firebase.ejecutar(
            db.publicar_campana,
            self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "Mi App", self.app_id
        )
        self.assertEqual(res_retry["resultado"], "ya_publicada")
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 200)
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["boletos_rifa"], 3)

    def test_same_app_id_with_different_content_or_owner_rejected(self):
        """5. Mismo app_id con diferente contenido o propietario es rechazado por conflicto."""
        self.firebase.ejecutar(
            db.publicar_campana,
            self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "Mi App", self.app_id
        )
        # Contenido distinto (distinta descripción)
        with self.assertRaises(db.PublicacionRechazada):
            self.firebase.ejecutar(
                db.publicar_campana,
                self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "Otra Desc", self.app_id
            )
        # Propietario distinto
        self.storage["usuarios.json"]["43"] = {"nombre": "Otro", "creditos": 1000, "boletos_rifa": 0}
        with self.assertRaises(db.PublicacionRechazada):
            self.firebase.ejecutar(
                db.publicar_campana,
                "43", "Otro", "https://play.google.com/store/apps/details?id=com.app", "Mi App", self.app_id
            )

    def test_local_mode_rejected_without_admin_recharge(self):
        """6. Modo local sin Firestore rechaza publicación en subirapp_start sin dar saldo admin."""
        with patch.object(db, "_firebase_configurado", return_value=False):
            with self.assertRaises(db.ErrorPersistenciaPublicacion):
                db.publicar_campana(self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "App", self.app_id)

    def test_lost_commit_ack_allows_idempotent_retry(self):
        """7. Commit aplicado cuya respuesta se pierde (lose_ack) responde error de persistencia y el reintento lo reconoce."""
        self.firebase.lose_ack = True
        with self.assertRaises(db.ErrorPersistenciaPublicacion):
            self.firebase.ejecutar(
                db.publicar_campana,
                self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "App", self.app_id
            )
        self.assertIn(self.app_id, self.storage["apps.json"])
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 200)

        # Reintento tras pérdida de ACK
        self.firebase.lose_ack = False
        res = self.firebase.ejecutar(
            db.publicar_campana,
            self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "App", self.app_id
        )
        self.assertEqual(res["resultado"], "ya_publicada")
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 200)

    def test_transaction_fail_leaves_no_partial_changes(self):
        """8. Fallo de transacción (fail_commit) no deja cambios parciales."""
        self.firebase.fail_commit = True
        with self.assertRaises(db.ErrorPersistenciaPublicacion):
            self.firebase.ejecutar(
                db.publicar_campana,
                self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "App", self.app_id
            )
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 1000)
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["boletos_rifa"], 2)
        self.assertNotIn(self.app_id, self.storage["apps.json"])

    def test_concurrent_balance_exhaustion_interleaved(self):
        """9. Concurrencia intercalada por último saldo disponible (competición de saldo)."""
        # Usuario solo tiene 800 créditos exactos
        self.storage["usuarios.json"][self.user_id]["creditos"] = 800
        app_id_2 = "app_42_test2"

        # Intercalar un descuento de saldo justo antes del commit de la primera transacción
        def intercalar_cobro():
            self.storage["usuarios.json"][self.user_id]["creditos"] = 0

        self.firebase.before_commit = intercalar_cobro

        # La primera transacción se reintentará por ConflictoLectura, y en el reintento fallará por saldo insuficiente
        with self.assertRaises(db.PublicacionRechazada):
            self.firebase.ejecutar(
                db.publicar_campana,
                self.user_id, "Juan Dev", "https://play.google.com/store/apps/details?id=com.app", "App 1", self.app_id
            )
        self.assertNotIn(self.app_id, self.storage["apps.json"])
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 0)


class TelegramHandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = "7967828114"  # Admin user ID
        self.storage = {
            "usuarios.json": {
                self.user_id: {
                    "nombre": "Admin User",
                    "creditos": 1000,
                    "boletos_rifa": 0,
                    "apps_testeando": []
                }
            },
            "apps.json": {}
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    async def test_subirapp_start_rejected_in_local_mode_without_admin_credit(self):
        """10. subirapp_start rechazado en modo local no otorga saldo admin ni crea datos."""
        from bot import subirapp_start, ConversationHandler
        self.storage["usuarios.json"][self.user_id]["creditos"] = 0
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.user_id)

        context = Mock()
        context.args = []
        context.user_data = {}

        with patch.object(db, "_firebase_configurado", return_value=False):
            res = await subirapp_start(update, context)

        self.assertEqual(res, ConversationHandler.END)
        update.message.reply_text.assert_called_once()
        self.assertIn("modo local", update.message.reply_text.call_args[0][0].lower())
        # Verificar que el usuario sigue con 0 créditos y no se le otorgó recarga admin
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 0)

    async def test_telegram_response_failure_after_commit_and_retry(self):
        """12. Commit aplicado con respuesta Telegram fallida: al reintentar solo hay 1 campaña, 1 cobro y 1 boleto."""
        from bot import subirapp_start, subirapp_link, subirapp_descripcion, ConversationHandler, SUBIR_DESCRIPCION

        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.user_id)
        update.message.from_user.first_name = "Admin User"

        context = Mock()
        context.args = []
        context.user_data = {}

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            # Preparar solicitud
            await subirapp_start(update, context)
            update.message.text = "https://play.google.com/store/apps/details?id=com.demo"
            await subirapp_link(update, context)

            # Simular que reply_text falla tras aplicar el commit en Firestore
            update.message.text = "App de Demostración"
            update.message.reply_text.side_effect = [RuntimeError("Telegram network failure")]

            with self.assertRaises(RuntimeError):
                await subirapp_descripcion(update, context)

            # Verificar que el commit ocurrió en Firestore y que user_data conserva los datos pendientes
            self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 200)
            self.assertEqual(len(self.storage["apps.json"]), 1)
            self.assertTrue(context.user_data.get("pub_estado_incierto"))
            self.assertIn("pub_app_id", context.user_data)

            # Reintento vía subirapp_start (recuperación de solicitud pendiente)
            update.message.reply_text.side_effect = None
            res_retry = await subirapp_start(update, context)
            self.assertEqual(res_retry, ConversationHandler.END)

        # Verificar que NO se volvió a cobrar ni a duplicar campaña ni boletos
        user = self.storage["usuarios.json"][self.user_id]
        self.assertEqual(user["creditos"], 200)
        self.assertEqual(user["boletos_rifa"], 1)
        self.assertEqual(len(self.storage["apps.json"]), 1)

    async def test_commit_applied_with_remaining_balance_below_800_recovered(self):
        """13. Commit aplicado con saldo restante < 800: el reintento recupera la publicación sin bloquearse por saldo."""
        from bot import subirapp_start, subirapp_link, subirapp_descripcion, ConversationHandler
        self.storage["usuarios.json"][self.user_id]["creditos"] = 800

        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.user_id)
        update.message.from_user.first_name = "User Single Charge"

        context = Mock()
        context.args = []
        context.user_data = {}

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            await subirapp_start(update, context)
            update.message.text = "https://play.google.com/store/apps/details?id=com.single"
            await subirapp_link(update, context)

            # Simular fallo de Telegram tras commit
            update.message.text = "App Saldo 800"
            update.message.reply_text.side_effect = [RuntimeError("Network drop post commit")]

            with self.assertRaises(RuntimeError):
                await subirapp_descripcion(update, context)

            # El saldo actual es 0 (800 - 800)
            self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 0)

            # Reintento con subirapp_start: debe ser recuperado ANTES del chequeo de saldo < 800
            update.message.reply_text.side_effect = None
            res_retry = await subirapp_start(update, context)
            self.assertEqual(res_retry, ConversationHandler.END)

        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 0)
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["boletos_rifa"], 1)
        self.assertEqual(len(self.storage["apps.json"]), 1)

    async def test_uncertain_result_conserves_id_and_content(self):
        """14. Resultado incierto: conserva el ID y contenido en context.user_data."""
        from bot import subirapp_start, subirapp_link, subirapp_descripcion, ConversationHandler
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.user_id)
        update.message.from_user.first_name = "User Incierto"

        context = Mock()
        context.args = []
        context.user_data = {}

        fake_module = Mock(firestore=Mock(transactional=self.firebase.transactional))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.object(db, "_firebase_configurado", return_value=True), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):

            await subirapp_start(update, context)
            update.message.text = "https://play.google.com/store/apps/details?id=com.incierto"
            await subirapp_link(update, context)
            saved_id = context.user_data["pub_app_id"]

            update.message.text = "App Incierta"
            with patch.object(db, "publicar_campana", side_effect=db.ErrorPersistenciaPublicacion("Timeout simulated")):
                res = await subirapp_descripcion(update, context)

            self.assertEqual(res, ConversationHandler.END)
            self.assertEqual(context.user_data.get("pub_app_id"), saved_id)
            self.assertEqual(context.user_data.get("app_link"), "https://play.google.com/store/apps/details?id=com.incierto")
            self.assertEqual(context.user_data.get("app_descripcion"), "App Incierta")
            self.assertTrue(context.user_data.get("pub_estado_incierto"))

    async def test_missing_id_in_descripcion_makes_no_writes(self):
        """15. ID ausente en subirapp_descripcion no genera escrituras ni cobra créditos."""
        from bot import subirapp_descripcion, ConversationHandler
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.user_id)
        update.message.text = "Descripcion sin ID"

        context = Mock()
        context.args = []
        context.user_data = {}  # Sin pub_app_id

        res = await subirapp_descripcion(update, context)
        self.assertEqual(res, ConversationHandler.END)
        update.message.reply_text.assert_called_once()
        self.assertIn("expiró", update.message.reply_text.call_args[0][0].lower())
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["creditos"], 1000)
        self.assertEqual(len(self.storage["apps.json"]), 0)

    async def test_explicit_new_publication_generates_different_id(self):
        """16. Publicación nueva explícita (/subirapp nueva) genera un ID diferente."""
        from bot import subirapp_start, SUBIR_LINK
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = int(self.user_id)

        context = Mock()
        context.args = []
        context.user_data = {"pub_app_id": "app_42_antigua", "app_link": "old", "app_descripcion": "old"}

        with patch.object(db, "_firebase_configurado", return_value=True):
            context.args = ["nueva"]
            res = await subirapp_start(update, context)
            self.assertEqual(res, SUBIR_LINK)
            self.assertNotEqual(context.user_data.get("pub_app_id"), "app_42_antigua")
            self.assertNotIn("app_link", context.user_data)


if __name__ == "__main__":
    unittest.main()
