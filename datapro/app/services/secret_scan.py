"""上传内容密钥扫描（告警不拦截）：识别疑似凭据并在文件条目上打标。

为什么只告警不拦截：上传是正常协作行为，文本里出现 API Key 可能是测试样例、
教程片段；直接拒绝会误伤。但密钥一旦落盘且可下载就是泄漏面（已有真实案例：
用户上传的 API.txt 含 2 条 sk- Key 且历史文件无归属、任何登录用户可下载），
所以打标 + 告警 + 列表可见，处置权交给上传者与管理员。

覆盖范围：所有文本类内容（编码按 utf-8/gb18030/latin-1 尝试）；xlsx 是 zip 容器，
原始字节扫不到单元格文本，解析后的单元格内容不在此扫描范围（见 _scan_cell_text）。
"""

import re

# 规则按误报风险从低到高排列；snippet 输出前会做脱敏，绝不出完整 Key
_RULES: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY( BLOCK)?-----")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("sk_style_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),          # OpenAI/DeepSeek 风格
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("credential_assignment", re.compile(
        r"(?:api[_-]?key|apikey|access[_-]?token|secret|password|passwd|pwd|bearer)\s*[:=]\s*"
        r"['\"]?([A-Za-z0-9_\-.+/]{16,})", re.IGNORECASE)),
]

_MAX_HITS_PER_RULE = 3
_MAX_SNIPPET = 24


def _mask(value: str) -> str:
    """片段脱敏：保留前 8 个字符供辨认，其余打码。"""
    head = value[:8]
    return head + "…" if len(value) > 8 else value


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="ignore")      # 二进制也不炸


def scan_text(text: str) -> list[dict]:
    """扫描文本，返回去重后的命中列表：[{rule, count, snippet}]（snippet 已脱敏）。"""
    hits: list[dict] = []
    for rule, pattern in _RULES:
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        # 片段取第一个命中的匹配文本（cred 规则取捕获组里的值）
        first = matches[0]
        value = first.group(1) if first.lastindex else first.group(0)
        snippet = _mask(value.strip(" '\"")[:_MAX_SNIPPET])
        line = text.count("\n", 0, first.start()) + 1
        hits.append({"rule": rule, "count": len(matches), "snippet": snippet, "line": line})
        if len(hits) >= _MAX_HITS_PER_RULE:
            break
    return hits


def scan(raw: bytes) -> list[dict]:
    """扫描文件原始字节（文本类；二进制内容一般无命中，不报错）。"""
    if not raw:
        return []
    return scan_text(_decode(raw))


def describe(hits: list[dict]) -> str:
    """给人看的告警摘要，如「sk_style_key×2、credential_assignment×1」。"""
    return "、".join(f"{h['rule']}×{h['count']}" for h in hits)
