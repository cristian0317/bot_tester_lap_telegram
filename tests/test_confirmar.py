"""Offline installation tests; no bot imports, credentials or real storage.

Run: python -B -m unittest discover -s tests -v
"""

import ast
import copy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock


def handler_namespace():
    path = Path(__file__).resolve().parents[1] / "bot.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = {"validar_confirmacion", "confirmar_start", "confirmar_foto", "checkin_start"}
    constants = {"USUARIOS_FILE", "APPS_FILE", "CONFIRMAR_FOTO", "CHECKIN_FOTO"}
    selected = [node for node in tree.body if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants for target in node.targets
        )
    )]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    clock = Mock()
    clock.now.return_value = datetime(2026, 9, 20, 12)
    namespace = {
        "datetime": clock, "ConversationHandler": NS(END=-1),
        "ReplyKeyboardRemove": Mock(return_value=None),
        "obtener_teclado_principal": Mock(return_value=None),
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reset_fixture()

    def reset_fixture(self):
        self.handlers = handler_namespace()
        self.storage = {
            "usuarios.json": {"42": {
                "nombre": "Tester", "apps_testeando": ["demo"],
                "creditos": 200, "boletos_rifa": 3, "referidos": ["43"],
            }},
            "apps.json": {"demo": {
                "dev_id": "700", "descripcion": "Demo", "estado": "en_prueba",
                "testers": ["42"], "pruebas": {}, "dias_requeridos": 14,
            }},
        }
        self.writes = []

        def save(name, data):
            self.writes.append(name)
            self.storage[name] = copy.deepcopy(data)

        self.handlers["cargar_archivo"] = lambda name: copy.deepcopy(self.storage[name])
        self.handlers["guardar_archivo"] = save
        self.update = NS(message=NS(
            from_user=NS(id=42, first_name="Tester"),
            photo=[NS(file_id="small"), NS(file_id="original")], reply_text=AsyncMock(),
        ))
        self.context = NS(args=["demo"], user_data={}, bot=NS(send_message=AsyncMock()))

    @property
    def app(self):
        return self.storage["apps.json"]["demo"]

    @property
    def user(self):
        return self.storage["usuarios.json"]["42"]

    def existing_trial(self, state="instalada"):
        return {
            "estado": state, "dias_completados": 14 if state == "completada" else 8,
            "fecha_inicio": "2026-09-01", "ultimo_checkin": "2026-09-19",
            "captura_inicial": "initial", "ultima_captura": "latest",
            "tester_nombre": "Original", "extra": {"preservar": [1, 2]},
        }

    async def call(self, name):
        return await self.handlers[name](self.update, self.context)

    async def assert_rejected(self, name, fragment):
        before = copy.deepcopy(self.storage)
        self.update.message.reply_text.reset_mock()
        self.context.bot.send_message.reset_mock()
        self.writes.clear()
        self.assertEqual(await self.call(name), -1)
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])
        self.context.bot.send_message.assert_not_awaited()
        reply = self.update.message.reply_text.await_args.args[0].lower()
        self.assertIn(fragment, reply)
        if name.startswith("confirmar"):
            self.assertNotIn("confirmar_app_id", self.context.user_data)
        return reply

    async def test_valid_confirmation_preserves_current_schema_and_other_data(self):
        before = copy.deepcopy(self.storage)
        self.assertEqual(await self.call("confirmar_start"), 10)
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])
        self.assertEqual(await self.call("confirmar_foto"), -1)
        expected = copy.deepcopy(before)
        expected["apps.json"]["demo"]["pruebas"]["42"] = {
            "tester_nombre": "Tester", "estado": "instalada", "fecha_inicio": "2026-09-20",
            "captura_inicial": "original", "dias_completados": 1, "ultimo_checkin": "2026-09-20",
        }
        self.assertEqual(self.storage, expected)
        self.assertEqual(self.writes, ["apps.json"])
        self.context.bot.send_message.assert_awaited_once()

    async def test_button_selects_last_membership_and_accepts_integer_tester_id(self):
        self.context.args = []
        self.app["testers"] = [42]
        self.assertEqual(await self.call("confirmar_start"), 10)
        self.assertEqual(self.context.user_data["confirmar_app_id"], "demo")
        await self.call("confirmar_foto")
        self.assertIn("42", self.app["pruebas"])

    async def test_unregistered_user_at_both_steps(self):
        for handler in ("confirmar_start", "confirmar_foto"):
            with self.subTest(handler=handler):
                self.reset_fixture()
                del self.storage["usuarios.json"]["42"]
                self.context.user_data["confirmar_app_id"] = "demo"
                await self.assert_rejected(handler, "no estás registrado")

    async def test_missing_campaign_at_both_steps(self):
        for handler in ("confirmar_start", "confirmar_foto"):
            with self.subTest(handler=handler):
                self.reset_fixture()
                del self.storage["apps.json"]["demo"]
                self.context.user_data["confirmar_app_id"] = "demo"
                await self.assert_rejected(handler, "no existe")

    async def test_campaign_must_be_running_at_both_steps(self):
        for handler in ("confirmar_start", "confirmar_foto"):
            for state in ("reclutando", "lista", "cerrada", None):
                with self.subTest(handler=handler, state=state):
                    self.reset_fixture()
                    self.app["estado"] = state
                    self.context.user_data["confirmar_app_id"] = "demo"
                    await self.assert_rejected(handler, "no está en prueba")

    async def test_absent_and_inconsistent_membership_at_both_steps(self):
        for handler in ("confirmar_start", "confirmar_foto"):
            for testers, memberships, fragment in (
                ([], [], "no estás inscrito"),
                (["42"], [], "inconsistente"),
                ([], ["demo"], "inconsistente"),
            ):
                with self.subTest(handler=handler, testers=testers, memberships=memberships):
                    self.reset_fixture()
                    self.app["testers"] = testers
                    self.user["apps_testeando"] = memberships
                    self.context.user_data["confirmar_app_id"] = "demo"
                    await self.assert_rejected(handler, fragment)

    async def test_expulsion_between_command_and_photo(self):
        self.assertEqual(await self.call("confirmar_start"), 10)
        self.app["testers"].remove("42")
        self.user["apps_testeando"].remove("demo")
        await self.assert_rejected("confirmar_foto", "no estás inscrito")

    async def test_reloads_registration_campaign_state_and_relationships_after_command(self):
        mutations = [
            (lambda: self.storage["usuarios.json"].pop("42"), "no estás registrado"),
            (lambda: self.storage["apps.json"].pop("demo"), "no existe"),
            (lambda: self.app.update(estado="lista"), "no está en prueba"),
            (lambda: self.app.update(testers=[]), "inconsistente"),
            (lambda: self.user.update(apps_testeando=[]), "inconsistente"),
        ]
        for mutate, fragment in mutations:
            with self.subTest(fragment=fragment):
                self.reset_fixture()
                self.assertEqual(await self.call("confirmar_start"), 10)
                mutate()
                await self.assert_rejected("confirmar_foto", fragment)

    async def test_active_and_completed_trials_are_preserved_exactly_at_both_steps(self):
        for handler in ("confirmar_start", "confirmar_foto"):
            for state, fragment in (("instalada", "en curso"), ("completada", "ya completaste")):
                with self.subTest(handler=handler, state=state):
                    self.reset_fixture()
                    self.app["pruebas"]["42"] = self.existing_trial(state)
                    self.context.user_data["confirmar_app_id"] = "demo"
                    await self.assert_rejected(handler, fragment)

    async def test_trial_created_while_waiting_for_photo_is_not_overwritten(self):
        for state in ("instalada", "completada"):
            with self.subTest(state=state):
                self.reset_fixture()
                self.assertEqual(await self.call("confirmar_start"), 10)
                self.app["pruebas"]["42"] = self.existing_trial(state)
                await self.assert_rejected("confirmar_foto", "se conserva")

    async def test_existing_empty_trial_is_not_overwritten(self):
        self.app["pruebas"]["42"] = {}
        self.context.user_data["confirmar_app_id"] = "demo"
        await self.assert_rejected("confirmar_foto", "en curso")

    async def test_completed_checkin_does_not_suggest_reconfirmation(self):
        self.app["pruebas"]["42"] = self.existing_trial("completada")
        self.context.user_data["checkin_app_id"] = "demo"
        reply = await self.assert_rejected("checkin_start", "ya completaste")
        for text in ("confirmar", "captura inicial", "/confirmar", "reinici"):
            self.assertNotIn(text, reply)
        self.assertNotIn("checkin_app_id", self.context.user_data)

    async def test_active_checkin_still_requests_daily_photo(self):
        self.app["pruebas"]["42"] = self.existing_trial()
        before = copy.deepcopy(self.storage)
        self.assertEqual(await self.call("checkin_start"), 11)
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])

    async def test_photo_without_pending_campaign_is_rejected(self):
        await self.assert_rejected("confirmar_foto", "error")

    async def test_button_without_memberships_clears_stale_selection(self):
        self.context.args = []
        self.user["apps_testeando"] = []
        self.context.user_data["confirmar_app_id"] = "old"
        await self.assert_rejected("confirmar_start", "no estás postulado")


if __name__ == "__main__":
    unittest.main()
