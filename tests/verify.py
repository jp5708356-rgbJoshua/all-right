"""数据层验收自检。

覆盖：建表、供应商 CRUD、物料 CRUD、字段校验、敏感字段隔离、持久化、历史价重算，
以及第 2 轮的订单 CRUD、状态机（跳级/异常/恢复/取消）、仪表盘数字、待办清单。
全程使用临时数据库，不碰 data/procurement.db。

    python tests/verify.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import (
    ai_import,
    config,
    contract_rules,
    db,
    metrics,
    push,
    repo_contracts as rc,
    repo_fx,
    repo_items as ri,
    repo_orders as ro,
    repo_quotes as rq,
    repo_recon as rr,
    repo_suppliers as rs,
    scorecard,
    state_machine as sm,
    todos,
)

today = date.today()

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {label}"
    if detail and not ok:
        line += f"  -> {detail}"
    print(line)


def expect_error(label: str, fn, exc_type=Exception) -> None:
    try:
        fn()
    except exc_type:
        check(label, True)
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"抛出了 {type(exc).__name__}: {exc}")
    else:
        check(label, False, "没有抛出预期异常")


def main() -> int:
    # 临时库放在项目内 tests/_tmp，避免依赖系统临时目录
    tmp = ROOT / "tests" / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    db_file = tmp / f"verify_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    conn = db.connect(db_file, journal_mode="MEMORY")
    db.init_db(conn)

    # ---- 1. 建表 ----
    tables = {r["name"] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'table'", (), conn)}
    expected = {
        "schema_meta", "suppliers", "items", "contracts", "orders",
        "quotes", "reconciliation", "order_exceptions", "fx_rates",
    }
    check("建表：9 张表全部存在", expected <= tables, f"缺 {sorted(expected - tables)}")

    fk_on = db.scalar("PRAGMA foreign_keys", (), conn)
    check("连接开启外键约束", int(fk_on or 0) == 1, f"PRAGMA foreign_keys = {fk_on}")

    # ---- 2. 供应商：最少字段 ----
    sid1 = rs.create_supplier({"name": "测试供应商甲"}, conn)
    row1 = rs.get_supplier(sid1, conn)
    check(
        "只填名称即可建供应商，风险等级默认低、状态默认活跃",
        row1["name"] == "测试供应商甲" and row1["risk_level"] == "低" and row1["status"] == "活跃",
        str(row1),
    )

    # ---- 3. 供应商：全字段 ----
    sid2 = rs.create_supplier(
        {
            "name": "测试供应商乙",
            "credit_code": "91330200TEST0001XY",
            "category": "五金件",
            "contact_name": "张三",
            "contact_phone": "13800000000",
            "bank_account": "6222020000000000",
            "qualification_expiry": "2026-12-31",
            "risk_level": "高",
            "risk_notes": "上批交期延迟两周",
        },
        conn,
    )
    row2 = rs.get_supplier(sid2, conn)
    check("全字段录入成功", row2["category"] == "五金件" and row2["risk_level"] == "高")

    # ---- 4. 校验 ----
    expect_error("名称为空被拒绝", lambda: rs.create_supplier({"name": "   "}, conn), rs.ValidationError)
    expect_error(
        "非法日期被拒绝",
        lambda: rs.create_supplier({"name": "日期错的供应商", "qualification_expiry": "2026/12/31"}, conn),
        rs.ValidationError,
    )
    expect_error(
        "非法风险等级被拒绝",
        lambda: rs.create_supplier({"name": "等级错的供应商", "risk_level": "极高"}, conn),
        rs.ValidationError,
    )

    # ---- 5. 敏感字段隔离 ----
    listed = rs.list_suppliers(conn=conn)
    leaked = [
        r["name"] for r in listed
        if any(k in r for k in ("contact_name", "contact_phone", "bank_account"))
    ]
    check("列表查询不返回联系人 / 电话 / 银行账号", not leaked, f"泄露于 {leaked}")
    check("详情查询返回联系人信息", rs.get_supplier(sid2, conn)["contact_phone"] == "13800000000")

    # ---- 6. 重名提示 ----
    dups = rs.duplicate_names("测试供应商乙", conn=conn)
    check("同名供应商可被检出（不阻止创建）", len(dups) == 1 and dups[0]["id"] == sid2)

    # ---- 7. 更新 ----
    rs.update_supplier(sid2, {"risk_level": "中", "status": "暂停", "risk_notes": "已整改"}, conn)
    row2 = rs.get_supplier(sid2, conn)
    check("修改风险等级与状态生效", row2["risk_level"] == "中" and row2["status"] == "暂停")

    # ---- 8. 排序：高风险优先 ----
    ordered = rs.list_suppliers(conn=conn)
    check("列表按风险等级排序（高在前）", ordered[0]["risk_level"] == "中", f"顺序 {[r['risk_level'] for r in ordered]}")

    # ---- 9. 物料 ----
    item_id = ri.create_item({"name": "测试物料"}, conn)
    item = ri.get_item(item_id, conn)
    check("只填名称即可建物料", item["name"] == "测试物料" and item["historical_low_price"] is None)

    ri.update_item(item_id, {"code": "M-001", "spec": "M8x30", "unit": "个"}, conn)
    check("物料补编码与规格成功", ri.get_item(item_id, conn)["code"] == "M-001")

    expect_error("物料名称必填", lambda: ri.create_item({"name": ""}, conn), ri.ValidationError)
    expect_error(
        "物料编码重复被拒绝",
        lambda: ri.create_item({"name": "重复编码物料", "code": "M-001"}, conn),
        ri.ValidationError,
    )

    # ---- 10. 历史价由比价自动重算 ----
    for price in (10.0, 8.5, 12.0):
        db.execute(
            "INSERT INTO quotes (item_id, supplier_id, quoted_price, currency, base_amount, quote_date) "
            "VALUES (?, ?, ?, 'CNY', ?, '2026-10-01')",
            (item_id, sid1, price, price),
            conn,
        )
    ri.recalc_historical_prices(item_id, conn)
    item = ri.get_item(item_id, conn)
    check(
        "历史低/高价由比价记录自动算出（8.5 / 12.0）",
        item["historical_low_price"] == 8.5 and item["historical_high_price"] == 12.0,
        f"得到 {item['historical_low_price']} / {item['historical_high_price']}",
    )

    # ---- 11. 持久化：关连接重开 ----
    conn.close()
    conn2 = db.connect(db_file, journal_mode="MEMORY")
    again = rs.get_supplier(sid2, conn2)
    check(
        "关闭连接重开，数据仍在（模拟重启程序）",
        again is not None and again["status"] == "暂停" and again["risk_notes"] == "已整改",
    )

    # ---- 12. 外键生效：删被引用供应商应失败 ----
    expect_error(
        "被报价引用的供应商不能硬删除",
        lambda: db.execute("DELETE FROM suppliers WHERE id = ?", (sid1,), conn2),
        Exception,
    )

    # ================= 第 2 轮：订单 + 状态机 + 仪表盘 =================

    # ---- 13. 状态机结构自检 ----
    check(
        "状态机每个状态最多一个后继（结构上无法跳级）",
        all(len(v) <= 1 for v in sm.ALLOWED_TRANSITIONS.values()),
        str(sm.ALLOWED_TRANSITIONS),
    )
    check("终态集合为 已完成 / 已取消", {"已完成", "已取消"} == sm.TERMINAL)
    check("已完成没有下一步", sm.next_states("已完成") == [])

    # ---- 14. 建单：只填必需字段 ----
    yesterday = today - timedelta(days=2)
    oid1 = ro.create_order(
        {
            "order_no": "PO-T-001",
            "supplier_id": sid1,
            "item_id": item_id,
            "quantity": 10,
            "unit_price": 5.0,
            "order_date": str(yesterday),
            "promised_delivery_date": str(yesterday),
        },
        conn2,
    )
    order1 = ro.get_order(oid1, conn2)
    check(
        "只填必需字段即可建单，状态默认已下单，总价按数量×单价自动算",
        order1["status"] == "已下单" and order1["total_amount"] == 50.0 and order1["base_amount"] == 50.0,
        f"status={order1['status']} total={order1['total_amount']} base={order1['base_amount']}",
    )
    check("未选物料时品名自动取物料名", order1["item_name_raw"] == "测试物料")

    expect_error(
        "缺订单号被拒绝",
        lambda: ro.create_order({"supplier_id": sid1, "quantity": 1, "order_date": str(today)}, conn2),
        ro.ValidationError,
    )
    expect_error(
        "缺供应商被拒绝",
        lambda: ro.create_order({"order_no": "PO-X", "quantity": 1, "order_date": str(today)}, conn2),
        ro.ValidationError,
    )
    expect_error(
        "数量为 0 被拒绝",
        lambda: ro.create_order(
            {"order_no": "PO-Y", "supplier_id": sid1, "item_name_raw": "某物", "quantity": 0, "order_date": str(today)},
            conn2,
        ),
        ro.ValidationError,
    )
    expect_error(
        "订单号重复被拒绝",
        lambda: ro.create_order(
            {"order_no": "PO-T-001", "supplier_id": sid1, "item_name_raw": "某物", "quantity": 1, "order_date": str(today)},
            conn2,
        ),
        ro.ValidationError,
    )

    # ---- 15. 跳级 / 倒流 / 单步 ----
    expect_error(
        "跳级被拒绝（已下单 → 已发货）",
        lambda: ro.update_status(oid1, "已发货", conn2),
        sm.StateError,
    )
    expect_error(
        "原地重复流转被拒绝（已下单 → 已下单）",
        lambda: ro.update_status(oid1, "已下单", conn2),
        sm.StateError,
    )
    ro.update_status(oid1, "供应商已确认", conn2)
    check("单步流转成功（已下单 → 供应商已确认）", ro.get_order(oid1, conn2)["status"] == "供应商已确认")

    # ---- 16. 异常标记与恢复 ----
    expect_error(
        "异常原因为空被拒绝",
        lambda: ro.mark_exception(oid1, "  ", "其他", conn2),
        ro.ValidationError,
    )
    expect_error(
        "异常类别非法被拒绝",
        lambda: ro.mark_exception(oid1, "有原因", "随便写的类别", conn2),
        ro.ValidationError,
    )
    ro.mark_exception(oid1, "供应商说原料缺货", "交期", conn2)
    order1 = ro.get_order(oid1, conn2)
    check(
        "标记异常后状态为异常，并记住标记前状态",
        order1["status"] == "异常" and order1["status_before_exception"] == "供应商已确认",
        f"status={order1['status']} before={order1['status_before_exception']}",
    )
    expect_error(
        "异常状态不能直接往下流转",
        lambda: ro.update_status(oid1, "生产中", conn2),
        sm.StateError,
    )
    expect_error(
        "异常状态不能重复标记异常",
        lambda: ro.mark_exception(oid1, "再来一次", "其他", conn2),
        sm.StateError,
    )

    restored = ro.restore_exception(oid1, conn2)
    check(
        "异常恢复到标记前状态",
        restored == "供应商已确认" and ro.get_order(oid1, conn2)["status"] == "供应商已确认",
        f"恢复到 {restored}",
    )
    history = ro.list_exceptions(oid1, conn2)
    check("异常历史记录已关闭（resolved_at 有值）", history and history[0]["resolved_at"] is not None)

    # ---- 17. 推进到已到货：自动记实际到货日 ----
    for step in ("生产中", "已发货", "已到货"):
        ro.update_status(oid1, step, conn2)
    order1 = ro.get_order(oid1, conn2)
    check(
        "推进到已到货时自动按今天记实际到货日",
        order1["actual_delivery_date"] == str(today),
        f"得到 {order1['actual_delivery_date']}",
    )

    # ---- 18. 终态锁死 ----
    ro.update_status(oid1, "已对账", conn2)
    ro.update_status(oid1, "已完成", conn2)
    check("已完成没有下一步", sm.next_states(ro.get_order(oid1, conn2)["status"]) == [])
    expect_error("已完成不能再标异常", lambda: ro.mark_exception(oid1, "事后追责", "其他", conn2), sm.StateError)
    expect_error("已完成不能取消", lambda: ro.cancel_order(oid1, "反悔", conn2), sm.StateError)

    # ---- 19. 取消 ----
    oid2 = ro.create_order(
        {"order_no": "PO-T-002", "supplier_id": sid1, "item_name_raw": "重复下单的物品", "quantity": 1,
         "order_date": str(today)},
        conn2,
    )
    ro.cancel_order(oid2, "重复下单", conn2)
    check("非终态可取消，状态变为已取消", ro.get_order(oid2, conn2)["status"] == "已取消")
    expect_error("已取消不能再流转", lambda: ro.update_status(oid2, "供应商已确认", conn2), sm.StateError)

    # ---- 20. 仪表盘四个数字 ----
    rs.update_supplier(sid2, {"risk_level": "高"}, conn2)
    oid3 = ro.create_order(
        {"order_no": "PO-T-003", "supplier_id": sid1, "item_name_raw": "逾期未到的物品", "quantity": 3,
         "order_date": str(yesterday), "promised_delivery_date": str(yesterday)},
        conn2,
    )
    oid4 = ro.create_order(
        {"order_no": "PO-T-004", "supplier_id": sid2, "item_name_raw": "高风险供应商的物品", "quantity": 2,
         "order_date": str(today), "promised_delivery_date": str(today + timedelta(days=3))},
        conn2,
    )
    check("进行中订单数 = 2", ro.count_active(conn2) == 2, f"得到 {ro.count_active(conn2)}")
    check("7 天内到期交期数 = 1", ro.count_due_soon(7, conn2) == 1, f"得到 {ro.count_due_soon(7, conn2)}")
    check("待对账订单数 = 0", ro.count_pending_recon(conn2) == 0, f"得到 {ro.count_pending_recon(conn2)}")
    check("高风险供应商进行中 = 1", ro.count_high_risk_active(conn2) == 1, f"得到 {ro.count_high_risk_active(conn2)}")

    # ---- 21. 待办清单 ----
    todo_list = todos.build_todos(conn2)
    check(
        "待办第 1 类命中逾期未到货订单",
        any(t["ref"] == "PO-T-003" and t["priority"] == 1 for t in todo_list),
        str([(t["priority"], t["ref"]) for t in todo_list]),
    )
    check(
        "待办第 4 类命中高风险供应商进行中订单",
        any(t["ref"] == "PO-T-004" and t["priority"] == 4 for t in todo_list),
        str([(t["priority"], t["ref"]) for t in todo_list]),
    )
    check(
        "待办按优先级升序排列",
        [t["priority"] for t in todo_list] == sorted(t["priority"] for t in todo_list),
    )

    # ================= 第 3 轮：比价 + 对账 =================

    # ---- 22. 比价录入与有效报价判定 ----
    item2 = ri.create_item({"name": "比价专用物料", "code": "M-002"}, conn2)
    sid3 = rs.create_supplier({"name": "测试供应商丙"}, conn2)

    q1 = rq.create_quote(
        {
            "item_id": item2,
            "supplier_id": sid1,
            "quoted_price": 10.0,
            "quote_date": str(today),
            "valid_until": str(today + timedelta(days=30)),
        },
        conn2,
    )
    quote1 = rq.list_quotes(item_id=item2, conn=conn2)[0]
    check(
        "只填必需字段即可录报价，base_amount 等于单价",
        quote1["quoted_price"] == 10.0 and quote1["base_amount"] == 10.0,
        str(quote1),
    )

    expect_error(
        "外币报价被拒绝（第 8 轮才接汇率）",
        lambda: rq.create_quote(
            {"item_id": item2, "supplier_id": sid1, "quoted_price": 1.0,
             "currency": "USD", "quote_date": str(today)},
            conn2,
        ),
        rq.ValidationError,
    )
    expect_error(
        "有效期早于报价日期被拒绝",
        lambda: rq.create_quote(
            {"item_id": item2, "supplier_id": sid1, "quoted_price": 1.0,
             "quote_date": str(today), "valid_until": str(today - timedelta(days=1))},
            conn2,
        ),
        rq.ValidationError,
    )
    expect_error(
        "报价为 0 被拒绝",
        lambda: rq.create_quote(
            {"item_id": item2, "supplier_id": sid1, "quoted_price": 0, "quote_date": str(today)},
            conn2,
        ),
        rq.ValidationError,
    )

    # 同一供应商再报一条更低的（应只取最新一条参与比价）
    rq.create_quote(
        {"item_id": item2, "supplier_id": sid1, "quoted_price": 8.0, "quote_date": str(today)},
        conn2,
    )
    rq.create_quote(
        {"item_id": item2, "supplier_id": sid2, "quoted_price": 9.0, "quote_date": str(today)},
        conn2,
    )
    # 第三家，但已过期
    rq.create_quote(
        {
            "item_id": item2,
            "supplier_id": sid3,
            "quoted_price": 7.0,
            "quote_date": str(today - timedelta(days=60)),
            "valid_until": str(today - timedelta(days=1)),
        },
        conn2,
    )

    data = rq.effective_quotes(item2, conn2)
    check(
        "有效报价家数按供应商去重（同家 2 条只算 1 家）",
        data["supplier_count"] == 2,
        f"得到 {data['supplier_count']}",
    )
    check(
        "同一供应商取最新一条参与比价（8.0 而非 10.0）",
        data["lowest"]["compare_price"] == 8.0,
        f"得到 {data['lowest']['compare_price'] if data['lowest'] else None}",
    )
    check(
        "过期报价不参与最低价（7.0 被排除）",
        all(r["supplier_id"] != sid3 for r in data["priced"]),
    )
    check("有效报价不足 3 家时置 need_more", data["need_more"] is True)
    check("过期报价被标记为已过期", len(data["expired"]) == 1, f"得到 {len(data['expired'])}")

    # 补第三家有效报价
    rq.create_quote(
        {"item_id": item2, "supplier_id": sid3, "quoted_price": 11.5, "quote_date": str(today)},
        conn2,
    )
    data = rq.effective_quotes(item2, conn2)
    check("凑够 3 家后 need_more 为 False", data["need_more"] is False and data["supplier_count"] == 3)
    check("最低价仍为 8.0", data["lowest"]["compare_price"] == 8.0)

    item2_row = ri.get_item(item2, conn2)
    check(
        "物料历史低/高价随报价自动重算（7.0 / 11.5）",
        item2_row["historical_low_price"] == 7.0 and item2_row["historical_high_price"] == 11.5,
        f"得到 {item2_row['historical_low_price']} / {item2_row['historical_high_price']}",
    )

    # 中选唯一性
    rq.select_quote(q1, "首批试单", conn2)
    selected = [q for q in rq.list_quotes(item_id=item2, conn=conn2) if q["is_selected"]]
    check("标记中选后同物料只有一条中选", len(selected) == 1 and selected[0]["id"] == q1)

    # ---- 23. 对账 ----
    recon = rr.upsert(oid4, {"goods_received": True}, conn2)
    check(
        "打勾自动记当天日期",
        recon["goods_received"] == 1 and recon["goods_received_date"] == str(today),
        f"得到 {recon['goods_received_date']}",
    )

    recon = rr.upsert(oid4, {"goods_received": False}, conn2)
    check(
        "取消打勾一并清掉日期",
        recon["goods_received"] == 0 and recon["goods_received_date"] is None,
    )

    recon = rr.upsert(
        oid4,
        {
            "goods_received": True,
            "invoice_received": True,
            "payment_done": False,
            "invoice_no": "INV-TEST-001",
        },
        conn2,
    )
    check(
        "三单未齐时 is_complete 为 False",
        rr.is_complete(recon) is False and recon["invoice_no"] == "INV-TEST-001",
    )

    recon = rr.upsert(oid4, {"payment_done": True}, conn2)
    check("三单齐后 is_complete 为 True", rr.is_complete(recon) is True)

    # 造两种缺口
    rr.upsert(oid1, {"goods_received": True, "invoice_received": True, "payment_done": True}, conn2)
    rr.upsert(oid3, {"goods_received": True}, conn2)
    rr.upsert(oid4, {"goods_received": True, "invoice_received": True, "payment_done": False}, conn2)

    goods_no_invoice = rr.list_rows("货到未收票", conn=conn2)
    check(
        "筛选「货到未收票」只命中货到票未到的单",
        [r["order_no"] for r in goods_no_invoice] == ["PO-T-003"],
        str([r["order_no"] for r in goods_no_invoice]),
    )

    invoice_no_pay = rr.list_rows("票到未付款", conn=conn2)
    check(
        "筛选「票到未付款」只命中票到款未付的单",
        [r["order_no"] for r in invoice_no_pay] == ["PO-T-004"],
        str([r["order_no"] for r in invoice_no_pay]),
    )

    all_rows = rr.list_rows("全部", conn=conn2)
    check(
        "已取消订单不出现在对账列表",
        all(r["order_no"] != "PO-T-002" for r in all_rows),
        str([r["order_no"] for r in all_rows]),
    )
    check("三单未齐计数正确（PO-T-003、PO-T-004）", rr.count_incomplete(conn2) == 2, f"得到 {rr.count_incomplete(conn2)}")

    # 待对账卡片：造一笔「已到货」且三单未齐的订单
    oid5 = ro.create_order(
        {"order_no": "PO-T-005", "supplier_id": sid1, "item_name_raw": "已到货待对账的物品", "quantity": 5,
         "order_date": str(today - timedelta(days=8))},
        conn2,
    )
    for step in ("供应商已确认", "生产中", "已发货", "已到货"):
        ro.update_status(oid5, step, conn2)
    check(
        "已到货且三单未齐 → 待对账订单数 = 1",
        ro.count_pending_recon(conn2) == 1,
        f"得到 {ro.count_pending_recon(conn2)}",
    )
    rr.upsert(oid5, {"goods_received": True, "invoice_received": True, "payment_done": True}, conn2)
    check(
        "三单齐后待对账订单数归零",
        ro.count_pending_recon(conn2) == 0,
        f"得到 {ro.count_pending_recon(conn2)}",
    )

    # ================= 第 4 轮：合同 + 指标 + 六类待办 =================

    # ---- 24. 合同 ----
    cid1 = rc.create_contract(
        {
            "supplier_id": sid1,
            "title": "年度五金件框架协议",
            "contract_no": "HT-2026-001",
            "start_date": str(today - timedelta(days=100)),
            "end_date": str(today + timedelta(days=20)),
            "price_valid_until": str(today + timedelta(days=5)),
        },
        conn2,
    )
    contract1 = rc.get_contract(cid1, conn2)
    check(
        "合同创建成功，供应商名带出",
        contract1["title"] == "年度五金件框架协议" and contract1["supplier_name"] == "测试供应商甲",
        str(contract1),
    )
    expect_error("合同标题必填", lambda: rc.create_contract({"contract_no": "HT-X"}, conn2), rc.ValidationError)
    expect_error(
        "合同日期格式错误被拒绝",
        lambda: rc.create_contract({"title": "格式错的合同", "end_date": "2026/12/31"}, conn2),
        rc.ValidationError,
    )

    expiring = rc.expiring_contracts(30, conn2)
    check(
        "到期合同被检出，取更早的日期（价格有效期 5 天）",
        len(expiring) == 1
        and expiring[0]["due_kind"] == "价格有效期到期"
        and expiring[0]["due_date"] == str(today + timedelta(days=5)),
        str(expiring),
    )
    check("到期窗口设成 3 天时不再命中（5 天后才到期）", rc.expiring_contracts(3, conn2) == [])

    # ---- 25. 资质到期 ----
    rs.update_supplier(sid1, {"qualification_expiry": str(today + timedelta(days=10))}, conn2)
    qual = rs.expiring_qualifications(30, conn2)
    check(
        "资质 10 天后到期被检出，剩余天数正确",
        len(qual) == 1 and qual[0]["days_left"] == 10,
        str(qual),
    )
    check("资质窗口设成 5 天时不再命中", rs.expiring_qualifications(5, conn2) == [])

    # ---- 26. 轻量指标（手工核对） ----
    # 造一笔准时到货的单：下单 10 天前，承诺交期今天，今天到货
    oid6 = ro.create_order(
        {"order_no": "PO-T-006", "supplier_id": sid1, "item_name_raw": "准时到货的物品", "quantity": 1,
         "order_date": str(today - timedelta(days=10)), "promised_delivery_date": str(today)},
        conn2,
    )
    for step in ("供应商已确认", "生产中", "已发货", "已到货"):
        ro.update_status(oid6, step, conn2)

    m = metrics.compute(conn=conn2)
    # 已到货：PO-T-001(逾期, 周期2天) / PO-T-005(无承诺交期, 周期8天) / PO-T-006(准时, 周期10天)
    check("已到货笔数 = 3", m["delivered_orders"] == 3, f"得到 {m['delivered_orders']}")
    check(
        "无承诺交期的到货订单被排除出 OTD 分母",
        m["otd_sample"] == 2 and m["otd_excluded_no_promise"] == 1,
        f"分母 {m['otd_sample']}，排除 {m['otd_excluded_no_promise']}",
    )
    check("准时笔数 = 1（PO-T-006）", m["otd_on_time"] == 1, f"得到 {m['otd_on_time']}")
    check("OTD = 50.0%", m["otd_rate"] is not None and abs(m["otd_rate"] - 50.0) < 1e-9, f"得到 {m['otd_rate']}")
    check(
        "平均订单周期 = (2+8+10)/3 ≈ 6.67 天",
        m["avg_cycle_days"] is not None and abs(m["avg_cycle_days"] - 20 / 3) < 1e-9,
        f"得到 {m['avg_cycle_days']}",
    )
    check("统计期订单数 = 5（不含已取消）", m["total_orders"] == 5, f"得到 {m['total_orders']}")
    check("异常订单数 = 1（PO-T-001）", m["exception_orders"] == 1, f"得到 {m['exception_orders']}")
    check(
        "异常订单占比 = 20.0%",
        m["exception_rate"] is not None and abs(m["exception_rate"] - 20.0) < 1e-9,
        f"得到 {m['exception_rate']}",
    )

    detail = metrics.delivery_detail(conn=conn2)
    check("指标明细可用于手工核对", len(detail) == 3 and all("cycle_days" in r for r in detail))

    # ---- 27. 六类待办 ----
    # 把货到 / 收票日期拨到 10 天前，让第 2、3 类待办命中
    rr.upsert(
        oid3,
        {"goods_received": True, "goods_received_date": str(today - timedelta(days=10))},
        conn2,
    )
    rr.upsert(
        oid4,
        {
            "goods_received": True,
            "invoice_received": True,
            "invoice_received_date": str(today - timedelta(days=10)),
            "payment_done": False,
        },
        conn2,
    )

    todo_list = todos.build_todos(conn2)
    priorities = {t["priority"] for t in todo_list}
    check(
        "待办第 2 类命中货到超期未收票",
        any(t["priority"] == 2 and t["ref"] == "PO-T-003" for t in todo_list),
        str([(t["priority"], t["ref"]) for t in todo_list]),
    )
    check(
        "待办第 3 类命中票到超期未付款",
        any(t["priority"] == 3 and t["ref"] == "PO-T-004" for t in todo_list),
        str([(t["priority"], t["ref"]) for t in todo_list]),
    )
    check(
        "待办第 5 类命中资质将到期供应商",
        any(t["priority"] == 5 and t["supplier"] == "测试供应商甲" for t in todo_list),
        str([(t["priority"], t["ref"]) for t in todo_list]),
    )
    check(
        "待办第 6 类命中合同到期",
        any(t["priority"] == 6 and t["ref"] == "HT-2026-001" for t in todo_list),
        str([(t["priority"], t["ref"]) for t in todo_list]),
    )
    check("六类待办都能产出（1~6 全覆盖）", priorities == {1, 2, 3, 4, 5, 6}, f"得到 {sorted(priorities)}")
    check(
        "待办按优先级升序排列",
        [t["priority"] for t in todo_list] == sorted(t["priority"] for t in todo_list),
    )

    # ---- 28. 配置写回保留注释 ----
    tmp_cfg = ROOT / "tests" / "_tmp" / f"cfg_{datetime.now().strftime('%H%M%S%f')}.yaml"
    tmp_cfg.write_text(
        "# 测试用配置\nreminders:\n  contract_days: 30    # 合同提醒\n  price_valid_days: 30\n",
        encoding="utf-8",
    )
    original_cfg_path = config.CONFIG_PATH
    try:
        config.CONFIG_PATH = tmp_cfg
        config.reload()
        wrote = config.update_yaml_scalar("reminders", "contract_days", 45)
        text = tmp_cfg.read_text(encoding="utf-8")
        check(
            "配置写回成功且保留注释与格式",
            wrote and "contract_days: 45" in text and "# 合同提醒" in text,
            text,
        )
        check("写回后读取到新值", config.reminder_days("contract_days") == 45)
        check(
            "写不存在的配置项返回 False",
            config.update_yaml_scalar("reminders", "not_exist_key", 1) is False,
        )
    finally:
        config.CONFIG_PATH = original_cfg_path
        config.reload()

    # ================= 第 5 轮：供应商评分卡 =================

    sid_s = rs.create_supplier({"name": "评分卡测试供应商"}, conn2)

    def _delivered(order_no: str, promised_offset: int) -> int:
        """建单并一路推到已到货，返回订单 id。promised_offset 相对今天的天数。"""
        oid = ro.create_order(
            {
                "order_no": order_no,
                "supplier_id": sid_s,
                "item_name_raw": "评分卡测试物品",
                "quantity": 1,
                "order_date": str(today - timedelta(days=10)),
                "promised_delivery_date": str(today + timedelta(days=promised_offset)),
            },
            conn2,
        )
        for step in ("供应商已确认", "生产中", "已发货", "已到货"):
            ro.update_status(oid, step, conn2)
        return oid

    _delivered("PO-S-001", 0)   # 承诺今天，今天到 -> 准时
    _delivered("PO-S-002", 0)   # 准时
    o_s3 = _delivered("PO-S-003", -5)  # 承诺 5 天前，今天到 -> 逾期

    ro.mark_exception(o_s3, "外观划伤", "质量", conn2)
    ro.restore_exception(o_s3, conn2)

    # 在该物料上报最低价 -> 溢价 0 -> cost 满分
    rq.create_quote(
        {"item_id": item2, "supplier_id": sid_s, "quoted_price": 7.0, "quote_date": str(today)},
        conn2,
    )

    card = scorecard.compute(sid_s, conn=conn2)
    dims = card["dimensions"]

    check(
        "交付分 = 2/3 准时 ≈ 66.7",
        dims["delivery"]["score"] is not None and abs(dims["delivery"]["score"] - 200 / 3) < 1e-9,
        f"得到 {dims['delivery']['score']}",
    )
    check(
        "质量分 = 1 - 1/3 ≈ 66.7",
        dims["quality"]["score"] is not None and abs(dims["quality"]["score"] - 200 / 3) < 1e-9,
        f"得到 {dims['quality']['score']}",
    )
    check(
        "成本分 = 100（本家即最低价，溢价 0）",
        dims["cost"]["score"] is not None and abs(dims["cost"]["score"] - 100.0) < 1e-9,
        f"得到 {dims['cost']['score']}",
    )
    check(
        "服务分未配置时为空",
        dims["service"]["score"] is None and "service" in card["empty_dimensions"],
    )
    check(
        "空缺维度不扣分：总分 = (66.7×0.3 + 66.7×0.3 + 100×0.25) / 0.85 ≈ 76.5",
        card["weighted_total"] is not None and abs(card["weighted_total"] - 65 / 0.85) < 1e-6,
        f"得到 {card['weighted_total']}",
    )
    check(
        "参与计算的权重合计 = 0.85（服务权重被剔除）",
        abs(card["counted_weight"] - 0.85) < 1e-9,
        f"得到 {card['counted_weight']}",
    )
    check("76.5 分评为 B", card["grade"] == "B", f"得到 {card['grade']}")

    # 配置人工服务分后重新计算
    _orig_get = config.get

    def _fake_get(path, default=None):
        if path == "scorecard.service_manual":
            return {str(sid_s): 80}
        return _orig_get(path, default)

    config.get = _fake_get
    try:
        card_with_service = scorecard.compute(sid_s, conn=conn2)
    finally:
        config.get = _orig_get

    check(
        "配置服务分 80 后计入权重：总分 = 65 + 80×0.15 = 77.0",
        card_with_service["weighted_total"] is not None
        and abs(card_with_service["weighted_total"] - 77.0) < 1e-6,
        f"得到 {card_with_service['weighted_total']}",
    )
    check(
        "配置服务分后四个维度都有值",
        card_with_service["empty_dimensions"] == [],
        str(card_with_service["empty_dimensions"]),
    )

    # 无数据供应商
    sid_empty = rs.create_supplier({"name": "无数据供应商"}, conn2)
    card_empty = scorecard.compute(sid_empty, conn=conn2)
    check(
        "无任何数据时总分为空、不评级",
        card_empty["weighted_total"] is None and card_empty["grade"] is None,
        str(card_empty["weighted_total"]),
    )
    check("无数据时四个维度全为空", len(card_empty["empty_dimensions"]) == 4)

    # 评级边界（阈值来自 config：S90 / A80 / B70）
    check("评级边界 90 → S", scorecard.grade_of(90) == "S")
    check("评级边界 89.9 → A", scorecard.grade_of(89.9) == "A")
    check("评级边界 80 → A", scorecard.grade_of(80) == "A")
    check("评级边界 79.9 → B", scorecard.grade_of(79.9) == "B")
    check("评级边界 70 → B", scorecard.grade_of(70) == "B")
    check("评级边界 69.9 → C", scorecard.grade_of(69.9) == "C")
    check("无总分时不评级", scorecard.grade_of(None) is None)

    # 权重来自配置且归一化
    weights = config.scorecard_weights()
    check("四维权重齐全且归一化为 1", set(weights) == {"quality", "cost", "delivery", "service"}
          and abs(sum(weights.values()) - 1.0) < 1e-9, str(weights))

    # 期间筛选：本月有数据，很久以前没有
    old_period = f"{today.year - 2:04d}-01"
    card_old = scorecard.compute(sid_s, period=old_period, conn=conn2)
    check(
        "按期间筛选后无数据则总分为空",
        card_old["weighted_total"] is None,
        str(card_old["weighted_total"]),
    )

    # ================= 第 6 轮：消息推送（单向） =================

    class _FakeResp:
        def __init__(self, body=None, ok=True, status=200):
            self._body = body if body is not None else {"errcode": 0, "errmsg": "ok"}
            self.ok = ok
            self.status_code = status
            self.content = b"{}"

        def json(self):
            return self._body

    class _FakeSession:
        def __init__(self, resp=None):
            self.calls = []
            self.resp = resp or _FakeResp()

        def post(self, url, json=None, timeout=None):
            self.calls.append({"url": url, "json": json, "timeout": timeout})
            return self.resp

    _push_orig_get = config.get

    def _push_cfg(**overrides):
        base = {
            "enabled": True,
            "channel": "dingtalk",
            "webhook_url": "https://example.invalid/robot/send?access_token=fake",
            "daily_time": "09:00",
            "max_orders_per_section": 10,
            "max_chars": 1800,
        }
        base.update(overrides)
        return base

    def _patch_push(**overrides):
        cfg = _push_cfg(**overrides)

        def _fake_get(path, default=None):
            if path == "push":
                return cfg
            return _push_orig_get(path, default)

        config.get = _fake_get
        return cfg

    # ---- 29. 摘要与文案 ----
    summary = push.build_summary(conn2)
    check("摘要含总数与分类", summary["total"] > 0 and isinstance(summary["groups"], dict))
    check(
        "摘要只带单号，不带金额等明细字段",
        all(set(g) == {"kind", "count", "refs"} for g in summary["groups"].values()),
        str(summary["groups"]),
    )

    text = push.render_text(summary)
    check("推送文案包含订单号", "PO-T-003" in text, text[:200])
    check(
        "推送文案不含联系方式与银行账号",
        all(secret not in text for secret in ("13800000000", "6222020000000000", "张三")),
    )
    check(
        "推送文案不含金额",
        "¥" not in text and "金额" not in text,
    )

    long_text = push.render_text(summary, max_chars=120)
    check("超长内容被截断并提示", len(long_text) <= 120 and "截断" in long_text, f"长度 {len(long_text)}")

    # ---- 30. 关闭时不发请求 ----
    _patch_push(enabled=False)
    fake = _FakeSession()
    try:
        result = push.push_once(conn2, session=fake)
    finally:
        config.get = _push_orig_get
    check("推送关闭时直接跳过", result.get("skipped") is True, str(result))
    check("推送关闭时没有任何网络请求", fake.calls == [], str(fake.calls))

    # ---- 31. 开启但缺地址 ----
    _patch_push(webhook_url="")
    fake = _FakeSession()
    try:
        result = push.push_once(conn2, session=fake)
    finally:
        config.get = _push_orig_get
    check("开启但没填 webhook 时返回错误", result.get("ok") is False and "webhook" in (result.get("error") or ""))
    check("缺地址时也不发请求", fake.calls == [])

    # ---- 32. 正常发送 ----
    _patch_push()
    fake = _FakeSession()
    try:
        result = push.push_once(conn2, session=fake)
    finally:
        config.get = _push_orig_get

    check("推送成功返回 ok", result.get("ok") is True, str(result))
    check("只发出一次请求", len(fake.calls) == 1, str(len(fake.calls)))
    call = fake.calls[0]
    check("请求地址来自配置", call["url"].startswith("https://example.invalid/"))
    check(
        "请求体是 text 消息结构",
        call["json"].get("msgtype") == "text" and "content" in call["json"].get("text", {}),
        str(call["json"])[:200],
    )
    check("请求带超时", call["timeout"] is not None)

    # ---- 33. 机器人返回错误 ----
    _patch_push()
    fake = _FakeSession(_FakeResp(body={"errcode": 310000, "errmsg": "invalid token"}, ok=False, status=200))
    try:
        result = push.push_once(conn2, session=fake)
    finally:
        config.get = _push_orig_get
    check("机器人拒绝时 ok 为 False", result.get("ok") is False and result.get("errcode") == 310000, str(result))

    # ---- 34. 没有待办不发空消息 ----
    empty_db = ROOT / "tests" / "_tmp" / f"empty_{datetime.now().strftime('%H%M%S%f')}.db"
    empty_conn = db.connect(empty_db, journal_mode="MEMORY")
    db.init_db(empty_conn)
    _patch_push()
    fake = _FakeSession()
    try:
        result = push.push_once(empty_conn, session=fake)
    finally:
        config.get = _push_orig_get
    empty_conn.close()
    check("没有待办时跳过，不发空消息", result.get("skipped") is True and fake.calls == [], str(result))

    # ---- 35. 代码里没有硬编码地址 ----
    push_src = (ROOT / "core" / "push.py").read_text(encoding="utf-8")
    check(
        "push.py 里没有硬编码 webhook 地址",
        "http://" not in push_src and "https://" not in push_src,
    )

    # ================= 第 7 轮：合同规则检查 =================

    # ---- 36. 规则库 ----
    rules = contract_rules.load_rules()
    categories = {r["category"] for r in rules}
    check("规则库加载成功", len(rules) >= 12, f"共 {len(rules)} 条")
    check(
        "六类规则齐全",
        categories == {"截止时间", "付款条款", "质保与服务", "数据安全与隐私", "违约与责任", "供应商资质"},
        str(sorted(categories)),
    )
    check(
        "每条规则字段完整",
        all(all(r.get(k) for k in ("id", "category", "level", "pattern", "message")) for r in rules),
    )
    check("规则 ID 不重复", len({r["id"] for r in rules}) == len(rules))

    # ---- 37. 样例合同命中 ----
    sample1 = ROOT / "samples" / "采购框架合同-样例1.txt"
    sample2 = ROOT / "samples" / "服务外包合同-样例2.txt"
    sample3 = ROOT / "samples" / "采购合同-样例3-较规范.txt"

    report1 = contract_rules.check_file(sample1)
    hit1 = {r["rule_id"] for r in report1["results"]}
    check(
        "样例1 命中全额预付 / 工作日交付 / 质保过短 / 资质模糊",
        {"PAY-001", "TERM-001", "WAR-001", "QUA-001"} <= hit1,
        str(sorted(hit1)),
    )
    check("样例1 命中 4 条", report1["hit_count"] == 4, f"得到 {report1['hit_count']}")

    report2 = contract_rules.check_file(sample2)
    hit2 = {r["rule_id"] for r in report2["results"]}
    check(
        "样例2 命中超长账期 / 数据外流 / 单方免责 / 责任上限 / 保密失效",
        {"PAY-002", "SEC-001", "LIA-002", "LIA-003", "SEC-003"} <= hit2,
        str(sorted(hit2)),
    )
    check("样例2 高风险 3 条", report2["by_level"]["高"] == 3, str(report2["by_level"]))

    report3 = contract_rules.check_file(sample3)
    check(
        "样例3（较规范）只命中自动续期 1 条，误报控制得住",
        report3["hit_count"] == 1 and report3["results"][0]["rule_id"] == "TERM-003",
        str([(r["rule_id"], r["matched_text"][:20]) for r in report3["results"]]),
    )
    check(
        "质保 180 天不被误判成不足 90 天",
        all(r["rule_id"] != "WAR-001" for r in report3["results"]),
    )

    # ---- 38. 位置引用 ----
    text1 = sample1.read_text(encoding="utf-8")
    expected_line = text1[: text1.index("预付全部货款")].count("\n") + 1
    pay_hit = next(r for r in report1["results"] if r["rule_id"] == "PAY-001")
    check(
        "行号与原文对得上",
        pay_hit["line_no"] == expected_line,
        f"期望 {expected_line}，得到 {pay_hit['line_no']}",
    )
    check("字符偏移可定位原文", text1[pay_hit["char_offset"]:].startswith(pay_hit["matched_text"]))
    check("上下文片段包含命中内容", pay_hit["matched_text"] in pay_hit["snippet"])
    check(
        "所有命中都强制标注需人工复核",
        all(r["needs_human_review"] is True for r in report1["results"] + report2["results"]),
    )

    # ---- 39. 异常输入 ----
    expect_error("空文本被拒绝", lambda: contract_rules.check("   "), ValueError)
    expect_error(
        "不支持的文件类型被拒绝",
        lambda: contract_rules.extract_text(b"abc", "合同.xlsx"),
        ValueError,
    )

    bad_rule_file = ROOT / "tests" / "_tmp" / f"bad_rules_{datetime.now().strftime('%H%M%S%f')}.yaml"
    bad_rule_file.write_text(
        "rules:\n  - id: X-001\n    category: 测试\n    pattern: 'abc'\n", encoding="utf-8"
    )
    expect_error(
        "规则缺字段时报错",
        lambda: contract_rules.load_rules(bad_rule_file),
        contract_rules.RuleError,
    )

    bad_regex_file = ROOT / "tests" / "_tmp" / f"bad_regex_{datetime.now().strftime('%H%M%S%f')}.yaml"
    bad_regex_file.write_text(
        "rules:\n  - id: X-002\n    category: 测试\n    level: 中\n    pattern: '(未闭合'\n    message: '测试'\n",
        encoding="utf-8",
    )
    expect_error(
        "正则写错时报错",
        lambda: contract_rules.load_rules(bad_regex_file),
        contract_rules.RuleError,
    )

    # ---- 40. docx 解析 ----
    try:
        from docx import Document

        doc = Document()
        doc.add_paragraph("甲方应于签约后预付全部货款的 100%。")
        docx_path = ROOT / "tests" / "_tmp" / f"contract_{datetime.now().strftime('%H%M%S%f')}.docx"
        doc.save(docx_path)
        docx_report = contract_rules.check_file(docx_path)
        check(
            "docx 能被解析并命中全额预付",
            any(r["rule_id"] == "PAY-001" for r in docx_report["results"]),
            str([r["rule_id"] for r in docx_report["results"]]),
        )
    except ImportError:
        check("docx 解析（python-docx 未安装，跳过）", True)

    # ================= 第 8 轮：多币种（手动汇率） =================

    # ---- 41. 汇率读取 ----
    check("基准币种为 CNY", repo_fx.base_currency() == "CNY", repo_fx.base_currency())
    check("同币种汇率恒为 1.0", repo_fx.get_rate("CNY", conn=conn2) == 1.0)
    check("没录过的币种返回 None", repo_fx.get_rate("EUR", conn=conn2) is None)

    repo_fx.upsert_rate("USD", 7.0, str(today - timedelta(days=30)), conn=conn2)
    repo_fx.upsert_rate("USD", 7.2, str(today - timedelta(days=1)), conn=conn2)

    check(
        "按业务日期取最近生效汇率（今天用 7.2）",
        repo_fx.get_rate("USD", on_date=str(today), conn=conn2) == 7.2,
        str(repo_fx.get_rate("USD", on_date=str(today), conn=conn2)),
    )
    check(
        "业务日期早于最新汇率时回退到上一条（10 天前用 7.0）",
        repo_fx.get_rate("USD", on_date=str(today - timedelta(days=10)), conn=conn2) == 7.0,
        str(repo_fx.get_rate("USD", on_date=str(today - timedelta(days=10)), conn=conn2)),
    )
    check(
        "早于所有生效日期时返回 None（不拿未来汇率倒推）",
        repo_fx.get_rate("USD", on_date=str(today - timedelta(days=60)), conn=conn2) is None,
    )

    # ---- 42. 汇率录入校验 ----
    expect_error("汇率为 0 被拒绝", lambda: repo_fx.upsert_rate("USD", 0, str(today), conn=conn2), repo_fx.ValidationError)
    expect_error("负汇率被拒绝", lambda: repo_fx.upsert_rate("USD", -1, str(today), conn=conn2), repo_fx.ValidationError)
    expect_error(
        "同币种录汇率被拒绝",
        lambda: repo_fx.upsert_rate("CNY", 1.0, str(today), conn=conn2),
        repo_fx.ValidationError,
    )
    expect_error(
        "币种代码过短被拒绝",
        lambda: repo_fx.upsert_rate("X", 1.0, str(today), conn=conn2),
        repo_fx.ValidationError,
    )

    # ---- 43. 外币报价 ----
    item_fx = ri.create_item({"name": "外币测试物料", "code": "M-FX"}, conn2)
    rq.create_quote(
        {
            "item_id": item_fx,
            "supplier_id": sid3,
            "quoted_price": 10.0,
            "currency": "USD",
            "quote_date": str(today),
        },
        conn2,
    )
    quote_fx = rq.list_quotes(item_id=item_fx, conn=conn2)[0]
    check(
        "外币报价 base_amount = 原币 × 汇率（10 × 7.2 = 72）",
        quote_fx["base_amount"] == 72.0,
        f"得到 {quote_fx['base_amount']}",
    )
    check(
        "原始外币金额与币种保留不被覆盖",
        quote_fx["quoted_price"] == 10.0 and quote_fx["currency"] == "USD",
    )

    expect_error(
        "缺汇率的外币报价被拦下",
        lambda: rq.create_quote(
            {"item_id": item_fx, "supplier_id": sid1, "quoted_price": 10.0,
             "currency": "EUR", "quote_date": str(today)},
            conn2,
        ),
        rq.ValidationError,
    )

    # ---- 44. 外币订单 ----
    oid_fx = ro.create_order(
        {
            "order_no": "PO-FX-001",
            "supplier_id": sid3,
            "item_name_raw": "外币物品",
            "quantity": 2,
            "unit_price": 10.0,
            "currency": "USD",
            "order_date": str(today),
        },
        conn2,
    )
    order_fx = ro.get_order(oid_fx, conn2)
    check(
        "外币订单 total=20、base_amount=144（20 × 7.2）",
        order_fx["total_amount"] == 20.0 and order_fx["base_amount"] == 144.0,
        f"total={order_fx['total_amount']} base={order_fx['base_amount']}",
    )

    expect_error(
        "缺汇率的外币订单被拦下",
        lambda: ro.create_order(
            {"order_no": "PO-FX-002", "supplier_id": sid3, "item_name_raw": "缺汇率的物品",
             "quantity": 1, "total_amount": 100, "currency": "GBP", "order_date": str(today)},
            conn2,
        ),
        ro.ValidationError,
    )

    # ---- 45. 汇率快照不被回刷 ----
    repo_fx.upsert_rate("USD", 8.0, str(today), conn=conn2)
    order_after = ro.get_order(oid_fx, conn2)
    quote_after = rq.list_quotes(item_id=item_fx, conn=conn2)[0]
    check(
        "改汇率后历史订单的 base_amount 不变（仍是 144）",
        order_after["base_amount"] == 144.0,
        f"得到 {order_after['base_amount']}",
    )
    check(
        "改汇率后历史报价的 base_amount 不变（仍是 72）",
        quote_after["base_amount"] == 72.0,
        f"得到 {quote_after['base_amount']}",
    )
    check(
        "新汇率对新录入生效（今天查询得到 8.0）",
        repo_fx.get_rate("USD", on_date=str(today), conn=conn2) == 8.0,
    )

    # ---- 46. 同一天重复录入是覆盖 ----
    repo_fx.upsert_rate("USD", 8.1, str(today), conn=conn2)
    usd_today = db.rows(
        "SELECT rate FROM fx_rates WHERE from_currency = 'USD' AND effective_date = ?",
        (str(today),),
        conn2,
    )
    check(
        "同币种同日期重复录入是覆盖而非新增",
        len(usd_today) == 1 and usd_today[0]["rate"] == 8.1,
        str(usd_today),
    )

    # ---- 47. 外币报价参与比价（用折算价比较） ----
    rq.create_quote(
        {"item_id": item_fx, "supplier_id": sid1, "quoted_price": 65.0,
         "currency": "CNY", "quote_date": str(today)},
        conn2,
    )
    fx_data = rq.effective_quotes(item_fx, conn2)
    check(
        "外币 72 与人民币 65 比价时，最低价取人民币 65",
        fx_data["lowest"]["compare_price"] == 65.0
        and fx_data["lowest"]["currency"] == "CNY",
        str(fx_data["lowest"]["compare_price"] if fx_data["lowest"] else None),
    )
    check("两家有效报价", fx_data["supplier_count"] == 2, str(fx_data["supplier_count"]))

    # ---- 47.5 评分卡多家核对（验收标准第 8 条要求至少 3 家） ----
    multi_cards = [scorecard.compute(s["id"], conn=conn2) for s in rs.list_suppliers(conn=conn2)]
    check(
        f"评分卡对 {len(multi_cards)} 家供应商都能算出结果",
        len(multi_cards) >= 3,
        f"只有 {len(multi_cards)} 家",
    )
    check(
        "各家总分都在 0~100 之间（或为空）",
        all(
            c["weighted_total"] is None or 0 <= c["weighted_total"] <= 100 for c in multi_cards
        ),
        str([(c["supplier_name"], c["weighted_total"]) for c in multi_cards]),
    )
    check(
        "各家等级都在 S/A/B/C 之内（或为空）",
        all(c["grade"] in (None, "S", "A", "B", "C") for c in multi_cards),
        str([(c["supplier_name"], c["grade"]) for c in multi_cards]),
    )

    # ================= 第 9 轮：AI 报价导入（可选） =================

    # ---- 48. 默认关闭，手动路径不受影响 ----
    check("默认未启用 AI 导入", ai_import.is_enabled() is False)
    check("未启用时能说明原因", "ai_import.enabled" in ai_import.disabled_reason())

    _orig_ai_get = config.get

    def _patch_ai(enabled=True, base_url="https://example.invalid/v1", model="test-model"):
        def _fake_get(path, default=None):
            if path == "ai_import":
                return {
                    "enabled": enabled,
                    "base_url": base_url,
                    "model": model,
                    "api_key_env": "PDESK_TEST_API_KEY",
                }
            return _orig_ai_get(path, default)

        config.get = _fake_get

    _saved_key = os.environ.get("PDESK_TEST_API_KEY")
    try:
        _patch_ai()
        os.environ.pop("PDESK_TEST_API_KEY", None)
        check("配好 base_url/model 但没 Key 时仍不启用", ai_import.is_enabled() is False)
        check("缺 Key 时提示环境变量名", "PDESK_TEST_API_KEY" in ai_import.disabled_reason())

        os.environ["PDESK_TEST_API_KEY"] = "test-key-123"
        check("三项齐全后才启用", ai_import.is_enabled() is True)

        _patch_ai(enabled=False)
        check("开关关闭时即使有 Key 也不启用", ai_import.is_enabled() is False)

        # ---- 49. JSON 解析 ----
        _patch_ai()
        rows = ai_import._parse_json_array(
            '```json\n[{"supplier_name":"测试供应商甲","unit_price":10.5,"currency":"CNY"}]\n```'
        )
        check("能剥掉 markdown 代码块", len(rows) == 1 and rows[0]["unit_price"] == 10.5)
        check(
            "缺失字段补成 None（不猜）",
            rows[0]["lead_time_days"] is None and rows[0]["valid_until"] is None and rows[0]["spec"] is None,
            str(rows[0]),
        )
        expect_error("非数组被拒绝", lambda: ai_import._parse_json_array('{"a":1}'), ai_import.ImportError_)
        expect_error("空数组被拒绝", lambda: ai_import._parse_json_array("[]"), ai_import.ImportError_)
        expect_error("非法 JSON 被拒绝", lambda: ai_import._parse_json_array("这不是json"), ai_import.ImportError_)

        # ---- 50. 调用模型 ----
        class _AIFakeResp:
            ok = True
            status_code = 200

            def __init__(self, content):
                self._content = content

            def json(self):
                return {
                    "choices": [{"message": {"content": self._content}}],
                    "usage": {"total_tokens": 120},
                }

        class _AIFakeSession:
            def __init__(self, content):
                self.calls = []
                self._resp = _AIFakeResp(content)

            def post(self, url, json=None, headers=None, timeout=None):
                self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
                return self._resp

        fake_ai = _AIFakeSession(
            '[{"supplier_name":"测试供应商甲","item_name":"某物料","unit_price":10.5,'
            '"currency":"CNY","lead_time_days":15}]'
        )
        ai_result = ai_import.extract_quotes("报价单内容", "quote.xlsx", session=fake_ai)
        check("模型返回被解析成结构化行", len(ai_result["rows"]) == 1, str(ai_result["rows"]))
        check("请求打到 chat/completions", fake_ai.calls[0]["url"].endswith("/chat/completions"))
        check(
            "请求带 Bearer 鉴权头",
            fake_ai.calls[0]["headers"]["Authorization"].startswith("Bearer "),
        )
        check("temperature 为 0（避免抽字段时发挥）", fake_ai.calls[0]["json"]["temperature"] == 0)
        check("system prompt 里写明不推测", "不要推测" in fake_ai.calls[0]["json"]["messages"][0]["content"])
        check("保留模型原始返回", "supplier_name" in ai_result["raw"])

        # ---- 51. Excel 读取 ----
        try:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.append(["供应商", "物料", "单价", "币种"])
            ws.append(["测试供应商甲", "某物料", 10.5, "CNY"])
            xlsx_path = ROOT / "tests" / "_tmp" / f"quote_{datetime.now().strftime('%H%M%S%f')}.xlsx"
            wb.save(xlsx_path)

            xlsx_text = ai_import.read_source(xlsx_path)
            check(
                "xlsx 能被读成文本",
                "测试供应商甲" in xlsx_text and "10.5" in xlsx_text,
                xlsx_text[:120],
            )
        except ImportError:
            check("xlsx 读取（openpyxl 未安装，跳过）", True)

        # ---- 52. 供应商匹配 ----
        all_suppliers = rs.list_suppliers(conn=conn2)
        check("供应商精确匹配", ai_import.match_supplier("测试供应商甲", all_suppliers) == sid1)
        check(
            "供应商包含匹配（全称能对上简称）",
            ai_import.match_supplier("测试供应商甲有限公司", all_suppliers) == sid1,
        )
        check("匹配不到返回 None", ai_import.match_supplier("从没见过的供应商", all_suppliers) is None)
        check("空名字返回 None", ai_import.match_supplier("", all_suppliers) is None)

        # ---- 53. 没有 Key 时抽取直接报错 ----
        os.environ.pop("PDESK_TEST_API_KEY", None)
        expect_error(
            "没有 Key 时调用抽取直接报错",
            lambda: ai_import.extract_quotes("文本", "x.txt", session=_AIFakeSession("[]")),
            ai_import.ImportError_,
        )
    finally:
        config.get = _orig_ai_get
        if _saved_key is None:
            os.environ.pop("PDESK_TEST_API_KEY", None)
        else:
            os.environ["PDESK_TEST_API_KEY"] = _saved_key

    # ================= 优化轮：待办边界 / 事务 / 分页 / 规则白名单 / 并发 =================

    # ---- 54. 已取消订单不该继续占着待办 ----
    oid_c = ro.create_order(
        {"order_no": "PO-CXL-001", "supplier_id": sid1, "item_name_raw": "取消测试物品",
         "quantity": 1, "order_date": str(today - timedelta(days=15))},
        conn2,
    )
    for step in ("供应商已确认", "生产中", "已发货", "已到货"):
        ro.update_status(oid_c, step, conn2)
    rr.upsert(
        oid_c,
        {"goods_received": True, "goods_received_date": str(today - timedelta(days=10))},
        conn2,
    )

    before_cancel = todos.build_todos(conn2)
    check(
        "货到超期未收票会进待办",
        any(t["ref"] == "PO-CXL-001" and t["priority"] == 2 for t in before_cancel),
        str([(t["priority"], t["ref"]) for t in before_cancel]),
    )

    ro.cancel_order(oid_c, "客户取消订单", conn2)
    after_cancel = todos.build_todos(conn2)
    check(
        "订单取消后不再出现在待办（第 2/3 类曾漏排除已取消）",
        all(t["ref"] != "PO-CXL-001" for t in after_cancel),
        str([(t["priority"], t["ref"]) for t in after_cancel]),
    )

    # ---- 55. 事务回滚 ----
    quotes_before = int(db.scalar("SELECT COUNT(*) FROM quotes", (), conn2) or 0)
    try:
        with db.transaction(conn2):
            db.execute(
                "INSERT INTO quotes (item_id, supplier_id, quoted_price, quote_date) "
                "VALUES (?, ?, ?, ?)",
                (item2, sid1, 1.0, str(today)),
                conn2,
            )
            raise RuntimeError("模拟事务中途失败")
    except RuntimeError:
        pass
    quotes_after = int(db.scalar("SELECT COUNT(*) FROM quotes", (), conn2) or 0)
    check(
        "事务中途失败整体回滚，不留半条记录",
        quotes_before == quotes_after,
        f"{quotes_before} -> {quotes_after}",
    )

    with db.transaction(conn2):
        db.execute(
            "INSERT INTO quotes (item_id, supplier_id, quoted_price, quote_date) "
            "VALUES (?, ?, ?, ?)",
            (item2, sid1, 2.0, str(today)),
            conn2,
        )
    quotes_committed = int(db.scalar("SELECT COUNT(*) FROM quotes", (), conn2) or 0)
    check(
        "事务正常结束会提交",
        quotes_committed == quotes_before + 1,
        f"{quotes_before} -> {quotes_committed}",
    )

    # ---- 56. 列表上限与计数 ----
    check(
        "count_orders 与不带 limit 的列表长度一致",
        ro.count_orders(conn=conn2) == len(ro.list_orders(conn=conn2)),
    )
    check("limit 生效", len(ro.list_orders(limit=2, conn=conn2)) == 2)
    check(
        "带筛选时 count 与 list 仍一致",
        ro.count_orders(only_active=True, conn=conn2)
        == len(ro.list_orders(only_active=True, conn=conn2)),
    )

    # ---- 57. 规则 exclude 白名单 ----
    fair_text = "因甲方自身原因造成的损失，乙方不承担任何责任。"
    fair_report = contract_rules.check(fair_text)
    check(
        "合理免责被 exclude 跳过",
        fair_report["hit_count"] == 0 and fair_report["suppressed"] >= 1,
        f"hit={fair_report['hit_count']} suppressed={fair_report['suppressed']}",
    )

    unfair_text = "乙方对服务过程中产生的损失概不负责。"
    unfair_report = contract_rules.check(unfair_text)
    check(
        "没有免责理由的单方免责仍然报出",
        any(r["rule_id"] == "LIA-002" for r in unfair_report["results"]),
        str([r["rule_id"] for r in unfair_report["results"]]),
    )

    # ---- 58. 每线程独立连接 ----
    import threading

    _orig_db_path = config.db_path
    _saved_journal = os.environ.get("PDESK_JOURNAL_MODE")
    thread_db = ROOT / "tests" / "_tmp" / f"thread_{datetime.now().strftime('%H%M%S%f')}.db"
    config.db_path = lambda: thread_db
    os.environ["PDESK_JOURNAL_MODE"] = "MEMORY"
    try:
        main_conn = db.get_conn()
        db.init_db(main_conn)
        seen: dict = {}

        def _worker() -> None:
            seen["conn"] = db.get_conn()
            seen["count"] = db.scalar("SELECT COUNT(*) FROM suppliers")

        worker = threading.Thread(target=_worker)
        worker.start()
        worker.join()

        check("不同线程拿到不同连接对象", seen.get("conn") is not main_conn)
        check("子线程的连接可用（空库返回 0）", seen.get("count") == 0, str(seen.get("count")))
    finally:
        db.close_conn()
        config.db_path = _orig_db_path
        if _saved_journal is None:
            os.environ.pop("PDESK_JOURNAL_MODE", None)
        else:
            os.environ["PDESK_JOURNAL_MODE"] = _saved_journal

    # ---- 59. 维护与快照 ----
    snap_target = ROOT / "tests" / "_tmp" / f"snap_{datetime.now().strftime('%H%M%S%f')}.db"
    try:
        db.snapshot(snap_target, conn2)
        snap_conn = db.connect(snap_target, journal_mode="MEMORY")
        snap_orders = int(db.scalar("SELECT COUNT(*) FROM orders", (), snap_conn) or 0)
        live_orders = int(db.scalar("SELECT COUNT(*) FROM orders", (), conn2) or 0)
        snap_conn.close()
        check(
            "导出的快照是自包含单文件，数据完整",
            snap_target.exists() and snap_orders == live_orders,
            f"快照 {snap_orders} / 当前 {live_orders}",
        )
    except Exception as exc:  # noqa: BLE001
        check("导出的快照是自包含单文件", False, f"{type(exc).__name__}: {exc}")

    try:
        maint = db.maintenance(conn2, path=db_file)
        check(
            "VACUUM 能执行并返回整理前后大小",
            isinstance(maint, dict) and "size_before" in maint and "size_after" in maint,
            str(maint),
        )
    except Exception as exc:  # noqa: BLE001
        check("VACUUM 能执行并返回整理前后大小", False, f"{type(exc).__name__}: {exc}")

    # ---- 60. SQL 注入与特殊字符 ----
    evil_name = "测试'; DROP TABLE suppliers; --"
    sid_evil = rs.create_supplier({"name": evil_name}, conn2)
    check(
        "带 SQL 片段的名称被当作普通字符串存取",
        rs.get_supplier(sid_evil, conn2)["name"] == evil_name,
    )
    check(
        "suppliers 表没有被删掉",
        int(db.scalar("SELECT COUNT(*) FROM suppliers", (), conn2) or 0) > 0,
    )

    evil_hits = rs.list_suppliers(keyword="'; DROP TABLE", conn=conn2)
    check(
        "搜索框里的引号被参数化，只按字面匹配",
        len(evil_hits) == 1 and evil_hits[0]["id"] == sid_evil,
        str([r["name"] for r in evil_hits]),
    )

    tricky_order_no = 'PO"\';--001'
    oid_evil = ro.create_order(
        {"order_no": tricky_order_no, "supplier_id": sid1, "item_name_raw": '带"引号\'的物品',
         "quantity": 1, "order_date": str(today)},
        conn2,
    )
    check(
        "订单号与品名里的引号原样保存",
        ro.get_order(oid_evil, conn2)["order_no"] == tricky_order_no,
    )

    long_name = "长" * 500
    sid_long = rs.create_supplier({"name": long_name}, conn2)
    check("500 字名称可正常存取", len(rs.get_supplier(sid_long, conn2)["name"]) == 500)

    newline_note = "第一行\n第二行\r\n第三行"
    rs.update_supplier(sid_long, {"risk_notes": newline_note}, conn2)
    check(
        "备注里的换行符原样保留",
        rs.get_supplier(sid_long, conn2)["risk_notes"] == newline_note,
    )

    # ---- 61. 数值边界 ----
    expect_error(
        "负数量被拒绝",
        lambda: ro.create_order(
            {"order_no": "PO-NEG-001", "supplier_id": sid1, "item_name_raw": "负数量",
             "quantity": -5, "order_date": str(today)},
            conn2,
        ),
        ro.ValidationError,
    )
    expect_error(
        "负单价被拒绝",
        lambda: ro.create_order(
            {"order_no": "PO-NEG-002", "supplier_id": sid1, "item_name_raw": "负单价",
             "quantity": 1, "unit_price": -1, "order_date": str(today)},
            conn2,
        ),
        ro.ValidationError,
    )
    expect_error(
        "负报价被拒绝",
        lambda: rq.create_quote(
            {"item_id": item2, "supplier_id": sid1, "quoted_price": -3,
             "quote_date": str(today)},
            conn2,
        ),
        rq.ValidationError,
    )

    big_order = ro.create_order(
        {"order_no": "PO-BIG-001", "supplier_id": sid1, "item_name_raw": "大额订单",
         "quantity": 1_000_000, "unit_price": 9_999.99, "order_date": str(today)},
        conn2,
    )
    big_row = ro.get_order(big_order, conn2)
    check(
        "百万级数量与千万级金额不溢出",
        big_row["total_amount"] == 9_999_990_000.0,
        str(big_row["total_amount"]),
    )

    ro.update_order(big_order, {"total_amount": None}, conn2)
    cleared = ro.get_order(big_order, conn2)
    check(
        "清空总价时同步清空折算金额",
        cleared["total_amount"] is None and cleared["base_amount"] is None,
    )
    ro.update_order(big_order, {"total_amount": 100}, conn2)
    restored = ro.get_order(big_order, conn2)
    check("重新填写总价时恢复折算金额", restored["base_amount"] == 100)
    ro.update_order(big_order, {"item_name_raw": "修改品名"}, conn2)
    check("仅改品名保留金额快照", ro.get_order(big_order, conn2)["base_amount"] == 100)

    conn2.close()
    try:
        db_file.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(db_file) + suffix).unlink(missing_ok=True)
    except OSError:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "─" * 46)
    print(f"通过 {passed} / {total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
