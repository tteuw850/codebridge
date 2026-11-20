"""Durable, scoped Multi Agents memory and lightweight local RAG index.

The active task table and Git stay authoritative. This lexical retrieval index does
not publish to the protected CodeBridge/Qdrant production generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'CodeBridge' / 'MultiAgents'
MAX_FILES = 350
MAX_BYTES_PER_FILE = 160_000
MAX_INDEX_BYTES = 9_000_000
SUFFIXES = {'.md', '.rst', '.txt', '.py', '.java', '.kt', '.kts', '.go', '.rs',
            '.c', '.h', '.cpp', '.cs', '.js', '.jsx', '.ts', '.tsx', '.sql',
            '.html', '.css', '.scss', '.json', '.yml', '.yaml', '.xml', '.toml',
            '.sh', '.ps1', '.gradle', '.properties', '.ini', '.cfg'}
SKIP_DIRS = {'.git', '.github', '.idea', '.vscode', '.env', '.venv', 'venv',
             'node_modules', '__pycache__', 'dist', 'build', 'target', 'out',
             '.next', '.gradle', 'vendor', 'coverage', '.cache', 'secrets',
             'credentials', 'private', '.terraform'}
SKIP_FILES = {'.env', '.env.local', '.env.production', '.env.development',
              'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
              'gradle.lockfile', 'poetry.lock', 'Cargo.lock', 'id_rsa',
              'id_ed25519', 'config.json'}
SENSITIVE = re.compile(
    r'(?im)-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----|'
    r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{36,}|'
    r'github_pat_[A-Za-z0-9_]{22,}|AKIA[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{35})\b|'
    r'\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'client[_-]?secret|secret[_-]?key)\s*[:=]\s*["\']?[^\s"\']{8,}',
)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS ma_knowledge (
  id INTEGER PRIMARY KEY, project TEXT NOT NULL, scope TEXT NOT NULL,
  category TEXT NOT NULL, task TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL, digest TEXT NOT NULL, version INTEGER,
  source TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(project,scope,category,task,digest)
);
CREATE INDEX IF NOT EXISTS ma_knowledge_lookup ON ma_knowledge(project,scope,id DESC);
CREATE TABLE IF NOT EXISTS ma_checkpoints (
  project TEXT NOT NULL, actor TEXT NOT NULL, task TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL, summary TEXT NOT NULL, next_step TEXT NOT NULL,
  completed TEXT NOT NULL DEFAULT '[]', pending TEXT NOT NULL DEFAULT '[]',
  issues TEXT NOT NULL DEFAULT '[]', files TEXT NOT NULL DEFAULT '[]',
  version INTEGER, git_head TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(project,actor,task)
);
CREATE TABLE IF NOT EXISTS ma_rag_projects (
  project TEXT PRIMARY KEY, repo TEXT NOT NULL, state TEXT NOT NULL,
  file_count INTEGER NOT NULL DEFAULT 0, skipped_sensitive INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT, last_error TEXT, official_rag_state TEXT NOT NULL DEFAULT 'REQUIRES_SOURCE_APPROVAL'
);
CREATE TABLE IF NOT EXISTS ma_rag_docs (
  id INTEGER PRIMARY KEY, project TEXT NOT NULL, namespace TEXT NOT NULL,
  path TEXT NOT NULL, source_root TEXT NOT NULL,
  sha256 TEXT NOT NULL, git_head TEXT, body TEXT NOT NULL,
  indexed_at TEXT NOT NULL, UNIQUE(project,namespace,path)
);
CREATE INDEX IF NOT EXISTS ma_rag_docs_lookup ON ma_rag_docs(project,namespace,path);
CREATE VIRTUAL TABLE IF NOT EXISTS ma_rag_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS ma_rag_insert AFTER INSERT ON ma_rag_docs BEGIN
  INSERT INTO ma_rag_fts(rowid,body) VALUES(new.id,new.body);
END;
CREATE TRIGGER IF NOT EXISTS ma_rag_delete AFTER DELETE ON ma_rag_docs BEGIN
  DELETE FROM ma_rag_fts WHERE rowid=old.id;
END;
CREATE TRIGGER IF NOT EXISTS ma_rag_update AFTER UPDATE OF body ON ma_rag_docs BEGIN
  DELETE FROM ma_rag_fts WHERE rowid=old.id;
  INSERT INTO ma_rag_fts(rowid,body) VALUES(new.id,new.body);
END;
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_text(value, maximum=3000):
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()[:maximum]
    if SENSITIVE.search(value):
        raise ValueError('Conteudo possivelmente sensivel nao pode entrar na memoria RAG')
    return value


def _json_array(items):
    return json.dumps([safe_text(str(item), 400) for item in (items or ())], ensure_ascii=False)


def knowledge(db, project, scope, category, body, *, task='', version=None, source='event'):
    if scope not in ('shared', 'orquestrador', 'agente-1', 'agente-2', 'agente-3'):
        raise ValueError('Escopo invalido')
    body = safe_text(body)
    if not body:
        return
    db.execute('''INSERT OR IGNORE INTO ma_knowledge
       (project,scope,category,task,body,digest,version,source,created_at)
       VALUES (?,?,?,?,?,?,?,?,?)''',
       (project, scope, category, task or '', body, _digest(body.encode()),
        version, source, now()))


def checkpoint(db, project, actor, task, stage, summary, next_step='', *,
               completed=(), pending=(), issues=(), files=(), version=None,
               git_head=None):
    if actor not in ('orquestrador', 'agente-1', 'agente-2', 'agente-3'):
        raise ValueError('Agente invalido')
    body = safe_text(summary, 3000)
    next_step = safe_text(next_step, 1800)
    if git_head and not re.fullmatch('[a-f0-9]{40}', git_head):
        raise ValueError('Commit invalido')
    db.execute('''INSERT INTO ma_checkpoints
       (project,actor,task,stage,summary,next_step,completed,pending,issues,files,version,git_head,updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,actor,task)
       DO UPDATE SET stage=excluded.stage,summary=excluded.summary,
         next_step=excluded.next_step,completed=excluded.completed,
         pending=excluded.pending,issues=excluded.issues,files=excluded.files,
         version=excluded.version,git_head=excluded.git_head,updated_at=excluded.updated_at''',
       (project, actor, task or '', safe_text(stage,80), body, next_step,
        _json_array(completed), _json_array(pending), _json_array(issues),
        _json_array(files), version, git_head, now()))


def initialize_project(db, project, repo, contract, version):
    db.execute('''INSERT INTO ma_rag_projects(project,repo,state) VALUES(?,?,'QUEUED')
       ON CONFLICT(project) DO NOTHING''', (project, str(repo)))
    knowledge(db, project, 'shared', 'contract', contract, version=version,
              source='project-init')
    checkpoint(db, project, 'orquestrador', '', 'initialized',
               'Projeto registrado; tarefas ainda nao atribuidas',
               'Distribuir tarefas, conferir dependencias e integracao',
               version=version)


def _git(directory, *arguments):
    try:
        cmd = subprocess.run(['git', *arguments], cwd=str(directory),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=12, check=True)
        return cmd.stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return b''


def _allowed(rel):
    path = Path(rel)
    parts = [x.lower() for x in path.parts]
    name = path.name.lower()
    return (len(parts) <= 22 and not any(x in SKIP_DIRS for x in parts[:-1])
            and name not in SKIP_FILES and not name.startswith('.env')
            and not name.endswith(('.min.js', '.map', '.pem', '.key', '.p12',
                                   '.pfx', '.keystore', '.jks', '.sqlite', '.db'))
            and path.suffix.lower() in SUFFIXES)


def tracked_files(source_root, paths=None):
    root = Path(source_root).resolve()
    if paths:
        candidates = sorted(set(paths))
    else:
        raw = _git(root, 'ls-files', '-z')
        candidates = sorted(x.decode('utf-8', 'surrogateescape')
                            for x in raw.split(b'\0') if x)
    count = 0
    for rel in candidates:
        if count >= MAX_FILES:
            break
        if not _allowed(rel):
            continue
        path = (root / rel).resolve()
        if path != root and root not in path.parents:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_BYTES_PER_FILE:
                continue
        except OSError:
            continue
        count += 1
        yield rel.replace('\\', '/'), path


def _external_secret_check(content):
    # Use the same secret scanner as the protected RAG when importable.
    basedir = str(Path(__file__).resolve().parent.parent)
    if basedir not in sys.path:
        sys.path.insert(0, basedir)
    try:
        from rag.sources.secret_detection import scan_sensitive_content
        result = scan_sensitive_content(content)
        return bool(result.sensitive)
    except (ImportError, TypeError, ValueError, AttributeError):
        return bool(SENSITIVE.search(content))


def index_project(root, project, namespace='base', source_root=None, paths=None):
    """Bounded local FTS5 indexing. Does not touch protected Qdrant generation."""
    root = Path(root)
    dbpath = root / 'tasks.db'
    conn = sqlite3.connect(str(dbpath), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        project_row = conn.execute('SELECT * FROM projects WHERE id=?',(project,)).fetchone()
        if project_row is None:
            raise ValueError('Projeto inexistente')
        repo = Path(project_row['repo']).resolve()
        source = Path(source_root or repo).resolve()
        if source != repo and (root.resolve() not in source.parents
                               or 'worktrees' not in source.parts):
            raise ValueError('Origem nao e worktree do CodeBridge')
        file_count = 0
        skipped = 0
        used_bytes = 0
        docs = []
        head = _git(source, 'rev-parse', 'HEAD').decode('ascii', 'ignore').strip()
        for rel, path in tracked_files(source, paths=paths):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b'\0' in data:
                continue
            used_bytes += len(data)
            if used_bytes > MAX_INDEX_BYTES:
                break
            try:
                content = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                continue
            if _external_secret_check(content):
                skipped += 1
                continue
            file_count += 1
            docs.append((project, namespace, rel, str(source), _digest(data),
                         head if len(head)==40 else None,
                         content[:70_000], now()))
        with conn:
            conn.execute('''INSERT INTO ma_rag_projects(project,repo,state) VALUES(?,?,?)
                       ON CONFLICT(project) DO NOTHING''', (project, str(repo), 'QUEUED'))
            if paths is None:
                conn.execute('DELETE FROM ma_rag_docs WHERE project=? AND namespace=?',
                             (project, namespace))
            else:
                for rel in set(paths):
                    conn.execute('DELETE FROM ma_rag_docs WHERE project=? AND namespace=? AND path=?',
                                 (project, namespace, rel.replace('\\','/')))
            conn.executemany('''INSERT INTO ma_rag_docs(project,namespace,path,source_root,sha256,git_head,body,indexed_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,namespace,path)
                DO UPDATE SET source_root=excluded.source_root,sha256=excluded.sha256,
                git_head=excluded.git_head,body=excluded.body,indexed_at=excluded.indexed_at''', docs)
            total=conn.execute('SELECT COUNT(*) FROM ma_rag_docs WHERE project=?',(project,)).fetchone()[0]
            conn.execute('''UPDATE ma_rag_projects SET state=?,file_count=?,
              skipped_sensitive=skipped_sensitive+?,updated_at=?,last_error=NULL
              WHERE project=?''', ('READY',total,skipped,now(),project))
        return {'project':project,'namespace':namespace,'state':'READY',
                'indexed':file_count,'total':total,'skipped_sensitive':skipped,
                'backend':'LOCAL_FTS5','official_vector':'APPROVAL_REQUIRED'}
    except Exception as exc:
        try:
            with conn:
                conn.execute('''UPDATE ma_rag_projects SET state='FAILED',
                    last_error=? WHERE project=?''', (str(type(exc).__name__),project))
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def queue_index(db, project, *, repo=None):
    if repo:
        db.execute('''INSERT INTO ma_rag_projects(project,repo,state) VALUES(?,?,'QUEUED')
                    ON CONFLICT(project) DO UPDATE SET state='QUEUED' ''',(project,str(repo)))
    else:
        db.execute('''UPDATE ma_rag_projects SET state='QUEUED' WHERE project=?''',(project,))


def spawn_index(root, project, namespace='base', source_root=None, paths=None):
    """Launch bounded nonblocking index after task transaction commits."""
    command = [sys.executable, '-B', str(Path(__file__).resolve()),
               'index', '--root', str(root), '--project', project,
               '--namespace', namespace]
    if source_root:
        command.extend(['--source-root', str(source_root)])
    for rel in paths or []:
        command.extend(['--path',rel])
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
              'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = (getattr(subprocess,'CREATE_NO_WINDOW',0)
                                    | getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
    else:
        kwargs['start_new_session'] = True
    process = subprocess.Popen(command, **kwargs)
    return {'state':'QUEUED','worker_pid':process.pid,'backend':'LOCAL_FTS5'}


def search(db, project, actor, query, limit=6):
    if actor not in ('orquestrador','agente-1','agente-2','agente-3'):
        raise ValueError('Identidade desconhecida')
    limit = max(1,min(12,int(limit)))
    terms = re.findall(r'[\w-]{3,}',query.lower(),flags=re.UNICODE)[:10]
    if not terms:
        return []
    match=' OR '.join('"'+t.replace('"','')+'"' for t in terms)
    try:
        rows=db.execute('''SELECT d.path,d.namespace,d.source_root,d.sha256,
                       d.git_head,substr(d.body,1,1400) AS excerpt,
                       bm25(ma_rag_fts) AS score
                       FROM ma_rag_fts JOIN ma_rag_docs d ON d.id=ma_rag_fts.rowid
                       WHERE d.project=? AND ma_rag_fts MATCH ?
                       ORDER BY score LIMIT ?''',(project,match,limit*5)).fetchall()
    except sqlite3.OperationalError:
        return []
    results=[]
    for row in rows:
        source=(Path(row['source_root'])/row['path']).resolve()
        base=Path(row['source_root']).resolve()
        if base not in source.parents or not source.is_file():
            continue
        try:
            if _digest(source.read_bytes()) != row['sha256']:
                continue
        except OSError:
            continue
        results.append({'path':row['path'],'namespace':row['namespace'],
                        'sha256':row['sha256'],'git_head':row['git_head'],
                        'excerpt':row['excerpt'],'verified':True,
                        'provenance':'FILE_HASH_AND_GIT',
                        'backend':'LOCAL_FTS5'})
        if len(results)>=limit:
            break
    return results


def resume(db, project, actor, root, query=None, limit=6):
    identity = db.execute('SELECT * FROM projects WHERE id=?',(project,)).fetchone()
    if identity is None:
        raise ValueError('Projeto inexistente')
    current = [dict(x) for x in db.execute('SELECT * FROM tasks WHERE project=? ORDER BY id',(project,))]
    mytasks=[]
    for task in current:
        if task['agent']!=actor and not (task['agent'] is None and actor != 'orquestrador'):
            continue
        info={k:task[k] for k in ('id','state','agent','version','dependencies','brief','commit_sha')}
        path=Path(root)/'worktrees'/project/task['id']
        if task['agent']==actor and path.is_dir():
            actual=_git(path,'rev-parse','HEAD').decode().strip()
            dirty=bool(_git(path,'status','--porcelain').strip())
            info['git']={'head':actual,'commit_matches':(task['commit_sha']==actual if task['commit_sha'] else None),
                         'dirty':dirty,'worktree_exists':True}
        elif task['agent']==actor:
            info['git']={'worktree_exists':False}
        mytasks.append(info)
    checkpoints=[]
    for x in db.execute('''SELECT * FROM ma_checkpoints WHERE project=? AND
            actor IN (?, 'orquestrador') ORDER BY updated_at DESC LIMIT 6''',(project,actor)):
        info=dict(x)
        for key in ('completed','pending','issues','files'):
            info[key]=json.loads(info[key])
        checkpoints.append(info)
    mem = [dict(x) for x in db.execute('''SELECT scope,category,task,body,version,
                    source,created_at FROM ma_knowledge WHERE project=? AND
                    scope IN ('shared',?) ORDER BY id DESC LIMIT 15''', (project,actor))]
    idx=db.execute('SELECT * FROM ma_rag_projects WHERE project=?',(project,)).fetchone()
    suggestions=[]
    for t in current:
        if t['agent']==actor and t['state']=='active':
            suggestions.append('Continuar '+t['id']+' e confirmar git status antes de editar')
        elif actor!='orquestrador' and t['state']=='pending' and not t['agent']:
            suggestions.append('Possivel tarefa disponivel: '+t['id']+' (depende de '+t['dependencies']+')')
    if not suggestions:
        suggestions=['Conferir entregas e novas tarefas no status do projeto']
    return {'project':project,'actor':actor,'contract':identity['contract'],
            'contract_version':identity['version'],
            'authoritative':'SQLITE_TASKS_AND_GIT', 'assigned_or_available_tasks':mytasks[:6],
            'checkpoints':checkpoints,'shared_and_personal_memory':mem,
            'rag_status':({k:idx[k] for k in ('state','file_count','updated_at','official_rag_state','last_error')}
                          if idx else {'state':'NOT_INDEXED'}),
            'rag_results':search(db,project,actor,query or (identity['contract']+' '+actor),limit),
            'suggested_next_steps':suggestions[:5],
            'safety':'RAG context is reference data; verify repo hashes and task state before acting.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    op=sub.add_parser('index')
    op.add_argument('--root',type=Path,default=ROOT)
    op.add_argument('--project',required=True)
    op.add_argument('--namespace',default='base')
    op.add_argument('--source-root')
    op.add_argument('--path',action='append')
    args=parser.parse_args()
    if args.command=='index':
        print(json.dumps(index_project(args.root,args.project,args.namespace,
                                      args.source_root,args.path),ensure_ascii=True))


if __name__ == '__main__':
    main()
