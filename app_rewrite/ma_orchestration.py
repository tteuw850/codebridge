"""CodeBridge Multi Agents: deterministic plan, event journal and auto-dispatch.
All authoritative state remains in tasks.db and Git. Does not start chat turns.
"""
import fnmatch
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

ACTORS=('agente-1','agente-2','agente-3')
ROLES=('orquestrador',)+ACTORS
KINDS=('progress','interface','decision','blocker','result')
MAX_TASKS=96
SCHEMA="""
CREATE TABLE IF NOT EXISTS ma_auto_tasks(
 project TEXT NOT NULL,task TEXT NOT NULL,preferred_actor TEXT,
 priority INTEGER NOT NULL DEFAULT 50,effort INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,PRIMARY KEY(project,task));
CREATE INDEX IF NOT EXISTS ma_auto_tasks_role ON ma_auto_tasks(project,preferred_actor);
CREATE TABLE IF NOT EXISTS ma_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,
 actor TEXT NOT NULL,task TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL,to_actor TEXT NOT NULL DEFAULT 'shared',
 body TEXT NOT NULL,version INTEGER NOT NULL,commit_sha TEXT,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ma_events_project ON ma_events(project,seq);
"""
ID=re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
WILDCARD=re.compile(r'[*?[]')
def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
def _valid_id(value):
    if not isinstance(value,str) or not ID.fullmatch(value):
        raise ValueError('ID invalido no plano')
    return value
def _path(item):
    if not isinstance(item,str) or not item.strip():
        raise ValueError('Escopo vazio')
    value=item.strip().replace('\\','/')
    p=PurePosixPath(value)
    if (p.is_absolute() or '..' in p.parts or value.startswith('/') or
        ':' in value or '\x00' in value or value.startswith('./') or value=='.'):
        raise ValueError('Escopo inseguro: '+value[:80])
    if len(value)>240:
        raise ValueError('Escopo excessivamente longo')
    return value
def _clash(a,b):
    if a==b or fnmatch.fnmatchcase(a,b) or fnmatch.fnmatchcase(b,a):
        return True
    # Conservative overlap heuristic for globs: static prefixes with common path.
    ax=WILDCARD.search(a);bx=WILDCARD.search(b)
    if ax and bx:
        pa=a[:ax.start()].rsplit('/',1)[0] if '/' in a[:ax.start()] else ''
        pb=b[:bx.start()].rsplit('/',1)[0] if '/' in b[:bx.start()] else ''
        return pa==pb or not pa or not pb or pa.startswith(pb+'/') or pb.startswith(pa+'/')
    return False
def _fields(item):
    if not isinstance(item,dict):
        raise ValueError('Tarefa deve ser objeto JSON')
    tid=_valid_id(item.get('id'))
    from ma_memory import safe_text
    brief=safe_text(item.get('brief',''),1000)
    if not brief:
        raise ValueError('Tarefa sem brief: '+tid)
    paths=item.get('scope')
    if not isinstance(paths,list) or not paths or len(paths)>20:
        raise ValueError('scope deve ter 1 a 20 caminhos: '+tid)
    scope=list(dict.fromkeys(_path(p) for p in paths))
    deps=item.get('depends',[])
    if not isinstance(deps,list) or len(deps)>30:
        raise ValueError('depends deve ser lista: '+tid)
    deps=list(dict.fromkeys(_valid_id(d) for d in deps))
    if tid in deps:
        raise ValueError('Autodependencia: '+tid)
    role=item.get('actor')
    if role not in (None,*ACTORS):
        raise ValueError('Agente preferido invalido: '+tid)
    pri=item.get('priority',50);eff=item.get('effort',1)
    if isinstance(pri,bool) or not isinstance(pri,int) or not 0<=pri<=100:
        raise ValueError('priority deve ser inteiro 0..100')
    if isinstance(eff,bool) or not isinstance(eff,int) or not 1<=eff<=100:
        raise ValueError('effort deve ser inteiro 1..100 (unidades relativas)')
    return dict(id=tid,brief=brief,scope=scope,depends=deps,actor=role,priority=pri,effort=eff)
def draft_plan(db, project, raw):
    try: payload=json.loads(raw)
    except (TypeError,ValueError) as e: raise ValueError('JSON de planejamento invalido') from e
    values=payload['tasks'] if isinstance(payload,dict) and isinstance(payload.get('tasks'),list) else payload
    if not isinstance(values,list) or not 1<=len(values)<=MAX_TASKS:
        raise ValueError('Informe 1 a 96 tarefas no plano')
    proposed=[_fields(x) for x in values]
    byid={x['id']:x for x in proposed}
    if len(byid)!=len(proposed):
        raise ValueError('IDs de tarefas repetidos')
    existing=[dict(x) for x in db.execute('SELECT id,scope,dependencies,state FROM tasks WHERE project=?',(project,))]
    if len(existing)+len(proposed)>MAX_TASKS:
        raise ValueError('Limite de 96 tarefas por projeto')
    known={e['id'] for e in existing}
    if known.intersection(byid):
        raise ValueError('ID ja utilizado: '+', '.join(sorted(known.intersection(byid))))
    for t in proposed:
        missing=set(t['depends'])-known-set(byid)
        if missing: raise ValueError('Dependencia inexistente: '+', '.join(sorted(missing)))
    # Verify graph is acyclic; existing tasks are immutable here.
    graph={e['id']:json.loads(e['dependencies']) for e in existing}
    graph.update({x['id']:x['depends'] for x in proposed})
    visited=set();visiting=set()
    def cycle(tid):
        if tid in visiting: raise ValueError('Ciclo de dependencias: '+tid)
        if tid in visited:return
        visiting.add(tid)
        for dep in graph[tid]: cycle(dep)
        visiting.remove(tid);visited.add(tid)
    for tid in graph: cycle(tid)
    scoped=[(x['id'],p) for x in proposed for p in x['scope']]
    scoped += [(x['id'],p) for x in existing for p in json.loads(x['scope'])]
    for i,(a,pa) in enumerate(scoped):
        for b,pb in scoped[i+1:]:
            if a!=b and _clash(pa,pb):
                raise ValueError('Escopos potencialmente sobrepostos: '+a+' '+pa+' / '+b+' '+pb)
    children={t['id']:[] for t in proposed}
    for t in proposed:
        for d in t['depends']:
            if d in children:children[d].append(t['id'])
    scores={}
    def tail(tid):
        if tid not in scores:
            scores[tid]=byid[tid]['effort']+max((tail(k) for k in children[tid]),default=0)
        return scores[tid]
    for tid in children:tail(tid)
    # Simulate release over three logical worker lanes. Units are not wall-clock time.
    finished={e['id']:0 for e in existing if e['state'] in ('delivered','integrated')}
    available={e['id']:0 for e in existing if e['state'] not in ('delivered','integrated')}
    slots={actor:0 for actor in ACTORS};stages=[];pending=set(byid)
    while pending:
        eligible=[byid[k] for k in pending if all(d in finished or d in available for d in byid[k]['depends'])]
        if not eligible:raise ValueError('Plano tem dependencias impossiveis')
        eligible.sort(key=lambda t:(-scores[t['id']],-t['priority'],t['id']))
        for t in eligible:
            deps=max((finished.get(d,available.get(d,0)) for d in t['depends']),default=0)
            role=t['actor'] or min(ACTORS,key=lambda x:(max(slots[x],deps),slots[x],x))
            start=max(slots[role],deps)
            end=start+t['effort']; slots[role]=end
            finished[t['id']]=end
            stages.append(dict(id=t['id'],recommended_actor=role,start_units=start,
                               end_units=end,critical_tail=scores[t['id']]))
            pending.remove(t['id'])
    total=sum(t['effort'] for t in proposed);makespan=max(slots.values(),default=0)
    return {'project':project,'task_count':len(proposed),'tasks':proposed,
            'schedule':sorted(stages,key=lambda t:(t['start_units'],t['recommended_actor'],t['id'])),
            'total_work_units':total,'estimated_parallel_units':makespan,
            'relative_parallelism':round(total/makespan,2) if makespan else 0,
            'metric_note':'Unidades relativas de esforco, nao minutos reais.'}
def apply_plan(db, project, plan):
    for t in plan['tasks']:
        db.execute('INSERT INTO tasks(project,id,scope,brief,dependencies) VALUES(?,?,?,?,?)',
                   (project,t['id'],json.dumps(t['scope']),t['brief'],json.dumps(t['depends'])))
        register_auto(db,project,t['id'],t['actor'],t['priority'],t['effort'])
    return len(plan['tasks'])
def register_auto(db,project,task,actor=None,priority=50,effort=1):
    if actor not in (None,*ACTORS):raise ValueError('Agente invalido')
    if isinstance(priority,bool) or not isinstance(priority,int) or not 0<=priority<=100:
        raise ValueError('priority deve ser inteiro de 0 a 100')
    if isinstance(effort,bool) or not isinstance(effort,int) or not 1<=effort<=100:
        raise ValueError('effort deve ser inteiro de 1 a 100')
    db.execute('''INSERT INTO ma_auto_tasks(project,task,preferred_actor,priority,effort,created_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(project,task) DO UPDATE SET
          preferred_actor=excluded.preferred_actor,priority=excluded.priority,effort=excluded.effort''',
          (project,task,actor,priority,effort,stamp()))
def check_claim(db,project,task,actor):
    row=db.execute('SELECT preferred_actor FROM ma_auto_tasks WHERE project=? AND task=?',
                   (project,task)).fetchone()
    if row and row['preferred_actor'] and row['preferred_actor']!=actor:
        raise ValueError('Tarefa reservada para '+row['preferred_actor'])
def choose_next(db,project,actor,version):
    if actor not in ACTORS:raise ValueError('Somente agentes executam next')
    active=db.execute("SELECT id FROM tasks WHERE project=? AND agent=? AND state='active'",
                      (project,actor)).fetchone()
    if active:return {'state':'BUSY','active_task':active['id']}
    rows=[dict(r) for r in db.execute('''SELECT t.*,a.priority,a.effort,a.preferred_actor
             FROM tasks t JOIN ma_auto_tasks a ON a.project=t.project AND a.task=t.id
             WHERE t.project=? AND t.state='pending' AND
             (a.preferred_actor IS NULL OR a.preferred_actor=?) ORDER BY a.priority DESC,t.id''',
             (project,actor))]
    if not rows:return {'state':'IDLE','reason':'NO_ELIGIBLE_AUTOMATIC_TASKS'}
    states={r['id']:dict(r) for r in db.execute('SELECT id,state,version FROM tasks WHERE project=?',(project,))}
    ready=[];waiting=[]
    for t in rows:
        deps=json.loads(t['dependencies'])
        blocked=[d for d in deps if states[d]['state']!='delivered' or states[d]['version']!=version]
        if blocked:waiting.append({'task':t['id'],'dependencies':blocked})
        else:ready.append(t)
    if not ready:return {'state':'WAITING','blocked':waiting}
    dependents={t['id']:0 for t in rows}
    for t in states.values():pass
    all_tasks=[dict(r) for r in db.execute('SELECT id,dependencies,state FROM tasks WHERE project=?',(project,))]
    for t in all_tasks:
        if t['state']=='pending':
            for d in json.loads(t['dependencies']):
                if d in dependents:dependents[d]+=1
    ready.sort(key=lambda t:(-t['priority'],-dependents[t['id']],-t['effort'],t['id']))
    return {'state':'READY','task_id':ready[0]['id'],
            'eligible_count':len(ready),'blocked_count':len(waiting)}
def publish(db,project,actor,task,kind,to_actor,body,version,commit=None):
    from ma_memory import safe_text,knowledge
    if kind not in KINDS:raise ValueError('Tipo de evento invalido')
    if to_actor not in ('shared',*ROLES):raise ValueError('Destinatario invalido')
    body=safe_text(body,2200)
    if not body:raise ValueError('Evento vazio')
    if task:
        taskrow=db.execute('SELECT agent,commit_sha FROM tasks WHERE project=? AND id=?',
                           (project,task)).fetchone()
        if not taskrow:raise ValueError('Tarefa inexistente')
        if actor!='orquestrador' and taskrow['agent']!=actor:
            raise ValueError('Agente nao pode publicar sobre tarefa alheia')
        commit=taskrow['commit_sha']
    cursor=db.execute('''INSERT INTO ma_events(project,actor,task,kind,to_actor,body,version,commit_sha,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',
           (project,actor,task or '',kind,to_actor,body,version,commit,stamp())).lastrowid
    # Preserve role-addressed events in the receiver's memory across chat swaps.
    scopes=['shared'] if to_actor=='shared' else [to_actor,actor]
    for scope in dict.fromkeys(scopes):
        knowledge(db,project,scope,'event-'+kind,body,task=task or '',
                  version=version,source='orchestration-event-'+str(cursor))
    return {'ok':True,'event_id':cursor,'kind':kind,'to':to_actor}
def read_events(db,project,actor,after=0,limit=20):
    if not isinstance(after,int) or after<0:raise ValueError('Cursor invalido')
    if not 1<=limit<=50:raise ValueError('limit deve estar entre 1 e 50')
    if actor=='orquestrador':
        rows=db.execute('SELECT * FROM ma_events WHERE project=? AND seq>? ORDER BY seq LIMIT ?',
                        (project,after,limit+1)).fetchall()
    else:
        rows=db.execute('''SELECT * FROM ma_events WHERE project=? AND seq>? AND
             (to_actor='shared' OR to_actor=? OR actor=?)
             ORDER BY seq LIMIT ?''',(project,after,actor,actor,limit+1)).fetchall()
    visible=[dict(r) for r in rows[:limit]]
    return {'state':'EVENTS' if visible else 'EMPTY','events':visible,
            'next_cursor':visible[-1]['seq'] if visible else after,
            'has_more':len(rows)>limit}
