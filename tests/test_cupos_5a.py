"""Pruebas para la Sub-Etapa 5A: Cupos de 15 por campaña y compatibilidad con antiguas.

Ejecutar con: python -m unittest discover -s tests -v
"""

import ast
import copy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

import db
from inscripcion_simulada import FirestoreSimulado, ScopedMexicoCityTZ

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
        elif isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in {
            "postular", "verapps", "iniciarprueba", "subirapp_descripcion", "misapps", "miscampanas"
        }:
            selected.append(node)
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    clock = Mock()
    clock.now.return_value = datetime(2026, 9, 22, 10, 0, 0)
    namespace = {
        "logger": Mock(),
        "datetime": clock,
        "ConversationHandler": NS(END=-1),
        "ReplyKeyboardRemove": Mock(return_value=None),
        "obtener_teclado_principal": Mock(return_value=None),
        "db": db,
    }
    exec(compile(module, str(BOT_PATH), "exec"), namespace)
    return namespace


class Cupos5ATests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tz_ctx = ScopedMexicoCityTZ()
        self.tz_ctx.__enter__()
        self.dev_id = "700"
        self.storage = {
            "usuarios.json": {
                self.dev_id: {"nombre": "Dev Carlos", "creditos": 1000, "boletos_rifa": 1},
            },
            "apps.json": {},
        }
        for i in range(1, 20):
            uid = str(i)
            self.storage["usuarios.json"][uid] = {
                "nombre": f"Tester_{uid}",
                "creditos": 100,
                "boletos_rifa": 0,
                "apps_testeando": [],
            }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)
        self.handlers = handler_namespace()
        self.handlers["cargar_archivo"] = lambda name: copy.deepcopy(self.storage[name])
        self.handlers["guardar_archivo"] = lambda name, data: self.storage.update({name: copy.deepcopy(data)})

    def tearDown(self):
        if hasattr(self, "tz_ctx"):
            self.tz_ctx.__exit__(None, None, None)

    def test_nueva_campana_crea_con_cupo_15(self):
        """1. Publicar una nueva campaña le asigna min_testers=15 y max_testers=15."""
        res = self.firebase.ejecutar(db.publicar_campana, self.dev_id, "Dev Carlos", "https://play.google.com/store/apps/details?id=com.app5a", "Nueva App 5A", "app_5a")
        self.assertEqual(res["resultado"], "creada")

        app = self.storage["apps.json"]["app_5a"]
        self.assertEqual(app["min_testers"], 15)
        self.assertEqual(app["max_testers"], 15)
        self.assertEqual(app["estado"], "reclutando")

    def test_inscripcion_nueva_campana_hasta_15_y_lista(self):
        """2. Postulación en nueva campaña (cupo 15): testers 1-14 siguen reclutando, tester 15 cambia a lista."""
        self.storage["apps.json"]["app_5a"] = {
            "dev_id": self.dev_id,
            "descripcion": "Nueva App 5A",
            "link": "https://example.invalid",
            "estado": "reclutando",
            "testers": [],
            "min_testers": 15,
            "max_testers": 15,
            "pruebas": {},
        }

        for i in range(1, 15):
            res = self.firebase.ejecutar(db.inscribir_tester, str(i), "app_5a")
            self.assertFalse(res["completa"])
            self.assertEqual(self.storage["apps.json"]["app_5a"]["estado"], "reclutando")

        # Tester 15 completa la meta
        res15 = self.firebase.ejecutar(db.inscribir_tester, "15", "app_5a")
        self.assertTrue(res15["completa"])
        self.assertEqual(self.storage["apps.json"]["app_5a"]["estado"], "lista")

    def test_rechazo_participante_16(self):
        """3. Campaña nueva llena con 15 testers rechaza la postulación del tester 16."""
        self.storage["apps.json"]["app_5a"] = {
            "dev_id": self.dev_id,
            "descripcion": "Nueva App 5A",
            "link": "https://example.invalid",
            "estado": "lista",
            "testers": [str(i) for i in range(1, 16)],
            "min_testers": 15,
            "max_testers": 15,
            "pruebas": {},
        }

        with self.assertRaises(db.InscripcionRechazada) as ctx:
            self.firebase.ejecutar(db.inscribir_tester, "16", "app_5a")
        self.assertIn("ya tiene 15 testers activos", str(ctx.exception))

    async def test_inicio_prueba_exige_15(self):
        """4. iniciarprueba rechaza con 14 testers e inicia exitosamente con 15."""
        self.storage["apps.json"]["app_5a"] = {
            "dev_id": self.dev_id,
            "descripcion": "Nueva App 5A",
            "link": "https://example.invalid",
            "estado": "reclutando",
            "testers": [str(i) for i in range(1, 15)],
            "min_testers": 15,
            "max_testers": 15,
            "pruebas": {},
        }

        update = NS(message=NS(from_user=NS(id=int(self.dev_id)), reply_text=AsyncMock()))
        context = NS(args=["app_5a"], bot=NS(send_message=AsyncMock()), user_data={})

        # Intentar iniciar con 14 testers
        await self.handlers["iniciarprueba"](update, context)
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Aún no juntás los 15 testers mínimos", reply)
        self.assertEqual(self.storage["apps.json"]["app_5a"]["estado"], "reclutando")

        # Inscribir tester 15 y volver a probar iniciarprueba
        self.firebase.ejecutar(db.inscribir_tester, "15", "app_5a")
        self.assertEqual(self.storage["apps.json"]["app_5a"]["estado"], "lista")

        update.message.reply_text.reset_mock()
        await self.handlers["iniciarprueba"](update, context)
        reply_ok = update.message.reply_text.await_args.args[0]
        self.assertIn("¡PRUEBA INICIADA EXITOSAMENTE!", reply_ok)
        self.assertEqual(self.storage["apps.json"]["app_5a"]["estado"], "en_prueba")

    async def test_catalogo_verapps_con_14_inscritos(self):
        """5. verapps muestra la campaña nueva con 14 inscritos y permite postulaciones."""
        self.storage["apps.json"]["app_5a"] = {
            "dev_id": self.dev_id,
            "descripcion": "Nueva App 5A",
            "link": "https://example.invalid",
            "estado": "reclutando",
            "testers": [str(i) for i in range(1, 15)],
            "min_testers": 15,
            "max_testers": 15,
            "pruebas": {},
        }

        update = NS(message=NS(from_user=NS(id=999), reply_text=AsyncMock()))
        context = NS(args=[], bot=NS(send_message=AsyncMock()), user_data={})

        await self.handlers["verapps"](update, context)
        update.message.reply_text.assert_awaited_once()
        text_reply = update.message.reply_text.await_args.args[0]
        self.assertIn("14/15", text_reply)
        self.assertIn("Quedan 1 plazas", text_reply)

    def test_expulsion_de_15_a_14_vuelve_a_reclutando(self):
        """6. Expulsar a un tester en campaña nueva lista (15) la regresa a 'reclutando' (14)."""
        self.storage["apps.json"]["app_5a"] = {
            "dev_id": self.dev_id,
            "descripcion": "Nueva App 5A",
            "link": "https://example.invalid",
            "estado": "lista",
            "testers": [str(i) for i in range(1, 16)],
            "min_testers": 15,
            "max_testers": 15,
            "pruebas": {},
        }
        for i in range(1, 16):
            self.storage["usuarios.json"][str(i)]["apps_testeando"] = ["app_5a"]

        res = self.firebase.ejecutar(db.expulsar_tester, "15", "app_5a")
        self.assertEqual(res["resultado"], "expulsado")
        app = self.storage["apps.json"]["app_5a"]
        self.assertEqual(len(app["testers"]), 14)
        self.assertEqual(app["estado"], "reclutando")

    def test_cupo_antiguo_12(self):
        """7. Campaña antigua explícita con min_testers=12 y max_testers=12 se completa a las 12 plazas."""
        self.storage["apps.json"]["app_antigua"] = {
            "dev_id": self.dev_id,
            "descripcion": "App Antigua 12",
            "link": "https://example.invalid",
            "estado": "reclutando",
            "testers": [str(i) for i in range(1, 12)],
            "min_testers": 12,
            "max_testers": 12,
            "pruebas": {},
        }

        # Tester 12 la marca como lista
        res12 = self.firebase.ejecutar(db.inscribir_tester, "12", "app_antigua")
        self.assertTrue(res12["completa"])
        self.assertEqual(self.storage["apps.json"]["app_antigua"]["estado"], "lista")

        # Tester 13 es rechazado por cupo lleno (12)
        with self.assertRaises(db.InscripcionRechazada) as ctx:
            self.firebase.ejecutar(db.inscribir_tester, "13", "app_antigua")
        self.assertIn("ya tiene 12 testers activos", str(ctx.exception))

    def test_campos_ausentes_compatibilidad_12(self):
        """8. Campaña antigua sin min_testers ni max_testers conserva el comportamiento de 12 por defecto."""
        self.storage["apps.json"]["app_legacy"] = {
            "dev_id": self.dev_id,
            "descripcion": "App Legacy",
            "link": "https://example.invalid",
            "estado": "reclutando",
            "testers": [str(i) for i in range(1, 12)],
            "pruebas": {},
        }

        res12 = self.firebase.ejecutar(db.inscribir_tester, "12", "app_legacy")
        self.assertTrue(res12["completa"])
        self.assertEqual(self.storage["apps.json"]["app_legacy"]["estado"], "lista")

    def test_valores_historicos_12_15(self):
        """9. Campaña histórica min_testers=12 / max_testers=15 se marca lista a las 12 pero rechaza solo al tester 16."""
        self.storage["apps.json"]["app_historica"] = {
            "dev_id": self.dev_id,
            "descripcion": "App Histórica 12/15",
            "link": "https://example.invalid",
            "estado": "reclutando",
            "testers": [str(i) for i in range(1, 12)],
            "min_testers": 12,
            "max_testers": 15,
            "pruebas": {},
        }

        # Tester 12 la marca lista (min_testers=12)
        res12 = self.firebase.ejecutar(db.inscribir_tester, "12", "app_historica")
        self.assertTrue(res12["completa"])
        self.assertEqual(self.storage["apps.json"]["app_historica"]["estado"], "lista")

        # Si regresa a reclutando y se llenan hasta 15
        self.storage["apps.json"]["app_historica"]["estado"] = "reclutando"
        self.storage["apps.json"]["app_historica"]["testers"] = [str(i) for i in range(1, 16)]

        # Tester 16 es rechazado por max_testers=15
        with self.assertRaises(db.InscripcionRechazada) as ctx:
            self.firebase.ejecutar(db.inscribir_tester, "16", "app_historica")
        self.assertIn("ya tiene 15 testers activos", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
