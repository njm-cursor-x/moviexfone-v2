"""Batched realtime tool loop.

Collect every function call until response.done, run them, send every
function_call_output, then send one response.create. Two tool rounds per
user turn. A slow tool may speak a one-line filler; there is no preamble.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

Executor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Sender = Callable[[dict[str, Any]], Awaitable[None]]

LIMIT_RESULT = {
    "error": "tool_call_limit",
    "note": "Answer with what you have or ask one question.",
}
FILLER_TEXT = "One sec."


class ToolLoop:
    def __init__(
        self,
        executor: Executor,
        *,
        max_rounds: int = 2,
        filler_after_s: float = 0.7,
        send: Sender | None = None,
        wait_playback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.executor = executor
        self.max_rounds = max_rounds
        self.filler_after_s = filler_after_s
        self._send = send
        self.wait_playback = wait_playback
        self.rounds = 0
        self.executed_calls = 0
        self._pending: list[tuple[str, str, dict[str, Any]]] = []
        self._audio_bytes = 0
        self._filler_response_id: str | None = None
        self._awaiting_filler_id = False
        self.filler_sent = 0

    async def handle_server_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "response.created" and self._awaiting_filler_id:
            response = event.get("response")
            if isinstance(response, dict):
                self._filler_response_id = str(response.get("id") or "")
            self._awaiting_filler_id = False
            return []
        if self._is_filler(event):
            if kind == "response.done":
                self._filler_response_id = None
            return []
        if kind == "response.output_audio.delta":
            delta = event.get("delta") or ""
            if isinstance(delta, str):
                self._audio_bytes += len(delta)
            return []
        if kind == "response.function_call_arguments.done":
            arguments = event.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    parsed = {}
            elif isinstance(arguments, dict):
                parsed = arguments
            else:
                parsed = {}
            self._pending.append(
                (str(event.get("call_id") or ""), str(event.get("name") or ""), parsed)
            )
            return []
        if kind == "input_audio_buffer.speech_started":
            self.rounds = 0
            self._pending = []
            return [{"type": "response.cancel"}]
        if kind == "response.done":
            if not self._pending:
                self._audio_bytes = 0
                self.rounds = 0
                return []
            return await self._submit()
        return []

    def _is_filler(self, event: dict[str, Any]) -> bool:
        if not self._filler_response_id:
            return False
        if event.get("response_id") == self._filler_response_id:
            return True
        response = event.get("response")
        return isinstance(response, dict) and response.get("id") == self._filler_response_id

    async def _submit(self) -> list[dict[str, Any]]:
        batch = self._pending
        self._pending = []
        self.rounds += 1
        had_audio = self._audio_bytes > 0
        self._audio_bytes = 0
        if self.rounds > self.max_rounds:
            results = [(call_id, LIMIT_RESULT) for call_id, _name, _args in batch]
        else:
            results = await self._run(batch)
        messages = [_output_message(call_id, result) for call_id, result in results]
        if had_audio and self.wait_playback is not None:
            await self.wait_playback()
        if self.rounds > self.max_rounds:
            messages.append({"type": "response.create", "response": {"tool_choice": "none"}})
        else:
            messages.append({"type": "response.create"})
        return messages

    async def _run(
        self, batch: list[tuple[str, str, dict[str, Any]]]
    ) -> list[tuple[str, dict[str, Any]]]:
        async def one(item: tuple[str, str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
            call_id, name, args = item
            self.executed_calls += 1
            try:
                result = await self.executor(name, args)
            except Exception:
                result = {
                    "error": "tool_failed",
                    "note": "Say you could not look that up. Do not invent times.",
                }
            return call_id, result

        async def run_all() -> list[tuple[str, dict[str, Any]]]:
            return list(await asyncio.gather(*(one(item) for item in batch)))

        task = asyncio.create_task(run_all())
        try:
            return list(await asyncio.wait_for(asyncio.shield(task), self.filler_after_s))
        except TimeoutError:
            await self._emit_filler()
            return list(await task)

    async def _emit_filler(self) -> None:
        self.filler_sent += 1
        self._awaiting_filler_id = True
        message = {
            "type": "conversation.item.create",
            "item": {
                "type": "force_message",
                "role": "assistant",
                "interruptible": True,
                "content": [{"type": "output_text", "text": FILLER_TEXT}],
            },
        }
        if self._send is not None:
            await self._send(message)


def _output_message(call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result),
        },
    }
