"""LLM Tool Schema（接口规范 §2.2，MVP 4 个工具）。

状态机按节点控制可用工具——不允许模型在同一轮自由选择（接口规范 §2.3）。
"""
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_schema",
            "description": "根据用户问题检索相关的数据表、字段及其业务口径。生成 SQL 前必须先调用本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户数据需求的中文描述"},
                    "top_k": {"type": "integer", "description": "返回表数量上限，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_caliber",
            "description": "查询业务指标的精确定义和计算口径。涉及指标时必须调用，禁止凭记忆回答口径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string", "description": "指标中文名，如：日活跃用户数"},
                },
                "required": ["metric_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_sql",
            "description": "对生成的 SQL 执行静态校验：语法、写操作检测、表白名单、字段存在性。返回每项检查结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "待校验的 SQL 语句"},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_readonly_sql",
            "description": "执行一条只读 SELECT 查询并返回结果（自动截断）。仅可执行已通过 validate_sql 的语句。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "已通过校验的 SELECT 语句"},
                    "reason": {"type": "string", "description": "本次查询意图，写入审计日志"},
                },
                "required": ["sql", "reason"],
            },
        },
    },
]

# 节点 → 可用工具白名单
NODE_TOOLS: dict[str, list[str]] = {
    "RECALL": ["search_schema", "get_caliber"],
    "GENERATE": [],
    "VALIDATE": ["validate_sql"],
    "EXECUTE": ["execute_readonly_sql"],
}


def tools_for_node(node: str) -> list[dict]:
    names = NODE_TOOLS.get(node, [])
    return [t for t in TOOLS if t["function"]["name"] in names]
