"""Import an existing Git working folder into CodeBridge Multi Agents safely.

UI chooses folder; backend verifies repository and creates authoritative task state.
No git init, git add, commit, clone or source-file mutations.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import chat_tasks
import ma_memory

_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def suggested_id(path: str | Path) -> str:
    name = Path(path).resolve().name
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    candidate = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:40].strip("-")
    return candidate or "meu-projeto"


def inspect_folder(path: str | Path) -> dict:
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("A pasta selecionada nao existe.")
    try:
        git_root = chat_tasks.git(folder, "rev-parse", "--show-toplevel").strip()
        repo = Path(git_root).resolve()
        if not repo.is_dir():
            raise ValueError("Raiz Git nao encontrada.")
        head = chat_tasks.git(repo, "rev-parse", "--verify", "HEAD").strip()
        if not re.fullmatch(r"[a-f0-9]{40}", head):
            raise ValueError("Commit HEAD Git invalido.")
        dirty = bool(chat_tasks.git(repo, "status", "--porcelain").strip())
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Multi Agents requer um repositorio Git com pelo menos um commit. "
            "Selecione um projeto Git existente ou inicialize e confirme "
            "um commit antes de adiciona-lo."
        ) from exc
    return {
        "selected": str(folder),
        "repo": str(repo),
        "git_head": head,
        "dirty": dirty,
        "nested_folder": folder != repo,
        "suggested_id": suggested_id(repo),
    }


def register_folder(root: str | Path, project: str, folder: str | Path, contract: str) -> dict:
    """Register a manually selected existing Git repo and queue local RAG indexing."""
    if not isinstance(project, str) or not _PROJECT.fullmatch(project):
        raise ValueError("ID deve ter 1 a 40 letras minusculas, numeros ou hifens.")
    contract = ma_memory.safe_text(contract, 3000)
    if not contract:
        raise ValueError("Informe um objetivo/contrato para o projeto.")
    metadata = inspect_folder(folder)
    repo = Path(metadata["repo"])
    root = Path(root).resolve()

    with chat_tasks.connect(root) as db:
        if db is not None:
            dup = db.execute("SELECT id, repo FROM projects WHERE id=?", (project,)).fetchone()
            if dup is not None:
                raise ValueError("Ja existe um projeto Multi Agents com este identificador.")
            other = db.execute("SELECT id, repo FROM projects").fetchall()
            for row in other:
                if Path(row["repo"]).resolve() == repo:
                    raise ValueError("Esta pasta ja pertence ao projeto Multi Agents: " + row["id"])

    result = chat_tasks.commands(SimpleNamespace(
        root=root, action="init", project=project, repo=repo, text=contract
    ))
    # Only start indexing after chat_tasks.commands has committed the SQLite transaction.
    try:
        result["rag_index"] = ma_memory.spawn_index(root, project, namespace="base")
    except (OSError, ValueError) as exc:
        result["rag_index"] = {
            "state": "FAILED_TO_LAUNCH",
            "error_type": type(exc).__name__,
            "suggestion": "Use rag-sync no projeto apos verificar o CodeBridge.",
        }
    result["project_folder"] = str(repo)
    result["git_initial_head"] = metadata["git_head"]
    return result
