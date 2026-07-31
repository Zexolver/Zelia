"""
ZELIA's long-term memory.

Nothing is fed in manually -- every conversation turn gets embedded and
stored automatically (auto_capture in config.yaml), and relevant memories
are pulled back in as context on future requests. Over time this becomes a
knowledge base built entirely out of your conversations with her.

Efficiency is the only design goal here (explicit user priority, not
recall sophistication):
  - Embeddings use chromadb's built-in ONNX MiniLM-L6-v2 runner
    (`DefaultEmbeddingFunction`) instead of the sentence-transformers/torch
    pipeline -- same model, but ONNX Runtime is faster for the
    single-sequence CPU inference this does on every turn, and it avoids
    pulling torch in just for this. Falls back to sentence-transformers
    only if the user points `embedding_model` at something other than the
    default in config.yaml.
  - `remember()` never blocks the conversation: writes go on a queue
    drained by one background thread, so embedding + persisting a turn
    happens after the reply is already on its way to the user, not before.
  - `recall()` stays synchronous since the agent loop needs the result
    before it can build a prompt -- that's the one part that's inherently
    on the critical path.
"""
import atexit
import queue
import threading
import time
import uuid

import chromadb
from chromadb.utils import embedding_functions

from src.utils.logger import get_logger

log = get_logger("second_brain")

DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"


def _build_embedder(embedding_model: str):
    if embedding_model == DEFAULT_MODEL_NAME:
        # ONNX Runtime build of the same model -- faster CPU inference than
        # the sentence-transformers/torch path below, no torch import needed.
        return embedding_functions.DefaultEmbeddingFunction()
    log.info("Non-default embedding model %s requested -- using sentence-transformers "
              "(slower than the ONNX default path).", embedding_model)
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=embedding_model)


class SecondBrain:
    def __init__(self, db_path: str, embedding_model: str, top_k: int = 5):
        self.top_k = top_k
        self.client = chromadb.PersistentClient(path=db_path)
        self.embedder = _build_embedder(embedding_model)
        self.collection = self.client.get_or_create_collection(
            name="zelia_memory", embedding_function=self.embedder
        )

        self._write_queue: "queue.Queue" = queue.Queue()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
        atexit.register(self.flush)

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None:
                self._write_queue.task_done()
                return
            text, meta, doc_id = item
            try:
                self.collection.add(documents=[text], metadatas=[meta], ids=[doc_id])
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to persist memory: %s", exc)
            finally:
                self._write_queue.task_done()

    def remember(self, text: str, role: str, metadata: dict | None = None) -> None:
        """Queue a piece of conversation/fact for storage. Non-blocking -- returns
        immediately, the actual embed+write happens on a background thread."""
        if not text.strip():
            return
        meta = {"role": role, "timestamp": time.time(), **(metadata or {})}
        self._write_queue.put((text, meta, str(uuid.uuid4())))

    def recall(self, query: str, top_k: int | None = None) -> list[str]:
        """Return the most relevant memories for a given query, most relevant first."""
        if self.collection.count() == 0:
            return []
        results = self.collection.query(query_texts=[query], n_results=top_k or self.top_k)
        docs = results.get("documents", [[]])[0]
        return docs

    def forget(self, query: str, top_k: int = 5) -> dict:
        """Finds memories similar to `query` and deletes them. For
        correcting a specific wrong/stale memory (e.g. a fabricated answer
        that would otherwise keep reinforcing itself as its own future
        context) -- not a bulk-clear, and not run automatically."""
        if self.collection.count() == 0:
            return {"ok": True, "deleted": 0}
        results = self.collection.query(query_texts=[query], n_results=top_k)
        ids = results.get("ids", [[]])[0]
        if not ids:
            return {"ok": True, "deleted": 0}
        self.collection.delete(ids=ids)
        log.info("Forgot %d memor%s matching %r", len(ids), "y" if len(ids) == 1 else "ies", query)
        return {"ok": True, "deleted": len(ids)}

    def flush(self, timeout: float = 5.0) -> None:
        """Block until queued writes are persisted. Called automatically on
        process exit; call directly if you need a guarantee before that."""
        try:
            self._write_queue.join()
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> dict:
        return {"total_memories": self.collection.count(), "pending_writes": self._write_queue.qsize()}

    def list_recent(self, limit: int = 100) -> list[dict]:
        """Most-recent-first listing for browsing/viewing the second brain
        (not a recall/search) -- e.g. the mobile app's memory viewer.
        chromadb's get() has no server-side ordering, so this fetches
        everything and sorts by the timestamp remember() always stores,
        then slices. Fine at this project's current memory-count scale;
        would need real pagination if the collection grows very large."""
        if self.collection.count() == 0:
            return []
        result = self.collection.get(include=["documents", "metadatas"])
        items = [
            {"id": id_, "text": doc, "role": meta.get("role", "unknown"), "timestamp": meta.get("timestamp", 0)}
            for id_, doc, meta in zip(result["ids"], result["documents"], result["metadatas"])
        ]
        items.sort(key=lambda m: m["timestamp"], reverse=True)
        return items[:limit]
