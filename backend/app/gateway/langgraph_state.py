"""Extract displayable text from LangGraph ``runs.wait`` / final state payloads."""

from __future__ import annotations

from typing import Any


def extract_final_ai_text(result: dict[str, Any] | list[Any]) -> str:
    """Extract the last AI message text from a LangGraph run result (state dict or messages list).

    Walks backwards through ``messages`` and stops at the last human message so we
    only consider the current turn. Matches channel manager behavior.
    """
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        messages = result.get("messages", [])
    else:
        return ""

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")

        if msg_type == "human":
            break

        if msg_type == "tool" and msg.get("name") == "ask_clarification":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content

        if msg_type == "ai":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts)
                if text:
                    return text
    return ""
