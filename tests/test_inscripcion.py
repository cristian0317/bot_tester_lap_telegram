"""Pruebas sin credenciales, red ni archivos de datos reales."""
import unittest
from unittest.mock import patch
import db
import test_postular as registration


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = registration.RegistrationTests.setUp
    reset_fixture = registration.RegistrationTests.reset_fixture
    register = registration.RegistrationTests.register
    assert_rejected = registration.RegistrationTests.assert_rejected
    reply = registration.RegistrationTests.reply

    async def test_commit_failure_never_announces_acceptance(self):
        self.firebase.fail_commit = True
        await self.assert_rejected("no se pudo confirmar")
        self.assertNotIn("aceptada", self.reply.lower())

    async def test_conflicting_last_seat_has_only_one_winner(self):
        self.apps[self.app_id]["testers"] = [str(100 + i) for i in range(11)]
        self.users["43"] = {"apps_testeando": []}
        self.firebase.before_commit = lambda: self.firebase.inscribir("43", self.app_id)
        await self.register()
        campaign = self.storage["apps.json"][self.app_id]
        self.assertEqual(campaign["estado"], "lista")
        self.assertEqual(len(campaign["testers"]), 12)
        self.assertIn("43", campaign["testers"])
        self.assertNotIn("42", campaign["testers"])
        self.assertEqual(self.storage["usuarios.json"]["42"]["apps_testeando"], [])
        self.assertEqual(self.storage["usuarios.json"]["43"]["apps_testeando"], [self.app_id])
        self.assertNotIn("aceptada", self.reply.lower())
        self.assertIn("llena", self.reply.lower())
        self.context.bot.send_message.assert_not_awaited()

    async def test_transaction_retry_notifies_once_after_commit(self):
        self.apps[self.app_id]["testers"] = [str(100 + i) for i in range(11)]
        self.firebase.before_commit = lambda: self.storage["apps.json"][self.app_id].update(descripcion="Actualizada")
        await self.register()
        self.assertEqual(self.firebase.attempts, 2)
        self.assertEqual(self.storage["apps.json"][self.app_id]["testers"].count("42"), 1)
        self.context.bot.send_message.assert_awaited_once()
        self.assertCountEqual(self.writes, ["apps.json", "usuarios.json"])
        self.context.bot.send_message.reset_mock()
        self.writes.clear()
        await self.assert_rejected("inscrito")

    async def test_lost_commit_ack_retry_does_not_duplicate_or_notify(self):
        self.firebase.lose_ack = True
        await self.register()
        self.assertNotIn("aceptada", self.reply.lower())
        self.assertIn(self.user_id, self.storage["apps.json"][self.app_id]["testers"])
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["apps_testeando"], [self.app_id])
        self.firebase.lose_ack = False
        self.writes.clear()
        self.update.message.reply_text.reset_mock()
        await self.assert_rejected("inscrito")

    async def test_inconsistent_registration_has_explicit_message(self):
        self.apps[self.app_id]["testers"] = [self.user_id]
        await self.assert_rejected("inconsistente")

    async def test_failed_notification_does_not_repeat_on_retry(self):
        self.apps[self.app_id]["testers"] = [str(100 + i) for i in range(11)]
        self.context.bot.send_message.side_effect = RuntimeError("notification failed")
        await self.register()
        self.assertIn("aceptada", self.reply.lower())
        self.writes.clear()
        self.update.message.reply_text.reset_mock()
        self.context.bot.send_message.reset_mock()
        await self.assert_rejected("inscrito")


class BackendSelectionTests(unittest.TestCase):
    def test_local_mode_fails_without_reading_or_writing_json(self):
        with patch.dict(db.os.environ, {}, clear=True), \
             patch.object(db.os.path, "exists", return_value=False), \
             patch.object(db, "_firebase_initialized", False), \
             patch.object(db, "_db", None), \
             patch.object(db, "_inicializar_firebase") as init, \
             patch.object(db, "_cargar_json_local") as read, \
             patch.object(db, "_guardar_json_local") as write:
            with self.assertRaisesRegex(db.ErrorPersistenciaInscripcion, "modo local"):
                db.inscribir_tester("42", "demo")
            init.assert_not_called()
            read.assert_not_called()
            write.assert_not_called()

    def test_configured_but_unavailable_does_not_fallback(self):
        for variable in ("FIREBASE_CREDENTIALS", "FIREBASE_KEY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
            with self.subTest(variable=variable), \
                 patch.dict(db.os.environ, {variable: "simulated"}, clear=True), \
                 patch.object(db, "_firebase_initialized", False), \
                 patch.object(db, "_db", None), \
                 patch.object(db, "_inicializar_firebase", return_value=None), \
                 patch.object(db, "_cargar_json_local") as read, \
                 patch.object(db, "_guardar_json_local") as write:
                with self.assertRaises(db.ErrorPersistenciaInscripcion):
                    db.inscribir_tester("42", "demo")
                read.assert_not_called()
                write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
