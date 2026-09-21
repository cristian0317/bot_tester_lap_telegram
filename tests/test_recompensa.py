"""Finalizacion y proteccion de saldos, sin red ni archivos de datos."""
import ast
import copy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from io import StringIO
import json
import db
from inscripcion_simulada import FirestoreSimulado


class RewardTests(unittest.TestCase):
    def setUp(self):
        self.storage = {
            "usuarios.json": {"42": {"creditos": 200, "boletos_rifa": 2, "apps_testeando": ["demo"]}},
            "apps.json": {"demo": {"estado": "en_prueba", "dev_id": "700", "testers": ["42"],
                "descripcion": "Demo", "pruebas": {"42": {"estado": "instalada", "dias_completados": 13,
                    "ultimo_checkin": "2026-09-19", "captura_inicial": "original", "fecha_inicio": "2026-09-01"}}}},
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    def pay(self):
        return self.firebase.ejecutar(db.registrar_checkin, "42", "demo", "photo", "2026-09-20")

    def assert_paid_once(self):
        user = self.storage["usuarios.json"]["42"]
        self.assertEqual(user["creditos"], 1000)
        self.assertEqual(user["boletos_rifa"], 3)
        trial = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(trial["estado"], "completada")
        self.assertEqual(trial["dias_completados"], 14)
        self.assertEqual(trial["captura_inicial"], "original")
        self.assertEqual(self.storage["recompensas.json"], {
            db.id_movimiento_recompensa("demo", "42"): {
                "usuario": "42", "campana": "demo", "importe": 800, "boletos": 1, "fecha": "SERVER_TIMESTAMP"}})

    def test_valid_completion_and_repeat(self):
        self.assertEqual(self.pay()["resultado"], "completada")
        self.assert_paid_once()
        before = copy.deepcopy(self.storage)
        self.writes.clear()
        self.assertEqual(self.pay()["resultado"], "ya_pagada")
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])

    def test_two_concurrent_completions(self):
        self.firebase.before_commit = self.pay
        self.assertEqual(self.pay()["resultado"], "ya_pagada")
        self.assert_paid_once()
        self.assertEqual(len(self.writes), 3)

    def test_failure_before_commit_changes_nothing(self):
        before = copy.deepcopy(self.storage)
        self.firebase.fail_commit = True
        with self.assertRaises(db.ErrorRecompensa):
            self.pay()
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])

    def test_transaction_retry_preserves_concurrent_balance_changes(self):
        self.firebase.before_commit = lambda: self.storage["usuarios.json"]["42"].update(creditos=250, boletos_rifa=4)
        self.assertEqual(self.pay()["resultado"], "completada")
        self.assertEqual(self.firebase.attempts, 2)
        self.assertEqual(self.storage["usuarios.json"]["42"]["creditos"], 1050)
        self.assertEqual(self.storage["usuarios.json"]["42"]["boletos_rifa"], 5)
        self.assertEqual(len(self.storage["recompensas.json"]), 1)

    def test_lost_commit_ack_does_not_pay_twice(self):
        self.firebase.lose_ack = True
        with self.assertRaises(db.ErrorRecompensa):
            self.pay()
        self.firebase.lose_ack = False
        self.assertEqual(self.pay()["resultado"], "ya_pagada")
        self.assert_paid_once()

    def test_legacy_completed_requires_reconciliation(self):
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["estado"] = "completada"
        before = copy.deepcopy(self.storage)
        with self.assertRaisesRegex(db.ErrorRecompensa, "conciliación"):
            self.pay()
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])

    def test_invalid_current_registration_and_campaign(self):
        for change in (
            lambda: self.storage["usuarios.json"].clear(),
            lambda: self.storage["apps.json"].clear(),
            lambda: self.storage["apps.json"]["demo"].update(estado="lista"),
            lambda: self.storage["apps.json"]["demo"].update(testers=[]),
            lambda: self.storage["usuarios.json"]["42"].update(apps_testeando=[]),
            lambda: self.storage["apps.json"]["demo"]["pruebas"].clear(),
        ):
            self.setUp()
            change()
            before = copy.deepcopy(self.storage)
            with self.assertRaises(db.ErrorRecompensa):
                self.pay()
            self.assertEqual(self.storage, before)
            self.assertEqual(self.writes, [])

    def test_progress_below_threshold_and_same_day_retry(self):
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 5
        self.assertEqual(self.pay()["resultado"], "checkin")
        self.assertEqual(self.pay()["resultado"], "ya_registrado")
        self.assertNotIn("recompensas.json", self.storage)
        self.assertEqual(self.storage["usuarios.json"]["42"]["creditos"], 200)

    def test_local_and_unavailable_backend_never_write_json(self):
        for configured in (False, True):
            with patch.object(db, "_firebase_configurado", return_value=configured), \
                 patch.object(db, "_inicializar_firebase", return_value=None), \
                 patch.object(db, "_guardar_json_local") as write:
                with self.assertRaises(db.ErrorRecompensa):
                    db.registrar_checkin("42", "demo", "photo", "2026-09-20")
                write.assert_not_called()

    def test_stale_general_writes_cannot_undo_reward_or_state(self):
        users = db.SnapshotColeccion(copy.deepcopy(self.storage["usuarios.json"]))
        apps = db.SnapshotColeccion(copy.deepcopy(self.storage["apps.json"]))
        users["42"]["creditos"] += 50
        apps["demo"]["pruebas"]["42"]["dias_completados"] = 3
        self.pay()
        for fn, data in ((db.guardar_usuarios, users), (db.guardar_apps, apps)):
            with self.assertRaises(db.ConflictoGuardado):
                self.firebase.ejecutar(fn, data)
        self.assert_paid_once()

    def test_general_write_retries_cannot_overwrite_concurrent_reward(self):
        users = db.SnapshotColeccion(copy.deepcopy(self.storage["usuarios.json"]))
        users["42"]["creditos"] += 50
        self.firebase.before_commit = self.pay
        with self.assertRaises(db.ConflictoGuardado):
            self.firebase.ejecutar(db.guardar_usuarios, users)
        self.assert_paid_once()

    def test_confirmed_general_balance_movement_is_preserved(self):
        users = db.SnapshotColeccion(copy.deepcopy(self.storage["usuarios.json"]))
        users["42"]["creditos"] += 50
        self.firebase.ejecutar(db.guardar_usuarios, users)
        self.pay()
        self.assertEqual(self.storage["usuarios.json"]["42"]["creditos"], 1050)

    def test_unversioned_bulk_and_individual_writes_rejected(self):
        for fn, args in ((db.guardar_usuarios, ({},)), (db.guardar_apps, ({},)),
                         (db.guardar_usuario, ("42", {})), (db.guardar_app, ("demo", {}))):
            with self.assertRaises(db.ConflictoGuardado):
                self.firebase.ejecutar(fn, *args)
        self.assertEqual(self.writes, [])


class RewardHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = RewardTests()
        self.fixture.setUp()
        tree = ast.parse(Path("bot.py").read_text(encoding="utf-8-sig"))
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "checkin_foto"]
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
        clock = Mock()
        clock.now.return_value = datetime(2026, 9, 20)
        self.namespace = {"db": NS(ErrorRecompensa=db.ErrorRecompensa,
            registrar_checkin=lambda *args: self.fixture.firebase.ejecutar(db.registrar_checkin, *args)),
            "datetime": clock, "ConversationHandler": NS(END=-1), "obtener_teclado_principal": lambda _: None}
        exec(compile(module, "bot.py", "exec"), self.namespace)
        self.update = NS(message=NS(from_user=NS(id=42, first_name="Tester"),
            photo=[NS(file_id="photo")], reply_text=AsyncMock()))
        self.context = NS(user_data={"checkin_app_id": "demo"}, bot=NS(send_message=AsyncMock()))

    async def run_handler(self):
        await self.namespace["checkin_foto"](self.update, self.context)

    async def test_notification_failure_after_payment_retry_has_no_second_payment(self):
        self.context.bot.send_message.side_effect = RuntimeError("offline")
        await self.run_handler()
        self.fixture.assert_paid_once()
        self.context.bot.send_message.assert_awaited_once()
        await self.run_handler()
        self.fixture.assert_paid_once()
        self.context.bot.send_message.assert_awaited_once()

    async def test_user_message_failure_after_payment_retry_has_no_second_payment(self):
        self.update.message.reply_text.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            await self.run_handler()
        self.fixture.assert_paid_once()
        self.update.message.reply_text.side_effect = None
        await self.run_handler()
        self.fixture.assert_paid_once()

    async def test_commit_failure_never_announces_payment(self):
        self.fixture.firebase.fail_commit = True
        before = copy.deepcopy(self.fixture.storage)
        await self.run_handler()
        text = self.update.message.reply_text.await_args.args[0]
        self.assertIn("No se pudo confirmar", text)
        self.assertNotIn("Otorgada", text)
        self.assertEqual(self.fixture.storage, before)
        self.context.bot.send_message.assert_not_awaited()


class MigrationProtectionTests(unittest.TestCase):
    def test_import_cannot_overwrite_existing_paid_documents(self):
        tree = ast.parse(Path("migrate_to_firebase.py").read_text(encoding="utf-8"))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "migrar")
        existing = {("usuarios", "42"): {"creditos": 1000},
                    ("apps", "demo"): {"estado": "en_prueba", "pruebas": {"42": {"estado": "completada"}}}}
        before = copy.deepcopy(existing)

        class Batch:
            def __init__(self):
                self.pending = []

            def create(self, ref, value):
                self.pending.append((ref, value))

            def commit(self):
                if any(ref in existing for ref, _ in self.pending):
                    raise RuntimeError("already exists")
                existing.update(self.pending)

        client = NS(batch=Batch, collection=lambda name: NS(document=lambda key: (name, key)))
        files = {"usuarios.json": '{"42": {"creditos": 200}}',
                 "apps.json": '{"demo": {"pruebas": {"42": {"estado": "instalada"}}}}'}
        namespace = {"is_firebase_active": lambda: True, "_inicializar_firebase": lambda: client,
            "USUARIOS_FILE": "usuarios.json", "APPS_FILE": "apps.json", "logger": Mock(),
            "os": NS(path=NS(exists=lambda _: True)), "json": json,
            "open": lambda name, *args, **kwargs: StringIO(files[name])}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "migrate_to_firebase.py", "exec"), namespace)
        namespace["migrar"]()
        self.assertEqual(existing, before)
        self.assertEqual(namespace["logger"].error.call_count, 2)


if __name__ == "__main__":
    unittest.main()
