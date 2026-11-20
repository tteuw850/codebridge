"""Session authentication and durable directed messages for Multi Agents."""
from datetime import datetime, timezone
from hashlib import sha256
from secrets import token_urlsafe
import time
from uuid import uuid4

ROLES = ("orquestrador", "agente-1", "agente-2", "agente-3")
LEASE = 180
SCHEMA = """
CREATE TABLE IF NOT EXISTS ma_sessions (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, actor TEXT NOT NULL,
  digest TEXT NOT NULL UNIQUE, heartbeat REAL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ma_session_role
  ON ma_sessions(project,actor) WHERE revoked=0;
CREATE TABLE IF NOT EXISTS ma_mail (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, sender TEXT NOT NULL,
  recipient TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
  deadline REAL NOT NULL, received REAL, ack_id TEXT UNIQUE, ack_at REAL,
  attempts INTEGER NOT NULL DEFAULT 1,
  UNIQUE(project,sender,recipient,id)
);
"""


def provision(db, project, actor):
    if actor not in ROLES:
        raise ValueError("Papel inválido")
    if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
        raise ValueError("Projeto inexistente")
    db.execute("UPDATE ma_sessions SET revoked=1 WHERE project=? AND actor=?", (project, actor))
    token = "cbma_" + token_urlsafe(32)
    db.execute(
        "INSERT INTO ma_sessions(id,project,actor,digest) VALUES(?,?,?,?)",
        (uuid4().hex, project, actor, sha256(token.encode()).hexdigest()),
    )
    return token


def authenticate(db, project, token, actor=None):
    if not token:
        raise ValueError("Informe a credencial de sessão emitida no painel")
    session = db.execute(
        "SELECT * FROM ma_sessions WHERE project=? AND digest=? AND revoked=0",
        (project, sha256(token.encode()).hexdigest()),
    ).fetchone()
    if not session or (actor and actor != session["actor"]):
        raise ValueError("A credencial não corresponde ao projeto ou papel")
    return session


def touch(db, session):
    db.execute("UPDATE ma_sessions SET heartbeat=? WHERE id=?", (time.time(), session["id"]))


def sessions(db, project):
    now = time.time()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_sessions'").fetchone():
        return []
    return [
        {**dict(row), "online": row["heartbeat"] is not None and now - row["heartbeat"] <= LEASE}
        for row in db.execute(
            "SELECT id,actor,heartbeat FROM ma_sessions WHERE project=? AND revoked=0 ORDER BY actor",
            (project,),
        )
    ]


def mail_status(row, now):
    if row["ack_id"]:
        return "acknowledged"
    if row["deadline"] < now:
        return "failed"
    return "received" if row["received"] else "pending"


def mail_view(db, project, session_id=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ma_mail'").fetchone():
        return []
    condition = " AND (m.sender=? OR m.recipient=?)" if session_id else ""
    args = (project, session_id, session_id) if session_id else (project,)
    sql = """
      SELECT m.*, s.actor AS from_actor, r.actor AS to_actor
      FROM ma_mail m JOIN ma_sessions s ON m.sender=s.id
      JOIN ma_sessions r ON m.recipient=r.id WHERE m.project=?
    """
    now = time.time()
    return [
        {**dict(row), "state": mail_status(row, now)}
        for row in db.execute(sql + condition + " ORDER BY m.created DESC, m.id DESC LIMIT 50", args)
    ]


def mailbox(db, project, session, args):
    action, now = args.action, time.time()
    if action == "heartbeat":
        return {"session": session["id"], "actor": session["actor"], "lease_seconds": LEASE}
    if action == "send":
        who = db.execute(
            "SELECT * FROM ma_sessions WHERE project=? AND actor=? AND revoked=0",
            (project, args.to),
        ).fetchone()
        prior = db.execute("SELECT * FROM ma_mail WHERE id=? AND project=?", (args.message, project)).fetchone()
        if prior:
            if prior["sender"] != session["id"] or not who or prior["recipient"] != who["id"] or prior["body"] != args.text:
                raise ValueError("ID de mensagem já utilizado")
            return {"id": args.message, "state": mail_status(prior, now), "duplicate": True}
        if not who or (not args.queue_offline and
                       (not who["heartbeat"] or now - who["heartbeat"] > LEASE)):
            raise ValueError("AGENTE_INDISPONIVEL")
        if who["id"] == session["id"]:
            raise ValueError("Uma sessão não pode enviar mensagem a si mesma")
        db.execute(
            "INSERT INTO ma_mail(id,project,sender,recipient,body,created,deadline) VALUES(?,?,?,?,?,?,?)",
            (args.message, project, session["id"], who["id"], args.text, now,
             now + (3600 if args.queue_offline else LEASE)),
        )
        return {"id": args.message, "state": "pending", "recipient_session": who["id"]}
    if action == "inbox":
        visible = mail_view(db, project, session["id"])
        ids = [(now, row["id"]) for row in visible if row["recipient"] == session["id"] and row["state"] == "pending"]
        db.executemany("UPDATE ma_mail SET received=? WHERE id=? AND received IS NULL", ids)
        return mail_view(db, project, session["id"])
    message = db.execute("SELECT * FROM ma_mail WHERE id=? AND project=?", (args.message, project)).fetchone()
    if not message:
        raise ValueError("Mensagem inexistente")
    if action == "retry":
        if message["sender"] != session["id"] or message["ack_id"] or message["deadline"] >= now or message["attempts"] >= 3:
            raise ValueError("Envio não pode ser repetido")
        receiver = db.execute("SELECT * FROM ma_sessions WHERE id=?", (message["recipient"],)).fetchone()
        if receiver["revoked"] or not receiver["heartbeat"] or now - receiver["heartbeat"] > LEASE:
            raise ValueError("AGENTE_INDISPONIVEL")
        db.execute(
            "UPDATE ma_mail SET deadline=?,received=NULL,attempts=attempts+1 WHERE id=?", (now + LEASE, args.message)
        )
        return {"id": args.message, "state": "pending", "attempt": message["attempts"] + 1}
    if session["id"] != message["recipient"] or session["id"] == message["sender"]:
        raise ValueError("O ACK exige a sessão destinatária")
    if message["ack_id"]:
        return {"ack": message["ack_id"], "in_reply_to": args.message, "duplicate": True}
    if not message["received"] or message["deadline"] < now:
        raise ValueError("Consulte a mensagem na caixa de entrada antes de confirmar")
    ack = uuid4().hex
    db.execute("UPDATE ma_mail SET ack_id=?,ack_at=? WHERE id=?", (ack, now, args.message))
    return {"ack": ack, "in_reply_to": args.message, "from_session": session["id"]}


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().strftime("%H:%M:%S") if epoch else "—"
