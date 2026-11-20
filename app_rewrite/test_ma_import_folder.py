import sys,time,ast,tempfile,subprocess,shutil,os,stat,json,hashlib,gc
from pathlib import Path
base=Path(r'C:\Users\Administrador\AppData\Local\Programs\CodeBridge\app_rewrite')
sys.path.insert(0,str(base))
import ma_project_import as imp
import ma_memory
import chat_tasks
root=Path(tempfile.mkdtemp(prefix='cb-manual-folder-'))
repo=root/'Meu Projeto Teste';repo.mkdir()
dbroot=root/'state'
results={}
def git(*a):
    r=subprocess.run(['git',*a],cwd=repo,capture_output=True,text=True,timeout=25)
    if r.returncode:raise RuntimeError('git failed: '+r.stderr[:250])
    return r.stdout.strip()
def check(name,condition):
    results[name]=bool(condition)
    if not condition:raise AssertionError(name)
try:
    git('init','-q')
    git('config','user.name','CodeBridge Teste')
    git('config','user.email','teste@invalid.example')
    (repo/'README.md').write_text('Requisito central: memoriaindexadaunica. Teste cadastro manual.\n',encoding='utf-8')
    git('add','README.md')
    git('commit','-qm','Base')
    (repo/'docs').mkdir()
    check('project_slug',imp.suggested_id(repo)=='meu-projeto-teste')
    root_info=imp.inspect_folder(repo)
    check('valid_git_repo',root_info['repo']==str(repo.resolve()) and not root_info['dirty'] and len(root_info['git_head'])==40)
    nested=imp.inspect_folder(repo/'docs')
    check('nested_git_detection',nested['nested_folder'] and nested['repo']==root_info['repo'])
    try:
        imp.inspect_folder(root)
        check('non_git_rejected',False)
    except ValueError:
        check('non_git_rejected',True)
    (repo/'README.md').write_text('Alteracao ainda sem commit\n',encoding='utf-8')
    check('dirty_git_detected',imp.inspect_folder(repo)['dirty'])
    git('checkout','--','README.md')
    check('clean_git_reconfirmed',not imp.inspect_folder(repo)['dirty'])
    created=imp.register_folder(dbroot,'meu-projeto-teste',repo,'Contrato: organizar trabalho por agentes no repositorio existente.')
    check('register_project',created['project']=='meu-projeto-teste' and created['project_folder']==str(repo.resolve()))
    check('auto_rag_queued',created['rag_index']['state']=='QUEUED')
    with chat_tasks.connect(dbroot) as db:
        projects=db.execute('SELECT id,repo,contract,base FROM projects').fetchall()
        check('sqlite_project_saved',len(projects)==1 and projects[0]['base']==git('rev-parse','HEAD'))
        cp=db.execute("SELECT COUNT(*) FROM ma_checkpoints WHERE project=?",
                      ('meu-projeto-teste',)).fetchone()[0]
        check('initial_checkpoint',cp>=1)
    for _ in range(70):
        with chat_tasks.connect(dbroot) as db:
            idx=db.execute("SELECT state,file_count,last_error FROM ma_rag_projects WHERE project=?",
                           ('meu-projeto-teste',)).fetchone()
            if idx and idx['state']=='READY':break
            if idx and idx['state']=='FAILED': raise RuntimeError('RAG failed '+str(idx['last_error']))
        time.sleep(.25)
    check('rag_index_ready',bool(idx and idx['state']=='READY' and idx['file_count']>=1))
    with chat_tasks.connect(dbroot) as db:
        found=ma_memory.search(db,'meu-projeto-teste','orquestrador','memoriaindexadaunica',limit=3)
        check('rag_verified',bool(found and found[0]['verified'] is True))
        check('sha256_correct',found[0]['sha256']==hashlib.sha256((repo/'README.md').read_bytes()).hexdigest())
    try:
        imp.register_folder(dbroot,'meu-projeto-teste',repo,'Contrato')
        check('duplicate_id_rejected',False)
    except ValueError:check('duplicate_id_rejected',True)
    try:
        imp.register_folder(dbroot,'outro-projeto',repo,'Contrato')
        check('duplicate_folder_rejected',False)
    except ValueError:check('duplicate_folder_rejected',True)
    try:
        imp.register_folder(dbroot,'INVALID SPACE',repo,'Contrato')
        check('invalid_id_rejected',False)
    except ValueError:check('invalid_id_rejected',True)
    check('git_not_modified',not git('status','--porcelain'))
    ui=ast.parse((base/'multiagents_panel.py').read_text(encoding='utf-8'))
    panel=next(node for node in ui.body if isinstance(node,ast.ClassDef) and node.name=='MultiAgentsPanel')
    methods={n.name for n in panel.body if isinstance(n,ast.FunctionDef)}
    check('ui_button_function',{'add_project_folder','refresh','pair_chat'}<=methods)
    check('ui_folder_dialog', 'getExistingDirectory' in (base/'multiagents_panel.py').read_text(encoding='utf-8'))
except Exception as exc:
    results['error']=repr(exc)
finally:
    gc.collect()
    def rm(f,p,e):
        os.chmod(p,stat.S_IWRITE|stat.S_IREAD)
        f(p)
    try:
        shutil.rmtree(root,onerror=rm)
        results['temporary_cleanup']=not root.exists()
    except Exception as e:
        results['cleanup_error']=str(e)[:300]
print(json.dumps(results,ensure_ascii=True))
if 'error' in results or 'cleanup_error' in results or not all(results.values()):sys.exit(1)
