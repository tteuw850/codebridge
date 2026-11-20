"""Show shared ChatGPT tasks in the CodeBridge desktop application."""
from datetime import datetime
from pathlib import Path
import sqlite3

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHeaderView, QInputDialog, QMessageBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QMenu, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from chat_tasks import ROOT, connect, snapshot
from ma_sessions import ROLES, provision, timestamp
from ma_project_import import inspect_folder, register_folder
from ma_role_instructions import ROLE_NAMES, build_instructions


class MultiAgentsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.projects = QComboBox()
        self.projects.setMinimumWidth(180)
        self.projects.currentTextChanged.connect(self.refresh)
        add_folder = QPushButton("Adicionar pasta de projeto")
        add_folder.setToolTip("Selecionar uma pasta Git existente e cadastrar no Multi Agents")
        add_folder.clicked.connect(self.add_project_folder)
        refresh = QPushButton("Atualizar")
        refresh.clicked.connect(self.refresh)
        copy = QPushButton("Copiar instruções dos chats ▾")
        copy.setToolTip("Escolha o papel e copie instruções prontas sem renovar a chave do chat")
        menu = QMenu(copy)
        for actor in ROLES:
            action = menu.addAction(ROLE_NAMES[actor])
            action.triggered.connect(
                lambda _checked=False, chosen_actor=actor: self.copy_instructions(chosen_actor)
            )
        copy.setMenu(menu)
        toolbar.addWidget(QLabel("Projeto"))
        toolbar.addWidget(self.projects, 1)
        toolbar.addWidget(add_folder)
        toolbar.addWidget(refresh)
        toolbar.addWidget(copy)
        pair = QPushButton("Vincular chat")
        pair.clicked.connect(self.pair_chat)
        toolbar.addWidget(pair)
        layout.addLayout(toolbar)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.project_folder = QLabel()
        self.project_folder.setWordWrap(True)
        layout.addWidget(self.project_folder)
        self.presence = QLabel()
        self.presence.setWordWrap(True)
        layout.addWidget(self.presence)
        self.tasks = QTableWidget(0, 6)
        self.tasks.setHorizontalHeaderLabels(("Tarefa", "Chat", "Estado", "Contrato", "Commit", "Terminal MCP"))
        self.tasks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tasks.verticalHeader().hide()
        self.tasks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.tasks, 2)
        layout.addWidget(QLabel("Atividade compartilhada"))
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        layout.addWidget(self.activity, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self, *_):
        try:
            data = snapshot()
        except (OSError, sqlite3.Error) as error:
            self.summary.setText(f"Falha ao consultar as tarefas: {error}")
            return
        current = self.projects.currentText()
        projects = {row["id"]: row for row in data["projects"]}
        if list(projects) != [self.projects.itemText(i) for i in range(self.projects.count())]:
            self.projects.blockSignals(True)
            self.projects.clear()
            self.projects.addItems(list(projects))
            if current in projects:
                self.projects.setCurrentText(current)
            self.projects.blockSignals(False)
        current = self.projects.currentText()
        project = projects.get(current)
        version = project["version"] if project else None
        self.summary.setText(
            f"Contrato v{version} · {project['contract']}" if project
            else "Nenhum projeto cadastrado. Clique em Adicionar pasta de projeto para começar."
        )
        if project:
            rag_label = "não iniciado"
            try:
                with connect(ROOT) as db:
                    if db is not None:
                        row = db.execute(
                            "SELECT state,file_count FROM ma_rag_projects WHERE project=?",
                            (current,)
                        ).fetchone()
                        if row:
                            rag_label = str(row["state"]) + " (" + str(row["file_count"]) + " arquivos)"
            except sqlite3.Error:
                pass
            self.project_folder.setText(
                "Pasta: " + project["repo"] + "  |  RAG local: " + rag_label
            )
        else:
            self.project_folder.setText("")
        detail = snapshot(project=current) if current else {"sessions": [], "mail": [], "messages": []}
        by_role = {row["actor"]: row for row in detail["sessions"]}
        presence = []
        for role in ROLES:
            session = by_role.get(role)
            if session and session["online"]:
                state = "Ativo · " + timestamp(session["heartbeat"])
            elif session:
                state = "Sem heartbeat recente" if session["heartbeat"] else "Aguardando chat"
            else:
                state = "Não vinculado"
            presence.append(f"{role}: {state}")
        self.presence.setText("   |   ".join(presence))
        tasks = [row for row in data["tasks"] if row["project"] == current]
        self.tasks.setRowCount(len(tasks))
        labels = {"pending": "Pendente", "active": "Em execução", "delivered": "Entregue", "integrated": "Integrada"}
        for index, task in enumerate(tasks):
            value = labels[task["state"]]
            if task["state"] != "integrated" and task["version"] and task["version"] != version:
                value = "Revisar contrato"
            for col, value in enumerate((task["id"], task["agent"] or "—", value,
                                         str(task["version"] or "—"), (task["commit_sha"] or "—")[:12],
                                         f"MA:{current}:{task['id']}")):
                self.tasks.setItem(index, col, QTableWidgetItem(value))
        lines = []
        for row in reversed(detail["messages"]):
            if row["project"] == current:
                stamp = datetime.fromisoformat(row["created"]).astimezone().strftime("%H:%M")
                task = f" · {row['task']}" if row["task"] else ""
                lines.append(f"{stamp} · {row['actor']}{task} · {row['text']}")
        statuses = {"pending": "Pendente", "received": "Recebida", "acknowledged": "ACK confirmado", "failed": "Expirada"}
        for message in reversed(detail["mail"]):
            status = statuses[message["state"]]
            lines.append(f"{timestamp(message['created'])} · {message['from_actor']} → {message['to_actor']} · "
                         f"{message['id']} · {status} · {message['body']}")
        # Project owner can inspect the durable partial-event feed in the panel.
        if current:
            with connect(ROOT) as events_db:
                if events_db and events_db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ma_events'"
                ).fetchone():
                    for ev in events_db.execute(
                        "SELECT actor,task,kind,to_actor,body,created_at FROM ma_events "
                        "WHERE project=? ORDER BY seq DESC LIMIT 20",(current,)
                    ):
                        label = (" / "+ev["task"]) if ev["task"] else ""
                        lines.append(
                            ev["created_at"][11:16]+" EVENTO "+ev["kind"]+" "
                            +ev["actor"]+label+" -> "+ev["to_actor"]+": "+ev["body"]
                        )
        content = "\n".join(lines)
        if self.activity.toPlainText() != content:
            self.activity.setPlainText(content)
            bar = self.activity.verticalScrollBar()
            bar.setValue(bar.maximum())

    def add_project_folder(self):
        """Register selected Git folder without modifying the source repository."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Selecionar pasta do projeto para Multi Agents", str(Path.home())
        )
        if not chosen:
            return
        try:
            info = inspect_folder(chosen)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Adicionar projeto", str(exc))
            return

        if info["nested_folder"]:
            decision = QMessageBox.question(
                self,
                "Usar raiz do repositório Git?",
                "A pasta selecionada pertence ao repositório:\n"
                + info["repo"] + "\n\n"
                "O Multi Agents usa o repositório Git inteiro, não só a subpasta. "
                "Deseja continuar com a raiz Git?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if decision != QMessageBox.StandardButton.Yes:
                return

        if info["dirty"]:
            decision = QMessageBox.question(
                self,
                "Alterações locais não confirmadas",
                "O repositório contém alterações sem commit. "
                "Os agentes usam worktrees criados a partir do último commit "
                "e NÃO receberão essas alterações automaticamente.\n\n"
                "Recomendado: cancelar, fazer commit das alterações seguras e tentar novamente.\n\n"
                "Deseja cadastrar mesmo assim?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if decision != QMessageBox.StandardButton.Yes:
                return

        project_id, ok = QInputDialog.getText(
            self, "Nome do projeto Multi Agents",
            "Identificador (letras minúsculas, números e hífen):",
            text=info["suggested_id"],
        )
        if not ok:
            return
        project_id = project_id.strip()
        if not project_id:
            QMessageBox.warning(self, "Adicionar projeto", "Informe um identificador.")
            return

        contract, ok = QInputDialog.getMultiLineText(
            self, "Objetivo do projeto",
            "Descreva o que os agentes precisam desenvolver:",
            "Desenvolver, testar e integrar melhorias no projeto "
            + info["suggested_id"] + " com três agentes e Git worktrees.",
        )
        if not ok:
            return

        try:
            result = register_folder(ROOT, project_id, info["repo"], contract)
        except (OSError, ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "Adicionar projeto", str(exc))
            return

        self.refresh()
        self.projects.setCurrentText(project_id)
        self.refresh()
        rag = result.get("rag_index", {})
        rag_message = (
            "Indexação RAG local iniciada em segundo plano."
            if rag.get("state") == "QUEUED" else
            "O projeto foi criado, mas a indexação não iniciou. Use rag-sync."
        )
        QMessageBox.information(
            self, "Projeto adicionado",
            "Projeto: " + project_id + "\n"
            "Pasta Git: " + result["project_folder"] + "\n"
            "Commit-base: " + result["git_initial_head"][:12] + "\n\n"
            + rag_message + "\n\n"
            "Agora selecione Vincular chat para associar o orquestrador e os agentes."
        )

    def pair_chat(self):
        project = self.projects.currentText()
        if not project:
            QMessageBox.information(self, "Multi Agents", "Clique em Adicionar pasta de projeto ou use init no terminal.")
            return
        actor, ok = QInputDialog.getItem(self, "Vincular chat", "Papel desta conversa", ROLES, editable=False)
        if not ok:
            return
        if QMessageBox.question(self, "Vincular chat", f"Gerar chave de {actor}? Uma chave anterior deste papel perde validade.") != QMessageBox.StandardButton.Yes:
            return
        try:
            with connect(ROOT, write=True) as db:
                token = provision(db, project, actor)
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Multi Agents", str(error))
            return
        protocol = (
            "Como orquestrador, decomponha a meta do usuario em tarefas pequenas e independentes. " \
            "Registre um plano com args [plan,--json,JSON] para simular "
            "as tarefas e [plan,--json,JSON,--apply] para registrar o plano aprovado. "
            "Defina IDs, brief, scope, depends, priority, effort e actor opcional; "
            "o escalonador valida dependencias e escopos antes da aplicacao. "
            "Tarefas antigas continuam manuais; use add --auto para novas tarefas avulsas. "
            "Consulte events --after CURSOR para acompanhar interfaces, bloqueios e progresso. "
            "Leia o handoff do heartbeat ou execute resume. "
            "Confira status, commits e resultados de integracao antes de encerrar. "
            if actor == "orquestrador" else
            "PROTOCOLO PADRAO: ao entrar no chat leia o handoff do heartbeat ou args resume. "
            "O CodeBridge mantem memoria individual e compartilhada e indexacao RAG local. "
            "Confirme a versao do contrato, branch e SHA256 do codigo antes de confiar em trechos RAG. "
            "Use next para assumir automaticamente a proxima tarefa do plano, "
            "ou claim para tarefas manuais. Ambos esperam dependencias ate 600 segundos. "
            "Publique avances parciais por args [event,--kind,progress,--task,ID,--text,TEXTO]; "
            "tipos: progress,interface,decision,blocker,result. "
            "Consulte events --after CURSOR e mantenha o cursor retornado. "
            "Se codebridge_exec retornar RUNNING, use codebridge_wait ate completar; "
            "se WAITING, repita a consulta quando necessario, sem inventar resultados. "
            "Execute no worktree, valide, faca commit e deliver. "
            "Depois do deliver, se next_task.state for ASSIGNED, continue a proxima "
            "tarefa imediatamente neste turno. Se IDLE, pode encerrar sem esperar outros. "
            "Nao prometa inicio automatico de um novo turno de ChatGPT. "
            "Registre detalhes com checkpoint, confira Git, contrato e RAG. "
        )
        QApplication.clipboard().setText(
            f"Use o CodeBridge no projeto {project}, como {actor}. "
            f"A chave desta conversa é {token}. Passe-a como --session antes do subcomando. "
            f"Use codebridge_exec com target MA:{project}:control e command JSON com session e args. "
            'Use args ["heartbeat"] para confirmar o vínculo. '
            + protocol +
            "Preserve a chave nesta conversa e mantenha-a fora da resposta."
        )
        self.refresh()

    def copy_instructions(self, actor):
        """Copy project + role instructions, without changing session credentials."""
        project = self.projects.currentText()
        if not project:
            QMessageBox.information(
                self, "Copiar instruções",
                "Selecione um projeto Multi Agents ou use Adicionar pasta de projeto."
            )
            return
        try:
            with connect(ROOT) as db:
                if db is None:
                    raise ValueError("O banco de projetos Multi Agents não está disponível.")
                project_row = db.execute(
                    "SELECT * FROM projects WHERE id=?", (project,)
                ).fetchone()
                if project_row is None:
                    raise ValueError("Projeto não encontrado. Atualize a lista.")
                instructions = build_instructions(dict(project_row), actor)
        except (OSError, ValueError, KeyError, sqlite3.Error) as error:
            QMessageBox.warning(self, "Copiar instruções", str(error))
            return
        QApplication.clipboard().setText(instructions)
        QMessageBox.information(
            self, "Instruções copiadas",
            "As instruções de " + ROLE_NAMES[actor] + " para o projeto "
            + project + " foram copiadas.\n\n"
            "Cole no chat desse papel, se já estiver vinculado. "
            "Para um chat novo, use Vincular chat primeiro. "
            "Esta ação NÃO criou nem alterou qualquer chave de sessão."
        )
