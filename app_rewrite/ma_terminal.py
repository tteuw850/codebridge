"""Route Multi Agents worktree and coordination jobs outside the shared CMD."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Condition, Thread
from time import monotonic
from uuid import uuid4

PREFIX = "maexec_"
TARGET = re.compile(r"MA:([a-z0-9][a-z0-9-]{0,39}):([a-z0-9][a-z0-9-]{0,39})", re.I)
DONE = {"FINISHED", "FAILED", "CANCELLED", "INTERRUPTED"}
LIMIT = 512_000
CONTROL = {
    "status", "heartbeat", "inbox", "send", "ack", "retry", "note", "claim",
    "sync", "accept-contract", "deliver", "add", "contract", "reopen", "integrate",
    "checkpoint", "resume", "rag-search", "rag-status", "rag-sync",
    "plan", "next", "event", "events",
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def problem(message):
    return {"operation_ok": False, "state": "ERROR", "error_type": "MultiAgentsError",
            "operation_error_type": "MultiAgentsError", "error_message": message,
            "operation_error_message": message, "failed_command": "[Multi Agents request]"}


class Execution:
    def __init__(self, target, lane, token):
        self.id = PREFIX + uuid4().hex
        self.target = target
        self.lane = lane
        self.token = token
        self.state = "CREATED"
        self.code = self.started = self.finished = self.child = None
        self.output = ""
        self.cancelled = False

    def summary(self):
        return {
            "operation_ok": True, "target": self.target, "execution_id": self.id,
            "state": self.state, "exit_code": self.code, "started_at": self.started,
            "finished_at": self.finished, "execution_recoverable": True,
            "failed_command": "[Multi Agents request]",
        }


class TaskTerminals:
    def __init__(self, tasks):
        self.tasks = tasks
        self.cv = Condition()
        self.jobs = {}
        self.lanes = {}
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="codebridge-ma")

    def prepare(self, project, task, token, request):
        with self.tasks.connect(self.tasks.ROOT) as db:
            if db is None:
                raise ValueError("Projeto Multi Agents inexistente")
            identity = self.tasks.sessions.authenticate(db, project, token)
            actor = identity["actor"]
            if task == "control":
                args = request.get("args")
                if not isinstance(args, list) or not args or not all(isinstance(x, str) for x in args):
                    raise ValueError("A sessão control exige args como lista")
                if args[0] not in CONTROL:
                    raise ValueError("Subcomando Multi Agents desconhecido")
                argv = [sys.executable, "-B", str(Path(self.tasks.__file__)), "--root", str(self.tasks.ROOT), args[0], project, *args[1:]]
                return (project, "control", actor), argv, None, token
            row = self.tasks.get_task(db, project, task)
            if row["agent"] != actor or row["state"] != "active":
                raise ValueError("A tarefa precisa estar ativa e pertencer à sessão")
            command = request.get("command")
            if not isinstance(command, str) or not command.strip() or len(command) > 7000 or "\0" in command:
                raise ValueError("Comando CMD vazio ou inválido")
            workspace = self.tasks.current_workspace(self.tasks.ROOT, project, task).resolve(strict=True)
            root = self.tasks.ROOT.resolve()
            if root not in workspace.parents:
                raise ValueError("Worktree fora da pasta Multi Agents")
            if os.name == "nt":
                cmd = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
                # CMD receives the raw script after /C; subprocess quoting is
                # intended for argv parsers and would escape its inner quotes.
                argv = f'"{cmd}" /d /s /c "{command}"'
            else:
                argv = ["/bin/sh", "-c", command]
            return (project, task), argv, workspace, None

    def start(self, target, body):
        match = TARGET.fullmatch(target)
        if not match:
            return problem("Use MA:<projeto>:<tarefa> ou MA:<projeto>:control")
        project, task = (part.lower() for part in match.groups())
        try:
            request = json.loads(body)
            token = request["session"]
            if not isinstance(token, str) or len(token) > 256:
                raise ValueError("Credencial inválida")
            lane, _, _, _ = self.prepare(project, task, token, request)
        except (KeyError, OSError, TypeError, ValueError) as error:
            return problem(str(error) if not isinstance(error, KeyError) else "Informe session no comando JSON")
        with self.cv:
            if len(self.jobs) >= 128:
                for key in [key for key, job in self.jobs.items() if job.state in DONE][:32]:
                    del self.jobs[key]
            if sum(job.state not in DONE for job in self.jobs.values()) >= 64:
                return problem("Fila Multi Agents cheia")
            job = Execution(target, lane, token)
            self.jobs[job.id] = job
            ready = lane not in self.lanes
            self.lanes.setdefault(lane, deque()).append((job, project, task, request))
            if ready:
                self.pool.submit(self.run_lane, lane)
            return job.summary()

    def run_lane(self, lane):
        while True:
            with self.cv:
                queue = self.lanes[lane]
                if not queue:
                    del self.lanes[lane]
                    return
                job, project, task, request = queue.popleft()
                if job.cancelled:
                    continue
                job.state = "RUNNING"
                job.started = stamp()
            self.run_one(job, project, task, request)

    def append(self, job, value):
        with self.cv:
            available = LIMIT - len(job.output)
            if available > 0:
                job.output += value[:available]
                if len(value) > available:
                    job.output += "\n[Saída limitada pelo CodeBridge]\n"
                self.cv.notify_all()

    def run_one(self, job, project, task, request):
        code, state = None, "FAILED"
        try:
            _, argv, cwd, credential = self.prepare(project, task, job.token, request)
            env = os.environ.copy()
            env.pop("CB_MULTIAGENTS_SESSION", None)
            if credential:
                env["CB_MULTIAGENTS_SESSION"] = credential
            with self.cv:
                if job.cancelled:
                    return
                job.child = subprocess.Popen(
                    argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            # Drain separately so a silent process can still time out.
            reader = Thread(target=self.read_pipe, args=(job,), daemon=True)
            reader.start()
            try:
                code = job.child.wait(timeout=900)
            except subprocess.TimeoutExpired:
                self.terminate(job.child)
                self.append(job, "\n[Tempo limite de execução]\n")
            reader.join(timeout=3)
            state = "FINISHED" if code == 0 else "FAILED"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.append(job, str(error))
        finally:
            with self.cv:
                job.token = None
                job.state = "CANCELLED" if job.cancelled else state
                job.code = code
                job.finished = stamp()
                self.cv.notify_all()

    def read_pipe(self, job):
        import codecs
        import locale
        decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))(errors="replace")
        with job.child.stdout as stream:
            while chunk := stream.read1(8192):
                self.append(job, decoder.decode(chunk))
            self.append(job, decoder.decode(b"", final=True))

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            taskkill = os.path.join(os.environ["SystemRoot"], "System32", "taskkill.exe")
            result = subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                                    capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode and process.poll() is None:
                process.kill()
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def dispatch(self, operation, payload):
        if operation == "EXECUTION_V2_START":
            return self.start(payload.get("target", ""), payload.get("command"))
        key = payload.get("execution_id")
        with self.cv:
            job = self.jobs.get(key)
            if not job:
                return problem("Execução Multi Agents ausente ou expirada")
            if operation in {"EXECUTION_V2_STATUS", "EXECUTION_V2_RESULT"}:
                return {**job.summary(), "output": job.output}
            if operation == "EXECUTION_V2_STOP":
                if job.state in DONE:
                    return {**job.summary(), "cancelled": False}
                job.cancelled = True
                process = job.child
                if job.state == "CREATED":
                    job.state = "CANCELLED"
                    job.finished = stamp()
                    job.token = None
                self.cv.notify_all()
            else:
                cursor = max(0, int(payload.get("cursor") or 0))
                max_chars = max(1, min(int(payload.get("max_chars") or 32768), 262144))
                seconds = min(120, max(0, int(payload.get("timeout_ms") or 0) / 1000))
                until = monotonic() + (seconds if operation == "EXECUTION_V2_WAIT" else 0)
                while job.state not in DONE and len(job.output) <= cursor:
                    remaining = until - monotonic()
                    if remaining <= 0 or not self.cv.wait(remaining):
                        break
                delta = job.output[cursor:cursor + max_chars]
                end = cursor + len(delta)
                complete, more = job.state in DONE, end < len(job.output)
                return {**job.summary(), "text": delta, "next_cursor": end, "complete": complete,
                        "has_more": more, "eof": complete and not more, "timed_out": not delta and not complete}
        if process:
            self.terminate(process)
        return {**job.summary(), "cancelled": True, "targeted": True, "reason": "user_requested"}


def wrap_operations(operations, tasks):
    terminals = TaskTerminals(tasks)
    for name in ("START", "STATUS", "RESULT", "OUTPUT", "WAIT", "STOP"):
        operation = "EXECUTION_V2_" + name
        previous = operations[operation]

        def execute(payload, request_id=None, *, operation=operation, previous=previous):
            target = str(payload.get("target") or "")
            execution = str(payload.get("execution_id") or "")
            if target.upper().startswith("MA:") or execution.startswith(PREFIX):
                return terminals.dispatch(operation, payload)
            return previous(payload, request_id=request_id)

        operations[operation] = execute
