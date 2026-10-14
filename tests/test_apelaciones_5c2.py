import os
import unittest
import copy
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import db
import bot
from inscripcion_simulada import FirestoreSimulado, ConflictoLectura


class Apelaciones5C2Tests(unittest.TestCase):
    def setUp(self):
        self.storage = {
            "apps.json": {
                "app_alpha": {
                    "dev_id": "dev_user",
                    "descripcion": "App Alpha",
                    "estado": "en_prueba",
                    "fecha_lanzamiento": "2026-09-20",
                    "timestamp_lanzamiento": "2026-09-20T10:00:00-06:00",
                    "testers": ["tester_juan", "dev_user"],
                    "pruebas": {
                        "tester_juan": {
                            "tester_nombre": "Juan Tester",
                            "estado": "instalada",
                            "dias_completados": 1,
                            "version_revision": 1,
                            "posibles_faltas": {
                                "2026-09-21": {
                                    "fecha_vencimiento": "2026-09-21",
                                    "estado": "injustificada",
                                    "estado_resolucion": "injustificada",
                                    "motivo_resolucion": "Sin entrega en tiempo",
                                    "actor_resolucion": "dev_user",
                                    "id_evento_resolucion": "evt_resolv_1"
                                },
                                "2026-09-22": {
                                    "fecha_vencimiento": "2026-09-22",
                                    "estado": "justificada",
                                    "estado_resolucion": "justificada",
                                    "motivo_resolucion": "Falla de red documentada",
                                    "actor_resolucion": "dev_user",
                                    "id_evento_resolucion": "evt_resolv_2"
                                },
                                "2026-09-23": {
                                    "fecha_vencimiento": "2026-09-23",
                                    "estado": "pendiente_revision"
                                }
                            },
                            "registros": {
                                "2026-09-20": {
                                    "tipo": "checkin",
                                    "estado_entrega": "puntual",
                                    "captura": "cap_20.png"
                                }
                            },
                            "historial_eventos_ids": ["evt_resolv_1", "evt_resolv_2"]
                        }
                    }
                },
                "app_beta": {
                    "dev_id": "other_dev",
                    "descripcion": "App Beta",
                    "estado": "en_prueba",
                    "fecha_lanzamiento": "2026-09-20",
                    "timestamp_lanzamiento": "2026-09-20T10:00:00-06:00",
                    "testers": ["dev_user"],
                    "pruebas": {
                        "dev_user": {
                            "tester_nombre": "Dev User as Tester",
                            "estado": "instalada",
                            "dias_completados": 1,
                            "version_revision": 1,
                            "posibles_faltas": {
                                "2026-09-21": {
                                    "fecha_vencimiento": "2026-09-21",
                                    "estado": "injustificada",
                                    "estado_resolucion": "injustificada",
                                    "motivo_resolucion": "Falta reportada",
                                    "actor_resolucion": "other_dev",
                                    "id_evento_resolucion": "evt_beta_1"
                                }
                            },
                            "registros": {
                                "2026-09-20": {
                                    "tipo": "checkin",
                                    "estado_entrega": "puntual",
                                    "captura": "cap_beta_20.png"
                                }
                            },
                            "historial_eventos_ids": ["evt_beta_1"]
                        }
                    }
                }
            },
            "usuarios.json": {
                "tester_juan": {"nombre": "Juan Tester", "creditos": 0, "boletos_rifa": 0, "apps_testeando": ["app_alpha"]},
                "dev_user": {"nombre": "Dev User", "creditos": 0, "boletos_rifa": 0, "apps_testeando": ["app_beta"]},
                "admin_user": {"nombre": "Admin User", "creditos": 0}
            },
            "auditoria_revisiones.json": {
                "evt_resolv_1": {
                    "id_evento": "evt_resolv_1",
                    "app_id": "app_alpha",
                    "user_id": "tester_juan",
                    "fecha_vencimiento": "2026-09-21",
                    "estado_resolucion_nuevo": "injustificada",
                    "motivo": "Sin entrega en tiempo"
                },
                "evt_resolv_2": {
                    "id_evento": "evt_resolv_2",
                    "app_id": "app_alpha",
                    "user_id": "tester_juan",
                    "fecha_vencimiento": "2026-09-22",
                    "estado_resolucion_nuevo": "justificada",
                    "motivo": "Falla de red documentada"
                },
                "evt_beta_1": {
                    "id_evento": "evt_beta_1",
                    "app_id": "app_beta",
                    "user_id": "dev_user",
                    "fecha_vencimiento": "2026-09-21",
                    "estado_resolucion_nuevo": "injustificada",
                    "motivo": "Falta reportada"
                }
            },
            "solicitudes_revisiones.json": {},
            "apelaciones_revisiones.json": {}
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)
        self.db_patcher = patch.object(db, "_inicializar_firebase", return_value=self.firebase)
        self.cfg_patcher = patch.object(db, "_firebase_configurado", return_value=True)
        self.init_patcher = patch.object(db, "_firebase_initialized", True)
        self.db_patcher.start()
        self.cfg_patcher.start()
        self.init_patcher.start()

    def tearDown(self):
        self.init_patcher.stop()
        self.cfg_patcher.stop()
        self.db_patcher.stop()

    def test_roles_compatibles_y_propiedad_de_casos(self):
        """1. Roles compatibles: Un usuario dev de App Alpha puede ser tester de App Beta y solo apelar casos propios."""
        # dev_user intenta apelar el caso de tester_juan en app_alpha -> RECHAZADO por permiso_denegado
        res_ajeno = self.firebase.ejecutar(
            db.crear_apelacion,
            "dev_user", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Justificación intentada por otro usuario", "sol_apel_ajena"
        )
        self.assertFalse(res_ajeno["exito"])
        self.assertEqual(res_ajeno["motivo"], "permiso_denegado")

        # dev_user apela su PROPIO caso como tester en app_beta -> APROBADO
        res_propio = self.firebase.ejecutar(
            db.crear_apelacion,
            "dev_user", "app_beta", "dev_user", "2026-09-21", "evt_beta_1",
            "Mi prueba falló por mantenimiento del servidor de Google Play", "sol_apel_dev_propia"
        )
        self.assertTrue(res_propio["exito"])
        self.assertEqual(res_propio["estado_apelacion"], "pendiente")

    def test_estados_apelables_injustificada_y_justificada(self):
        """2. Tanto injustificada como justificada son apelables; pendientes sin resolución no lo son."""
        # A. Apelar injustificada (2026-09-21)
        res_inj = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Adjunto reporte de ticket de soporte del ISP", "sol_apel_inj"
        )
        self.assertTrue(res_inj["exito"])
        self.assertEqual(res_inj["estado_resolucion_apelado"], "injustificada")

        # B. Apelar justificada (2026-09-22) - El tester sostiene que sí cumplió y debe contar como día
        res_just = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-22", "evt_resolv_2",
            "Entregué evidencia a tiempo, solicito cómputo de día completado", "sol_apel_just"
        )
        self.assertTrue(res_just["exito"])
        self.assertEqual(res_just["estado_resolucion_apelado"], "justificada")

        # C. Fecha sin resolución manual (2026-09-23) -> Rechazado
        res_pend = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-23", "evt_inexistente",
            "Intento de apelar fecha sin resolución", "sol_apel_pend"
        )
        self.assertFalse(res_pend["exito"])
        self.assertIn(res_pend["motivo"], ["estado_no_apelable", "fecha_sin_resolucion", "resolucion_no_encontrada"])

    def test_garantia_una_apelacion_pendiente_por_resolucion_e_idempotencia(self):
        """3. Garantía transaccional de 1 sola apelación pendiente por resolución e idempotencia con id_solicitud."""
        # Primera apelación: éxito
        res1 = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Justificación válida inicial", "sol_idemp_1"
        )
        self.assertTrue(res1["exito"])

        # Reintento con exactamente el mismo id_solicitud y contenido: devuelve resultado cacheado (idempotencia)
        res_reintento = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Justificación válida inicial", "sol_idemp_1"
        )
        self.assertTrue(res_reintento["exito"])
        self.assertEqual(res_reintento["id_apelacion"], res1["id_apelacion"])

        # Intento de crear OTRA apelación diferente para la misma resolución mientras la primera está pendiente:
        res_duplicada = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Otra justificación diferente pero para la misma fecha pendiente", "sol_idemp_2"
        )
        self.assertFalse(res_duplicada["exito"])
        self.assertEqual(res_duplicada["motivo"], "apelacion_ya_pendiente")

        # Reutilizar id_solicitud con contenido diferente: rechazado
        res_reuso_invalido = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Texto alterado con mismo id de solicitud", "sol_idemp_1"
        )
        self.assertFalse(res_reuso_invalido["exito"])
        self.assertEqual(res_reuso_invalido["motivo"], "id_solicitud_reutilizado_con_contenido_diferente")

    def test_admin_aceptar_apelacion_actualiza_prueba_y_recalcula_dias(self):
        """4. Admin acepta apelación: actualiza la prueba, recalcula días válidos y audita con SERVER_TIMESTAMP."""
        # Crear apelación
        res_ap = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Comprobante entregado a soporte", "sol_ap_acc"
        )
        id_apelacion = res_ap["id_apelacion"]

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            # Non-admin intenta resolver -> Rechazado
            res_no_admin = self.firebase.ejecutar(
                db.resolver_apelacion,
                "dev_user", "app_alpha", id_apelacion, "aceptada", "cumplida_por_revision",
                "Motivo dev no autorizado", "sol_res_no_admin"
            )
            self.assertFalse(res_no_admin["exito"])
            self.assertEqual(res_no_admin["motivo"], "permiso_denegado")

            # Admin resuelve aceptando como cumplida_por_revision (de 1 día pasa a 2 días)
            res_admin = self.firebase.ejecutar(
                db.resolver_apelacion,
                "admin_user", "app_alpha", id_apelacion, "aceptada", "cumplida_por_revision",
                "Comprobante verificado con éxito en logs de red", "sol_res_admin",
                version_revision_esperada=1
            )
            self.assertTrue(res_admin["exito"])
            self.assertEqual(res_admin["decision"], "aceptada")
            self.assertEqual(res_admin["nueva_resolucion"], "cumplida_por_revision")
            self.assertEqual(res_admin["dias_completados"], 2)
            self.assertEqual(res_admin["version_revision"], 2)

        # Comprobar actualización en la campaña
        app = self.storage["apps.json"]["app_alpha"]
        prueba = app["pruebas"]["tester_juan"]
        self.assertEqual(prueba["dias_completados"], 2)
        self.assertEqual(prueba["version_revision"], 2)
        self.assertEqual(prueba["posibles_faltas"]["2026-09-21"]["estado_resolucion"], "cumplida_por_revision")

        # Comprobar estado de apelación
        apel = self.storage["apelaciones_revisiones.json"][id_apelacion]
        self.assertEqual(apel["estado_apelacion"], "aceptada")
        self.assertEqual(apel["nueva_resolucion"], "cumplida_por_revision")
        self.assertEqual(apel["motivo_admin"], "Comprobante verificado con éxito en logs de red")

    def test_admin_rechazar_apelacion_conserva_resolucion_original(self):
        """5. Admin rechaza apelación: mantiene la resolución original y los días verificados sin alteración."""
        res_ap = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Justificación insuficiente", "sol_ap_rej"
        )
        id_apelacion = res_ap["id_apelacion"]

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_admin = self.firebase.ejecutar(
                db.resolver_apelacion,
                "admin_user", "app_alpha", id_apelacion, "rechazada", None,
                "La justificación presentada no acredita entrega dentro del horario", "sol_res_rej",
                version_revision_esperada=1
            )
            self.assertTrue(res_admin["exito"])
            self.assertEqual(res_admin["decision"], "rechazada")
            self.assertEqual(res_admin["nueva_resolucion"], "injustificada")

        app = self.storage["apps.json"]["app_alpha"]
        prueba = app["pruebas"]["tester_juan"]
        self.assertEqual(prueba["dias_completados"], 1) # No cambió
        self.assertEqual(prueba["posibles_faltas"]["2026-09-21"]["estado_resolucion"], "injustificada")

        apel = self.storage["apelaciones_revisiones.json"][id_apelacion]
        self.assertEqual(apel["estado_apelacion"], "rechazada")

    def test_deteccion_de_resolucion_modificada_y_conflicto_concurrente(self):
        """6. Si la resolución apelada cambió o la prueba tiene versión obsoleta, la transacción aborta."""
        res_ap = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Justificación pendiente", "sol_ap_conf"
        )
        id_apelacion = res_ap["id_apelacion"]

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            # A. Conflicto de versión concurrente (se esperaba version 1 pero se envía version 0)
            res_ver = self.firebase.ejecutar(
                db.resolver_apelacion,
                "admin_user", "app_alpha", id_apelacion, "aceptada", "cumplida_por_revision",
                "Motivo prueba versión", "sol_res_v0",
                version_revision_esperada=0
            )
            self.assertFalse(res_ver["exito"])
            self.assertEqual(res_ver["motivo"], "conflicto_modificacion_concurrente")

            # B. Dev modificó previamente la resolución de esa fecha (cambió a justificada)
            app = self.storage["apps.json"]["app_alpha"]
            app["pruebas"]["tester_juan"]["posibles_faltas"]["2026-09-21"]["estado_resolucion"] = "justificada"
            app["pruebas"]["tester_juan"]["posibles_faltas"]["2026-09-21"]["id_evento_resolucion"] = "evt_otro_dev"

            res_mod = self.firebase.ejecutar(
                db.resolver_apelacion,
                "admin_user", "app_alpha", id_apelacion, "aceptada", "cumplida_por_revision",
                "Motivo prueba resolución modificada", "sol_res_mod",
                version_revision_esperada=1
            )
            self.assertFalse(res_mod["exito"])
            self.assertEqual(res_mod["motivo"], "resolucion_apelada_modificada")

    def test_casos_post_expulsion_consulta_apelacion_y_resolucion_sin_reactivacion(self):
        """7. Tester expulsado: consulta /miscasos desde auditoría, apela y se resuelve sin recrear prueba activa."""
        # Expulsar a tester_juan de app_alpha
        self.storage["apps.json"]["app_alpha"]["pruebas"].pop("tester_juan", None)
        self.storage["apps.json"]["app_alpha"]["testers"] = ["dev_user"]
        self.storage["usuarios.json"]["tester_juan"]["apps_testeando"] = []

        # 1. Consulta /miscasos del tester expulsado
        casos = db.obtener_casos_tester("tester_juan")
        self.assertEqual(len(casos), 1)
        self.assertFalse(casos[0]["activa"])
        self.assertTrue(casos[0].get("post_expulsion"))
        self.assertIn("2026-09-21", casos[0]["fechas"])

        # 2. El tester expulsado crea una apelación sobre su fecha histórica
        res_ap = self.firebase.ejecutar(
            db.crear_apelacion,
            "tester_juan", "app_alpha", "tester_juan", "2026-09-21", "evt_resolv_1",
            "Solicito revisión histórica post expulsión", "sol_ap_post_exp"
        )
        self.assertTrue(res_ap["exito"])
        id_apelacion = res_ap["id_apelacion"]

        # 3. Admin resuelve la apelación del expulsado
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_res = self.firebase.ejecutar(
                db.resolver_apelacion,
                "admin_user", "app_alpha", id_apelacion, "aceptada", "cumplida_por_revision",
                "Revisión histórica de admin aprobada", "sol_res_post_exp"
            )
            self.assertTrue(res_res["exito"])
            self.assertTrue(res_res["post_expulsion"])

        # Garantía estricta: NO se recrea la prueba en app["pruebas"] ni se inscribe al usuario
        app = self.storage["apps.json"]["app_alpha"]
        self.assertNotIn("tester_juan", app.get("pruebas", {}))
        self.assertNotIn("tester_juan", app.get("testers", []))

    def test_deshabilitacion_de_ajustardias(self):
        """8. /ajustardias queda deshabilitado en backend y responde con redirección a /casos."""
        # A. Función en db eleva NotImplementedError
        with self.assertRaises(NotImplementedError) as ctx:
            db.deshabilitar_ajuste_arbitrario_dias()
        self.assertIn("revisar_falta_obligacion", str(ctx.exception))

        # B. Handler en bot responde informativo
        update = Mock()
        update.message = AsyncMock()
        update.message.from_user.id = 123456
        context = Mock()

        import asyncio
        asyncio.run(bot.ajustardias(update, context))
        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        mensaje_texto = args[0]
        self.assertIn("/ajustardias ha sido deshabilitado", mensaje_texto)
        self.assertIn("/casos", mensaje_texto)


if __name__ == "__main__":
    unittest.main()
