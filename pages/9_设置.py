"""设置：提醒天数写回 config.yaml。

权重、阈值、Webhook 一律以 config.yaml 为唯一来源，本页只改数值，
不引入第二份配置。汇率与推送在第 8 / 6 轮接入，这里先展示当前状态。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config, db, push as push_mod, repo_fx

st.set_page_config(page_title="设置", page_icon="⚙️", layout="wide")

from core.auth import require_login
require_login(st)


@st.cache_resource
def _boot() -> None:
    db.init_db()


_boot()

st.title("设置")
from core import workspace
workspace.banner(st)


flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)

REMINDERS = [
    ("delivery_due_days", "交期临近预警", "仪表盘「N 天内到期交期」卡片用这个天数"),
    ("goods_to_invoice_days", "货到超 N 天未收票", "超过这个天数还没收票就进待办"),
    ("invoice_to_payment_days", "票到超 N 天未付款", "超过这个天数还没付款就进待办"),
    ("qualification_days", "供应商资质到期前 N 天", "资质到期提醒窗口"),
    ("contract_days", "合同到期前 N 天", "合同终止日提醒窗口"),
    ("price_valid_days", "价格有效期到期前 N 天", "框架价到期提醒窗口"),
]

st.subheader("提醒天数")
st.caption("保存后立即生效。")

with st.form("reminder_form"):
    values = {}
    cols = st.columns(3)
    for idx, (key, label, hint) in enumerate(REMINDERS):
        values[key] = cols[idx % 3].number_input(
            label, min_value=0, max_value=365, value=int(config.reminder_days(key)), step=1, help=hint
        )
    saved = st.form_submit_button("保存提醒天数", type="primary")

if saved:
    failed = [key for key, value in values.items() if not config.update_yaml_scalar("reminders", key, value)]
    if failed:
        st.error(
            "以下配置项在 config.yaml 里没找到，请手动编辑："
            + "、".join(f"reminders.{k}" for k in failed)
        )
    else:
        st.session_state["flash"] = "提醒天数已保存，仪表盘与待办已按新值计算"
        st.rerun()

st.divider()

st.subheader("配置文件")
st.code(str(config.CONFIG_PATH), language="text")
c1, c2 = st.columns([1, 3])
if c1.button("重新加载配置"):
    config.reload()
    st.session_state["flash"] = "已重新加载 config.yaml"
    st.rerun()
c2.caption("手工改完 config.yaml 后点这个按钮，不用重启程序。")

st.divider()

st.subheader("数据库维护与备份")
db_file = config.db_path()
size_kb = db.db_size() / 1024
st.caption(f"当前数据库：`{db_file}`（{size_kb:,.0f} 千字节）")

b1, b2 = st.columns(2)
if b1.button("整理数据库（回收空间）"):
    try:
        result = db.maintenance()
        st.success(
            f"整理完成：{result['size_before'] / 1024:,.0f} 千字节 → {result['size_after'] / 1024:,.0f} 千字节"
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"整理失败：{exc}")

if b2.button("导出完整备份"):
    try:
        from core import backups
        target = backups.create_bundle()
        st.session_state["_backup_download"] = target.read_bytes()
        st.session_state["_backup_name"] = target.name
        st.success("备份已生成，请点击下方按钮下载到手机。")
    except Exception as exc:
        st.error(f"导出失败：{exc}")
if "_backup_download" in st.session_state:
    st.download_button("下载完整备份 ZIP", st.session_state["_backup_download"],
                       file_name=st.session_state["_backup_name"], mime="application/zip")
st.caption("备份包含数据库、设置及工作清单，可能包含敏感业务信息，请妥善保管。恢复需停机后通过管理命令操作，禁止在线覆盖数据库。")

st.divider()

st.subheader("消息推送")
push_cfg = config.push_config()
enabled = bool(push_cfg.get("enabled"))
has_url = bool((push_cfg.get("webhook_url") or "").strip())

p1, p2, p3 = st.columns(3)
p1.metric("推送状态", "已开启" if enabled else "已关闭")
p2.metric("渠道", {"dingtalk": "钉钉", "wecom": "企业微信"}.get(push_cfg.get("channel"), "未设置"))
p3.metric("机器人接收地址", "已配置" if has_url else "未配置")

with st.form("push_switch_form"):
    new_enabled = st.checkbox("启用每日推送", value=enabled)
    saved_push = st.form_submit_button("保存开关")

if saved_push:
    config.update_yaml_scalar("push", "enabled", "true" if new_enabled else "false")
    st.session_state["flash"] = f"每日推送已{'开启' if new_enabled else '关闭'}"
    st.rerun()

push_result = st.session_state.pop("push_result", None)
if push_result:
    if push_result.get("skipped"):
        st.info(f"未发送：{push_result.get('reason')}")
    elif push_result.get("ok"):
        st.success(f"已推送 {push_result.get('total')} 条摘要，{push_result.get('chars')} 字")
        st.code(push_result.get("preview") or "", language="text")
    else:
        st.error(f"推送失败：{push_result.get('error')}")

if st.button("立即推送一次"):
    st.session_state["push_result"] = push_mod.push_once()
    st.rerun()

with st.expander("配置消息机器人"):
    st.markdown(
        "1. 在钉钉 / 企业微信群里添加「自定义机器人」，取得机器人接收地址\n"
        "2. 打开 `config.yaml`，把地址填到 `push.webhook_url`，`push.channel` 填 `dingtalk` 或 `wecom`\n"
        "3. 回来勾上「启用每日推送」并保存\n"
        "4. 点「立即推送一次」验证能收到消息\n\n"
        "地址只在配置文件里，页面不回显也不写入，避免误截图泄露。"
    )
    st.caption("每日自动发送需要另行配置系统定时任务，详见使用说明。")

st.subheader("多币种与汇率")
st.caption(
    f"基准币种：`{repo_fx.base_currency()}`。汇率只手动维护，不接任何实时接口；"
    "外币报价和订单按业务日期取最近生效的一条，折算结果写进快照，之后改汇率不回刷历史。"
)

rates = repo_fx.list_rates()
if rates:
    st.dataframe(
        [
            {
                "编号": r["id"],
                "币种对": f"{r['from_currency']} → {r['to_currency']}",
                "汇率": r["rate"],
                "生效日期": r["effective_date"],
            }
            for r in rates
        ],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("还没有录汇率。录外币报价或订单时，缺汇率会被拦下并提示来这页补。")

with st.form("new_rate"):
    r1, r2, r3 = st.columns(3)
    rate_currency = r1.text_input("外币代码", value="USD", max_chars=6, help="如 USD（美元）、EUR（欧元）、JPY（日元）")
    rate_value = r2.number_input(
        f"汇率（1 外币 = ? {repo_fx.base_currency()}）",
        min_value=0.000001,
        value=7.1000,
        step=0.01,
        format="%.4f",
    )
    rate_date = r3.date_input("生效日期", value=date.today())
    saved_rate = st.form_submit_button("保存汇率", type="primary")

if saved_rate:
    try:
        repo_fx.upsert_rate(rate_currency, rate_value, rate_date)
        st.session_state["flash"] = f"已保存 {rate_currency.upper()} 汇率 {rate_value:.4f}（{rate_date} 起生效）"
        st.rerun()
    except repo_fx.ValidationError as exc:
        st.error(str(exc))

if rates:
    with st.expander("删除汇率"):
        rate_options = {
            f"#{r['id']}　{r['from_currency']} → {r['to_currency']}　{r['rate']}　@{r['effective_date']}": r["id"]
            for r in rates
        }
        picked_rate = st.selectbox("选择要删除的汇率", list(rate_options))
        if st.button("确认删除"):
            repo_fx.delete_rate(rate_options[picked_rate])
            st.session_state["flash"] = "已删除该条汇率"
            st.rerun()
        st.caption("删除只影响之后的新录入，已经折算并落库的历史快照不会变。")
