"""AI 报价导入（可选功能）。

边界（需求 §4.5）：
- 没有 API Key 时整块功能不渲染，手动录入是默认路径，永远可用
- 模型只做字段抽取和归一化，输出结构化报价行
- 缺失或含义不清的字段留空，不猜
- 币种换算、总价校验、排序、最低价判断全部留在确定性代码里
- 抽取结果必须经用户确认才入库，修正痕迹保留（edited_by_user / raw_payload）

本模块只负责「读文件 → 调模型 → 解析 JSON → 结构校验」，
不写库、不折算、不做任何业务判断。
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

from . import config, contract_rules

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

EXCEL_SUFFIXES = (".xlsx", ".xlsm")

QUOTE_FIELDS = (
    "supplier_name",
    "item_name",
    "spec",
    "quantity",
    "unit",
    "unit_price",
    "currency",
    "lead_time_days",
    "payment_terms",
    "valid_until",
    "note",
)

SYSTEM_PROMPT = """你是采购报价单据的信息抽取器。只做字段搬运，不做计算、不做推测。

从用户给的文件内容里抽取所有报价行，输出严格的 JSON 数组，不要任何解释文字。

每个元素包含这些字段（找不到就填 null）：
- supplier_name: 供应商名称
- item_name: 物料或服务名称
- spec: 规格型号
- quantity: 数量（纯数字）
- unit: 单位
- unit_price: 单价（纯数字，去掉币种符号和千分位）
- currency: 币种代码，如 CNY / USD / EUR
- lead_time_days: 交期天数（整数）
- payment_terms: 付款条件原文
- valid_until: 报价有效期，格式 YYYY-MM-DD
- note: 其它值得记录的信息

硬性规则：
1. 只输出 JSON 数组本身，不要 markdown 代码块，不要前后说明。
2. 原文里没有、或者含义不清楚的字段，一律填 null，绝对不要推测或补全。
3. 金额原样搬运，不要做任何汇率换算或加减。
4. 不要合并行，不要编造原文没有的报价行。
"""


class ImportError_(RuntimeError):
    """导入过程中的可读错误。"""


def _cfg() -> dict:
    return dict(config.get("ai_import", {}) or {})


def api_key() -> str | None:
    env_name = _cfg().get("api_key_env") or "OPENAI_API_KEY"
    value = os.environ.get(env_name)
    return value.strip() if value and value.strip() else None


def is_enabled() -> bool:
    """三项都齐才算启用：开关打开 + base_url/model 配好 + 环境变量里有 Key。"""
    cfg = _cfg()
    if not cfg.get("enabled"):
        return False
    if not (cfg.get("base_url") and cfg.get("model")):
        return False
    return api_key() is not None


def disabled_reason() -> str:
    cfg = _cfg()
    if not cfg.get("enabled"):
        return "config.yaml 里 ai_import.enabled 为 false"
    if not cfg.get("base_url") or not cfg.get("model"):
        return "config.yaml 里 ai_import.base_url / model 没配全"
    env_name = cfg.get("api_key_env") or "OPENAI_API_KEY"
    return f"环境变量 {env_name} 里没有 API Key"


def _excel_to_text(data: bytes) -> str:
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError_("读 Excel 需要 openpyxl，请先 pip install openpyxl") from exc

    workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"## 工作表：{sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if cell is None else str(cell).strip() for cell in row]
            if any(cells):
                parts.append(" | ".join(cells))
    workbook.close()
    return "\n".join(parts)


def read_source(source, filename: str | None = None) -> str:
    """读 Excel / PDF / Word / 纯文本，统一成给模型看的纯文本。"""
    name = filename or getattr(source, "name", "") or ""
    suffix = Path(name).suffix.lower()

    if suffix in EXCEL_SUFFIXES:
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        elif isinstance(source, bytes):
            data = source
        else:
            data = source.read()
        return _excel_to_text(data)

    if suffix == ".xls":
        raise ImportError_("不支持老的 .xls 格式，请另存为 .xlsx 再上传")

    return contract_rules.extract_text(source, filename)


def _endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def _parse_json_array(content: str) -> list[dict]:
    text = (content or "").strip()
    # 模型偶尔仍会包一层 ```json，这里剥掉
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"模型返回的不是合法 JSON：{exc}") from exc

    if not isinstance(data, list):
        raise ImportError_("模型返回的顶层不是数组，拒绝继续")

    rows = [row for row in data if isinstance(row, dict)]
    if not rows:
        raise ImportError_("模型没有抽出任何报价行")

    return [{field: row.get(field) for field in QUOTE_FIELDS} for row in rows]


def extract_quotes(text: str, filename: str = "", session=None) -> dict:
    """调模型抽报价行。只返回结构化数据，不做任何业务处理。"""
    if requests is None and session is None:
        raise ImportError_("未安装 requests，请先 pip install requests")

    cfg = _cfg()
    key = api_key()
    if not key:
        raise ImportError_(disabled_reason())

    base_url = cfg.get("base_url") or ""
    model = cfg.get("model") or ""
    if not base_url or not model:
        raise ImportError_("ai_import.base_url 或 model 没配")

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"文件名：{filename or '未命名'}\n\n{text[:12000]}"},
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    client = session or requests

    try:
        response = client.post(_endpoint(base_url), json=payload, headers=headers, timeout=60)
    except Exception as exc:
        raise ImportError_(f"调用模型失败：{type(exc).__name__}: {exc}") from exc

    if not getattr(response, "ok", False):
        raise ImportError_(f"模型接口返回 {getattr(response, 'status_code', '?')}")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ImportError_("模型返回结构异常，取不到 message.content") from exc

    return {
        "rows": _parse_json_array(content),
        "raw": content,
        "model": model,
        "usage": body.get("usage"),
    }


def match_supplier(name, suppliers: list[dict]) -> int | None:
    """把抽出的供应商名匹配到档案里的 id。匹配不到返回 None，由页面提示。"""
    if not name or not str(name).strip():
        return None
    target = str(name).strip()

    for supplier in suppliers:
        if supplier["name"].strip() == target:
            return supplier["id"]

    for supplier in suppliers:
        existing = supplier["name"].strip()
        if target in existing or existing in target:
            return supplier["id"]

    return None
