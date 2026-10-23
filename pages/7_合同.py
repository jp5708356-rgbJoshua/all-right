"""合同清单 + 规则检查。

规则检查只做正则命中，不做语义判断、不调模型、不给修改建议。
每条命中都强制标注「需人工复核」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import contract_rules as cr, db, repo_contracts, repo_suppliers

st.set_page_config(page_title="合同", page_icon="📄", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("合同")
from core import workspace
workspace.banner(st)


flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)

tab_list, tab_check = st.tabs(["合同清单", "规则检查"])

# ---------------------------------------------------------------- 清单
with tab_list:
    suppliers = repo_suppliers.list_suppliers()
    supplier_options = {"全部": None, **{s["name"]: s["id"] for s in suppliers}}
    c1, c2 = st.columns([1, 2])
    picked_supplier = c1.selectbox("供应商", list(supplier_options))
    keyword = c2.text_input("搜索", placeholder="合同编号 / 标题")

    contracts = repo_contracts.list_contracts(
        supplier_id=supplier_options[picked_supplier],
        keyword=keyword,
    )

    if contracts:
        st.dataframe(
            [
                {
                    "编号": c["id"],
                    "合同编号": c["contract_no"] or "—",
                    "标题": c["title"],
                    "供应商": c["supplier_name"] or "—",
                    "开始": c["start_date"] or "—",
                    "结束": c["end_date"] or "—",
                    "价格有效期至": c["price_valid_until"] or "—",
                    "文件": "已登记" if c["file_path"] else "未登记",
                }
                for c in contracts
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"共 {len(contracts)} 份。新增合同在「供应商」页的详情里操作。")
    else:
        st.info("还没有合同。到「供应商」页打开某个供应商的详情，在「关联合同」里添加。")

# ---------------------------------------------------------------- 规则检查
with tab_check:
    st.caption(
        "上传合同文件或选择已登记文件的合同，跑一遍规则库。"
        "命中结果只代表「出现了这类表述」，是否构成风险仍需人工判断。"
    )

    rules = []
    try:
        rules = cr.load_rules()
        st.caption(f"当前规则库：{len(rules)} 条，覆盖 {len({r['category'] for r in rules})} 个类别。")
    except (cr.RuleError, FileNotFoundError) as exc:
        st.error(f"规则库加载失败：{exc}")

    mode = st.radio("合同来源", ["上传文件", "选择已登记的合同"], horizontal=True)

    text = ""
    filename = ""

    if mode == "上传文件":
        uploaded = st.file_uploader("选择合同文件", type=["txt", "md", "docx", "pdf"])
        if uploaded is not None:
            filename = uploaded.name
            try:
                text = cr.extract_text(uploaded, uploaded.name)
            except (ValueError, ImportError) as exc:
                st.error(str(exc))
    else:
        with_file = [
            c for c in repo_contracts.list_contracts() if (c["file_path"] or "").strip()
        ]
        if not with_file:
            st.info("还没有登记过文件路径的合同。可以先在供应商详情里补上，或直接上传文件。")
        else:
            options = {f"{c['title']}（{c['contract_no'] or '无编号'}）": c for c in with_file}
            picked = st.selectbox("选择合同", list(options))
            chosen = options[picked]
            file_path = Path(chosen["file_path"])
            filename = file_path.name
            if not file_path.exists():
                st.error(f"文件不存在：{file_path}")
            else:
                try:
                    text = cr.extract_text(file_path)
                    st.caption(f"已读取 {filename}，{len(text)} 字")
                except (ValueError, OSError) as exc:
                    st.error(str(exc))

    # 没有读到文本或规则库没加载成功时，按钮不渲染
    if text and rules and st.button("开始检查", type="primary"):
        st.session_state["contract_report"] = cr.check(text, rules)
        st.session_state["contract_filename"] = filename

    report = st.session_state.get("contract_report")
    if report:
        st.divider()
        st.subheader(f"检查结果：{st.session_state.get('contract_filename') or '未命名'}")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("命中总数", report["hit_count"])
        m2.metric("高风险", report["by_level"].get("高", 0))
        m3.metric("中风险", report["by_level"].get("中", 0))
        m4.metric("低风险", report["by_level"].get("低", 0))

        if report["no_hit"]:
            st.warning(
                "未命中任何规则。这只说明规则库里没有匹配到关键词，"
                "不代表合同没有问题，仍需人工通读复核。"
            )
        else:
            st.dataframe(
                [
                    {
                        "等级": r["level"],
                        "规则": r["rule_id"],
                        "类别": r["category"],
                        "行号": r["line_no"],
                        "命中原文": r["matched_text"],
                        "上下文": r["snippet"],
                        "提示": r["message"],
                        "需人工复核": "是" if r["needs_human_review"] else "否",
                    }
                    for r in report["results"]
                ],
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                f"共检查 {report['rule_count']} 条规则，文本 {report['text_length']} 字。"
                "行号以纯文本行为准，docx / pdf 转换后行号可能与原文排版不完全对应。"
            )

        with st.expander("按类别汇总"):
            counts: dict[str, int] = {}
            for r in report["results"]:
                counts[r["category"]] = counts.get(r["category"], 0) + 1
            if counts:
                st.dataframe(
                    [{"类别": k, "命中条数": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("没有命中。")

        st.caption(
            "本功能不做语义分析、不生成修改建议、不调用外部模型。"
            "规则库在 rules/contract_rules.yaml，可自行增删正则。"
        )
