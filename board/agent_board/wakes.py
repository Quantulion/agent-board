from __future__ import annotations

from typing import Dict, Optional, Tuple

from .model import Event
from .store import PROJECT_THREAD, ProjectStore


class WakeContext:
    """Дополняет причины agent_wake подробностями из соседних событий и тредов (контракт prompts.wake_message).

    События должны подаваться строго по порядку seq: причина берёт данные из событий, записанных перед ней.
    """

    def __init__(self):
        self._last: Dict[Tuple[str, str, str], str] = {}

    def observe(self, slug: str, event: Event) -> None:
        d = event.data
        if event.type == "message_posted":
            self._last[(slug, "message", str(d.get("thread")))] = str(d.get("message_id"))
        elif event.type == "question_answered":
            self._last[(slug, "question", str(d.get("key")))] = str(d.get("question_id"))
        elif event.type == "proposal_rejected":
            self._last[(slug, "proposal", "")] = str(d.get("id"))

    def enrich(self, slug: str, store: ProjectStore, event: Event) -> dict:
        r = dict(event.data)
        reason = r.get("reason")
        key = str(r.get("key"))
        if reason == "message":
            # комментарий пользователя к задаче лежит в треде задачи, а будит агента проекта
            self._add_message(r, store, str(r.get("task") or key), r.get("message_id"))
        elif reason == "escalation":
            self._add_message(r, store, PROJECT_THREAD, self._last.get((slug, "message", PROJECT_THREAD)))
        elif reason == "answer":
            self._add_answer(r, store, key, self._last.pop((slug, "question", key), None))
        elif reason == "proposal_rejected" and not r.get("id"):
            pid = self._last.get((slug, "proposal", ""))
            if pid:
                r["id"] = pid
        return r

    @staticmethod
    def _add_message(r: dict, store: ProjectStore, thread: str, message_id: Optional[str]) -> None:
        if not message_id:
            return
        for m in reversed(store.read_thread(thread)):
            if m.id == message_id:
                r["message_id"] = m.id
                r["text"] = m.text
                r["author"] = m.author
                return

    @staticmethod
    def _add_answer(r: dict, store: ProjectStore, key: str, question_id: Optional[str]) -> None:
        if not question_id:
            return
        task = store.load_tasks().get(key)
        question = next((q for q in task.questions if q.id == question_id), None) if task else None
        r["question_id"] = question_id
        if question is None:
            return
        r["question"] = question.text
        answer = question.answer or {}
        if answer.get("choice"):
            r["choice"] = answer["choice"]
        if answer.get("comment"):
            r["comment"] = answer["comment"]
