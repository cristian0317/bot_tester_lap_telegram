import copy
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
import zoneinfo
from datetime import tzinfo, timezone, timedelta

class DummyZoneInfo(tzinfo):
    def __init__(self, key="America/Mexico_City"):
        self.key = key
    def utcoffset(self, dt):
        return timedelta(hours=-6)
    def tzname(self, dt):
        return self.key
    def dst(self, dt):
        return timedelta(0)
    def __str__(self):
        return self.key
    def __repr__(self):
        return f"ZoneInfo(key={self.key!r})"

class ScopedMexicoCityTZ:
    """Context manager para aplicar el doble de prueba de zona horaria con alcance delimitado."""
    def __init__(self):
        self.patcher = None

    def __enter__(self):
        try:
            zoneinfo.ZoneInfo("America/Mexico_City")
        except Exception:
            orig = zoneinfo.ZoneInfo
            def fake_zi(key):
                if key == "America/Mexico_City":
                    return DummyZoneInfo(key)
                return orig(key)
            self.patcher = patch.object(zoneinfo, "ZoneInfo", side_effect=fake_zi)
            self.patcher.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.patcher:
            self.patcher.stop()


import db




class ConflictoLectura(RuntimeError):
    pass


class DocumentoInexistente(RuntimeError):
    pass


class DocumentoExistente(RuntimeError):
    pass


class Ref:
    def __init__(self, client, collection, doc_id):
        self.client = client
        self.collection = str(collection)
        self._doc_id = str(doc_id)

    @property
    def id(self):
        return self._doc_id

    @property
    def collection_name(self):
        col = self.collection
        return col if col.endswith(".json") else col + ".json"

    @property
    def path(self):
        return self.collection_name, self._doc_id

    def get(self, transaction=None):
        if transaction is not None:
            assert not transaction.pending, "Todas las lecturas deben preceder las escrituras"
            transaction.reads[self.path] = transaction.versions.get(self.path, 0)
            data = transaction.snapshot.get(self.collection_name, {}).get(self._doc_id)
        else:
            data = self.client.storage.get(self.collection_name, {}).get(self._doc_id)
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

    def get(self, ref):
        return ref.get(self)

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
    def __init__(self, storage, writes=None):
        self.storage = storage
        self.writes = writes if writes is not None else []
        self.before_commit = None
        self.fail_commit = False
        self.lose_ack = False
        self.attempts = 0
        self.versions = {}
        self.observed = copy.deepcopy(storage)

    def _sync_versions(self):
        all_cols = self.storage.keys() | self.observed.keys()
        for col in all_cols:
            curr_docs = self.storage.get(col, {})
            old_docs = self.observed.get(col, {})
            for key in curr_docs.keys() | old_docs.keys():
                if (key in curr_docs) != (key in old_docs) or curr_docs.get(key) != old_docs.get(key):
                    path = (col, str(key))
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

        # Validar y preparar todo en una copia privada antes de aplicar nada
        committed = copy.deepcopy(self.storage)
        for operation, ref, fields in transaction.pending:
            col_name = ref.collection_name
            collection = committed.setdefault(col_name, {})
            key_str = ref.id
            if operation == "update":
                if key_str not in collection:
                    raise DocumentoInexistente(ref.path)
                collection[key_str].update(fields)
            elif operation == "create":
                if key_str in collection:
                    raise DocumentoExistente(ref.path)
                collection[key_str] = copy.deepcopy(fields)
            else:  # "set"
                collection[key_str] = copy.deepcopy(fields)

        # Aplicar los cambios solo si todas las operaciones fueron válidas
        self.storage.clear()
        self.storage.update(committed)
        for _, ref, _ in transaction.pending:
            self.versions[ref.path] = self.versions.get(ref.path, 0) + 1
        self.observed = copy.deepcopy(self.storage)
        self.writes.extend(ref.collection_name for _, ref, _ in transaction.pending)
        if self.lose_ack:
            raise RuntimeError("commit succeeded but acknowledgement lost")

    def collection(self, name):
        col_key = name if name.endswith(".json") else name + ".json"
        docs = self.storage.get(col_key, {})
        return NS(
            document=lambda key: Ref(self, name, key),
            stream=lambda: [NS(id=str(k), to_dict=lambda d=v: copy.deepcopy(d)) for k, v in docs.items()]
        )

    def transaction(self):
        return Transaction(self)

    def batch(self):
        return Batch(self)

    def transactional(self, callback):
        def execute(transaction, *args, **kwargs):
            for _ in range(5):
                self.attempts += 1
                transaction.reset()
                result = callback(transaction, *args, **kwargs)
                try:
                    self._commit(transaction)
                except ConflictoLectura:
                    continue
                return result
            raise RuntimeError("retry limit")
        return execute

    def inscribir(self, user_id, app_id):
        fake_module = NS(firestore=NS(transactional=self.transactional))
        with ScopedMexicoCityTZ(), \
             patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):
            return db.inscribir_tester(user_id, app_id)

    def ejecutar(self, operation, *args, **kwargs):
        fake_module = NS(firestore=NS(transactional=self.transactional, SERVER_TIMESTAMP="SERVER_TIMESTAMP"))
        with ScopedMexicoCityTZ(), \
             patch.object(db, "_firebase_initialized", True), \
             patch.object(db, "_inicializar_firebase", return_value=self), \
             patch.dict("sys.modules", {"firebase_admin": fake_module}):
            return operation(*args, **kwargs)
