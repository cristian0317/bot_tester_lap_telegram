"""Tests for ETAPA 5B of TestCoin: historial diario, corte horario, avisos y conciliación.

Run: python -m unittest discover -s tests -p "test_*.py"
"""

import ast
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import db
from inscripcion_simulada import FirestoreSimulado, ScopedMexicoCityTZ


class Historial5BTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tz_scope = ScopedMexicoCityTZ()
        self.tz_scope.__enter__()
        self.reset_fixture()

    def tearDown(self):
        if hasattr(self, "tz_scope") and self.tz_scope:
            self.tz_scope.__exit__(None, None, None)

    def reset_fixture(self):
        self.storage = {
            "usuarios.json": {
                "42": {
                    "nombre": "Tester",
                    "apps_testeando": ["demo"],
                    "creditos": 200,
                    "boletos_rifa": 3,
                }
            },
            "apps.json": {
                "demo": {
                    "dev_id": "700",
                    "descripcion": "Demo App",
                    "estado": "en_prueba",
                    "testers": ["42"],
                    "fecha_lanzamiento": "2026-09-20",
                    "timestamp_lanzamiento": "2026-09-20T10:00:00-06:00",
                    "pruebas": {},
                    "dias_requeridos": 14,
                }
            },
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    def test_calcular_vencimiento_primera_entrega(self):
        """1. Vencimiento primera entrega a las 17:00 CDT del día siguiente al lanzamiento."""
        # Con timestamp ISO de lanzamiento
        venc = db.calcular_vencimiento_primera_entrega("2026-09-20T10:00:00-06:00", None)
        self.assertEqual(venc, "2026-09-21")

        # Con fecha legacy (sin inventar hora)
        venc_legacy = db.calcular_vencimiento_primera_entrega(None, "2026-09-20")
        self.assertEqual(venc_legacy, "2026-09-21")

    def test_evaluar_puntualidad_entrega(self):
        """2. Evaluación de puntualidad con timestamp original de Telegram."""
        cd_tz = db.get_mexico_city_tz()
        # Mensaje a las 16:59:59 CDT de la fecha de vencimiento -> puntual
        dt_puntual = datetime(2026, 9, 21, 16, 59, 59, tzinfo=cd_tz)
        res_p = db.evaluar_puntualidad_entrega(dt_puntual, "2026-09-21")
        self.assertEqual(res_p, "puntual")

        # Mensaje a las 17:00:00 CDT de la fecha de vencimiento -> puntual (inclusive)
        dt_exacto = datetime(2026, 9, 21, 17, 0, 0, tzinfo=cd_tz)
        res_e = db.evaluar_puntualidad_entrega(dt_exacto, "2026-09-21")
        self.assertEqual(res_e, "puntual")

        # Mensaje a las 17:00:01 CDT de la fecha de vencimiento -> tardío
        dt_tardio = datetime(2026, 9, 21, 17, 0, 1, tzinfo=cd_tz)
        res_t = db.evaluar_puntualidad_entrega(dt_tardio, "2026-09-21")
        self.assertEqual(res_t, "tardio")

    def test_confirmar_instalacion_registra_vencimiento_y_v1(self):
        """3. Confirmar instalación guarda fecha_vencimiento_1 y estructura de registros."""
        res = self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            id_mensaje="42_101",
            telegram_timestamp=datetime(2026, 9, 20, 12, 0, 0, tzinfo=db.get_mexico_city_tz()),
            fecha_vencimiento="2026-09-21",
        )
        self.assertEqual(res["resultado"], "creada")
        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["estado"], "instalada")
        self.assertEqual(prueba["dias_completados"], 1)
        self.assertIn("2026-09-21", prueba["registros"])
        reg_v1 = prueba["registros"]["2026-09-21"]
        self.assertEqual(reg_v1["tipo"], "instalacion")
        self.assertEqual(reg_v1["id_mensaje"], "42_101")
        self.assertEqual(reg_v1["estado_entrega"], "puntual")

    def test_deduplicacion_por_chat_id_message_id(self):
        """4. Deduplicación por id_mensaje evita procesar la misma captura dos veces."""
        # Primera instalación
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            id_mensaje="42_101",
        )
        # Check-in posterior con el mismo id_mensaje
        res_dup = self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_checkin",
            "2026-09-21",
            id_mensaje="42_101",
            fecha_vencimiento="2026-09-21",
        )
        self.assertEqual(res_dup["resultado"], "duplicado")

    def test_resubmision_misma_fecha_agrega_versiones_anteriores(self):
        """5. Reenviar foto para la misma fecha agrega a versiones_anteriores sin degradar la primera ni sumar días extra."""
        # Instalación inicial puntual
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            id_mensaje="42_101",
            telegram_timestamp=datetime(2026, 9, 20, 12, 0, 0, tzinfo=db.get_mexico_city_tz()),
            fecha_vencimiento="2026-09-21",
        )
        dias_antes = self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"]

        # Segunda foto para la misma fecha de vencimiento (2026-09-21) recibida más tarde
        res_act = self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_nueva",
            "2026-09-21",
            id_mensaje="42_102",
            telegram_timestamp=datetime(2026, 9, 21, 18, 0, 0, tzinfo=db.get_mexico_city_tz()),
            fecha_vencimiento="2026-09-21",
        )
        self.assertEqual(res_act["resultado"], "actualizado")
        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        # Conserva días completados
        self.assertEqual(prueba["dias_completados"], dias_antes)
        # Conserva captura puntual y guarda versión previa en versiones_anteriores
        reg = prueba["registros"]["2026-09-21"]
        self.assertEqual(reg["estado_entrega"], "puntual")
        self.assertEqual(reg["captura"], "foto_nueva")
        self.assertEqual(len(reg["versiones_anteriores"]), 1)
        self.assertEqual(reg["versiones_anteriores"][0], "foto_inst")

    def test_avisos_programados_reclamacion_transaccional_y_limite_reintentos(self):
        """6. Reclamación transaccional de avisos con límite de 3 reintentos y revalidación."""
        # Preparar prueba activa sin la entrega satisfecha
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            fecha_vencimiento="2026-09-21",
        )

        now_1500 = datetime(2026, 9, 22, 15, 0, 0, tzinfo=db.get_mexico_city_tz())
        # Reclamar aviso de las 15:00 para la entrega 2026-09-22
        rec1 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
        self.assertTrue(rec1.get("elegible"))
        self.assertEqual(rec1.get("intentos"), 1)

        # Confirmar enviado con token de reclamación válido
        tok = rec1.get("token_reclamacion")
        self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion=tok)

        # Intentar reclamar de nuevo -> ya enviado
        rec2 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
        self.assertFalse(rec2.get("elegible"))
        self.assertEqual(rec2.get("motivo"), "ya_enviado")

    def test_marcar_posibles_faltas_y_resolucion_retardada(self):
        """7. Detección de posibles faltas por reinicio del bot y resolución tras llegada tardía de mensaje puntual."""
        # Crear instalación
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            fecha_vencimiento="2026-09-21",
        )

        # Ejecutar corte a las 17:30 CDT para detectar falta en fecha 2026-09-22
        dt_corte_1730 = datetime(2026, 9, 22, 17, 30, 0, tzinfo=db.get_mexico_city_tz())
        res_corte = self.firebase.ejecutar(db.marcar_posibles_faltas, dt_corte_1730)
        self.assertIsInstance(res_corte, int)
        self.assertGreaterEqual(res_corte, 1)

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertIn("2026-09-22", prueba.get("posibles_faltas", {}))
        self.assertEqual(prueba["posibles_faltas"]["2026-09-22"]["estado"], "pendiente_revision")

        # Llega mensaje tardío pero con timestamp de Telegram anterior a las 17:00 CDT de 2026-09-22
        tg_dt_a_tiempo = datetime(2026, 9, 22, 16, 30, 0, tzinfo=db.get_mexico_city_tz())
        res_checkin = self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_retardada",
            "2026-09-23",
            id_mensaje="42_200",
            telegram_timestamp=tg_dt_a_tiempo,
            fecha_vencimiento="2026-09-22",
        )
        self.assertEqual(res_checkin["resultado"], "checkin")
        prueba_actualizada = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        # La falta debe quedar resuelta
        falta_est = prueba_actualizada["posibles_faltas"]["2026-09-22"]["estado"]
        self.assertEqual(falta_est, "resuelta_por_mensaje_a_tiempo")

    def test_dias_completados_no_suma_entrega_tardia(self):
        """8. Entregas tardías se registran como tardío y no cuentan automáticamente como día completado."""
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            fecha_vencimiento="2026-09-21",
        )
        dias_iniciales = self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"]

        # Entregar tarde (después de las 17:00 CDT de 2026-09-22)
        dt_despues = datetime(2026, 9, 22, 18, 0, 0, tzinfo=db.get_mexico_city_tz())
        self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_tardia",
            "2026-09-22",
            id_mensaje="42_300",
            telegram_timestamp=dt_despues,
            fecha_vencimiento="2026-09-22",
        )
        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["registros"]["2026-09-22"]["estado_entrega"], "tardio")
        self.assertEqual(prueba["dias_completados"], dias_iniciales)

    def test_campana_legacy_marca_requiere_conciliacion(self):
        """9. Campañas legacy sin registros previo marcan requiere_conciliacion sin otorgar recompensa automática."""
        # Forzar estado legacy en la prueba
        self.storage["apps.json"]["demo"]["pruebas"]["42"] = {
            "tester_nombre": "Tester Legacy",
            "estado": "instalada",
            "fecha_inicio": "2026-09-01",
            "captura_inicial": "foto_leg",
            "dias_completados": 13,
            "ultimo_checkin": "2026-09-13",
        }

        res_checkin = self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_14",
            "2026-09-14",
            id_mensaje="42_legacy_14",
            fecha_vencimiento="2026-09-14",
        )
        self.assertTrue(res_checkin.get("requiere_conciliacion"))
        self.assertEqual(res_checkin.get("resultado"), "checkin")
        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertTrue(prueba.get("requiere_conciliacion"))

    def test_dos_workers_reclamando_mismo_aviso(self):
        """10. Concurrencia de dos trabajadores intentando reclamar el mismo aviso."""
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            fecha_vencimiento="2026-09-21",
        )
        now_1500 = datetime(2026, 9, 22, 15, 0, 0, tzinfo=db.get_mexico_city_tz())
        rec1 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
        self.assertTrue(rec1.get("elegible"))
        token1 = rec1.get("token_reclamacion")
        self.assertIsNotNone(token1)

        # Segundo trabajador inmediatamente en el mismo minuto
        rec2 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
        self.assertFalse(rec2.get("elegible"))
        self.assertEqual(rec2.get("motivo"), "en_proceso")

    def test_recuperacion_aviso_abandonado_y_worker_antiguo_bloqueado(self):
        """11. Recuperación de aviso abandonado y bloqueo de worker antiguo con token desfasado."""
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            fecha_vencimiento="2026-09-21",
        )
        t0 = datetime(2026, 9, 22, 15, 0, 0, tzinfo=db.get_mexico_city_tz())
        rec1 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=t0)
        token1 = rec1.get("token_reclamacion")

        # 6 minutos después (timeout del lock de 5m expira)
        t_lat = t0 + timedelta(seconds=360)
        rec2 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=t_lat)
        self.assertTrue(rec2.get("elegible"))
        token2 = rec2.get("token_reclamacion")
        self.assertNotEqual(token1, token2)

        # Worker 1 resucita y pretende confirmar con token desfasado
        self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion=token1)
        aviso_doc = self.storage.get("avisos_programados.json", {}).get("demo_42_2026-09-22_1500", {})
        self.assertEqual(aviso_doc.get("token_reclamacion"), token2)
        self.assertEqual(aviso_doc.get("estado"), "enviando")

        # Worker 2 confirma con token activo
        self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion=token2)
        aviso_doc_act = self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]
        self.assertEqual(aviso_doc_act.get("estado"), "enviado")

    def test_duplicacion_atomica_id_mensaje_entre_campanas_y_fechas(self):
        """12. Deduplicación atómica impide usar el mismo message_id en campañas o fechas distintas."""
        self.storage["apps.json"]["demo2"] = {
            "dev_id": "701", "descripcion": "App 2", "estado": "en_prueba",
            "testers": ["42"], "fecha_lanzamiento": "2026-09-20", "pruebas": {},
        }
        self.storage["usuarios.json"]["42"]["apps_testeando"].append("demo2")

        # Registrar primera instalación
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            id_mensaje="msg_compartido_999",
            fecha_vencimiento="2026-09-21",
        )

        # Reusar el mismo id_mensaje para demo2 -> Rechazado
        with self.assertRaises(db.ConfirmacionRechazada):
            self.firebase.ejecutar(
                db.confirmar_instalacion,
                "42",
                "demo2",
                "foto_inst2",
                "2026-09-20",
                "Tester",
                id_mensaje="msg_compartido_999",
                fecha_vencimiento="2026-09-21",
            )

        # Reusar el mismo id_mensaje para checkin de otra fecha en demo -> Rechazado duplicado
        res_dup_fecha = self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_chk2",
            "2026-09-22",
            id_mensaje="msg_compartido_999",
            fecha_vencimiento="2026-09-22",
        )
        self.assertEqual(res_dup_fecha.get("resultado"), "duplicado")

    def test_inscritos_sin_instalacion_en_posibles_faltas(self):
        """13. Inscritos en la campaña sin confirmación de instalación son incluidos en posibles faltas tras el corte."""
        self.storage["apps.json"]["demo"]["testers"].append("99")

        dt_corte = datetime(2026, 9, 21, 17, 30, 0, tzinfo=db.get_mexico_city_tz())
        res_corte = self.firebase.ejecutar(db.marcar_posibles_faltas, dt_corte)
        self.assertGreaterEqual(res_corte, 1)

        prueba_99 = self.storage["apps.json"]["demo"]["pruebas"]["99"]
        self.assertEqual(prueba_99["estado"], "sin_instalacion")
        self.assertIn("2026-09-21", prueba_99["posibles_faltas"])
        self.assertEqual(prueba_99["posibles_faltas"]["2026-09-21"]["estado"], "pendiente_revision")

    def test_bloqueo_pagos_sin_cerrar_prueba_ni_impedir_entregas_futuras(self):
        """14. Bloqueo de pago automático cuando existen entregas tardías sin cerrar la prueba ni frenar check-ins posteriores."""
        self.firebase.ejecutar(
            db.confirmar_instalacion,
            "42",
            "demo",
            "foto_inst",
            "2026-09-20",
            "Tester",
            fecha_vencimiento="2026-09-21",
        )

        # Registrar 1 entrega tardía
        dt_tardio = datetime(2026, 9, 22, 18, 0, 0, tzinfo=db.get_mexico_city_tz())
        self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_tardia",
            "2026-09-22",
            id_mensaje="chk_tardio",
            telegram_timestamp=dt_tardio,
            fecha_vencimiento="2026-09-22",
        )

        # Completar 13 entregas puntuales adicionales (total 14 puntuales)
        for i in range(3, 16):
            venc_str = f"2026-09-{i:02d}"
            tg_dt = datetime(2026, 9, i, 12, 0, 0, tzinfo=db.get_mexico_city_tz())
            res_c = self.firebase.ejecutar(
                db.registrar_checkin,
                "42",
                "demo",
                f"foto_{i}",
                venc_str,
                id_mensaje=f"chk_{i}",
                telegram_timestamp=tg_dt,
                fecha_vencimiento=venc_str,
            )

        # Al llegar al día 14 puntual: requiere conciliación por la entrega tardía y NO otorga 800 créditos
        creditos_despues = self.storage["usuarios.json"]["42"]["creditos"]
        self.assertEqual(creditos_despues, 200) # Sin pago automático
        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["estado"], "instalada") # Prueba NO se cierra
        self.assertTrue(prueba.get("requiere_conciliacion"))

        # Tester puede seguir enviando entregas en días posteriores (ej. día 16)
        res_future = self.firebase.ejecutar(
            db.registrar_checkin,
            "42",
            "demo",
            "foto_16",
            "2026-09-16",
            id_mensaje="chk_16",
            telegram_timestamp=datetime(2026, 9, 16, 12, 0, 0, tzinfo=db.get_mexico_city_tz()),
            fecha_vencimiento="2026-09-16",
        )
        self.assertEqual(res_future["resultado"], "checkin")

    def test_continuidad_entregas_despues_del_pago(self):
        """15. Prueba pagada 1 vez permite seguir entregando nuevas fechas sin duplicar pago ni saldo."""
        registros_initial = {
            f"2026-09-{i:02d}": {
                "tipo": "checkin", "id_mensaje": f"chk_{i}", "estado_entrega": "puntual", "captura": f"foto_{i}"
            } for i in range(1, 14)
        }
        self.storage["apps.json"]["demo"]["pruebas"]["42"] = {
            "tester_nombre": "Tester", "estado": "instalada", "dias_completados": 13,
            "ultimo_checkin": "2026-09-13", "captura_inicial": "foto_1", "fecha_inicio": "2026-09-01",
            "registros": registros_initial
        }

        # 1er pago al completar día 14 puntual (2026-09-14)
        res_pay = self.firebase.ejecutar(
            db.registrar_checkin, "42", "demo", "foto_14", "2026-09-14", id_mensaje="chk_14", fecha_vencimiento="2026-09-14"
        )
        self.assertEqual(res_pay["resultado"], "completada")
        self.assertEqual(self.storage["usuarios.json"]["42"]["creditos"], 1000)
        self.assertEqual(self.storage["usuarios.json"]["42"]["boletos_rifa"], 4)

        # Entrega posterior para el día 15 (2026-09-15) en la misma campaña activa
        res_day15 = self.firebase.ejecutar(
            db.registrar_checkin, "42", "demo", "foto_15", "2026-09-15", id_mensaje="chk_15", fecha_vencimiento="2026-09-15"
        )
        self.assertEqual(res_day15["resultado"], "checkin")
        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados"], 15)
        self.assertIn("2026-09-15", prueba["registros"])
        self.assertEqual(prueba["estado"], "instalada")

        # Verificar que el saldo no cambió y el movimiento de recompensa no se duplicó
        self.assertEqual(self.storage["usuarios.json"]["42"]["creditos"], 1000)
        self.assertEqual(self.storage["usuarios.json"]["42"]["boletos_rifa"], 4)
        mov_id = db.id_movimiento_recompensa("demo", "42")
        self.assertIn(mov_id, self.storage.get("recompensas.json", {}))

    def test_confirmacion_aviso_sin_token_token_antiguo_o_documento_inexistente(self):
        """16. Rechazo de confirmación de avisos sin token, token desfasado o documento inexistente."""
        # 1. Sin token -> Rechazado sin escrituras
        res_no_tok = self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion=None)
        self.assertFalse(res_no_tok)

        # 2. Documento inexistente -> Rechazado sin escrituras
        res_no_doc = self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion="tok_fake")
        self.assertFalse(res_no_doc)

        # Reclamar aviso válido para crear documento con token activo
        self.firebase.ejecutar(
            db.confirmar_instalacion, "42", "demo", "foto_inst", "2026-09-20", "Tester", fecha_vencimiento="2026-09-21"
        )
        now_1500 = datetime(2026, 9, 22, 15, 0, 0, tzinfo=db.get_mexico_city_tz())
        rec = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
        tok_valido = rec["token_reclamacion"]

        # 3. Token desfasado/incorrecto -> Rechazado sin modificar estado
        res_bad_tok = self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion="tok_incorrecto")
        self.assertFalse(res_bad_tok)
        aviso_doc = self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]
        self.assertEqual(aviso_doc["estado"], "enviando")

        # 4. Token válido -> Aceptado
        res_ok = self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion=tok_valido)
        self.assertTrue(res_ok)
        self.assertEqual(self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]["estado"], "enviado")

        # 5. Reintento seguro con token válido -> Idempotente y conserva timestamp_envio
        ts_original = self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]["timestamp_envio"]
        res_retry = self.firebase.ejecutar(db.confirmar_aviso_enviado, "demo", "42", "2026-09-22", "1500", token_reclamacion=tok_valido)
        self.assertTrue(res_retry)
        ts_nuevo = self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]["timestamp_envio"]
        self.assertEqual(ts_original, ts_nuevo)

    def test_ausencia_america_mexico_city_bloquea_operaciones_calendario(self):
        """17. La ausencia de datos IANA America/Mexico_City eleva RuntimeError y bloquea escrituras."""
        import zoneinfo
        orig_zi = zoneinfo.ZoneInfo
        def fail_zi(key):
            if key == "America/Mexico_City":
                raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")
            return orig_zi(key)

        # Agregar tester sin instalación previa registrada
        self.storage["usuarios.json"]["777"] = {"nombre": "Tester 777", "apps_testeando": ["demo"], "creditos": 0, "boletos_rifa": 0}
        self.storage["apps.json"]["demo"]["testers"].append("777")

        before_storage = copy.deepcopy(self.storage)
        before_writes = copy.deepcopy(self.writes)

        fake_module = NS(firestore=NS(transactional=self.firebase.transactional, SERVER_TIMESTAMP="SERVER_TIMESTAMP"))
        with patch.object(zoneinfo, "ZoneInfo", side_effect=fail_zi), \
             patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self.firebase), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):
            # 1. get_mexico_city_tz eleva RuntimeError directamente
            with self.assertRaises(RuntimeError) as ctx:
                db.get_mexico_city_tz()
            self.assertIn("America/Mexico_City", str(ctx.exception))

            # 2. Operación real de calendario (confirmar_instalacion) eleva ErrorPersistenciaConfirmacion y bloquea escrituras
            with self.assertRaises((RuntimeError, db.ErrorPersistenciaConfirmacion)) as ctx2:
                db.confirmar_instalacion(
                    "777",
                    "demo",
                    "foto_tz_fail",
                    "2026-09-20",
                    "Tester 777",
                    id_mensaje="msg_tz_fail",
                    fecha_vencimiento="2026-09-21"
                )
            exc_str = str(ctx2.exception) + (" " + str(ctx2.exception.__cause__) if ctx2.exception.__cause__ else "")
            self.assertIn("America/Mexico_City", exc_str)

            # 3. Operación de corte eleva RuntimeError
            with self.assertRaises(RuntimeError) as ctx3:
                db.marcar_posibles_faltas(datetime(2026, 9, 21, 17, 30, 0))
            self.assertIn("America/Mexico_City", str(ctx3.exception))

        # Comprobar que no hubo ninguna escritura en el almacenamiento
        self.assertEqual(self.storage, before_storage)
        self.assertEqual(self.writes, before_writes)

    def test_recuperacion_real_planificador_tras_reinicio(self):
        """18. Demuestra la recuperación real ejecutando las rutinas del bot con almacenamiento y Telegram simulados."""
        import asyncio
        import bot as bot_module

        # 1. Verificar que el registro en main() configura el planificador de inicio y periódico
        app_mock = Mock()
        job_queue_mock = Mock()
        app_mock.job_queue = job_queue_mock
        app_mock.run_polling = Mock()
        app_mock.run_webhook = Mock()
        with patch.object(bot_module.Application, "builder", return_value=Mock(token=Mock(return_value=Mock(post_init=Mock(return_value=Mock(build=Mock(return_value=app_mock))))))):
            bot_module.main()
            job_queue_mock.run_once.assert_called_with(bot_module.ejecutar_recuperacion_inicio, when=1)
            job_queue_mock.run_repeating.assert_called_with(bot_module.ejecutar_procesamiento_periodico, interval=300, first=10)

        # Configuración inicial de campaña activa 'demo'
        self.firebase.ejecutar(
            db.confirmar_instalacion, "42", "demo", "foto_42", "2026-09-20", "Tester 42", fecha_vencimiento="2026-09-21"
        )
        self.storage["apps.json"]["demo"]["testers"].append("99")
        # User 99 sin objeto 'prueba' aún, primera fecha de vencimiento es 2026-09-22
        self.storage["apps.json"]["demo"]["fecha_lanzamiento"] = "2026-09-21"

        bot_mock = AsyncMock()
        context = NS(bot=bot_mock)

        with patch.object(bot_module, "cargar_archivo", side_effect=lambda f: copy.deepcopy(self.storage.get(f, {}))):
            # A) Barridos a las 10:00 y 10:05: cero envíos y cero avisos nuevos
            now_1000 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=db.get_mexico_city_tz())
            res_1000 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1000)))
            self.assertEqual(res_1000["avisos_enviados"], 0)
            self.assertEqual(len(self.storage.get("avisos_programados.json", {})), 0)

            now_1005 = datetime(2026, 9, 22, 10, 5, 0, tzinfo=db.get_mexico_city_tz())
            res_1005 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1005)))
            self.assertEqual(res_1005["avisos_enviados"], 0)

            # B) Contexto sin bot: ninguna confirmación ni envío contabilizado
            context_no_bot = NS(bot=None)
            now_1500 = datetime(2026, 9, 22, 15, 0, 0, tzinfo=db.get_mexico_city_tz())
            res_no_bot = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context_no_bot, now_dt=now_1500)))
            self.assertEqual(res_no_bot["avisos_enviados"], 0)
            # El aviso quedó en estado 'enviando' sin confirmar
            self.assertEqual(self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]["estado"], "enviando")

            # C) 15:00 y 15:05 después de envío confirmado con bot: un solo envío para user 42 y user 99 (primera instalacion)
            # Simular 10 min transcurridos para recuperar el aviso previo no confirmado
            now_1510 = datetime(2026, 9, 22, 15, 10, 0, tzinfo=db.get_mexico_city_tz())
            res_1510 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1510)))
            self.assertEqual(res_1510["avisos_enviados"], 2)  # User 42 y User 99 (primera instalación)
            self.assertEqual(self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]["estado"], "enviado")
            self.assertEqual(self.storage["avisos_programados.json"]["demo_99_2026-09-22_1500"]["estado"], "enviado")

            # Segundo barrido a las 15:15: cero envíos adicionales (ya enviados)
            now_1515 = datetime(2026, 9, 22, 15, 15, 0, tzinfo=db.get_mexico_city_tz())
            res_1515 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1515)))
            self.assertEqual(res_1515["avisos_enviados"], 0)

            # D) Aviso de las 15:00 abandonado y recuperado a las 16:10: misma identidad y contador de intentos conservado
            # Simular que el aviso de user 42 fue reseteado a 'enviando' con ultimo_intento 10 min atras
            self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"] = {
                "app_id": "demo", "user_id": "42", "fecha_vencimiento": "2026-09-22", "tipo": "1500",
                "estado": "enviando", "intentos": 1, "ultimo_intento": (now_1510 - timedelta(minutes=10)).isoformat(),
                "token_reclamacion": "tok_viejo"
            }
            now_1610 = datetime(2026, 9, 22, 16, 10, 0, tzinfo=db.get_mexico_city_tz())
            res_1610 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1610)))
            self.assertGreaterEqual(res_1610["avisos_enviados"], 1)
            aviso_recovered = self.storage["avisos_programados.json"]["demo_42_2026-09-22_1500"]
            self.assertEqual(aviso_recovered["estado"], "enviado")
            self.assertEqual(aviso_recovered["tipo"], "1500")
            self.assertEqual(aviso_recovered["intentos"], 2)
            self.assertNotEqual(aviso_recovered["token_reclamacion"], "tok_viejo")

            # E) Franja de las 16:30: identidad diferente de la de las 15:00, pero estable entre barridos
            now_1630 = datetime(2026, 9, 22, 16, 30, 0, tzinfo=db.get_mexico_city_tz())
            res_1630 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1630)))
            self.assertIn("demo_42_2026-09-22_1630", self.storage["avisos_programados.json"])
            self.assertNotEqual("demo_42_2026-09-22_1630", "demo_42_2026-09-22_1500")

            # F) Después del corte (17:05 CDT): cero envíos y ninguna fecha trasladada
            now_1705 = datetime(2026, 9, 22, 17, 5, 0, tzinfo=db.get_mexico_city_tz())
            res_1705 = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, now_dt=now_1705)))
            self.assertEqual(res_1705["avisos_enviados"], 0)

            # G) Dos trabajadores: se conserva la protección transaccional
            rec_w1 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
            rec_w2 = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-09-22", "1500", now_dt=now_1500)
            self.assertFalse(rec_w2["elegible"])

    def test_callback_reloj_progresivo_cancela_envios_tras_corte(self):
        """19. Callback con reloj progresivo: la llegada a las 17:00 cancela envíos no iniciados y usa hora actualizada."""
        import asyncio
        import bot as bot_module

        self.storage["avisos_programados.json"] = {}
        bot_mock = AsyncMock()
        context = NS(bot=bot_mock)

        # Reloj progresivo: 1. 16:59:50 CDT, 2. 16:59:55 CDT, 3. 17:00:05 CDT (corte cruzado)
        times = [
            datetime(2026, 9, 22, 16, 59, 50, tzinfo=db.get_mexico_city_tz()),
            datetime(2026, 9, 22, 16, 59, 55, tzinfo=db.get_mexico_city_tz()),
            datetime(2026, 9, 22, 17, 0, 5, tzinfo=db.get_mexico_city_tz()),
            datetime(2026, 9, 22, 17, 0, 10, tzinfo=db.get_mexico_city_tz()),
        ]
        time_idx = 0
        def progressive_clock():
            nonlocal time_idx
            if time_idx < len(times):
                val = times[time_idx]
                time_idx += 1
                return val
            return times[-1]

        with patch.object(bot_module, "cargar_archivo", side_effect=lambda f: copy.deepcopy(self.storage.get(f, {}))):
            res = self.firebase.ejecutar(lambda: asyncio.run(bot_module.procesar_avisos_y_recuperacion(context, clock=progressive_clock)))

        # Al cruzarse las 17:00 antes de invocar send_message, no se enviaron avisos ni se confirmaron
        self.assertEqual(res["avisos_enviados"], 0)
        bot_mock.send_message.assert_not_called()

    def test_reclamar_aviso_fecha_invalida_rechaza_sin_escrituras(self):
        """20. Una fecha de vencimiento inválida rechaza el reclamo sin pasarlo por alto ni escribir nada."""
        before_storage = copy.deepcopy(self.storage)
        before_writes = copy.deepcopy(self.writes)

        res = self.firebase.ejecutar(db.reclamar_aviso, "demo", "42", "2026-99-99", "1500")
        self.assertFalse(res["elegible"])
        self.assertEqual(res["motivo"], "fecha_invalida")
        self.assertEqual(self.storage, before_storage)
        self.assertEqual(self.writes, before_writes)

