import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from inscripcion_simulada import (
    FirestoreSimulado, ConflictoLectura, DocumentoInexistente, DocumentoExistente
)


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.storage = {
            "usuarios.json": {"42": {"nombre": "Juan"}},
            "apps.json": {"app_1": {"nombre": "App Test"}},
        }
        self.writes = []
        self.firebase = FirestoreSimulado(self.storage, self.writes)

    def test_concurrent_change_on_read_document_causes_retry(self):
        """1. Cambio concurrente en un documento leído genera ConflictoLectura y reintento."""
        def callback(tx):
            ref = self.firebase.collection("usuarios").document("42")
            doc = ref.get(tx)
            tx.update(ref, {"nombre": doc.to_dict()["nombre"] + " Modificado"})
            return True

        self.firebase.before_commit = lambda: self.storage["usuarios.json"]["42"].update(nombre="Juan Concurrente")
        res = self.firebase.transactional(callback)(self.firebase.transaction())
        self.assertTrue(res)
        self.assertEqual(self.firebase.attempts, 2)
        self.assertEqual(self.storage["usuarios.json"]["42"]["nombre"], "Juan Concurrente Modificado")

    def test_change_on_unread_document_does_not_cause_conflict(self):
        """2. Cambio en un documento ajeno no genera conflicto en la transacción."""
        def callback(tx):
            ref_app = self.firebase.collection("apps").document("app_1")
            app_doc = ref_app.get(tx)
            tx.update(ref_app, {"nombre": app_doc.to_dict()["nombre"] + " V2"})
            return True

        self.firebase.before_commit = lambda: self.storage["usuarios.json"]["42"].update(nombre="Juan Concurrente")
        res = self.firebase.transactional(callback)(self.firebase.transaction())
        self.assertTrue(res)
        self.assertEqual(self.firebase.attempts, 1)
        self.assertEqual(self.storage["apps.json"]["app_1"]["nombre"], "App Test V2")
        self.assertEqual(self.storage["usuarios.json"]["42"]["nombre"], "Juan Concurrente")

    def test_failed_write_prevents_partial_application(self):
        """3. Fallo de una escritura en transacción/lote impide la aplicación parcial de las demás."""
        tx = self.firebase.transaction()
        ref_user = self.firebase.collection("usuarios").document("42")
        ref_inexistent = self.firebase.collection("usuarios").document("999")
        tx.update(ref_user, {"nombre": "Juan Actualizado"})
        tx.update(ref_inexistent, {"nombre": "Fantasma"})

        with self.assertRaises(DocumentoInexistente):
            self.firebase._commit(tx)

        # Verificar que el documento 42 no fue modificado parcialmente
        self.assertEqual(self.storage["usuarios.json"]["42"]["nombre"], "Juan")
        self.assertNotIn("999", self.storage["usuarios.json"])

    def test_update_inexistent_document_fails(self):
        """4a. update falla si el documento no existe."""
        tx = self.firebase.transaction()
        ref_inexistent = self.firebase.collection("usuarios").document("999")
        tx.update(ref_inexistent, {"nombre": "Nadie"})
        with self.assertRaises(DocumentoInexistente):
            self.firebase._commit(tx)

    def test_create_existing_document_fails(self):
        """4b. create falla si el documento ya existe."""
        tx = self.firebase.transaction()
        ref_user = self.firebase.collection("usuarios").document("42")
        tx.create(ref_user, {"nombre": "Duplicado"})
        with self.assertRaises(DocumentoExistente):
            self.firebase._commit(tx)

    def test_create_conflict_when_created_after_initial_read(self):
        """4c. create falla si otra operación lo crea después de la lectura inicial."""
        tx = self.firebase.transaction()
        ref_new = self.firebase.collection("usuarios").document("100")
        doc = ref_new.get(tx)
        self.assertFalse(doc.exists)
        tx.create(ref_new, {"nombre": "Nuevo"})

        # Simular que otra operación crea el documento '100' antes del commit
        self.firebase.before_commit = lambda: self.storage["usuarios.json"].update({"100": {"nombre": "Creado Por Otro"}})

        with self.assertRaises(ConflictoLectura):
            self.firebase._commit(tx)


if __name__ == "__main__":
    unittest.main()
