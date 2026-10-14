import unittest
import copy
from datetime import datetime
from unittest.mock import patch

import db
from inscripcion_simulada import FirestoreSimulado, ConflictoLectura


class AuditRefMockSinKey:
    """Mock de DocumentReference que expone .id pero eleva AttributeError si se intenta acceder a .key."""
    def __init__(self, doc_id):
        self._id = str(doc_id)
        self.collection_name = "auditoria_revisiones.json"

    @property
    def id(self):
        return self._id

    @property
    def path(self):
        return self.collection_name, self._id

    @property
    def key(self):
        raise AttributeError("DocumentReference no tiene atributo 'key'. Usá '.id'.")


class Faltas5C1Tests(unittest.TestCase):
    def setUp(self):
        self.storage = {
            "apps.json": {
                "demo": {
                    "dev_id": "dev_owner",
                    "descripcion": "Demo App 5C",
                    "estado": "en_prueba",
                    "fecha_lanzamiento": "2026-09-20",
                    "timestamp_lanzamiento": "2026-09-20T10:00:00-06:00",
                    "testers": ["42", "99"],
                    "pruebas": {
                        "42": {
                            "tester_nombre": "Tester 42",
                            "estado": "instalada",
                            "dias_completados": 1,
                            "version_revision": 0,
                            "posibles_faltas": {
                                "2026-09-21": {
                                    "fecha_vencimiento": "2026-09-21",
                                    "estado": "pendiente_revision"
                                }
                            },
                            "registros": {
                                "2026-09-21": {
                                    "tipo": "checkin",
                                    "estado_entrega": "puntual",
                                    "captura": "cap_21.png"
                                }
                            },
                            "historial_eventos_ids": []
                        }
                    }
                }
            },
            "usuarios.json": {
                "42": {"nombre": "Tester 42", "creditos": 0, "boletos_rifa": 0, "apps_testeando": ["demo"]},
                "dev_owner": {"nombre": "Dev Owner", "creditos": 0},
                "dev_other": {"nombre": "Dev Other", "creditos": 0},
                "admin_user": {"nombre": "Admin User", "creditos": 0}
            },
            "auditoria_revisiones.json": {},
            "solicitudes_revisiones.json": {}
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

    def test_permisos_dev_propietario_y_admin_vs_ajeno(self):
        """1. Dev propietario y admin pueden revisar; Dev ajeno es rechazado por permisos."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        # Dev ajeno -> Rechazado
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_ajeno = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_other", "demo", "42", "2026-09-21", "justificada",
                "Fallo documentado en la red", "sol_perm_1", 0, dt_now
            )
            self.assertFalse(res_ajeno["exito"])
            self.assertEqual(res_ajeno["motivo"], "permiso_denegado")

        # Dev propietario -> Aprobado
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_dev = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Fallo documentado en la red", "sol_perm_2", 0, dt_now
            )
            self.assertTrue(res_dev["exito"])

        # Admin configurado -> Aprobado
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_admin = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "admin_user", "demo", "42", "2026-09-21", "cumplida_por_revision",
                "Verificado manualmente por admin", "sol_perm_3", 1, dt_now
            )
            self.assertTrue(res_admin["exito"])

    def test_configuracion_administrativa_ausente_sin_privilegios(self):
        """2. Ausencia de ADMIN_IDS no concede privilegios administrativos implícitos."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": ""}):
            res_no_admin = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "admin_user", "demo", "42", "2026-09-21", "justificada",
                "Intento sin config admin", "sol_perm_4", 0, dt_now
            )
            self.assertFalse(res_no_admin["exito"])
            self.assertEqual(res_no_admin["motivo"], "permiso_denegado")

    def test_validacion_fecha_invalida(self):
        """3. Rechazo estricto de fechas con formato corrupto o invalido."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_corrupta = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-99-99", "justificada",
                "Motivo explicativo valido", "sol_f_inv", 0, dt_now
            )
            self.assertFalse(res_corrupta["exito"])
            self.assertEqual(res_corrupta["motivo"], "fecha_invalida")

    def test_validacion_fecha_futura(self):
        """4. Rechazo de fechas posteriores a la fecha actual."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_futura = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-25", "justificada",
                "Motivo explicativo valido", "sol_f_fut", 0, dt_now
            )
            self.assertFalse(res_futura["exito"])
            self.assertEqual(res_futura["motivo"], "fecha_futura")

    def test_validacion_fecha_anterior_a_obligaciones(self):
        """5. Rechazo de fechas anteriores a la primera entrega obligatoria."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_pasada = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-19", "justificada",
                "Motivo explicativo valido", "sol_f_ant", 0, dt_now
            )
            self.assertFalse(res_pasada["exito"])
            self.assertEqual(res_pasada["motivo"], "fecha_anterior_a_obligaciones")

    def test_conteo_dias_cumplidos_y_ausencia_justificada(self):
        """6. cumplida_por_revision suma día; justificada no suma día pero no sanciona."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_just = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Fallo tecnico justificado", "sol_c1", 0, dt_now
            )
            self.assertTrue(res_just["exito"])
            self.assertEqual(res_just["dias_completados"], 0)

            self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-22"] = {
                "fecha_vencimiento": "2026-09-22",
                "estado": "pendiente_revision"
            }
            res_cumplida = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-22", "cumplida_por_revision",
                "Captura recibida por soporte", "sol_c2", 1, dt_now
            )
            self.assertTrue(res_cumplida["exito"])
            self.assertEqual(res_cumplida["dias_completados"], 1)

    def test_conservacion_progreso_antiguo_e_incertidumbre_legacy(self):
        """7. Identificación explícita de datos legacy sin usarlos como piso mínimo."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 5
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {}
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Ajuste conservando legacy", "sol_leg", 0, dt_now
            )
            self.assertTrue(res["exito"])
            prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
            self.assertEqual(prueba["dias_completados_legacy"], 5)
            self.assertTrue(prueba["incertidumbre_progreso_legacy"])
            self.assertEqual(prueba["dias_completados"], 0)

    def test_correccion_legitima_reduce_dias_sin_clasificar_como_legacy(self):
        """8. Una corrección legítima en un caso 5C que reduce el conteo NO se marca como legacy."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 2
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {
            "2026-09-21": {"tipo": "checkin", "estado_entrega": "puntual", "captura": "c21.png"},
            "2026-09-22": {"tipo": "checkin", "estado_entrega": "puntual", "captura": "c22.png"}
        }
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-21"] = {
            "fecha_vencimiento": "2026-09-21",
            "estado": "pendiente_revision"
        }
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "injustificada",
                "Invalidadas entregas por revision", "sol_corr_1", 0, dt_now
            )
            self.assertTrue(res["exito"])
            prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
            self.assertEqual(prueba["dias_completados"], 1)
            self.assertNotIn("dias_completados_legacy", prueba)
            self.assertNotIn("incertidumbre_progreso_legacy", prueba)

    def test_reintentos_idempotentes_y_reuso_incorrecto_id(self):
        """9. Reintentar id_solicitud devuelve cached sin escrituras; reuso con distinto contenido rechaza."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res1 = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Fallo de servidor verificado", "sol_unique", 0, dt_now
            )
            self.assertTrue(res1["exito"])

            res2 = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "cumplida_por_revision",
                "Cambio posterior por admin", "sol_unique_2", 1, dt_now
            )
            self.assertTrue(res2["exito"])

            res1_retry = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Fallo de servidor verificado", "sol_unique", 0, dt_now
            )
            self.assertEqual(res1, res1_retry)

            res_bad = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "injustificada",
                "Contenido distinto", "sol_unique", 2, dt_now
            )
            self.assertFalse(res_bad["exito"])
            self.assertEqual(res_bad["motivo"], "id_solicitud_reutilizado_con_contenido_diferente")

    def test_solicitud_repetida_despues_de_expulsion(self):
        """10. Solicitud ya procesada devuelve resultado cached aunque el tester haya sido expulsado."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res1 = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Fallo aceptado", "sol_exp_1", 0, dt_now
            )
            self.assertTrue(res1["exito"])

            self.storage["apps.json"]["demo"]["pruebas"].pop("42", None)
            self.storage["apps.json"]["demo"]["testers"].remove("42")

            res_retry = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Fallo aceptado", "sol_exp_1", 0, dt_now
            )
            self.assertEqual(res1, res_retry)
            self.assertNotIn("42", self.storage["apps.json"]["demo"]["pruebas"])

    def test_conflicto_de_version_concurrente(self):
        """11. Pasar versión desfasada en solicitud nueva retorna conflicto_modificacion_concurrente."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_conflict = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "justificada",
                "Version esperada desfasada", "sol_v1", 1, dt_now
            )
            self.assertFalse(res_conflict["exito"])
            self.assertEqual(res_conflict["motivo"], "conflicto_modificacion_concurrente")

    def test_fallo_commit_no_deja_cambios_parciales(self):
        """12. Fallo en commit de Firestore es atómico: no guarda app, auditoría ni solicitud."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        before_storage = copy.deepcopy(self.storage)
        self.firebase.fail_commit = True

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            with self.assertRaises(RuntimeError):
                self.firebase.ejecutar(
                    db.revisar_falta_obligacion,
                    "dev_owner", "demo", "42", "2026-09-21", "justificada",
                    "Fallo simulado", "sol_fail", 0, dt_now
                )
        self.assertEqual(self.storage, before_storage)

    def test_evidencia_tardia_post_resolucion_manual(self):
        """13. Checkin recibido tras resolución manual guarda evidencia y marca requiere_nueva_revision."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-22"] = {
            "fecha_vencimiento": "2026-09-22",
            "estado": "pendiente_revision"
        }

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_man = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-22", "injustificada",
                "Falta de prueba explicita", "sol_m1", 0, dt_now
            )
            self.assertTrue(res_man["exito"])

            with patch.object(db, "_firebase_configurado", return_value=True):
                self.firebase.ejecutar(
                    db.registrar_checkin,
                    "42", "demo", "captura_tardia.png", "2026-09-22",
                    "msg_tardio_100", "2026-09-22T16:59:00-06:00", "2026-09-22"
                )

        falta = self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-22"]
        self.assertEqual(falta["estado_resolucion"], "injustificada")
        self.assertTrue(falta["requiere_nueva_revision"])
        self.assertIn("evidencia_tardia_post_resolucion", falta)
        self.assertEqual(falta["evidencia_tardia_post_resolucion"]["captura"], "captura_tardia.png")

    def test_compatibilidad_referencia_audit_ref_id_con_api_real(self):
        """14. _revisar_falta_transaccional usa audit_ref.id y no depende de atributo 'key'."""
        from inscripcion_simulada import ScopedMexicoCityTZ
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        mock_audit_ref = AuditRefMockSinKey("evt_real_123")
        app_ref = self.firebase.collection("apps").document("demo")
        sol_ref = self.firebase.collection("solicitudes_revisiones").document("demo_42_sol_real_1")

        with ScopedMexicoCityTZ(), patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            tx = self.firebase.transaction()
            res = db._revisar_falta_transaccional(
                tx,
                app_ref,
                mock_audit_ref,
                sol_ref,
                "dev_owner",
                "demo",
                "42",
                "2026-09-21",
                "justificada",
                "Prueba con mock de DocumentReference real",
                "sol_real_1",
                0,
                "SERVER_TIMESTAMP",
                dt_now
            )
            self.firebase._commit(tx)
            self.assertTrue(res["exito"])
            self.assertEqual(res["id_evento"], "evt_real_123")
            self.assertIn("evt_real_123", self.storage["auditoria_revisiones.json"])

    def test_checkin_en_prueba_legacy_conserva_dias_legacy_12_y_bloquea_pago(self):
        """15. Prueba antigua con 12 días sin historial diario recibe check-in: conserva 12 legacy y bloquea pago."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 12
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {}
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["historial_eventos_ids"] = []

        with patch.object(db, "_firebase_configurado", return_value=True):
            res = self.firebase.ejecutar(
                db.registrar_checkin,
                "42", "demo", "captura_post_legacy_1.png", "2026-09-21",
                "msg_leg_chk_1", "2026-09-21T12:00:00-06:00", "2026-09-21"
            )

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados_legacy"], 12)
        self.assertTrue(prueba["incertidumbre_progreso_legacy"])
        self.assertTrue(prueba["requiere_conciliacion_legacy"])
        self.assertTrue(prueba["requiere_conciliacion"])
        self.assertEqual(prueba["dias_completados"], 1)
        self.assertEqual(len(prueba["registros"]), 1)
        self.assertIn("2026-09-21", prueba["registros"])
        self.assertEqual(res["resultado"], "checkin")

    def test_historial_completo_evidencias_post_resolucion_sin_sobrescribir_ni_duplicar(self):
        """16. Fecha resuelta manualmente recibe dos mensajes distintos y un reintento: conserva ambas evidencias sin duplicar."""
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-22"] = {
            "fecha_vencimiento": "2026-09-22",
            "estado": "pendiente_revision"
        }

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res_man = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-22", "injustificada",
                "Resolución manual previa", "sol_m_post", 0, dt_now
            )
            self.assertTrue(res_man["exito"])

        with patch.object(db, "_firebase_configurado", return_value=True):
            self.firebase.ejecutar(
                db.registrar_checkin,
                "42", "demo", "cap_post_1.png", "2026-09-22",
                "msg_post_1", "2026-09-22T17:15:00-06:00", "2026-09-22"
            )

        with patch.object(db, "_firebase_configurado", return_value=True):
            self.firebase.ejecutar(
                db.registrar_checkin,
                "42", "demo", "cap_post_2.png", "2026-09-22",
                "msg_post_2", "2026-09-22T17:30:00-06:00", "2026-09-22"
            )

        with patch.object(db, "_firebase_configurado", return_value=True):
            self.firebase.ejecutar(
                db.registrar_checkin,
                "42", "demo", "cap_post_1.png", "2026-09-22",
                "msg_post_1", "2026-09-22T17:15:00-06:00", "2026-09-22"
            )

        falta = self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-22"]
        self.assertEqual(falta["estado_resolucion"], "injustificada")
        self.assertTrue(falta["requiere_nueva_revision"])
        self.assertIn("evidencias_post_resolucion", falta)
        evs = falta["evidencias_post_resolucion"]
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["id_mensaje"], "msg_post_1")
        self.assertEqual(evs[0]["estado_entrega"], "tardio")
        self.assertEqual(evs[1]["id_mensaje"], "msg_post_2")
        self.assertEqual(evs[1]["estado_entrega"], "tardio")

    def test_contador_2_con_solo_un_registro_valido_sin_revisiones_previas_marca_legacy(self):
        """17. Contador 2 con dos registros pero solo uno válido y sin revisiones previas conserva 2 legacy y señala incertidumbre."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 2
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {
            "2026-09-21": {"tipo": "checkin", "estado_entrega": "puntual", "captura": "cap21.png"},
            "2026-09-22": {"tipo": "checkin", "estado_entrega": "tardio", "captura": "cap22.png"}
        }
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-21"] = {
            "fecha_vencimiento": "2026-09-21",
            "estado": "pendiente_revision"
        }
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["historial_eventos_ids"] = []
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "cumplida_por_revision",
                "Revisión de fecha puntual", "sol_unval_1", 0, dt_now
            )
            self.assertTrue(res["exito"])

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados_legacy"], 2)
        self.assertTrue(prueba["incertidumbre_progreso_legacy"])
        self.assertTrue(prueba["requiere_conciliacion_legacy"])

    def test_caso_moderno_contador_2_dos_fechas_validas_primera_revision_invalida_fecha_sin_legacy(self):
        """18. Caso moderno con contador 2 y 2 fechas válidas: primera revisión invalida una fecha, deja contador verificado en 1 y NO inventa legacy."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 2
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {
            "2026-09-21": {"tipo": "checkin", "estado_entrega": "puntual", "captura": "cap21.png"},
            "2026-09-22": {"tipo": "checkin", "estado_entrega": "puntual", "captura": "cap22.png"}
        }
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-21"] = {
            "fecha_vencimiento": "2026-09-21",
            "estado": "pendiente_revision"
        }
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["historial_eventos_ids"] = []
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "injustificada",
                "Fecha invalidada por falta de calidad", "sol_mod_1", 0, dt_now
            )
            self.assertTrue(res["exito"])

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados"], 1)
        self.assertNotIn("dias_completados_legacy", prueba)
        self.assertNotIn("incertidumbre_progreso_legacy", prueba)

    def test_revision_repetida_o_checkin_posterior_no_sobrescribe_dias_legacy_ya_preservado(self):
        """19. Repetir revisiones o check-ins posteriores NO sobrescribe ni altera dias_completados_legacy ya preservado."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 12
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {}
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-21"] = {
            "fecha_vencimiento": "2026-09-21",
            "estado": "pendiente_revision"
        }
        dt_now = datetime(2026, 9, 22, 12, 0, 0)

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res1 = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-21", "cumplida_por_revision",
                "Ajuste inicial conservando legacy", "sol_rep_1", 0, dt_now
            )
            self.assertTrue(res1["exito"])

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados_legacy"], 12)
        self.assertEqual(prueba["dias_completados"], 1)

        self.storage["apps.json"]["demo"]["pruebas"]["42"]["posibles_faltas"]["2026-09-22"] = {
            "fecha_vencimiento": "2026-09-22",
            "estado": "pendiente_revision"
        }
        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            res2 = self.firebase.ejecutar(
                db.revisar_falta_obligacion,
                "dev_owner", "demo", "42", "2026-09-22", "justificada",
                "Segunda revisión manual", "sol_rep_2", 1, dt_now
            )
            self.assertTrue(res2["exito"])

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados_legacy"], 12)

        with patch.object(db, "_firebase_configurado", return_value=True):
            self.firebase.ejecutar(
                db.registrar_checkin,
                "42", "demo", "cap_chk_rep.png", "2026-09-22",
                "msg_chk_rep_1", "2026-09-22T12:00:00-06:00", "2026-09-22"
            )

        prueba = self.storage["apps.json"]["demo"]["pruebas"]["42"]
        self.assertEqual(prueba["dias_completados_legacy"], 12)

    def test_fallo_commit_no_guarda_parcialmente_preservacion_legacy_ni_revision(self):
        """20. Un fallo de commit en Firestore no guarda parcialmente la preservación del contador legacy ni la revisión."""
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["dias_completados"] = 12
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["registros"] = {}
        self.storage["apps.json"]["demo"]["pruebas"]["42"]["historial_eventos_ids"] = []
        dt_now = datetime(2026, 9, 22, 12, 0, 0)
        before_storage = copy.deepcopy(self.storage)
        self.firebase.fail_commit = True

        with patch.dict("os.environ", {"ADMIN_IDS": "admin_user"}):
            with self.assertRaises(RuntimeError):
                self.firebase.ejecutar(
                    db.revisar_falta_obligacion,
                    "dev_owner", "demo", "42", "2026-09-21", "justificada",
                    "Revision que fallara en commit", "sol_fail_commit_leg", 0, dt_now
                )

        self.assertEqual(self.storage, before_storage)


if __name__ == "__main__":
    unittest.main()

