"""Modelo en memoria, no un emulador ni una validación del SDK.

Modela lecturas por documento, versiones, reintentos optimistas y commits
atómicos con set (reemplazo), update (campos superiores) y create.
No modela locks, red, consultas, field paths, transforms ni tiempos del servidor.
Las mutaciones directas de fixtures se detectan por contenido; para representar
una escritura sin cambio de contenido o un ciclo A-B-A, usar commits simulados.
"""
import copy
from types import SimpleNamespace as NS
from unittest.mock import patch
import db


class ConflictoLectura(RuntimeError):
    pass


class DocumentoInexistente(RuntimeError):
    pass


class DocumentoExistente(RuntimeError):
    pass


class Ref:
    def __init__(self, client, collection, key):
        self.client, self.collection, self.key = client, collection, key

    @property
    def path(self):
        return self.collection + ".json", self.key

    def get(self, transaction):
        assert not transaction.pending, "Todas las lecturas deben preceder las escrituras"
        transaction.reads[self.path] = transaction.versions.get(self.path, 0)
        data = transaction.snapshot.get(self.collection + ".json", {}).get(self.key)
        return NS(exists=data is not None, to_dict=lambda: copy.deepcopy(data))


class Transaction:
    def __init__(self, client):
        self.client = client
        self.reset()

    def reset(self):
        self.client._sync_versions()
        self.snapshot = copy.deepcopy(self.client.storage)
        self.versions = self.client.versions.copy()
        self.reads = {}
        self.pending = []

    def update(self, ref, data):
        self.pending.append(("update", ref, copy.deepcopy(data)))

    def set(self, ref, data):
        self.pending.append(("set", ref, copy.deepcopy(data)))

    def create(self, ref, data):
        self.pending.append(("create", ref, copy.deepcopy(data)))


class Batch(Transaction):
    """Mismas precondiciones de escritura, sin lecturas ni reintentos."""
    def commit(self):
        self.client._commit(self)


class FirestoreSimulado:
    def __init__(self, storage, writes):
        self.storage, self.writes = storage, writes
        self.before_commit = None
        self.fail_commit = False
        self.lose_ack = False
        self.attempts = 0
        self.versions = {}
        self.observed = copy.deepcopy(storage)

    def _sync_versions(self):
        for collection in self.storage.keys() | self.observed.keys():
            current, old = self.storage.get(collection, {}), self.observed.get(collection, {})
            for key in current.keys() | old.keys():
                if (key in current) != (key in old) or current.get(key) != old.get(key):
                    path = collection, key
                    self.versions[path] = self.versions.get(path, 0) + 1
        self.observed = copy.deepcopy(self.storage)

    def _commit(self, transaction):
        if self.before_commit:
            hook, self.before_commit = self.before_commit, None
            hook()
        self._sync_versions()
        if any(self.versions.get(path, 0) != version for path, version in transaction.reads.items()):
            raise ConflictoLectura("Un documento leído cambió")
        if self.fail_commit:
            raise RuntimeError("simulated commit failure")
        # Validar y preparar todo en una copia privada antes de publicar nada.
        committed = copy.deepcopy(self.storage)
        for operation, ref, fields in transaction.pending:
            collection = committed.setdefault(ref.collection + ".json", {})
            if operation == "update":
                if ref.key not in collection:
                    raise DocumentoInexistente(ref.path)
                collection[ref.key].update(fields)
            elif operation == "create":
                if ref.key in collection:
                    raise DocumentoExistente(ref.path)
                collection[ref.key] = fields
            else:
                collection[ref.key] = fields
        self.storage.clear()
        self.storage.update(committed)
        for _, ref, _ in transaction.pending:
            self.versions[ref.path] = self.versions.get(ref.path, 0) + 1
        self.observed = copy.deepcopy(self.storage)
        self.writes.extend(ref.collection + ".json" for _, ref, _ in transaction.pending)
        if self.lose_ack:
            raise RuntimeError("commit succeeded but acknowledgement lost")

    def collection(self, name):
        return NS(document=lambda key: Ref(self, name, key))

    def transaction(self):
        return Transaction(self)

    def batch(self):
        return Batch(self)

    def transactional(self, callback):
        def execute(transaction, *args):
            for _ in range(5):
                self.attempts += 1
                transaction.reset()
                result = callback(transaction, *args)
                try:
                    self._commit(transaction)
                except ConflictoLectura:
                    continue
                return result
            raise RuntimeError("retry limit")
        return execute

    def inscribir(self, user_id, app_id):
        fake_module = NS(firestore=NS(transactional=self.transactional))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):
            return db.inscribir_tester(user_id, app_id)

    def ejecutar(self, operation, *args):
        fake_module = NS(firestore=NS(transactional=self.transactional, SERVER_TIMESTAMP="SERVER_TIMESTAMP"))
        with patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):
            return operation(*args)
