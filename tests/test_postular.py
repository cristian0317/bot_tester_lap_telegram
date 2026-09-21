"""Offline registration tests: extract handlers without importing the running bot.

Run with: python -m unittest discover -s tests -v
No Telegram/Firebase packages, credentials, or persistent data are used.
"""

import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import db
from inscripcion_simulada import FirestoreSimulado


BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"


def handler_namespace():
    tree = ast.parse(BOT_PATH.read_text(encoding="utf-8-sig"))
    selected = []
    constants = {"USUARIOS_FILE", "APPS_FILE", "TESTERS_POR_CAMPANA"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.AsyncFunctionDef) and node.name in {
            "postular", "verapps"
        }:
            selected.append(node)
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    namespace = {"logger": Mock(), "obtener_teclado_principal": Mock(return_value=None)}
    exec(compile(module, str(BOT_PATH), "exec"), namespace)
    return namespace


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reset_fixture()

    def reset_fixture(self):
        self.handlers = handler_namespace()
        self.user_id = "42"
        self.app_id = "app_piloto"
        self.users = {
            self.user_id: {
                "nombre": "Tester", "creditos": 200, "boletos_rifa": 2,
                "referidos": ["43"], "apps_testeando": [],
            },
            "700": {"nombre": "Developer", "creditos": 800},
        }
        self.apps = {
            self.app_id: {
                "dev_id": "700", "descripcion": "Piloto", "link": "https://example.invalid",
                "estado": "reclutando", "testers": [], "min_testers": 12,
                "max_testers": 15, "dias_requeridos": 14, "pruebas": {},
            }
        }
        self.storage = {"usuarios.json": self.users, "apps.json": self.apps}
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)
        self.handlers["db"] = SimpleNamespace(
            inscribir_tester=self.firebase.inscribir,
            InscripcionRechazada=db.InscripcionRechazada,
            ErrorPersistenciaInscripcion=db.ErrorPersistenciaInscripcion,
        )

        def save(name, data):
            self.writes.append(name)
            self.storage[name] = copy.deepcopy(data)

        self.handlers["cargar_archivo"] = lambda name: copy.deepcopy(self.storage[name])
        self.handlers["guardar_archivo"] = save
        self.update = SimpleNamespace(message=SimpleNamespace(
            from_user=SimpleNamespace(id=int(self.user_id), first_name="Tester"),
            reply_text=AsyncMock(),
        ))
        self.context = SimpleNamespace(
            args=[self.app_id], bot=SimpleNamespace(send_message=AsyncMock()), user_data={}
        )

    @property
    def reply(self):
        return "\n".join(call.args[0] for call in self.update.message.reply_text.await_args_list)

    async def register(self):
        await self.handlers["postular"](self.update, self.context)

    async def assert_rejected(self, message_fragment):
        before = copy.deepcopy(self.storage)
        await self.register()
        self.assertEqual(self.storage, before)
        self.assertEqual(self.writes, [])
        self.assertIn(message_fragment, self.reply.lower())
        self.context.bot.send_message.assert_not_awaited()

    async def test_acceptance_persists_both_sides_without_other_changes(self):
        before = copy.deepcopy(self.storage)
        await self.register()
        expected = copy.deepcopy(before)
        expected["usuarios.json"][self.user_id]["apps_testeando"] = [self.app_id]
        expected["apps.json"][self.app_id].update(
            testers=[self.user_id], min_testers=12, max_testers=12
        )
        self.assertEqual(self.storage, expected)
        self.assertCountEqual(self.writes, ["apps.json", "usuarios.json"])
        self.assertIn("aceptada", self.reply.lower())
        self.context.bot.send_message.assert_not_awaited()

    async def test_exactly_one_argument_required(self):
        for args in ([], [self.app_id, "extra"]):
            with self.subTest(args=args):
                self.reset_fixture()
                self.context.args = args
                await self.assert_rejected("/postular")

    async def test_registration_required(self):
        del self.users[self.user_id]
        await self.assert_rejected("registr")

    async def test_unknown_campaign(self):
        self.context.args = ["app_inexistente"]
        await self.assert_rejected("no existe")

    async def test_own_app_rejected_for_string_and_integer_owner(self):
        for owner in (self.user_id, int(self.user_id)):
            with self.subTest(owner=owner):
                self.reset_fixture()
                self.apps[self.app_id]["dev_id"] = owner
                await self.assert_rejected("propia")

    async def test_duplicate_on_either_side_is_rejected_without_repair(self):
        for tester_ids, app_ids in (
            ([self.user_id], [self.app_id]),
            ([self.user_id], []),
            ([int(self.user_id)], []),
            ([], [self.app_id]),
        ):
            with self.subTest(testers=tester_ids, apps=app_ids):
                self.reset_fixture()
                self.apps[self.app_id]["testers"] = tester_ids
                self.users[self.user_id]["apps_testeando"] = app_ids
                await self.assert_rejected("inscrito")

    async def test_non_recruiting_campaigns_are_unavailable_even_when_full(self):
        for state, count in (("lista", 5), ("en_prueba", 0), ("en_prueba", 12), ("cerrada", 0)):
            with self.subTest(state=state, count=count):
                self.reset_fixture()
                self.apps[self.app_id].update(
                    estado=state, testers=[str(100 + i) for i in range(count)]
                )
                await self.assert_rejected("disponible")

    async def test_full_campaign_rejects_legacy_capacity_of_fifteen(self):
        for state in ("reclutando", "lista"):
            with self.subTest(state=state):
                self.reset_fixture()
                self.apps[self.app_id].update(
                    estado=state, testers=[str(100 + i) for i in range(12)]
                )
                await self.assert_rejected("llena")

    async def test_old_records_without_membership_arrays(self):
        del self.users[self.user_id]["apps_testeando"]
        del self.apps[self.app_id]["testers"]
        await self.register()
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["apps_testeando"], [self.app_id])
        self.assertEqual(self.storage["apps.json"][self.app_id]["testers"], [self.user_id])

    async def test_twelfth_marks_ready_not_started_and_thirteenth_is_rejected(self):
        self.apps[self.app_id]["testers"] = [str(100 + i) for i in range(11)]

        async def notification(**kwargs):
            self.assertCountEqual(self.writes, ["apps.json", "usuarios.json"])
            self.assertEqual(str(kwargs["chat_id"]), "700")
            self.assertIn("/iniciarprueba", kwargs["text"])

        self.context.bot.send_message.side_effect = notification
        await self.register()
        campaign = self.storage["apps.json"][self.app_id]
        self.assertEqual(len(campaign["testers"]), 12)
        self.assertEqual(campaign["estado"], "lista")
        self.assertNotIn("fecha_lanzamiento", campaign)
        self.assertEqual(campaign["pruebas"], {})
        self.assertIn("12", self.reply)
        self.context.bot.send_message.assert_awaited_once()

        self.storage["usuarios.json"]["43"] = {"apps_testeando": []}
        self.update.message.from_user.id = 43
        self.update.message.reply_text.reset_mock()
        self.context.bot.send_message.reset_mock()
        self.writes.clear()
        await self.assert_rejected("llena")

    async def test_retry_does_not_write_or_notify_again(self):
        await self.register()
        self.writes.clear()
        self.update.message.reply_text.reset_mock()
        await self.assert_rejected("inscrito")

    async def test_notification_failure_preserves_twelfth_registration(self):
        self.apps[self.app_id]["testers"] = [str(100 + i) for i in range(11)]
        self.context.bot.send_message.side_effect = RuntimeError("offline notification failure")
        await self.register()
        self.assertIn(self.user_id, self.storage["apps.json"][self.app_id]["testers"])
        self.assertEqual(self.storage["apps.json"][self.app_id]["estado"], "lista")
        self.assertEqual(self.storage["usuarios.json"][self.user_id]["apps_testeando"], [self.app_id])
        self.assertIn("aceptada", self.reply.lower())

    async def test_catalog_only_shows_recruiting_campaigns_with_room(self):
        for app_id, state, count in (
            ("app_llena", "reclutando", 12), ("app_lista", "lista", 5),
            ("app_activa", "en_prueba", 5),
        ):
            self.apps[app_id] = dict(self.apps[self.app_id], estado=state,
                                     testers=[str(100 + i) for i in range(count)])
        await self.handlers["verapps"](self.update, self.context)
        self.assertIn(f"/postular {self.app_id}", self.reply)
        for app_id in ("app_llena", "app_lista", "app_activa"):
            self.assertNotIn(app_id, self.reply)
        self.assertEqual(self.writes, [])


class WiringTests(unittest.TestCase):
    def test_command_handler_and_menu_are_registered(self):
        tree = ast.parse(BOT_PATH.read_text(encoding="utf-8-sig"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        registrations = [
            node for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "CommandHandler" and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "postular"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertIsInstance(registrations[0].args[1], ast.Name)
        self.assertEqual(registrations[0].args[1].id, "postular")
        post_init = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "post_init")
        self.assertTrue(any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "BotCommand" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "postular"
            for node in ast.walk(post_init)
        ))


if __name__ == "__main__":
    unittest.main()
