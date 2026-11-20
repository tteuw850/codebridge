"""E2E deterministic test CodeBridge orchestration in throwaway Git repo."""
from pathlib import Path
import sys,subprocess,json,tempfile,time,shutil,os,stat,gc
base=Path(r'C:\Users\Administrador\AppData\Local\Programs\CodeBridge\app_rewrite')
sys.path.insert(0,str(base))
import chat_tasks as ct
import ma_sessions as ms
root=Path(tempfile.mkdtemp(prefix='cb-optim-e2e-'))
repo=root/'repo';repo.mkdir();state=root/'state';project='parallel'
report={'test':'ORCHESTRATOR_PARALLEL_EVENTS_DYNAMIC','checks':{},'cleanup':None}
def git(cwd,*args):
    r=subprocess.run(['git','-c','core.hooksPath=NUL',*args],cwd=str(cwd),capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError('git: '+r.stderr[:450])
    return r.stdout.strip()
def cli(argv,key=None,timeout=35):
    args=[sys.executable,str(base/'chat_tasks.py'),'--root',str(state)]
    if key:args+=['--session',key]
    args+=argv
    r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError('CLI '+repr(argv[:3])+' '+r.stderr[:650])
    return json.loads(r.stdout)
def check(k,value):report['checks'][k]=bool(value);assert value,k
def addfile_commit(taskid,actor,assigned):
    w=Path(assigned['workspace'])
    f=w/'docs'/f'{taskid}.md';f.parent.mkdir(parents=True,exist_ok=True)
    f.write_text('Resultado validado da tarefa '+taskid+'.\n',encoding='utf-8')
    git(w,'add',f'docs/{taskid}.md')
    git(w,'commit','-qm',f'Deliver {taskid}')
    head=git(w,'rev-parse','HEAD')
    r=cli(['deliver',project,taskid,actor,head,'--text','Entrega validada'],keys[actor])
    if r.get('ok') is not True:raise ValueError('deliver falhou '+repr(r))
    return r
try:
    git(repo,'init','-q')
    git(repo,'config','user.name','CodeBridge Teste')
    git(repo,'config','user.email','cb@invalid.example')
    (repo/'README.md').write_text('Planejamento paralelo com eventos e memoria.\n',encoding='utf-8')
    git(repo,'add','.')
    git(repo,'commit','-qm','Base')
    cli(['init',project,str(repo),'--text','Plano de relatorios independentes'])
    with ct.connect(state,write=True) as db:
        keys={r:ms.provision(db,project,r) for r in ms.ROLES}
    tasks=[
      dict(id='a',brief='Criar relatorio A',scope=['docs/a.md'],depends=[],actor='agente-1',priority=100,effort=3),
      dict(id='b',brief='Criar relatorio B',scope=['docs/b.md'],depends=[],actor='agente-2',priority=100,effort=3),
      dict(id='c',brief='Criar relatorio C',scope=['docs/c.md'],depends=[],actor='agente-3',priority=100,effort=3),
      dict(id='d',brief='Relatorio D usa A',scope=['docs/d.md'],depends=['a'],actor='agente-1',priority=95,effort=2),
      dict(id='e',brief='Relatorio E usa B',scope=['docs/e.md'],depends=['b'],actor='agente-2',priority=95,effort=2),
      dict(id='f',brief='Relatorio F usa B C',scope=['docs/f.md'],depends=['b','c'],actor='agente-3',priority=95,effort=2),
      dict(id='g',brief='Relatorio G usa D',scope=['docs/g.md'],depends=['d'],actor='agente-1',priority=90,effort=1)]
    raw=json.dumps({'tasks':tasks},separators=(',',':'))
    dry=cli(['plan',project,'--json',raw],keys['orquestrador'])
    check('planner_dryrun',not dry['applied'] and dry['plan']['task_count']==7)
    check('planner_three_lanes',len(set(t['recommended_actor'] for t in dry['plan']['schedule']))==3)
    with ct.connect(state) as db:
        check('dryrun_no_write',db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==0)
    applied=cli(['plan',project,'--json',raw,'--apply'],keys['orquestrador'])
    check('planner_seven_tasks_registered',applied['applied'] and len(applied['plan']['tasks'])==7)
    try:
        bad=json.dumps({'tasks':[dict(id='overlap',brief='Conflito',scope=['docs/a.md'])]})
        cli(['plan',project,'--json',bad],keys['orquestrador'])
        check('planner_overlap_rejected',False)
    except RuntimeError:
        check('planner_overlap_rejected',True)
    assigned={r:cli(['next',project,r,'--wait-seconds','0'],keys[r]) for r in ('agente-1','agente-2','agente-3')}
    check('auto_first_three',all(assigned[r]['task_id']==n for r,n in [('agente-1','a'),('agente-2','b'),('agente-3','c')]))
    busy=cli(['next',project,'agente-1','--wait-seconds','0'],keys['agente-1'])
    check('busy_prevents_double_assignment',busy['state']=='BUSY')
    ev1=cli(['event',project,'--task','a','--kind','interface','--to','shared','--text','Contrato interface A: status e data'],keys['agente-1'])
    ev2=cli(['event',project,'--task','b','--kind','decision','--to','agente-3','--text','Detalhe exclusivo para agente tres'],keys['agente-2'])
    check('events_inserted',ev1['event_id']<ev2['event_id'])
    e1=cli(['events',project,'--after','0'],keys['agente-1'])
    e3=cli(['events',project,'--after','0'],keys['agente-3'])
    check('private_event_visibility',all(x['event_id']!=ev2['event_id'] for x in e1['events']) if False else
          all(x['seq']!=ev2['event_id'] for x in e1['events']) and
          any(x['seq']==ev2['event_id'] for x in e3['events']))
    start=e1['next_cursor']
    cmd=[sys.executable,str(base/'chat_tasks.py'),'--root',str(state),'--session',keys['agente-1'],
         'events',project,'--after',str(start),'--wait-seconds','4']
    watcher=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    time.sleep(.4)
    cli(['event',project,'--task','b','--kind','progress','--text','Parcial B disponivel'],keys['agente-2'])
    out,err=watcher.communicate(timeout=9)
    check('longpoll_wakeup',watcher.returncode==0 and bool(json.loads(out)['events']))
    a=addfile_commit('a','agente-1',assigned['agente-1'])
    check('deliver_auto_dispatch_d',a['next_task'].get('task_id')=='d')
    b=addfile_commit('b','agente-2',assigned['agente-2'])
    check('deliver_auto_dispatch_e',b['next_task'].get('task_id')=='e')
    c=addfile_commit('c','agente-3',assigned['agente-3'])
    check('deliver_auto_dispatch_f',c['next_task'].get('task_id')=='f')
    d=addfile_commit('d','agente-1',a['next_task'])
    check('deliver_auto_dispatch_g',d['next_task'].get('task_id')=='g')
    e=addfile_commit('e','agente-2',b['next_task'])
    f=addfile_commit('f','agente-3',c['next_task'])
    g=addfile_commit('g','agente-1',d['next_task'])
    check('final_agents_idle',all(x['next_task']['state']=='IDLE' for x in (e,f,g)))
    with ct.connect(state) as db:
        check('all_delivered',db.execute("SELECT count(*) FROM tasks WHERE state='delivered'").fetchone()[0]==7)
        check('event_journal_persistent',db.execute('SELECT COUNT(*) FROM ma_events').fetchone()[0]==3)
    resp=cli(['resume',project],keys['agente-3'])
    check('resume_events',any(x.get('kind')=='decision' for x in resp['recent_events']['events']))
    integ=cli(['integrate',project,'--','python','-c','print("verified")'],keys['orquestrador'],timeout=90)
    check('integration_seven_tasks',bool(integ.get('commit')))
    check('no_wait_after_delivery',all('project_barrier' not in x for x in (a,b,c,d,e,f,g)))
except Exception as exc:
    report['error']=repr(exc)
finally:
    def rm_error(f,name,exc):
        os.chmod(name,stat.S_IWRITE|stat.S_IREAD)
        f(name)
    gc.collect()
    try:
        shutil.rmtree(root,onerror=rm_error)
        report['cleanup']=not root.exists()
    except Exception as exc:
        report['cleanup_error']=repr(exc)
print(json.dumps(report,ensure_ascii=True))
if report.get('error') or report.get('cleanup_error') or not all(report['checks'].values()):
    sys.exit(1)
