from __future__ import annotations

import json

import pytest

from moviexfone.voice.loop import FILLER_TEXT, ToolLoop


async def _ok(_name: str, args: dict[str, object]) -> dict[str, object]:
    return {"echo": args, "speak": "At Regal, seven."}


def _call(call_id: str, name: str = "search_showtimes", **args: object) -> dict[str, object]:
    return {
        "type": "response.function_call_arguments.done",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _done() -> dict[str, object]:
    return {"type": "response.done", "response": {"id": "resp-answer"}}


@pytest.mark.asyncio
async def test_batches_every_call_into_one_response_create() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    async def executor(name: str, args: dict[str, object]) -> dict[str, object]:
        seen.append((name, args))
        return {"ok": True, "speak": "seven"}

    loop = ToolLoop(executor)
    assert await loop.handle_server_event(_call("a", location="Union Square")) == []
    assert await loop.handle_server_event(_call("b", name="get_movie_info", movie="Verity")) == []
    assert seen == []
    outbound = await loop.handle_server_event(_done())
    assert [item["type"] for item in outbound] == [
        "conversation.item.create",
        "conversation.item.create",
        "conversation.item.create",
    ]
    assert outbound[2]["item"]["type"] == "force_message"
    assert len(seen) == 2
    outputs = [
        item["item"]
        for item in outbound
        if item.get("item", {}).get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in outputs] == ["a", "b"]


@pytest.mark.asyncio
async def test_second_round_runs_and_third_is_capped() -> None:
    calls = {"n": 0}

    async def executor(_name: str, _args: dict[str, object]) -> dict[str, object]:
        calls["n"] += 1
        return {"ok": True}

    loop = ToolLoop(executor, max_rounds=2)
    await loop.handle_server_event(_call("1", location="Queens"))
    await loop.handle_server_event(_done())
    await loop.handle_server_event(_call("2", location="Brooklyn"))
    await loop.handle_server_event(_done())
    await loop.handle_server_event(_call("3", location="Bronx"))
    outbound = await loop.handle_server_event(_done())
    assert calls["n"] == 2
    body = json.loads(outbound[0]["item"]["output"])
    assert body["error"] == "tool_call_limit"
    assert sum(1 for item in outbound if item["type"] == "response.create") == 1


@pytest.mark.asyncio
async def test_answer_without_tools_does_not_create_another_response() -> None:
    loop = ToolLoop(_ok)
    outbound = await loop.handle_server_event(_done())
    assert outbound == []


@pytest.mark.asyncio
async def test_slow_tool_sends_filler_before_the_single_response_create() -> None:
    import asyncio

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def executor(_name: str, _args: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"ok": True}

    loop = ToolLoop(executor, filler_after_s=0.01, send=send)
    await loop.handle_server_event(_call("1", location="SoHo"))
    outbound = await loop.handle_server_event(_done())
    assert loop.filler_sent == 1
    assert sent[0]["item"]["type"] == "force_message"
    assert sent[0]["item"]["content"][0]["text"] == FILLER_TEXT
    assert FILLER_TEXT != "Let me check"
    assert sum(1 for item in outbound if item["type"] == "response.create") == 1


@pytest.mark.asyncio
async def test_playback_wait_only_when_audio_already_started() -> None:
    waits = {"n": 0}

    async def wait() -> None:
        waits["n"] += 1

    loop = ToolLoop(_ok, wait_playback=wait)
    await loop.handle_server_event({"type": "response.output_audio.delta", "delta": "aaaa"})
    await loop.handle_server_event(_call("1", location="Chelsea"))
    await loop.handle_server_event(_done())
    assert waits["n"] == 1

    loop2 = ToolLoop(_ok, wait_playback=wait)
    await loop2.handle_server_event(_call("1", location="Chelsea"))
    await loop2.handle_server_event(_done())
    assert waits["n"] == 1
