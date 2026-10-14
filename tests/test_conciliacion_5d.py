"""Pruebas unitarias e integradas para la Sub-Etapa 5D de TestCoin.
Conciliación definitiva, liquidación transaccional de recompensas y cierre de campañas.

Ejecutar con: python -m unittest discover -s tests -v
"""

import copy
from datetime import datetime
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import db
from inscripcion_simulada import FirestoreSimulado, ConflictoLectura


class Conciliacion5DTests(unittest.TestCase):
    def setUp(self):
        self.dt_now = datetime(2026, 9, 25, 12, 0, 0)
        self.dev_id = "dev_master"
        self.app_id = "app_demo_5d"

        # 14 fechas válidas
        self.fechas_14 = [f"2026-09-{i:02d}" for i in range(1, 15)]
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
                "tester_1": {
                    "nombre": "Tester Uno",
                    "creditos": 200,
                    "boletos_rifa": 0,
                    "apps_testeando": [self.app_id]
                },
                "tester_incompleto": {
                    "nombre": "Tester Incompleto",
                    "creditos": 100,
                    "boletos_rifa": 0,
                    "apps_testeando": [self.app_id]
                },
                "tester_justificado": {
                    "nombre": "Tester Justificado",
                    "creditos": 300,
                    "boletos_rifa": 1,
                    "apps_testeando": [self.app_id]
                },
                "tester_apelando": {
                    "nombre": "Tester Apelando",
                    "creditos": 400,
                    "boletos_rifa": 0,
                    "apps_testeando": [self.app_id]
                },
                "tester_expulsado": {
                    "nombre": "Tester Expulsado",
                    "creditos": 50,
                    "boletos_rifa": 0,
                    "apps_testeando": []
                },
                "tester_legacy": {
                    "nombre": "Tester Legacy",
                    "creditos": 150,
                    "boletos_rifa": 0,
                    "apps_testeando": [self.app_id]
                },
                self.dev_id: {
                    "nombre": "Dev Master",
                    "creditos": 5000,
                    "boletos_rifa": 5,
                    "apps_testeando": []
                }
            },
            "apps.json": {
                self.app_id: {
                    "dev_id": self.dev_id,
                    "descripcion": "Demo App 5D",
                    "estado": "en_prueba",
                    "fecha_lanzamiento": "2026-09-01",
                    "timestamp_lanzamiento": "2026-09-01T10:00:00-06:00",
                    "min_testers": 15,
                    "max_testers": 15,
                    "testers": ["tester_1", "tester_incompleto", "tester_justificado", "tester_apelando", "tester_legacy"],
                    "pruebas": {
                        "tester_1": {
                            "tester_nombre": "Tester Uno",
                            "estado": "instalada",
                            "fecha_inicio": "2026-09-01",
                            "dias_completados": 14,
                            "registros": copy.deepcopy(registros_14),
                            "posibles_faltas": {}
                        },
                        "tester_incompleto": {
                            "tester_nombre": "Tester Incompleto",
                            "estado": "instalada",
                            "fecha_inicio": "2026-09-01",
                            "dias_completados": 13,
                            "registros": {f: registros_14[f] for f in self.fechas_14[:13]},
                            "posibles_faltas": {}
                        },
                        "tester_justificado": {
                            "tester_nombre": "Tester Justificado",
                            "estado": "instalada",
                            "fecha_inicio": "2026-09-01",
                            "dias_completados": 14,
                            # 14 días puntuales + 1 día adicional justificado
                            "registros": copy.deepcopy(registros_14),
                            "posibles_faltas": {
                                "2026-09-15": {
                                    "fecha_vencimiento": "2026-09-15",
                                    "estado": "justificada",
                                    "estado_resolucion": "justificada",
                                    "id_evento_resolucion": "evt_just_15"
                                }
                            }
                        },
                        "tester_apelando": {
                            "tester_nombre": "Tester Apelando",
                            "estado": "instalada",
                            "fecha_inicio": "2026-09-01",
                            "dias_completados": 14,
                            "registros": copy.deepcopy(registros_14),
                            "posibles_faltas": {
                                "2026-09-15": {
                                    "fecha_vencimiento": "2026-09-15",
                                    "estado": "injustificada",
                                    "estado_resolucion": "injustificada",
                                    "id_evento_resolucion": "evt_apel_15"
                                }
                            }
                        },
                        "tester_expulsado": {
                            "tester_nombre": "Tester Expulsado",
                            "estado": "expulsada",
                            "fecha_inicio": "2026-09-01",
                            "dias_completados": 14,
                            "registros": copy.deepcopy(registros_14),
                            "posibles_faltas": {}
                        },
                        "tester_legacy": {
                            "tester_nombre": "Tester Legacy",
                            "estado": "instalada",
                            "fecha_inicio": "2026-09-01",
                            "dias_completados": 14,
                            "dias_completados_legacy": 12,
                            "incertidumbre_progreso_legacy": True,
                            "requiere_conciliacion_legacy": True,
                            "requiere_conciliacion": True,
                            "registros": {f: registros_14[f] for f in self.fechas_14[:2]},
                            "posibles_faltas": {}
                        }
                    }
                }
            },
            "recompensas.json": {},
            "apelaciones_revisiones.json": {
                f"{self.app_id}_tester_apelando_evt_apel_15": {
                    "id_apelacion": f"{self.app_id}_tester_apelando_evt_apel_15",
                    "app_id": self.app_id,
                    "user_id": "tester_apelando",
                    "fecha_vencimiento": "2026-09-15",
                    "id_evento_resolucion_apelada": "evt_apel_15",
                    "estado_resolucion_apelado": "injustificada",
                    "estado_apelacion": "pendiente",
                    "justificacion_tester": "Falla de red documentada"
                }
            },
            "auditoria_cierres.json": {},
            "solicitudes_liquidaciones.json": {},
            "solicitudes_cierres.json": {}
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    # --- 1. Tester con 14 días válidos ---
    def test_tester_14_dias_validos_liquida_exitosamente(self):
        """1. Tester con 14 días válidos recibe exactamente 800 créditos, 1 boleto y registro inmutable."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertTrue(res["exito"])
        self.assertEqual(res["resultado"], "liquidada")
        self.assertEqual(res["importe"], 800)
        self.assertEqual(res["boletos"], 1)

        # Saldo del usuario
        user = self.storage["usuarios.json"]["tester_1"]
        self.assertEqual(user["creditos"], 1000)  # 200 + 800
        self.assertEqual(user["boletos_rifa"], 1)  # 0 + 1

        # Registro en recompensas.json
        id_mov = db.id_movimiento_recompensa(self.app_id, "tester_1")
        self.assertIn(id_mov, self.storage["recompensas.json"])
        rec = self.storage["recompensas.json"][id_mov]
        self.assertEqual(rec["usuario"], "tester_1")
        self.assertEqual(rec["campana"], self.app_id)
        self.assertEqual(rec["importe"], 800)
        self.assertEqual(rec["boletos"], 1)
        self.assertEqual(rec["estado"], "liquidada")
        self.assertEqual(rec["motivo"], "cumplimiento_14_dias")
        self.assertEqual(rec["dias_auditados"], 14)

        # Estado de la prueba
        prueba = self.storage["apps.json"][self.app_id]["pruebas"]["tester_1"]
        self.assertTrue(prueba["recompensa_liquidada"])
        self.assertEqual(prueba["estado"], "completada")

    # --- 2. Tester con días insuficientes ---
    def test_tester_dias_insuficientes_rechaza_liquidacion(self):
        """2. Tester con 13 días válidos rechaza liquidación sin alterar créditos ni boletos."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_incompleto", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertFalse(res["exito"])
        self.assertEqual(res["motivo"], "dias_insuficientes")
        self.assertEqual(res["dias_validos"], 13)

        # Verificación de integridad: saldos intactos
        user = self.storage["usuarios.json"]["tester_incompleto"]
        self.assertEqual(user["creditos"], 100)
        self.assertEqual(user["boletos_rifa"], 0)
        id_mov = db.id_movimiento_recompensa(self.app_id, "tester_incompleto")
        self.assertNotIn(id_mov, self.storage["recompensas.json"])

    # --- 3. Tester con falta justificada ---
    def test_tester_falta_justificada_no_suma_dia_pero_permite_liquidar_con_14_puntuales(self):
        """3. Falta justificada no suma día, pero permite liquidación si cuenta con 14 días puntuales."""
        # tester_justificado tiene 14 días puntuales y 1 justificada
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_justificado", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertTrue(res["exito"])
        self.assertEqual(res["resultado"], "liquidada")

        user = self.storage["usuarios.json"]["tester_justificado"]
        self.assertEqual(user["creditos"], 1100)  # 300 + 800
        self.assertEqual(user["boletos_rifa"], 2)  # 1 + 1

    # --- 4. Tester con apelación pendiente ---
    def test_tester_con_apelacion_pendiente_bloquea_liquidacion(self):
        """4. Tester con 14 días pero apelación pendiente de resolución bloquea liquidación."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_apelando", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertFalse(res["exito"])
        self.assertEqual(res["motivo"], "apelacion_pendiente")

        user = self.storage["usuarios.json"]["tester_apelando"]
        self.assertEqual(user["creditos"], 400)
        self.assertEqual(user["boletos_rifa"], 0)

    # --- 5. Tester expulsado ---
    def test_tester_expulsado_bloquea_liquidacion(self):
        """5. Tester expulsado de la campaña no es elegible para liquidación de recompensa."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_expulsado", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertFalse(res["exito"])
        self.assertEqual(res["motivo"], "tester_expulsado")

        user = self.storage["usuarios.json"]["tester_expulsado"]
        self.assertEqual(user["creditos"], 50)
        self.assertEqual(user["boletos_rifa"], 0)

    # --- 6. Campaña con 15 testers ---
    def test_campana_con_15_testers_resumen_y_metricas(self):
        """6. Campaña con 15 testers consolida conteos exactos de completados, pendientes y bloqueos."""
        # Poblar hasta 15 testers
        testers_15 = [f"tester_{i}" for i in range(1, 16)]
        self.storage["apps.json"][self.app_id]["testers"] = testers_15
        pruebas = {}
        for i, t in enumerate(testers_15):
            self.storage["usuarios.json"][t] = {"creditos": 0, "boletos_rifa": 0, "apps_testeando": [self.app_id]}
            dias = 14 if i < 10 else 10  # 10 completados, 5 en progreso
            pruebas[t] = {
                "tester_nombre": f"Tester {i}",
                "estado": "instalada",
                "fecha_inicio": "2026-09-01",
                "dias_completados": dias,
                "registros": {f: {"tipo": "checkin", "estado_entrega": "puntual"} for f in self.fechas_14[:dias]},
                "posibles_faltas": {}
            }
        self.storage["apps.json"][self.app_id]["pruebas"] = pruebas

        resumen = self.firebase.ejecutar(db.obtener_resumen_conciliacion_campana, self.app_id)
        self.assertTrue(resumen["encontrada"])
        self.assertEqual(resumen["total_testers_inscritos"], 15)
        self.assertEqual(resumen["total_completados"], 10)
        self.assertEqual(resumen["total_recompensas_pendientes"], 10)
        self.assertEqual(resumen["total_liquidadas"], 0)
        self.assertFalse(resumen["puede_cerrarse"])  # Hay recompensas pendientes

    # --- 7. Dos intentos simultáneos de liquidación (concurrencia ACID) ---
    def test_dos_intentos_simultaneos_liquidacion_concurrencia(self):
        """7. Conflicto concurrente entre dos llamadas de liquidación: la segunda detecta pago sin duplicar."""
        id_mov = db.id_movimiento_recompensa(self.app_id, "tester_1")

        def hook_competidor():
            # Simula que otro worker o proceso transaccional liquidó antes del commit
            self.storage["usuarios.json"]["tester_1"]["creditos"] = 1000
            self.storage["usuarios.json"]["tester_1"]["boletos_rifa"] = 1
            self.storage["recompensas.json"][id_mov] = {
                "usuario": "tester_1", "campana": self.app_id, "importe": 800,
                "boletos": 1, "estado": "liquidada", "dias_auditados": 14
            }
            self.storage["apps.json"][self.app_id]["pruebas"]["tester_1"]["recompensa_liquidada"] = True

        self.firebase.before_commit = hook_competidor

        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now
        )
        # La transacción reintentó, leyó el nuevo estado y respondió idempotentemente
        self.assertTrue(res["exito"])
        self.assertEqual(res["resultado"], "ya_liquidada")
        self.assertGreaterEqual(self.firebase.attempts, 2)

        # Saldos finales no duplicados
        user = self.storage["usuarios.json"]["tester_1"]
        self.assertEqual(user["creditos"], 1000)
        self.assertEqual(user["boletos_rifa"], 1)

    # --- 8. Repetición de liquidación ya completada ---
    def test_repeticion_de_liquidacion_ya_completada_es_idempotente(self):
        """8. Reintentar liquidación sobre tester previamente liquidado devuelve ya_liquidada de inmediato."""
        # Primera liquidación
        res1 = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertEqual(res1["resultado"], "liquidada")

        # Segunda liquidación repetida
        res2 = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertTrue(res2["exito"])
        self.assertEqual(res2["resultado"], "ya_liquidada")

        # Saldos no aumentan dos veces
        user = self.storage["usuarios.json"]["tester_1"]
        self.assertEqual(user["creditos"], 1000)
        self.assertEqual(user["boletos_rifa"], 1)

    # --- 9. Fallo de Firestore durante la transacción ---
    def test_fallo_firestore_durante_transaccion_no_deja_cambios_parciales(self):
        """9. Si el commit de Firestore falla, no se alteran usuarios, recompensas ni campañas (ACID)."""
        snapshot_previo = copy.deepcopy(self.storage)
        self.firebase.fail_commit = True

        with self.assertRaises(RuntimeError):
            self.firebase.ejecutar(
                db.liquidar_recompensa_tester,
                self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now
            )

        # Ningún cambio parcial sobrevivió
        self.assertEqual(self.storage, snapshot_previo)

    # --- 10. Reinicio del bot durante el proceso (idempotencia id_solicitud) ---
    def test_reinicio_del_bot_idempotencia_con_id_solicitud(self):
        """10. Idempotencia con id_solicitud devuelve la respuesta previa tras caída o reintento de bot."""
        id_sol = "sol_liq_999"
        res1 = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now, id_solicitud=id_sol
        )
        self.assertEqual(res1["resultado"], "liquidada")

        # Simular reinicio y reenvío con mismo id_solicitud
        res2 = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_1", actor_id="admin_1", now_dt=self.dt_now, id_solicitud=id_sol
        )
        self.assertTrue(res2["exito"])
        self.assertEqual(res2["resultado"], "liquidada")
        self.assertEqual(res2["importe"], 800)

        # Verificar que no se acreditaron 1600 créditos
        user = self.storage["usuarios.json"]["tester_1"]
        self.assertEqual(user["creditos"], 1000)

    # --- 11. Campaña antigua con progreso legado ---
    def test_campana_antigua_con_progreso_legado_requiere_conciliacion(self):
        """11. Campaña antigua con progreso legacy sin auditoría queda en requiere_conciliacion_legacy."""
        res = self.firebase.ejecutar(
            db.liquidar_recompensa_tester,
            self.app_id, "tester_legacy", actor_id="admin_1", now_dt=self.dt_now
        )
        self.assertFalse(res["exito"])
        self.assertEqual(res["motivo"], "requiere_conciliacion_legacy")

        # Sin otorgar ni retirar créditos
        user = self.storage["usuarios.json"]["tester_legacy"]
        self.assertEqual(user["creditos"], 150)

    # --- 12. Campaña con datos que requieren conciliación bloquea cierre ---
    def test_campana_con_datos_que_requieren_conciliacion_bloquea_cierre(self):
        """12. Si una campaña contiene testers con marcas legacy sin solventar, rechaza el cierre definitivo."""
        self.storage["apps.json"][self.app_id]["pruebas"].pop("tester_apelando")
        self.storage["apps.json"][self.app_id]["testers"].remove("tester_apelando")

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_1"}):
            res = self.firebase.ejecutar(
                db.cerrar_campana_definitiva,
                self.app_id, actor_id="admin_1", now_dt=self.dt_now
            )
            self.assertFalse(res["exito"])
            self.assertEqual(res["motivo"], "casos_requieren_conciliacion")

    # --- 13. Intento de cierre con liquidaciones pendientes rechazado ---
    def test_intento_cierre_con_liquidaciones_pendientes_rechazado(self):
        """13. Una campaña con testers con 14 días pendientes de liquidar no puede cerrarse."""
        # Resolver el tester legacy y apelación para aislar la condición de recompensas pendientes
        self.storage["apps.json"][self.app_id]["pruebas"].pop("tester_legacy")
        self.storage["apps.json"][self.app_id]["testers"].remove("tester_legacy")
        self.storage["apps.json"][self.app_id]["pruebas"].pop("tester_apelando")
        self.storage["apps.json"][self.app_id]["testers"].remove("tester_apelando")

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_1"}):
            res = self.firebase.ejecutar(
                db.cerrar_campana_definitiva,
                self.app_id, actor_id="admin_1", now_dt=self.dt_now
            )
            self.assertFalse(res["exito"])
            self.assertEqual(res["motivo"], "recompensas_pendientes")
            self.assertIn("tester_1", res["detalles"])

    # --- 14. Cierre exitoso después de resolver todos los casos ---
    def test_cierre_exitoso_despues_de_resolver_todos_los_casos(self):
        """14. Tras liquidar todas las recompensas y sin apelaciones pendientes, la campaña se cierra definitivamente."""
        # 1. Limpiar casos problemáticos de la campaña demo
        self.storage["apps.json"][self.app_id]["pruebas"].pop("tester_legacy")
        self.storage["apps.json"][self.app_id]["testers"].remove("tester_legacy")
        self.storage["apps.json"][self.app_id]["pruebas"].pop("tester_apelando")
        self.storage["apps.json"][self.app_id]["testers"].remove("tester_apelando")

        # 2. Liquidar los testers completados (tester_1 y tester_justificado)
        res_batch = self.firebase.ejecutar(db.liquidar_campana_pendientes, self.app_id, actor_id="admin_1", now_dt=self.dt_now)
        self.assertTrue(res_batch["exito"])
        self.assertEqual(res_batch["liquidados_exitosos"], 2)

        # 3. Proceder al cierre definitivo con el dev propietario
        res_cierre = self.firebase.ejecutar(
            db.cerrar_campana_definitiva,
            self.app_id, actor_id=self.dev_id, motivo="Campaña completada exitosamente", now_dt=self.dt_now
        )
        self.assertTrue(res_cierre["exito"])
        self.assertEqual(res_cierre["resultado"], "cerrada_definitivamente")

        app = self.storage["apps.json"][self.app_id]
        self.assertEqual(app["estado"], "cerrada")
        self.assertEqual(app["estado_conciliacion"], "cerrada_definitivamente")
        self.assertIn("fecha_cierre_definitivo", app)
        self.assertEqual(app["actor_cierre"], self.dev_id)

        # Verificar evento de auditoría de cierre
        self.assertTrue(len(self.storage["auditoria_cierres.json"]) > 0)


if __name__ == "__main__":
    unittest.main()
