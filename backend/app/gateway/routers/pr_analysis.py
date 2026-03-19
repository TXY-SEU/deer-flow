"""PR test-suggestion API: run lead_agent via LangGraph Server with fetch_pr_diff + test-suggestion skill."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from langgraph_sdk import get_client
from pydantic import BaseModel, HttpUrl, TypeAdapter

from app.gateway.config import get_gateway_config
from app.gateway.langgraph_state import extract_final_ai_text
from app.gateway.routers.suggestions import _strip_markdown_code_fence

logger = logging.getLogger(__name__)

router = APIRouter(tags=["PR Analysis"])

LEAD_ASSISTANT_ID = "lead_agent"
DEFAULT_RUN_CONFIG: dict[str, object] = {"recursion_limit": 100}
DEFAULT_RUN_CONTEXT_BASE: dict[str, object] = {
    "thinking_enabled": True,
    "is_plan_mode": False,
    "subagent_enabled": False,
}


class PRAnalysisRequest(BaseModel):
    pr_url: HttpUrl


class TestSuggestionItem(BaseModel):
    id: str
    codeModification: str
    impact: str
    suggestion: list[str]


_test_suggestion_list_adapter = TypeAdapter(list[TestSuggestionItem])


def _github_pr_host_allowed(url: str) -> bool:
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in ("github.com", "www.github.com")


def _parse_test_suggestion_json_array(text: str) -> list[object]:
    """Parse a JSON array from model output; uses JSONDecoder.raw_decode from first '['."""
    cleaned = _strip_markdown_code_fence(text.strip())
    start = cleaned.find("[")
    if start == -1:
        msg = "No JSON array start '[' in model output"
        raise ValueError(msg)
    decoder = json.JSONDecoder()
    try:
        data, _end = decoder.raw_decode(cleaned, start)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON: {e}"
        raise ValueError(msg) from e
    if not isinstance(data, list):
        msg = "Top-level JSON value is not an array"
        raise TypeError(msg)
    return data


def _build_user_prompt(pr_url: str) -> str:
    # 定义一个符合接口要求的最小 JSON 示例
    example_output = """
[
  {
    "id": "1",
    "codeModification": "用户认证服务新增token刷新机制",
    "impact": "影响用户会话持续性和API访问安全性",
    "suggestion": ["token过期自动刷新机制验证", "刷新失败引导重新登录", "并发刷新只执行一次", "refresh接口参数响应验证", "连续1000次刷新响应时间<500ms", "过期token返回403防劫持", "刷新后旧token立即失效", "刷新操作日志记录"]
  },
  {
    "id": "2",
    "codeModification": "refresh_tokens表新增",
    "impact": "影响token刷新和用户登录状态管理",
    "suggestion": ["登录后token正确插入", "重复插入触发唯一约束", "刷新后记录更新", "登出后token删除", "过期记录自动清理", "多设备独立刷新", "外键约束验证", "查询性能<50ms@100万条"]
  }
]
"""
    
    return f"""Please analyze the following PR: {pr_url}

INSTRUCTIONS:
1. Use the `fetch_pr_diff` tool to get the git diff output for this PR.
2. Act strictly according to the `test-suggestion` skill injected in your instructions.
3. Follow the workflow: Parse diff -> Identify changes -> Assess impact -> Generate tests.

CRITICAL OUTPUT FORMAT REQUIREMENTS:
- Your entire response must be a single, valid JSON array.
- Each item in the array must be an object with exactly these 4 keys: "id", "codeModification", "impact", "suggestion".
- The "id" should be a unique string identifier for the suggestion.
- The "codeModification" should describe what code was changed.
- The "impact" should describe the potential effect or importance of the change.
- The "suggestion" must be an array of strings, each string being a specific test suggestion.
- Do NOT use any other field names such as "test_type", "title", "description", "priority", etc.
- Do NOT wrap the JSON in markdown code fences (```json ... ```).

Here is the EXACT format you MUST follow:
{example_output}

Output only the JSON array, nothing else.
"""


@router.post("/api/pr-test-suggestion", response_model=list[TestSuggestionItem])
async def generate_pr_test_suggestion(req: PRAnalysisRequest) -> list[TestSuggestionItem]:
    if not _github_pr_host_allowed(str(req.pr_url)):
        raise HTTPException(
            status_code=400,
            detail="Only https://github.com/.../pull/... URLs are supported.",
        )

    gw = get_gateway_config()
    client = get_client(url=gw.langgraph_url)
    pr_url_str = str(req.pr_url).rstrip("/")
    prompt = _build_user_prompt(pr_url_str)

    try:
        thread = await client.threads.create()
    except Exception as e:
        logger.exception("Failed to create LangGraph thread: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create agent thread") from e

    thread_id = thread.get("thread_id")
    if not thread_id:
        raise HTTPException(status_code=500, detail="LangGraph thread response missing thread_id")

    run_context = {**DEFAULT_RUN_CONTEXT_BASE, "thread_id": thread_id}

    try:
        result = await asyncio.wait_for(
            client.runs.wait(
                thread_id,
                LEAD_ASSISTANT_ID,
                input={"messages": [{"role": "human", "content": prompt}]},
                config=DEFAULT_RUN_CONFIG,
                context=run_context,
            ),
            timeout=gw.pr_test_suggestion_timeout_seconds,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"PR analysis exceeded {gw.pr_test_suggestion_timeout_seconds}s timeout.",
        ) from None
    except Exception as e:
        logger.exception("LangGraph runs.wait failed: %s", e)
        raise HTTPException(status_code=500, detail="Agent execution failed") from e

    final_text = extract_final_ai_text(result)
    if not final_text.strip():
        raise HTTPException(
            status_code=502,
            detail="Agent returned no assistant text to parse.",
        )

    try:
        raw_list = _parse_test_suggestion_json_array(final_text)
        validated = _test_suggestion_list_adapter.validate_python(raw_list)
    except Exception as e:
        logger.warning("Failed to parse test suggestions: %s", e)
        preview = final_text[:500] + ("…" if len(final_text) > 500 else "")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to parse agent output as test-suggestion JSON. {e!s}. Preview: {preview}",
        ) from e

    return validated
