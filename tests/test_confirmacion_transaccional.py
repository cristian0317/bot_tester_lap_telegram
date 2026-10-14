"""Pruebas para la ETAPA 4D: Confirmación de instalación transaccional en TestCoin.

Ejecutar con: python -m unittest discover -s tests -v
"""

import ast
import copy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import db
from inscripcion_simulada import FirestoreSimulado

BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"


def handler_namespace():
    tree = ast.parse(BOT_PATH.read_text(encoding="utf-8-sig"))
    selected = []
    constants = {"USUARIOS_FILE", "APPS_FILE", "CONFIRMAR_FOTO"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in {
            "validar_confirmacion", "confirmar_start", "confirmar_foto"
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


class ConfirmacionTransaccionalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tester_id = "42"
        self.app_id = "app_demo"
        self.dev_id = "700"
        self.fecha_hoy = "2026-09-22"
        self.storage = {
            "usuarios.json": {
                self.tester_id: {
                    "nombre": "Tester Juan",
                    "creditos": 200,
                    "boletos_rifa": 1,
                    "apps_testeando": [self.app_id],
                },
                "43": {
                    "nombre": "Tester Maria",
                    "creditos": 300,
                    "boletos_rifa": 0,
                    "apps_testeando": [self.app_id],
                },
                self.dev_id: {
                    "nombre": "Dev Carlos",
                    "creditos": 1000,
                },
            },
            "apps.json": {
                self.app_id: {
                    "dev_id": self.dev_id,
                    "descripcion": "App Demo",
                    "estado": "en_prueba",
                    "testers": [self.tester_id, "43"],
                    "min_testers": 12,
                    "max_testers": 12,
                    "pruebas": {
                        "43": {
                            "tester_nombre": "Tester Maria",
                            "estado": "instalada",
                            "fecha_inicio": "2026-09-15",
                            "captura_inicial": "foto_43",
                            "dias_completados": 7,
                            "ultimo_checkin": "2026-09-21",
                        }
                    },
                }
            },
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    def _ejecutar(self, *args, **kwargs):
        return self.firebase.ejecutar(db.confirmar_instalacion, *args, **kwargs)

    def test_primera_confirmacion_valida(self):
        """1. Primera confirmación válida: crea la prueba inicial con estado='instalada' y dia 1."""
        res = self._ejecutar(self.tester_id, self.app_id, "foto_42_init", self.fecha_hoy, "Tester Juan")
        self.assertEqual(res["resultado"], "creada")
        self.assertEqual(res["fecha_inicio"], self.fecha_hoy)
        self.assertEqual(res["dev_id"], self.dev_id)

        app = self.storage["apps.json"][self.app_id]
        self.assertIn(self.tester_id, app["pruebas"])
        prueba = app["pruebas"][self.tester_id]
        self.assertEqual(prueba["estado"], "instalada")
        self.assertEqual(prueba["dias_completados"], 1)
        self.assertEqual(prueba["fecha_inicio"], self.fecha_hoy)
        self.assertEqual(prueba["captura_inicial"], "foto_42_init")
        self.assertEqual(prueba["ultimo_checkin"], self.fecha_hoy)
        self.assertEqual(prueba["tester_nombre"], "Tester Juan")

    def test_usuario_o_campana_inexistentes(self):
        """2. Usuario o campaña inexistentes: rechazo pre-escritura con ConfirmacionRechazada."""
        before = copy.deepcopy(self.storage)
        with self.assertRaises(db.ConfirmacionRechazada) as ctx:
            self._ejecutar("999", self.app_id, "foto", self.fecha_hoy, "Desconocido")
        self.assertIn("No estás registrado", str(ctx.exception))
        self.assertEqual(self.storage, before)

        with self.assertRaises(db.ConfirmacionRechazada) as ctx:
            self._ejecutar(self.tester_id, "app_fantasma", "foto", self.fecha_hoy, "Tester Juan")
        self.assertIn("no existe", str(ctx.exception))
        self.assertEqual(self.storage, before)

    def test_campana_reclutando_o_lista(self):
        """3. Campaña en reclutando o lista: rechazo pre-escritura con ConfirmacionRechazada."""
        before = copy.deepcopy(self.storage)
        for estado in ("reclutando", "lista", "cerrada"):
            self.storage["apps.json"][self.app_id]["estado"] = estado
            with self.assertRaises(db.ConfirmacionRechazada) as ctx:
                self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
            self.assertIn("no está en prueba", str(ctx.exception))

    def test_inscripcion_ausente_o_inconsistente(self):
        """4. Inscripción ausente o inconsistente: rechazo sin reparar datos ni escribir."""
        app = self.storage["apps.json"][self.app_id]
        user = self.storage["usuarios.json"][self.tester_id]

        # Inconsistencia: en app pero no en usuario
        user["apps_testeando"] = []
        with self.assertRaises(db.ConfirmacionRechazada) as ctx:
            self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
        self.assertIn("inconsistente", str(ctx.exception))

        # Inconsistencia: en usuario pero no en app
        user["apps_testeando"] = [self.app_id]
        app["testers"] = ["43"]
        with self.assertRaises(db.ConfirmacionRechazada) as ctx:
            self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
        self.assertIn("inconsistente", str(ctx.exception))

        # Ausente en ambos
        user["apps_testeando"] = []
        app["testers"] = ["43"]
        with self.assertRaises(db.ConfirmacionRechazada) as ctx:
            self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
        self.assertIn("no estás inscrito", str(ctx.exception).lower())

    def test_prueba_activa_o_completada_conservacion_exacta(self):
        """5. Prueba activa o completada: devolución idempotente y conservación exacta."""
        for state, res_esperado, msg_esperado in (
            ("instalada", "ya_instalada", "en curso"),
            ("completada", "ya_completada", "completaste"),
        ):
            with self.subTest(state=state):
                self.setUp()
                app = self.storage["apps.json"][self.app_id]
                app["pruebas"][self.tester_id] = {
                    "tester_nombre": "Original",
                    "estado": state,
                    "fecha_inicio": "2026-09-01",
                    "captura_inicial": "foto_orig",
                    "dias_completados": 14 if state == "completada" else 5,
                    "ultimo_checkin": "2026-09-20",
                }
                before = copy.deepcopy(app["pruebas"][self.tester_id])
                res = self._ejecutar(self.tester_id, self.app_id, "foto_nueva", self.fecha_hoy, "Tester Modificado")
                self.assertEqual(res["resultado"], res_esperado)
                self.assertIn(msg_esperado, res["mensaje"])
                self.assertEqual(app["pruebas"][self.tester_id], before)

    def test_dos_confirmaciones_concurrentes(self):
        """6. Dos confirmaciones concurrentes: solo una crea la prueba; la segunda es idempotente."""
        res1 = self._ejecutar(self.tester_id, self.app_id, "foto_1", self.fecha_hoy, "Tester Juan")
        self.assertEqual(res1["resultado"], "creada")

        res2 = self._ejecutar(self.tester_id, self.app_id, "foto_2", self.fecha_hoy, "Tester Juan")
        self.assertEqual(res2["resultado"], "ya_instalada")

        app = self.storage["apps.json"][self.app_id]
        self.assertEqual(app["pruebas"][self.tester_id]["captura_inicial"], "foto_1")

    def test_expulsion_concurrente_antes_del_commit(self):
        """7. Expulsión concurrente antes del commit: relee la inscripción eliminada y rechaza."""
        def expulsa_durante_lectura():
            self.storage["apps.json"][self.app_id]["testers"].remove(self.tester_id)
            self.storage["usuarios.json"][self.tester_id]["apps_testeando"].remove(self.app_id)

        self.firebase.before_commit = expulsa_durante_lectura
        with self.assertRaises(db.ConfirmacionRechazada) as ctx:
            self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
        self.assertIn("no estás inscrito", str(ctx.exception).lower())
        self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id].get("pruebas", {}))

    def test_expulsion_posterior_a_la_confirmacion(self):
        """8. Expulsión posterior a la confirmación: remueve al tester y su prueba de la campaña."""
        res_conf = self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
        self.assertEqual(res_conf["resultado"], "creada")
        self.assertIn(self.tester_id, self.storage["apps.json"][self.app_id]["pruebas"])

        res_exp = self.firebase.ejecutar(db.expulsar_tester, self.tester_id, self.app_id)
        self.assertEqual(res_exp["resultado"], "expulsado")
        self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id]["pruebas"])
        self.assertNotIn(self.tester_id, self.storage["apps.json"][self.app_id]["testers"])

    def test_fallo_de_commit_sin_escrituras_parciales(self):
        """9. Fallo de commit sin escrituras parciales."""
        before = copy.deepcopy(self.storage)
        self.firebase.fail_commit = True
        with self.assertRaises(db.ErrorPersistenciaConfirmacion):
            self._ejecutar(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
        self.assertEqual(self.storage, before)

    def test_respuesta_perdida_y_reintento(self):
        """10. Respuesta perdida tras commit: el reintento reconoce la prueba creada sin duplicar."""
        self.firebase.lose_ack = True
        with self.assertRaises(db.ErrorPersistenciaConfirmacion):
            self._ejecutar(self.tester_id, self.app_id, "foto_orig", self.fecha_hoy, "Tester Juan")

        # El commit ocurrió
        app = self.storage["apps.json"][self.app_id]
        self.assertIn(self.tester_id, app["pruebas"])

        # Reintento tras respuesta perdida
        self.firebase.lose_ack = False
        res_retry = self._ejecutar(self.tester_id, self.app_id, "foto_reintento", self.fecha_hoy, "Tester Juan")
        self.assertEqual(res_retry["resultado"], "ya_instalada")
        self.assertEqual(app["pruebas"][self.tester_id]["captura_inicial"], "foto_orig")

    def test_modo_local_sin_escrituras(self):
        """11. Modo local sin escrituras: rechaza con ErrorPersistenciaConfirmacion sin modificar JSON."""
        before = copy.deepcopy(self.storage)
        with patch.object(db, "_firebase_configurado", return_value=False):
            with self.assertRaises(db.ErrorPersistenciaConfirmacion) as ctx:
                db.confirmar_instalacion(self.tester_id, self.app_id, "foto", self.fecha_hoy, "Tester Juan")
            self.assertIn("no está disponible en modo local", str(ctx.exception))
        self.assertEqual(self.storage, before)

    def test_conservacion_de_pruebas_de_otros_testers(self):
        """12. Conservación de pruebas de otros testers."""
        prueba_43_antes = copy.deepcopy(self.storage["apps.json"][self.app_id]["pruebas"]["43"])
        res = self._ejecutar(self.tester_id, self.app_id, "foto_42", self.fecha_hoy, "Tester Juan")
        self.assertEqual(res["resultado"], "creada")

        pruebas = self.storage["apps.json"][self.app_id]["pruebas"]
        self.assertIn("43", pruebas)
        self.assertEqual(pruebas["43"], prueba_43_antes)
        self.assertIn(self.tester_id, pruebas)

    async def test_recorrido_confirmar_foto_con_telegram_simulado(self):
        """13. Recorrido de confirmar_foto con Telegram simulado."""
        handlers = handler_namespace()
        handlers["db"] = NS(
            confirmar_instalacion=lambda u, a, f, d, n, *args, **kwargs: self.firebase.ejecutar(
                db.confirmar_instalacion, u, a, f, d, n, *args, **kwargs
            ),
            ConfirmacionRechazada=db.ConfirmacionRechazada,
            ErrorPersistenciaConfirmacion=db.ErrorPersistenciaConfirmacion,
        )

        update = NS(message=NS(
            from_user=NS(id=int(self.tester_id), first_name="Tester Juan"),
            photo=[NS(file_id="foto_pequena"), NS(file_id="foto_telegram_hd")],
            reply_text=AsyncMock(),
        ))
        context = NS(
            user_data={"confirmar_app_id": self.app_id},
            bot=NS(send_message=AsyncMock()),
        )

        res = await handlers["confirmar_foto"](update, context)
        self.assertEqual(res, -1)

        # Verificar respuesta al usuario
        update.message.reply_text.assert_awaited_once()
        text_reply = update.message.reply_text.await_args.args[0]
        self.assertIn("¡Instalación confirmada exitosamente!", text_reply)

        # Verificar notificación al desarrollador
        context.bot.send_message.assert_awaited_once()
        dev_call = context.bot.send_message.await_args
        self.assertEqual(dev_call.kwargs["chat_id"], self.dev_id)
        self.assertIn("¡Nuevo tester activado!", dev_call.kwargs["text"])

        # Verificar que la prueba se guardó en Firestore simulado
        app = self.storage["apps.json"][self.app_id]
        self.assertEqual(app["pruebas"][self.tester_id]["captura_inicial"], "foto_telegram_hd")


if __name__ == "__main__":
    unittest.main()
