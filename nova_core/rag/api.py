"""nova_core.rag.api — the desktop's document surface.

Registered onto the desk Flask app. Thin by design: the pipeline lives in
`library`, and this exposes it to the SPA plus the `rag_search` tool.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path

from flask import jsonify, request

from . import embeddings as E
from .library import Library, library, user_scope

log = logging.getLogger("nova.rag.api")

_ingest_lock = threading.Lock()


def _scope() -> str:
    """The current user's private scope.

    Falls back to a local identity when no account is signed in, so a
    local-only NOVA still has a working document library.
    """
    try:
        import nova_account
        acct = nova_account.account()
        if acct.signed_in and acct.user_id:
            return user_scope(acct.user_id)
    except Exception:
        pass
    return user_scope("local")


def _preference() -> str:
    try:
        from desk.settings import get as _get
        return str(_get("embedding_backend", "") or "")
    except Exception:
        return ""


def _lib() -> Library:
    return library(embedding_preference=_preference())


def register(app, require_token) -> None:
    """Attach document endpoints to the desk app."""

    @app.get("/api/documents")
    @require_token
    def api_documents():
        lib = _lib()
        docs = lib.documents([_scope()])
        return jsonify({
            "ok": True,
            "documents": [{
                "id": d.id, "filename": d.filename, "title": d.title,
                "kind": d.kind, "chunks": d.chunk_count, "bytes": d.bytes,
                "pages": d.page_count, "added_at": d.added_at,
                "backend": d.embedding_backend, "warnings": d.warnings,
            } for d in docs],
            "stats": lib.stats([_scope()]),
        })

    @app.post("/api/documents")
    @require_token
    def api_documents_add():
        """Accept an uploaded file, or index one already on disk."""
        lib = _lib()
        scope = _scope()

        upload = request.files.get("file")
        if upload is not None and upload.filename:
            tmpdir = Path(tempfile.mkdtemp(prefix="nova-upload-"))
            target = tmpdir / Path(upload.filename).name
            try:
                upload.save(str(target))
                with _ingest_lock:
                    result = lib.add_file(target, scope=scope,
                                          source=f"upload:{upload.filename}")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            body = request.get_json(silent=True) or {}
            path = (body.get("path") or "").strip()
            if not path:
                return jsonify({"ok": False, "error": "no_file",
                                "message": "Attach a file or give a path."}), 400
            with _ingest_lock:
                result = lib.add_file(path, scope=scope, source=path)

        status = 200 if result.ok else 400
        return jsonify({
            "ok": result.ok, "status": result.status,
            "message": result.message, "chunks": result.chunks,
            "warnings": result.warnings or [],
            "document_id": result.document.id if result.document else "",
        }), status

    @app.delete("/api/documents/<doc_id>")
    @require_token
    def api_documents_delete(doc_id: str):
        lib = _lib()
        doc = lib.store.get_document(doc_id)
        if doc is None or doc.scope != _scope():
            # Not found rather than forbidden: another user's document must
            # not be confirmed to exist.
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": lib.forget(doc_id)})

    @app.get("/api/documents/search")
    @require_token
    def api_documents_search():
        q = (request.args.get("q") or "").strip()
        try:
            limit = min(int(request.args.get("limit", "6")), 20)
        except ValueError:
            limit = 6
        hits = _lib().search(q, scopes=[_scope()], limit=limit)
        return jsonify({"ok": True, "hits": [{
            "text": h.chunk.text, "citation": h.citation(),
            "score": h.score, "how": h.how,
            "document_id": h.document.id, "locator": h.chunk.locator,
        } for h in hits]})

    @app.get("/api/documents/embeddings")
    @require_token
    def api_embedding_choices():
        """What the setup screen offers, with live availability."""
        lib = _lib()
        res = lib.embeddings
        return jsonify({
            "ok": True,
            "chosen": _preference() or "auto",
            "active": res.name,
            "honoured": res.honoured,
            "note": res.message(),
            "choices": E.describe_choices(),
        })

    @app.post("/api/documents/embeddings")
    @require_token
    def api_embedding_choose():
        body = request.get_json(silent=True) or {}
        choice = (body.get("choice") or "").strip().lower()
        if choice not in E.CHOICES and choice != "auto":
            return jsonify({"ok": False, "error": "unknown_choice"}), 400
        try:
            from desk.settings import set_many
            set_many({"embedding_backend": choice})
        except Exception:
            pass
        res = _lib().set_embedding_preference(choice)
        return jsonify({"ok": True, "active": res.name,
                        "honoured": res.honoured, "note": res.message()})

    @app.post("/api/documents/embeddings/download")
    @require_token
    def api_embedding_download():
        """Fetch the local model. Long-running, so it reports rather than
        blocking the UI thread silently."""
        def _work():
            try:
                E.download_onnx_model()
                log.info("[RAG] local embedding model ready")
            except Exception as e:
                log.warning("[RAG] model download failed: %s", e)

        threading.Thread(target=_work, name="nova-model-download",
                         daemon=True).start()
        return jsonify({"ok": True, "message": "Downloading the local model."})


# -- the tool ---------------------------------------------------------------

def rag_search(args: dict, meta: dict | None = None) -> str:
    """Tool entry point: search the user's documents and return cited passages.

    Results are wrapped as evidence, and the caller enters an untrusted scope
    while reasoning over them -- a document is content NOVA did not author.
    """
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return "No search query was given."
    try:
        limit = min(int(args.get("limit", 5)), 12)
    except (TypeError, ValueError):
        limit = 5

    lib = _lib()
    scopes = [_scope()]
    for extra in (args.get("scopes") or []):
        if isinstance(extra, str) and extra.startswith("project:"):
            scopes.append(extra)

    hits = lib.search(query, scopes=scopes, limit=limit)
    if not hits:
        stats = lib.stats(scopes)
        if not stats["documents"]:
            return ("There are no documents in your library yet. Add one and "
                    "NOVA can answer from it.")
        return (f"Nothing in your {stats['documents']} document(s) matched "
                f"{query!r}.")
    return lib.context_block(hits)


__all__ = ["register", "rag_search"]
