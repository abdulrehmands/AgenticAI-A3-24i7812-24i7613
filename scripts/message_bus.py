"""Message bus — in-process dict (default) or optional Redis lists + PUBLISH pub/sub."""
from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

AGENTS = frozenset({"ceo", "product", "engineer", "marketing", "qa"})
MESSAGE_TYPES = frozenset({"task", "result", "revision_request", "confirmation", "failure"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _msg_dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _msg_loads(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def validate_message(msg: dict[str, Any]) -> None:
    required = ("message_id", "from_agent", "to_agent", "message_type", "payload", "timestamp")
    for k in required:
        if k not in msg:
            raise ValueError(f"message missing required field: {k}")
    if msg["from_agent"] not in AGENTS:
        raise ValueError(f"invalid from_agent: {msg['from_agent']}")
    if msg["to_agent"] not in AGENTS:
        raise ValueError(f"invalid to_agent: {msg['to_agent']}")
    if msg["message_type"] not in MESSAGE_TYPES:
        raise ValueError(f"invalid message_type: {msg['message_type']}")
    if not isinstance(msg["payload"], dict):
        raise ValueError("payload must be an object")
    if "parent_message_id" in msg and msg["parent_message_id"] is not None:
        if not isinstance(msg["parent_message_id"], str):
            raise ValueError("parent_message_id must be a string")


def _trace_send(from_agent: str, to_agent: str, message_type: str, msg: dict[str, Any], payload: dict[str, Any]) -> None:
    if os.environ.get("LAUNCHMIND_TRACE_BUS", "1").lower() not in ("1", "true", "yes"):
        return
    pkeys = list(payload.keys())
    ellipsis = "..." if len(pkeys) > 8 else ""
    print(
        f"[BUS] {from_agent} -> {to_agent} | {message_type} | id={msg['message_id']} "
        f"| ts={msg['timestamp']} | payload_keys={pkeys[:8]}{ellipsis}"
    )


class MessageBus:
    def __init__(self) -> None:
        self._queues: dict[str, list[dict[str, Any]]] = {a: [] for a in AGENTS}
        self._history: list[dict[str, Any]] = []

    def send(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        payload: dict[str, Any],
        *,
        parent_message_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        msg = {
            "message_id": message_id or str(uuid.uuid4()),
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message_type": message_type,
            "payload": copy.deepcopy(payload),
            "timestamp": _now_iso(),
        }
        if parent_message_id is not None:
            msg["parent_message_id"] = parent_message_id
        validate_message(msg)
        self._queues[to_agent].append(copy.deepcopy(msg))
        self._history.append(copy.deepcopy(msg))
        _trace_send(from_agent, to_agent, message_type, msg, payload)
        return copy.deepcopy(msg)

    def peek(self, agent: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._queues.get(agent, []))

    def pop_for(self, agent: str) -> dict[str, Any] | None:
        q = self._queues.get(agent, [])
        if not q:
            return None
        return copy.deepcopy(q.pop(0))

    def pop_matching(
        self,
        agent: str,
        *,
        from_agent: str | None = None,
        message_type: str | None = None,
    ) -> dict[str, Any] | None:
        q = self._queues.get(agent, [])
        for i, m in enumerate(q):
            if from_agent is not None and m["from_agent"] != from_agent:
                continue
            if message_type is not None and m["message_type"] != message_type:
                continue
            removed = q.pop(i)
            return copy.deepcopy(removed)
        return None

    def drain_for(self, agent: str) -> list[dict[str, Any]]:
        q = self._queues.get(agent, [])
        out = copy.deepcopy(q)
        q.clear()
        return out

    def full_history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._history)

    def history_for_agent(self, agent: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(m) for m in self._history if m["from_agent"] == agent or m["to_agent"] == agent]

    def pretty_log(self) -> str:
        lines = []
        for m in self._history:
            lines.append(json.dumps(m, indent=2))
        return "\n---\n".join(lines)


class RedisMessageBus:
    """Redis LIST per agent (FIFO) + history list; PUBLISH on each send for pub/sub observers."""

    def __init__(self, url: str, prefix: str = "launchmind") -> None:
        import redis

        self._r = redis.from_url(url, decode_responses=True)
        self._prefix = prefix

    def _qkey(self, agent: str) -> str:
        return f"{self._prefix}:queue:{agent}"

    def _hkey(self) -> str:
        return f"{self._prefix}:history"

    def _pub_channel(self) -> str:
        return f"{self._prefix}:bus"

    def _load_queue(self, agent: str) -> list[dict[str, Any]]:
        raw = self._r.lrange(self._qkey(agent), 0, -1)
        return [_msg_loads(s) for s in raw]

    def _save_queue(self, agent: str, msgs: list[dict[str, Any]]) -> None:
        key = self._qkey(agent)
        pipe = self._r.pipeline()
        pipe.delete(key)
        for m in msgs:
            pipe.rpush(key, _msg_dumps(m))
        pipe.execute()

    def send(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        payload: dict[str, Any],
        *,
        parent_message_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        msg = {
            "message_id": message_id or str(uuid.uuid4()),
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message_type": message_type,
            "payload": copy.deepcopy(payload),
            "timestamp": _now_iso(),
        }
        if parent_message_id is not None:
            msg["parent_message_id"] = parent_message_id
        validate_message(msg)
        canonical = copy.deepcopy(msg)
        blob = _msg_dumps(canonical)
        pipe = self._r.pipeline()
        pipe.rpush(self._qkey(to_agent), blob)
        pipe.rpush(self._hkey(), blob)
        pipe.publish(self._pub_channel(), blob)
        pipe.execute()
        _trace_send(from_agent, to_agent, message_type, msg, payload)
        return copy.deepcopy(msg)

    def peek(self, agent: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._load_queue(agent))

    def pop_for(self, agent: str) -> dict[str, Any] | None:
        raw = self._r.lpop(self._qkey(agent))
        if raw is None:
            return None
        return copy.deepcopy(_msg_loads(raw))

    def pop_matching(
        self,
        agent: str,
        *,
        from_agent: str | None = None,
        message_type: str | None = None,
    ) -> dict[str, Any] | None:
        key = self._qkey(agent)
        raw_list = self._r.lrange(key, 0, -1)
        if not raw_list:
            return None
        idx: int | None = None
        chosen: dict[str, Any] | None = None
        for i, raw in enumerate(raw_list):
            m = _msg_loads(raw)
            if from_agent is not None and m["from_agent"] != from_agent:
                continue
            if message_type is not None and m["message_type"] != message_type:
                continue
            idx = i
            chosen = m
            break
        if idx is None or chosen is None:
            return None
        remaining = raw_list[:idx] + raw_list[idx + 1 :]
        pipe = self._r.pipeline()
        pipe.delete(key)
        for raw in remaining:
            pipe.rpush(key, raw)
        pipe.execute()
        return copy.deepcopy(chosen)

    def drain_for(self, agent: str) -> list[dict[str, Any]]:
        key = self._qkey(agent)
        raw_list = self._r.lrange(key, 0, -1)
        if not raw_list:
            return []
        self._r.delete(key)
        return [copy.deepcopy(_msg_loads(s)) for s in raw_list]

    def full_history(self) -> list[dict[str, Any]]:
        raw = self._r.lrange(self._hkey(), 0, -1)
        return [copy.deepcopy(_msg_loads(s)) for s in raw]

    def history_for_agent(self, agent: str) -> list[dict[str, Any]]:
        return [m for m in self.full_history() if m["from_agent"] == agent or m["to_agent"] == agent]

    def pretty_log(self) -> str:
        lines = [json.dumps(m, indent=2) for m in self.full_history()]
        return "\n---\n".join(lines)


Bus = MessageBus | RedisMessageBus


def get_message_bus() -> Bus:
    url = (os.environ.get("LAUNCHMIND_REDIS_URL") or os.environ.get("REDIS_URL") or "").strip()
    if url:
        prefix = os.environ.get("LAUNCHMIND_REDIS_PREFIX", "launchmind").strip() or "launchmind"
        return RedisMessageBus(url, prefix=prefix)
    return MessageBus()
