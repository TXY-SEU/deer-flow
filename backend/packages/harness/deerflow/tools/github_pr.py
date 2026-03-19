import os
import httpx
from langchain_core.tools import tool

# ==========================================
# Diff 压缩引擎配置 (借鉴 pr-agent 优化策略)
# ==========================================

# 全局最大字符数限制 (100,000 字符约等于 2.5万 Token，对当前主流模型非常安全)
MAX_GLOBAL_CHARS = 100000 
# 单个文件的最大字符数限制 (防止某个超大业务文件把配额全部吃光)
MAX_FILE_CHARS = 20000    

# 低价值文件后缀黑名单 (丢弃纯数据、自动生成、依赖锁定等无关文件)
IGNORED_SUFFIXES = (
    ".lock", ".svg", ".png", ".jpg", ".jpeg", ".gif", 
    ".csv", ".min.js", ".min.css", ".map", "go.sum",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml"
)

def _compress_diff(raw_diff: str) -> str:
    """
    智能压缩超大 Git Diff，保障核心上下文并防止 Token 超限。
    策略：
    1. 快速放行：如果总体积很小，直接返回。
    2. 文件过滤：剥离依赖锁文件、图片和压缩脚本。
    3. 大文件截断 (Clip)：单个文件超长时，截断并保留文件名。
    4. 宏观补偿 (Macro-context)：由于超限被丢弃的文件，将其文件名作为系统提示附在末尾。
    """
    if len(raw_diff) <= MAX_GLOBAL_CHARS:
        return raw_diff  # 快速放行

    # Git diff 通常以 "diff --git a/... b/..." 作为每个文件的分隔
    parts = ("\n" + raw_diff).split("\ndiff --git ")
    if len(parts) <= 1:
        # 兜底：如果解析失败，使用简单粗暴的全局截断
        return raw_diff[:MAX_GLOBAL_CHARS] + "\n\n[SYSTEM: Diff truncated globally due to extreme length]"

    processed_chunks = []
    current_len = 0
    filtered_files =[]
    skipped_files = []
    clipped_files = []

    # parts[0] 通常是空的或是 commit message preamble
    preamble = parts[0].lstrip()
    if preamble:
        processed_chunks.append(preamble)
        current_len += len(preamble)

    # 逐个文件块处理
    for part in parts[1:]:
        chunk = "diff --git " + part
        
        # 提取文件名 (例如 "a/backend/app.py b/backend/app.py")
        first_line = part.split("\n", 1)[0]
        # 通过 " b/" 分割来获取最终文件名
        filename = first_line.split(" b/")[-1] if " b/" in first_line else first_line.split(" ")[-1]

        # 策略 1：过滤低价值文件 (Noise Reduction)
        if filename.endswith(IGNORED_SUFFIXES):
            filtered_files.append(filename)
            continue

        chunk_len = len(chunk)

        # 策略 2：单文件大补丁截断 (Clip)
        if chunk_len > MAX_FILE_CHARS:
            chunk = chunk[:MAX_FILE_CHARS] + f"\n\n...[SYSTEM: Rest of the diff for '{filename}' was clipped due to file size limit]"
            chunk_len = len(chunk)
            clipped_files.append(filename)

        # 策略 3：全局 Token 预算控制 (Hard Stop)
        if current_len + chunk_len > MAX_GLOBAL_CHARS:
            skipped_files.append(filename)
            continue

        processed_chunks.append(chunk)
        current_len += chunk_len

    # 组装最终安全的 Diff 内容
    final_diff = "\n".join(processed_chunks)
    
    # 策略 4：宏观上下文补偿机制 (Macro-context Preservation)
    if filtered_files or skipped_files or clipped_files:
        summary = "\n\n" + "="*50 + "\n"
        summary += "SYSTEM NOTICE: The original PR diff was extremely large and has been optimized.\n"
        if filtered_files:
            summary += f"- Filtered (Low value/Auto-generated): {', '.join(filtered_files)}\n"
        if clipped_files:
            summary += f"- Clipped (Too large, partially shown): {', '.join(clipped_files)}\n"
        if skipped_files:
            summary += f"- Skipped (Context limit reached, code omitted): {', '.join(skipped_files)}\n"
        summary += "\nEven if the code for some files is omitted or clipped, please CONSIDER THEIR FILENAMES when assessing the overall BUSINESS IMPACT and generating test suggestions.\n"
        summary += "="*50 + "\n"
        
        final_diff += summary

    return final_diff


def _github_auth_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


@tool
def fetch_pr_diff(pr_url: str) -> str:
    """
    Fetch the git diff content from a given GitHub Pull Request URL.
    Input should be a valid GitHub PR URL (e.g., https://github.com/owner/repo/pull/123).
    """
    # 移除可能存在的尾部斜杠
    pr_url = pr_url.rstrip("/")
    
    # GitHub 提供了一个便捷的特性：在 PR URL 后加上 .diff 即可获取 raw diff
    diff_url = f"{pr_url}.diff"
    
    try:
        # 使用 httpx 获取 diff，跟随重定向
        with httpx.Client(follow_redirects=True) as client:
            response = client.get(diff_url, headers=_github_auth_headers())
            response.raise_for_status()
            diff_content = response.text
            
            if not diff_content.strip():
                return "Error: PR diff is empty or URL is invalid."
            
            # 【核心修改点】：通过引擎压缩原始 Diff
            compressed_diff = _compress_diff(diff_content)
            return compressed_diff
            
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return "Error: PR not found. It may be private and missing an authorization token."
        return f"HTTP Error fetching PR diff: {e.response.status_code}"
    except Exception as e:
        return f"Failed to fetch PR diff: {str(e)}"