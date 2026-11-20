"""Shared task state for ChatGPT conversations using the CodeBridge terminal."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import ma_sessions as sessions
import ma_memory
import ma_orchestration as orchestration


ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodeBridge" / "MultiAgents"
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, base TEXT NOT NULL,
  contract TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tasks (
  project TEXT NOT NULL, id TEXT NOT NULL, scope TEXT NOT NULL, brief TEXT NOT NULL,
  dependencies TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  agent TEXT, version INTEGER, base TEXT, commit_sha TEXT, evidence TEXT, files TEXT,
  PRIMARY KEY (project,id)
);
CREATE TABLE IF NOT EXISTS ma_integrations (
  project TEXT NOT NULL, version INTEGER NOT NULL, attempt TEXT NOT NULL,
  expires REAL NOT NULL, commit_sha TEXT, PRIMARY KEY (project,version)
);
CREATE TABLE IF NOT EXISTS messages (
  seq INTEGER PRIMARY KEY, project TEXT NOT NULL, actor TEXT NOT NULL,
  task TEXT, text TEXT NOT NULL, created TEXT NOT NULL
);
"""


@contextmanager
def connect(root, write=False):
    if write:
        root.mkdir(parents=True, exist_ok=True)
    elif not (root / "tasks.db").is_file():
        yield None
        return
    db = sqlite3.connect(root / "tasks.db", timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if write:
            db.executescript(SCHEMA + sessions.SCHEMA + ma_memory.SCHEMA + orchestration.SCHEMA)
            db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def git(repo, *args):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=repo, capture_output=True, timeout=45,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "Git falhou")
    return result.stdout.decode("utf-8", "surrogateescape")


def key(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value):
        raise argparse.ArgumentTypeError("Use 1–40 letras minúsculas, números ou hífens")
    return value


def record(db, project, actor, task, text):
    db.execute(
        "INSERT INTO messages(project,actor,task,text,created) VALUES(?,?,?,?,?)",
        (project, actor, task, text, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def require(db, sql, args, label):
    row = db.execute(sql, args).fetchone()
    if row is None:
        raise ValueError(label + " não encontrado")
    return row


def get_task(db, project, task):
    return require(db, "SELECT * FROM tasks WHERE project=? AND id=?", (project, task), "Tarefa")


def snapshot(root=ROOT, project=None):
    with connect(root) as db:
        if db is None:
            return {"projects": [], "tasks": [], "messages": [], "sessions": [], "mail": []}
        where, args = (" WHERE project=?", (project,)) if project else ("", ())
        filter_project = " WHERE id=?" if project else ""
        return {
            "projects": [dict(r) for r in db.execute("SELECT * FROM projects" + filter_project + " ORDER BY id", args)],
            "tasks": [dict(r) for r in db.execute("SELECT * FROM tasks" + where + " ORDER BY project,id", args)],
            "sessions": sessions.sessions(db, project) if project else [],
            "mail": sessions.mail_view(db, project) if project else [],
            "messages": [
                dict(r) for r in db.execute(
                    "SELECT * FROM messages" + where + " ORDER BY seq DESC LIMIT 40", args
                )
            ],
        }


def current_workspace(root, project, task):
    return root / "worktrees" / project / task


def verify_delivery(root, project, task, version, commit):
    workspace = current_workspace(root, project["id"], task["id"])
    if task["version"] != version:
        raise ValueError("Contrato atualizado. Consulte status e execute accept-contract antes de entregar")
    if git(workspace, "status", "--porcelain").strip():
        raise ValueError("Faça commit das alterações antes de entregar")
    if git(workspace, "rev-parse", "HEAD").strip() != commit:
        raise ValueError("A entrega deve indicar o commit atual do worktree")
    git(workspace, "merge-base", "--is-ancestor", task["base"], commit)
    changed = git(workspace, "diff", "--no-renames", "--name-only", "-z", task["base"], commit)
    names = [name for name in changed.split("\0") if name]
    scopes = json.loads(task["scope"])
    outside = [name for name in names if not any(fnmatch.fnmatchcase(name, pattern) for pattern in scopes)]
    if outside:
        raise ValueError("Arquivos fora do escopo: " + ", ".join(outside))
    return names


def claim(root, db, project, task, actor):
    project_id, task_id = project["id"], task["id"]
    workspace = current_workspace(root, project_id, task_id)
    if task["agent"] == actor and task["state"] == "active":
        return workspace
    if task["state"] != "pending":
        raise ValueError("Tarefa já atribuída a " + (task["agent"] or "outro chat"))
    orchestration.check_claim(db,project_id,task_id,actor)
    active = db.execute(
        "SELECT 1 FROM tasks WHERE project=? AND agent=? AND state='active'", (project_id, actor)
    ).fetchone()
    if active:
        raise ValueError("Este chat já tem uma tarefa ativa")
    heads = []
    for dep_id in json.loads(task["dependencies"]):
        dependency = get_task(db, project_id, dep_id)
        if dependency["state"] != "delivered" or dependency["version"] != project["version"]:
            raise ValueError("Aguardando entrega atualizada de " + dep_id)
        heads.append(dependency["commit_sha"])
    workspace.parent.mkdir(parents=True, exist_ok=True)
    branch = "cbma/" + project_id + "/" + task_id + "-" + uuid4().hex[:8]
    git(project["repo"], "worktree", "add", "-b", branch, str(workspace), project["base"])
    try:
        if heads:
            git(workspace, "merge", "--no-ff", "--no-edit", *heads)
        base = git(workspace, "rev-parse", "HEAD").strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        git(project["repo"], "worktree", "remove", "--force", str(workspace))
        git(project["repo"], "branch", "-D", branch)
        raise
    db.execute(
        "UPDATE tasks SET agent=?,version=?,base=?,state='active' WHERE project=? AND id=?",
        (actor, project["version"], base, project_id, task_id),
    )
    record(db, project_id, actor, task_id, "pending → claimed → active")
    return workspace


def dispatch_once(root, db, project, actor):
    ready=orchestration.choose_next(db,project["id"],actor,project["version"])
    if ready["state"]!="READY":
        return ready
    task=get_task(db,project["id"],ready["task_id"])
    workspace=claim(root,db,project,task,actor)
    ma_memory.checkpoint(db,project["id"],actor,task["id"],"active",
        "Tarefa automaticamente atribuida: "+task["brief"],
        "Executar escopo, testar, fazer commit e deliver",
        pending=json.loads(task["dependencies"]),files=json.loads(task["scope"]),
        version=project["version"],git_head=git(workspace,"rev-parse","HEAD").strip())
    return {"state":"ASSIGNED","task_id":task["id"],"workspace":str(workspace),
            "task":dict(get_task(db,project["id"],task["id"])),
            "contract":project["contract"],"version":project["version"],
            "suggestion":"Continuar esta tarefa neste mesmo turno."}


def integrate(root, db, project, check):
    if not check:
        raise ValueError("Informe um comando de teste após --")
    tasks = db.execute("SELECT * FROM tasks WHERE project=?", (project["id"],)).fetchall()
    if not tasks or any(t["state"] != "delivered" or t["version"] != project["version"] for t in tasks):
        raise ValueError("Integração exige todas as entregas na versão atual do contrato")
    owners = {}
    for task in tasks:
        for path in json.loads(task["files"]):
            canonical = os.path.normcase(path)
            if canonical in owners:
                raise ValueError(f"Escopo sobreposto em {path}: {owners[canonical]} e {task['id']}")
            owners[canonical] = task["id"]
    version = project["version"]
    reservation = db.execute("SELECT * FROM ma_integrations WHERE project=? AND version=?", (project["id"], version)).fetchone()
    if reservation and (reservation["commit_sha"] or reservation["expires"] > time.time()):
        raise ValueError("Já existe uma integração ativa ou concluída para esta versão")
    revision = uuid4().hex[:10]
    db.execute(
        "INSERT OR REPLACE INTO ma_integrations(project,version,attempt,expires) VALUES(?,?,?,?)",
        (project["id"], version, revision, time.time() + 600),
    )
    db.commit()
    try:
        return build_integration(root, db, project, tasks, revision, check)
    except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error):
        db.rollback()
        db.execute("DELETE FROM ma_integrations WHERE project=? AND version=? AND attempt=? AND commit_sha IS NULL", (project["id"], version, revision))
        record(db, project["id"], "orquestrador", None, f"Integração {revision} falhou. Consulte o diretório ou o log da tentativa.")
        db.commit()
        raise


def build_integration(root, db, project, tasks, revision, check):
    location = root / "integration" / project["id"] / revision
    location.parent.mkdir(parents=True, exist_ok=True)
    branch = "cbma/" + project["id"] + "/integration-" + revision
    git(project["repo"], "worktree", "add", "-b", branch, str(location), project["base"])
    try:
        for task in tasks:
            git(location, "merge", "--no-ff", "--no-edit", task["commit_sha"])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(f"Falha na integração em {location}: {error}") from error
    head = git(location, "rev-parse", "HEAD").strip()
    log_path = location.with_suffix(".log")
    with log_path.open("wb") as log:
        result = subprocess.run(check, cwd=location, stdout=log, stderr=subprocess.STDOUT, timeout=300)
    if result.returncode or git(location, "rev-parse", "HEAD").strip() != head or git(location, "diff", "HEAD", "--").strip():
        raise ValueError(f"Teste de integração falhou ou alterou o código. Log: {log_path}")
    db.execute("BEGIN IMMEDIATE")
    current = db.execute("SELECT version FROM projects WHERE id=?", (project["id"],)).fetchone()
    fresh = db.execute("SELECT * FROM tasks WHERE project=? ORDER BY id", (project["id"],)).fetchall()
    if current["version"] != project["version"] or [dict(t) for t in fresh] != [dict(t) for t in sorted(tasks, key=lambda item: item["id"])]:
        raise ValueError("O contrato ou as entregas mudaram durante o teste")
    changed = db.execute(
        "UPDATE ma_integrations SET commit_sha=? WHERE project=? AND version=? AND attempt=? AND expires>?",
        (head, project["id"], project["version"], revision, time.time()),
    ).rowcount
    if changed != 1:
        raise ValueError("A reserva da integração expirou")
    db.execute("UPDATE tasks SET state='integrated' WHERE project=? AND state='delivered'", (project["id"],))
    for task in tasks:
        record(db, project["id"], "orquestrador", task["id"], f"delivered → integrated · {head}")
    record(db, project["id"], "orquestrador", None, f"Integração validada: {head} em {location}. Log: {log_path}")
    return {"workspace": str(location), "branch": branch, "commit": head}


def wait_for_event(root, args):
    """Long-poll outside write transactions so other agents can deliver/send."""
    seconds = float(args.wait_seconds)
    if not 0 <= seconds <= 600:
        raise ValueError("--wait-seconds deve estar entre 0 e 600")
    end = time.monotonic() + seconds
    pending = []
    next_heartbeat = 0
    while True:
        # Refresh liveness without holding a transaction while waiting.
        if time.monotonic() >= next_heartbeat:
            with connect(root, write=True) as heartbeat_db:
                heartbeat_session = sessions.authenticate(
                    heartbeat_db, args.project, args.session)
                sessions.touch(heartbeat_db, heartbeat_session)
            next_heartbeat = time.monotonic() + 30
        with connect(root) as db:
            if db is None:
                raise ValueError("Projeto não encontrado")
            identity = sessions.authenticate(db, args.project, args.session)
            if args.action == "claim":
                if identity["actor"] != args.actor:
                    raise ValueError("A sessão não corresponde ao agente")
                project = require(db, "SELECT * FROM projects WHERE id=?", (args.project,), "Projeto")
                task = get_task(db, args.project, args.task)
                if task["state"] != "pending":
                    return None
                pending = [dep_id for dep_id in json.loads(task["dependencies"])
                           if (lambda d: d["state"] != "delivered" or
                               d["version"] != project["version"])(get_task(db, args.project, dep_id))]
                if not pending:
                    return None
            else:
                found = db.execute(
                    "SELECT 1 FROM ma_mail WHERE project=? AND recipient=? AND ack_id IS NULL AND deadline>=? LIMIT 1",
                    (args.project, identity["id"], time.time()),
                ).fetchone()
                if found:
                    return None
        remaining = end - time.monotonic()
        if remaining <= 0:
            if args.action == "claim":
                return {"state": "WAITING", "task": args.task,
                        "pending_dependencies": pending, "retriable": True,
                        "waited_seconds": seconds,
                        "hint": "Use claim --wait-seconds novamente; nao declare tarefa concluida"}
            return None
        time.sleep(min(0.5, remaining))


def _wait_next(root,args):
    seconds=float(args.wait_seconds)
    if not 0<=seconds<=600:
        raise ValueError("--wait-seconds deve estar entre 0 e 600")
    deadline=time.monotonic()+seconds
    last_refresh=0
    last=None
    while True:
        if time.monotonic()>=last_refresh:
            with connect(root,write=True) as db:
                identity=sessions.authenticate(db,args.project,args.session,args.actor)
                sessions.touch(db,identity)
            last_refresh=time.monotonic()+30
        with connect(root) as db:
            project=require(db,"SELECT * FROM projects WHERE id=?",(args.project,),"Projeto")
            status=orchestration.choose_next(db,args.project,args.actor,project["version"])
        if status["state"]!="WAITING":
            return None
        last=status
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return {"state":"WAITING","blocked":last["blocked"],
                    "waited_seconds":seconds,"retriable":True,
                    "hint":"Execute next novamente ou aguarde um evento de entrega."}
        time.sleep(min(.5,remaining))


def _wait_events(root,args):
    seconds=float(args.wait_seconds)
    if not 0<=seconds<=600:
        raise ValueError("--wait-seconds deve estar entre 0 e 600")
    deadline=time.monotonic()+seconds
    heartbeat=0
    while True:
        if time.monotonic()>=heartbeat:
            with connect(root,write=True) as db:
                identity=sessions.authenticate(db,args.project,args.session)
                sessions.touch(db,identity)
            heartbeat=time.monotonic()+30
        with connect(root) as db:
            identity=sessions.authenticate(db,args.project,args.session)
            found=orchestration.read_events(db,args.project,identity["actor"],args.after,args.limit)
        if found["events"]:
            return found
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return {"state":"WAITING","after":args.after,
                    "waited_seconds":seconds,"retriable":True}
        time.sleep(min(.5,remaining))


def commands(args):
    root, action = args.root.resolve(), args.action
    if action in ("claim", "inbox") and args.wait_seconds:
        result = wait_for_event(root, args)
        if result is not None:
            return result
    if action=="next" and args.wait_seconds:
        result=_wait_next(root,args)
        if result is not None:
            return result
    if action=="events" and args.wait_seconds:
        result=_wait_events(root,args)
        if result is not None:
            return result
    if action in ('resume','rag-search','rag-status','events'):
        with connect(root,write=True) as db:
            require(db,'SELECT id FROM projects WHERE id=?',(args.project,),'Projeto')
            identity=sessions.authenticate(db,args.project,args.session)
            sessions.touch(db,identity)
            if action=='resume':
                response=ma_memory.resume(db,args.project,identity['actor'],root,
                                          query=args.query,limit=args.limit)
                latest=db.execute("SELECT COALESCE(MAX(seq),0) FROM ma_events WHERE project=?",
                                  (args.project,)).fetchone()[0]
                response["recent_events"]=orchestration.read_events(
                    db,args.project,identity["actor"],max(0,latest-20),10)
                return response
            if action=='events':
                return orchestration.read_events(db,args.project,identity['actor'],
                                                 args.after,args.limit)
            if action=='rag-search':
                return {'project':args.project,'actor':identity['actor'],
                        'results':ma_memory.search(db,args.project,identity['actor'],
                                                   args.query,limit=args.limit),
                        'backend':'LOCAL_FTS5'}
            row=db.execute('SELECT * FROM ma_rag_projects WHERE project=?',
                           (args.project,)).fetchone()
            return {'project':args.project,'rag':dict(row) if row else None}
    if action == "status":
        response = snapshot(root, args.project)
        response.pop("mail", None)
        return response
    if action == "sync":
        with connect(root) as db:
            if db is None:
                raise ValueError("Projeto não encontrado")
            current = require(db, "SELECT * FROM projects WHERE id=?", (args.project,), "Projeto")
            identity = sessions.authenticate(db, args.project, args.session, args.actor)
            task = get_task(db, args.project, args.task)
            if task["agent"] != identity["actor"]:
                raise ValueError("A tarefa pertence a outro chat")
            return {"state": task["state"], "task_version": task["version"], "current_version": current["version"], "contract": current["contract"]}
    with connect(root, write=True) as db:
        if action == "init":
            repo = Path(git(args.repo, "rev-parse", "--show-toplevel").strip()).resolve()
            base = git(repo, "rev-parse", "HEAD").strip()
            db.execute("INSERT INTO projects(id,repo,base,contract) VALUES(?,?,?,?)", (args.project, str(repo), base, args.text))
            record(db, args.project, "orquestrador", None, "Projeto criado")
            ma_memory.initialize_project(db,args.project,repo,args.text,1)
            return {"project": args.project, "base": base}
        project = require(db, "SELECT * FROM projects WHERE id=?", (args.project,), "Projeto")
        coordinator = {"add", "contract", "integrate", "reopen", "plan"}
        identity = sessions.authenticate(
            db, args.project, args.session, "orquestrador" if action in coordinator else getattr(args, "actor", None)
        )
        sessions.touch(db, identity)
        if action in {"heartbeat", "inbox", "send", "ack", "retry"}:
            response=sessions.mailbox(db, args.project, identity, args)
            if action=='heartbeat':
                response['handoff']=ma_memory.resume(db,args.project,identity['actor'],root,limit=2)
                latest=db.execute("SELECT COALESCE(MAX(seq),0) FROM ma_events WHERE project=?",
                                  (args.project,)).fetchone()[0]
                response['recent_events']=orchestration.read_events(
                    db,args.project,identity['actor'],max(0,latest-10),5)
            return response
        if action == "plan":
            if args.file:
                repo=Path(project['repo']).resolve()
                source=(repo/args.file).resolve()
                try:
                    source.relative_to(repo)
                except ValueError as exc:
                    raise ValueError('Plano deve estar dentro do repositorio') from exc
                if not source.is_file() or source.stat().st_size>100_000:
                    raise ValueError('Plano inexistente ou muito grande')
                payload=source.read_text(encoding='utf-8')
            else:
                payload=args.json
            plan=orchestration.draft_plan(db,args.project,payload)
            if args.apply:
                count=orchestration.apply_plan(db,args.project,plan)
                for t in plan['tasks']:
                    ma_memory.knowledge(db,args.project,'shared','planned-task',
                        t['id']+': '+t['brief']+' | deps: '+','.join(t['depends']),
                        task=t['id'],version=project['version'],source='validated-plan')
                ma_memory.checkpoint(db,args.project,'orquestrador','','plan-applied',
                    str(count)+' tarefas planejadas e registradas',
                    'Vincular agentes e usar next para receber tarefas',
                    version=project['version'])
                record(db,args.project,'orquestrador',None,
                       str(count)+' tarefas adicionadas pelo planejamento automatico')
            return {'applied':bool(args.apply),'plan':plan}
        if action == "contract":
            db.execute("UPDATE projects SET contract=?,version=version+1 WHERE id=?", (args.text, args.project))
            record(db, args.project, "orquestrador", None, "Contrato atualizado para v" + str(project["version"] + 1))
            ma_memory.knowledge(db,args.project,'shared','contract',args.text,version=project['version']+1,source='contract-update')
            ma_memory.checkpoint(db,args.project,'orquestrador','','contract-updated',
                'Contrato atualizado para v'+str(project['version']+1),'Revalidar tarefas e dependencias',
                version=project['version']+1)
        elif action == "note":
            if args.task:
                get_task(db, args.project, args.task)
            record(db, args.project, identity["actor"], args.task, args.text)
            ma_memory.knowledge(db,args.project,
                'shared' if identity['actor']=='orquestrador' else identity['actor'],
                'note',args.text,task=args.task or '',version=project['version'],source='authenticated-note')
        elif action == "add":
            for dependency in args.depends:
                get_task(db, args.project, dependency)
            count = db.execute("SELECT count(*) FROM tasks WHERE project=?", (args.project,)).fetchone()[0]
            if count >= orchestration.MAX_TASKS:
                raise ValueError("Limite de 96 tarefas registradas por projeto")
            db.execute(
                "INSERT INTO tasks(project,id,scope,brief,dependencies) VALUES(?,?,?,?,?)",
                (args.project, args.task, json.dumps(args.path), args.text, json.dumps(args.depends)),
            )
            record(db, args.project, "orquestrador", args.task, "Tarefa criada")
            if args.auto:
                orchestration.register_auto(db,args.project,args.task,args.for_agent,
                                            args.priority,args.effort)
            elif args.for_agent:
                raise ValueError('--for-agent requer --auto')
            ma_memory.knowledge(db,args.project,'shared','task-created',
                args.task+': '+args.text+' | deps: '+','.join(args.depends),
                task=args.task,version=project['version'],source='task-registration')
        elif action == "event":
            record_info=orchestration.publish(
                db,args.project,identity['actor'],args.task or '',args.kind,
                args.to,args.text,project['version'])
            record(db,args.project,identity['actor'],args.task,
                   'Evento '+args.kind+' #'+str(record_info['event_id']))
            return record_info
        elif action == "checkpoint":
            if args.task:
                owned=get_task(db,args.project,args.task)
                if identity['actor']!='orquestrador' and owned['agent']!=identity['actor']:
                    raise ValueError('Checkpoint de outra tarefa nao autorizado')
            ma_memory.checkpoint(db,args.project,identity['actor'],args.task or '',
                'manual',args.text,args.next_step,completed=args.done,
                pending=args.pending,issues=args.issue,files=args.file,
                version=project['version'])
            ma_memory.knowledge(db,args.project,identity['actor'],'checkpoint',
                args.text+' | proximo: '+args.next_step,task=args.task or '',
                version=project['version'],source='authenticated-checkpoint')
            record(db,args.project,identity['actor'],args.task,'Checkpoint persistido')
            return {'ok':True,'checkpoint':'SAVED'}
        elif action == "rag-sync":
            if args.task:
                t=get_task(db,args.project,args.task)
                if identity['actor']!='orquestrador' and t['agent']!=identity['actor']:
                    raise ValueError('Indexacao do worktree de outro agente nao autorizada')
            ma_memory.queue_index(db,args.project)
            return {'ok':True,'rag_sync':'QUEUED'}
        elif action == "integrate":
            return integrate(root, db, project, args.check[1:] if args.check[:1] == ["--"] else args.check)
        elif action == "next":
            if identity['actor']!=args.actor:
                raise ValueError('Sessao nao corresponde ao agente')
            return dispatch_once(root,db,project,args.actor)
        else:
            task = get_task(db, args.project, args.task)
            if action == "claim":
                workspace = claim(root, db, project, task, args.actor)
                ma_memory.checkpoint(db,args.project,args.actor,args.task,'active',
                    'Tarefa assumida: '+task['brief'],
                    'Executar escopo e validar testes antes de commit',
                    pending=json.loads(task['dependencies']), files=json.loads(task['scope']),
                    version=project['version'],
                    git_head=git(workspace,'rev-parse','HEAD').strip())
                return {"workspace": str(workspace), "task": dict(get_task(db, args.project, args.task)), "contract": project["contract"], "version": project["version"]}
            if action == "reopen":
                if task["state"] != "delivered":
                    raise ValueError("Somente uma tarefa entregue pode ser reaberta")
                db.execute("UPDATE tasks SET state='active',commit_sha=NULL,evidence=NULL,files=NULL WHERE project=? AND id=?", (args.project, args.task))
                record(db, args.project, "orquestrador", args.task, "delivered → active: " + args.text)
                return {"ok": True}
            if action == "deliver" and task["agent"] == args.actor and task["state"] in ("delivered", "integrated") and task["commit_sha"] == args.commit:
                return {"ok": True, "duplicate": True}
            if task["agent"] != args.actor or task["state"] != "active":
                raise ValueError("A tarefa pertence a outro chat ou aguarda atribuição")
            if action == "accept-contract":
                for dep_id in json.loads(task["dependencies"]):
                    dependency = get_task(db, args.project, dep_id)
                    if dependency["state"] != "delivered" or dependency["version"] != project["version"]:
                        raise ValueError("Aguardando contrato atualizado de " + dep_id)
                    git(current_workspace(root, args.project, args.task), "merge-base", "--is-ancestor", dependency["commit_sha"], "HEAD")
                db.execute(
                    "UPDATE tasks SET version=? WHERE project=? AND id=?",
                    (project["version"], args.project, args.task),
                )
                record(db, args.project, args.actor, args.task, "Contrato revisado")
            elif action == "deliver":
                files = verify_delivery(root, project, task, project["version"], args.commit)
                db.execute(
                    "UPDATE tasks SET state='delivered',commit_sha=?,evidence=?,files=? WHERE project=? AND id=?",
                    (args.commit, args.text, json.dumps(files), args.project, args.task),
                )
                record(db, args.project, args.actor, args.task, "active → delivered: " + args.commit)
                ma_memory.checkpoint(db,args.project,args.actor,args.task,'delivered',
                    'Entrega validada por Git: '+args.commit+' | '+args.text,
                    'Tarefa entregue; verificar inbox ou aguardar nova atribuicao',
                    completed=files,files=files,version=project['version'],git_head=args.commit)
                ma_memory.knowledge(db,args.project,'shared','delivery',
                    'Tarefa '+args.task+' entregue; commit '+args.commit+'; arquivos: '+', '.join(files),
                    task=args.task,version=project['version'],source='validated-git-delivery')
                ma_memory.queue_index(db,args.project)
        return {"ok": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--session", default=os.environ.get("CB_MULTIAGENTS_SESSION"))
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "init", "add", "claim", "note", "contract", "sync", "accept-contract", "deliver", "integrate", "reopen", "heartbeat", "inbox", "send", "ack", "retry", "checkpoint", "resume", "rag-search", "rag-status", "rag-sync", "plan", "next", "event", "events"):
        command = sub.add_parser(action)
        command.add_argument("project", type=key, nargs="?" if action == "status" else None)
        if action in ("add", "claim", "sync", "accept-contract", "deliver", "reopen"):
            command.add_argument("task", type=key)
        if action in ('checkpoint','rag-sync'):
            command.add_argument('--task',type=key,default=None)
        if action in ("claim", "sync", "accept-contract", "deliver", "next"):
            command.add_argument("actor", choices=("agente-1", "agente-2", "agente-3"))
        if action in ("init", "add", "note", "contract", "deliver", "reopen", "send", "checkpoint", "event"):
            command.add_argument("--text", required=True)
        if action=='plan':
            sources=command.add_mutually_exclusive_group(required=True)
            sources.add_argument('--json')
            sources.add_argument('--file')
            command.add_argument('--apply',action='store_true')
        if action=='event':
            command.add_argument('--task',type=key)
            command.add_argument('--kind',choices=orchestration.KINDS,required=True)
            command.add_argument('--to',choices=('shared',*sessions.ROLES),default='shared')
        if action=='events':
            command.add_argument('--after',type=int,default=0)
            command.add_argument('--limit',type=int,default=20)
        if action=='checkpoint':
            command.add_argument('--next-step',default='')
            for param in ('done','pending','issue','file'):
                command.add_argument('--'+param,action='append',default=[])
        if action in ('resume','rag-search'):
            command.add_argument('--query',default='' if action=='resume' else None,
                                 required=action=='rag-search')
            command.add_argument('--limit',type=int,default=6)
        if action == "init":
            command.add_argument("repo", type=Path)
        if action in ("claim", "inbox", "next", "events"):
            # Only before starting work: claim and next default to dependency wait.
            command.add_argument("--wait-seconds",type=float,
                                 default=600.0 if action in ("claim","next") else 0.0)
        if action == "send":
            command.add_argument("--queue-offline", action="store_true")
        if action == "add":
            command.add_argument("--path", action="append", required=True)
            command.add_argument("--depends", action="append", type=key, default=[])
            command.add_argument("--auto", action="store_true")
            command.add_argument("--for-agent",choices=orchestration.ACTORS)
            command.add_argument("--priority",type=int,default=50)
            command.add_argument("--effort",type=int,default=1)
        if action == "deliver":
            command.add_argument("commit")
        if action == "integrate":
            command.add_argument("check", nargs=argparse.REMAINDER)
        if action in ("send", "ack", "retry"):
            command.add_argument("message", type=key)
        if action == "send":
            command.add_argument("to", choices=sessions.ROLES)
        if action == "note":
            command.add_argument("--task", type=key)
            command.add_argument("--actor", choices=("orquestrador", "agente-1", "agente-2", "agente-3"), )
    args=parser.parse_args()
    result=commands(args)  # commits task and memory state before spawning workers
    if args.action in ('init','deliver','rag-sync'):
        try:
            source=None
            namespace='base'
            paths=None
            if args.action=='deliver':
                namespace=args.task
                source=args.root.resolve()/'worktrees'/args.project/args.task
                with connect(args.root.resolve()) as db:
                    row=get_task(db,args.project,args.task)
                    paths=json.loads(row['files']) if row['files'] else None
            elif args.action=='rag-sync' and args.task:
                namespace=args.task
                source=args.root.resolve()/'worktrees'/args.project/args.task
            result['rag_index']=ma_memory.spawn_index(args.root.resolve(),args.project,
                                        namespace=namespace,source_root=source,paths=paths)
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            result['rag_index']={'state':'FAILED','error_type':type(exc).__name__}
    # Once the delivery is durably committed, attempt to assign the next ready
    # auto-planned task. Never wait on other agents after delivery.
    if args.action=="deliver":
        try:
            with connect(args.root.resolve(),write=True) as db:
                sessions.authenticate(db,args.project,args.session,args.actor)
                current=require(db,"SELECT * FROM projects WHERE id=?",
                                (args.project,),"Projeto")
                result['next_task']=dispatch_once(args.root.resolve(),db,current,args.actor)
        except (OSError,ValueError,subprocess.SubprocessError,sqlite3.Error) as exc:
            result['next_task']={'state':'ERROR','error_type':type(exc).__name__}
    print(json.dumps(result,ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(1)
