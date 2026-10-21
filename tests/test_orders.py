"""补充验收：每个test函数的docstring指向浏览器验收ID，逐断言输出输入/预期/实际。
所有测试仅复制代码和配置至临时目录，强制改为临时数据库，不打开原业务库。
运行：python supplemental_tests.py --app ../采购工作台_中文 --report-dir ./results
"""
import argparse
import fnmatch
import importlib.util
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

APP_ROOT = None
RECORDS = []
MAPPINGS = {}
EXCEPTION_KEYS = ("is_exception", "exception_reason", "status_before_exception", "exception_marked_at")
LEGACY_DDL = """
                CREATE TABLE IF NOT EXISTS suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                    credit_code TEXT, category TEXT, contact_name TEXT,
                    contact_phone TEXT, bank_account TEXT, qualification_expiry TEXT,
                    risk_level TEXT DEFAULT '低' CHECK(risk_level IN ('低','中','高')),
                    status TEXT DEFAULT '活跃' CHECK(status IN ('活跃','暂停','淘汰')),
                    risk_notes TEXT
                );
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT, name TEXT NOT NULL CHECK(length(trim(name)) > 0),
                    spec TEXT, unit TEXT,
                    historical_low_price REAL, historical_high_price REAL
                );
                CREATE INDEX IF NOT EXISTS idx_suppliers_name ON suppliers(name);
                CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
            """


def snapshot(connection):
    return {table: {"sql": connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0],
                    "columns": [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")],
                    "rows": [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]}
            for table in ("suppliers", "items")}


class SupplementalTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.root = Path(self.sandbox.name) / "app"
        self.root.mkdir()
        for filename in ("app.py", "db.py", "state_machine.py", "config.yaml"):
            shutil.copy2(APP_ROOT / filename, self.root / filename)
        import yaml
        config = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        config["db_path"] = "data/isolated.db"
        (self.root / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (self.root / "data").mkdir()
        with closing(sqlite3.connect(self.root / "data/isolated.db")) as con:
            con.executescript(LEGACY_DDL)
            con.execute("INSERT INTO suppliers(name,qualification_expiry) VALUES (?,?)", ("夹具供应商A", "2030-01-02"))
            con.execute("INSERT INTO suppliers(name,qualification_expiry) VALUES (?,?)", ("夹具供应商B", "2031-02-03"))
            con.execute("INSERT INTO items(name) VALUES (?)", ("夹具物料A",))
            con.execute("INSERT INTO items(name) VALUES (?)", ("夹具物料B",))
            con.commit()
            self.before_upgrade = snapshot(con)
        self.old_modules = {name: sys.modules.get(name) for name in ("state_machine", "db")}
        for name in ("state_machine", "db"):
            spec = importlib.util.spec_from_file_location(name, self.root / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        self.db = sys.modules["db"]
        self.sm = sys.modules["state_machine"]
        self.db.init_db()
        self.at = None

    def tearDown(self):
        for name, module in self.old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.sandbox.cleanup()

    def check(self, case, inputs, expected, actual):
        equal = expected == actual
        RECORDS.append({"test": self._testMethodName, "acceptance": self._testMethodDoc,
                        "case": case, "input": inputs, "expected": expected,
                        "actual": actual, "result": "通过" if equal else "失败"})
        self.assertEqual(actual, expected, case)

    def start(self, page="订单"):
        from streamlit.testing.v1 import AppTest
        self.at = AppTest.from_file(str(self.root / "app.py"), default_timeout=20).run()
        self.render(page=page)

    def render(self, page=None):
        if self.at is None:
            raise RuntimeError("页面尚未启动")
        if page is not None:
            self.at.sidebar.radio[0].set_value(page)
        self.at.run()
        if self.at.exception:
            raise RuntimeError("页面出现未捕获异常：" + str([e.message for e in self.at.exception]))

    def widget(self, kind, label):
        return next(w for w in getattr(self.at, kind) if w.label == label)

    def set_value(self, kind, label, value):
        self.widget(kind, label).set_value(value)
        self.render()

    def click(self, label):
        self.widget("button", label).click()
        self.render()

    def make(self, number="A", **changes):
        values = {"order_no": number, "supplier_id": 1, "item_name_raw": "手填快照",
                  "quantity": "1", "order_date": "2026-10-21"}
        values.update(changes)
        return self.db.save_order(values)

    def edit_values(self, oid, **changes):
        values = {key: self.db.get_order(oid).get(key) for key in self.db.ORDER_EDITABLE}
        values.update(changes)
        return values

    def open_order(self, oid):
        self.set_value("selectbox", "新增或打开订单", oid)

    def frozen(self, oid):
        return {key: self.db.get_order(oid)[key] for key in EXCEPTION_KEYS}

    def capture_error(self, call):
        try:
            call()
        except self.db.OrderError as error:
            return {"field": error.field, "message": str(error)}
        return None

    def test_legacy_upgrade(self):
        """A-3、A-4：第1轮带数据升级、重复初始化、原结构及数据保留。"""
        self.db.init_db()
        with closing(self.db.get_connection()) as con:
            self.check("旧表与数据逐项一致", "第1轮DDL，2供应商+2物料，重复初始化", self.before_upgrade, snapshot(con))
            self.check("业务表范围", "查询sqlite_master，排除sqlite_%内部表", ["items", "orders", "quotes", "reconciliation", "suppliers"],
                       sorted(row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")))

    def test_supplier_qualification_date_switch(self):
        """F-6：供应商资质日期不保存，A→B→A，观察UI与DB。"""
        self.start("供应商档案")
        self.set_value("selectbox", "新增或打开详情", 1)
        self.set_value("date_input", "资质到期日（可空）", date(2029, 4, 5))
        self.set_value("selectbox", "新增或打开详情", 2)
        self.check("切到B显示B的日期", {"A原日期":"2030-01-02", "A未保存输入":"2029-04-05", "B日期":"2031-02-03"},
                   date(2031,2,3), self.widget("date_input", "资质到期日（可空）").value)
        self.set_value("selectbox", "新增或打开详情", 1)
        self.check("切回A不残留未保存输入", "A→B→A，未点保存", date(2030,1,2), self.widget("date_input", "资质到期日（可空）").value)
        self.check("A的DB未改变", "数据库读取供应商1", "2030-01-02", self.db.get_record("suppliers",1)["qualification_expiry"])

    def _order_date_switch(self, field, label):
        a=self.make("A", order_date="2026-10-21", promised_delivery_date="2026-11-01", actual_delivery_date=None)
        b=self.make("B", order_date="2026-10-22", promised_delivery_date=None, actual_delivery_date="2026-11-02")
        original_a=self.db.get_order(a)[field]
        original_b=self.db.get_order(b)[field]
        self.start(); self.open_order(a)
        self.set_value("date_input", label, date(2029,4,5))
        self.open_order(b)
        self.check("切到B显示B的值", {"field":field,"A原值":original_a,"A未保存输入":"2029-04-05","B值":original_b},
                   None if original_b is None else date.fromisoformat(original_b), self.widget("date_input",label).value)
        self.open_order(a)
        self.check("切回A显示DB值", {"field":field,"步骤":"A改日期不保存→B→A"},
                   None if original_a is None else date.fromisoformat(original_a), self.widget("date_input",label).value)
        self.check("A的DB未改变", {"order_id":a,"field":field},original_a,self.db.get_order(a)[field])

    def test_order_date_switch(self):
        """H-5a：下单日期切换，未保存草稿不串记录。"""
        self._order_date_switch("order_date","下单日期（必填）")

    def test_promised_delivery_date_switch(self):
        """H-5b：承诺交期切换，含B为空的场景。"""
        self._order_date_switch("promised_delivery_date","承诺交期（可空）")

    def test_actual_delivery_date_switch(self):
        """H-5c：实际到货日期切换，含A原值为空的场景。"""
        self._order_date_switch("actual_delivery_date","实际到货日期（可空）")

    def _required_error(self, kind, label, empty, field, message):
        oid=self.make()
        before=self.db.get_order(oid)
        self.start();self.open_order(oid)
        self.set_value(kind,label,empty);self.click("保存订单")
        self.check("字段错误可见", {"清空字段":field,"操作":"保存订单"}, [message,"订单未保存："+message], [e.value for e in self.at.error])
        nodes=list(self.at.main)
        field_index=next(i for i,n in enumerate(nodes) if getattr(n,"label",None)==label)
        error_index=next(i for i,n in enumerate(nodes) if getattr(n,"type",None)=="error")
        save_index=next(i for i,n in enumerate(nodes) if getattr(n,"label",None)=="保存订单")
        self.check("组件树错误位于字段之后、保存按钮之前", {"字段索引":field_index,"错误索引":error_index,"保存按钮索引":save_index},
                   True,field_index < error_index < save_index)
        self.check("保存失败无写入", {"field":field,"输入":empty},before,self.db.get_order(oid))

    def test_order_date_clear_buttons(self):
        """D-7d、H-5：R4真实浏览器发现日期删空会恢复旧值，显式清空按钮回归。"""
        oid=self.make(promised_delivery_date="2026-11-01",actual_delivery_date="2026-11-02")
        self.start();self.open_order(oid)
        self.click("清空下单日期")
        self.check("下单日期控件明确为空","点击清空下单日期",None,self.widget("date_input","下单日期（必填）").value)
        self.click("保存订单")
        self.check("必填拒绝可见","下单日期空时保存",["下单日期必填","订单未保存：下单日期必填"],[e.value for e in self.at.error])
        self.set_value("date_input","下单日期（必填）",date(2026,10,21))
        self.click("清空承诺交期");self.click("清空实际到货日期");self.click("保存订单")
        row=self.db.get_order(oid)
        self.check("可选日期清空落库","清空承诺交期与实际到货日期后保存",[None,None],[row["promised_delivery_date"],row["actual_delivery_date"]])

    def test_order_filters_reset(self):
        """H-1：R5日期删除恢复旧值，重置筛选按钮一次清空五项并恢复列表。"""
        self.make("A",is_urgent=1);self.make("B",supplier_id=2)
        self.start()
        self.set_value("selectbox","筛选供应商",2)
        self.set_value("selectbox","筛选状态",self.sm.PRODUCING)
        self.set_value("date_input","下单日期从",date(2026,10,22))
        self.set_value("date_input","下单日期至",date(2026,10,23))
        self.set_value("selectbox","筛选紧急",1)
        self.check("交叉筛选为空","供应商2、生产中、10/22至10/23、紧急",0,len(self.at.dataframe[0].value))
        self.click("重置筛选")
        actual=[self.widget("selectbox","筛选供应商").value,self.widget("selectbox","筛选状态").value,
                self.widget("date_input","下单日期从").value,self.widget("date_input","下单日期至").value,
                self.widget("selectbox","筛选紧急").value]
        self.check("五个筛选全部重置","点击重置筛选",[None]*5,actual)
        self.check("全部列表恢复","重置后真实dataframe",2,len(self.at.dataframe[0].value))

    def test_required_order_no_error(self):
        """D-7a：清空订单号，实际为order_no字段错误而非form错误。"""
        self._required_error("text_input","订单号（必填）","","order_no","订单号必填")

    def test_required_quantity_error(self):
        """D-7b：清空数量，实际为quantity字段错误而非form错误。"""
        self._required_error("text_input","数量（必填，最多六位小数）","","quantity","数量必填")

    def test_required_supplier_error(self):
        """D-7c：清空供应商，实际为supplier_id字段错误。"""
        self._required_error("selectbox","供应商（必选）",None,"supplier_id","请选择有效的供应商或物料")

    def test_required_order_date_error(self):
        """D-7d：清空下单日期，实际为order_date字段错误。"""
        self._required_error("date_input","下单日期（必填）",None,"order_date","下单日期必填")

    def test_form_error_injection_visibility(self):
        """D-7e（故障注入，仅自动补充）：验证form错误可见和组件树位置，不证明自然可触发。"""
        oid=self.make();before=self.db.get_order(oid)
        self.start();self.open_order(oid)
        with patch.object(self.db,"save_order",side_effect=self.db.OrderError("form","注入：表单整体保存失败")):
            self.click("保存订单")
        self.check("注入form错误可见", {"故障注入":"save_order抛OrderError(form)","非自然浏览器复现":True},
                   ["订单未保存：注入：表单整体保存失败"],[e.value for e in self.at.error])
        nodes=list(self.at.main)
        save_index=next(i for i,n in enumerate(nodes) if getattr(n,"label",None)=="保存订单")
        error_index=next(i for i,n in enumerate(nodes) if getattr(n,"type",None)=="error")
        self.check("R2修复后form错误在保存按钮之前", {"保存按钮索引":save_index,"错误索引":error_index,"视觉是否直观":"待真实浏览器"},True,error_index<save_index)
        self.check("注入失败不写库","原订单与保存后完整比对",before,self.db.get_order(oid))

    def test_exception_timestamp_after_edit(self):
        """E-3、E-4、E-6：异常期间编辑数量单价，四字段不变，恢复正常。"""
        oid=self.make(unit_price="2")
        self.start();self.open_order(oid)
        self.set_value("text_input","异常原因（标记时必填）","夹具交期风险");self.click("标记异常")
        before=self.frozen(oid)
        self.set_value("text_input","数量（必填，最多六位小数）","1.5")
        self.set_value("text_input","单价（可空，最多六位小数）","3.25")
        self.click("保存订单")
        self.check("异常四字段完全不变", {"修改quantity":"1.5","修改unit_price":"3.25","修改前四字段":before},before,self.frozen(oid))
        self.check("普通金额确已修改","1.5 × 3.25，ROUND_HALF_UP", "4.88",self.db.get_order(oid)["total_amount"])
        self.click("恢复原状态")
        self.check("恢复后四字段清空","点击恢复原状态",dict(zip(EXCEPTION_KEYS,(0,None,None,None))),self.frozen(oid))

    def test_exception_duplicate_order_no(self):
        """E-5、D-1：异常期间改成重复订单号被拒绝，异常字段和原数据不变。"""
        oid=self.make("A");self.make("B")
        self.db.perform_order_action(oid,"mark_exception",exception_reason="夹具风险")
        before=self.db.get_order(oid)
        self.start();self.open_order(oid)
        self.set_value("text_input","订单号（必填）","B");self.click("保存订单")
        self.check("重复号错误可见",{"A改为":"B","A当前异常":True},["订单号已存在，请使用其他订单号","订单未保存：订单号已存在，请使用其他订单号"],[e.value for e in self.at.error])
        self.check("订单和四异常字段全部不变","保存失败后读取完整原记录",before,self.db.get_order(oid))

    def _material_create(self, item, raw, expected_name, success):
        self.start()
        self.set_value("text_input","订单号（必填）","M")
        self.set_value("selectbox","供应商（必选）",1)
        self.set_value("selectbox","关联物料（可空）",item)
        self.set_value("text_input","物料名称快照（未关联物料时必填）",raw)
        self.set_value("text_input","数量（必填，最多六位小数）","1")
        self.set_value("date_input","下单日期（必填）",date(2026,10,21))
        self.click("保存订单")
        records=self.db.list_orders()
        self.check("是否落库",{"item_id":item,"item_name_raw":raw},1 if success else 0,len(records))
        if success:
            record=records[0]
            self.check("列表与详情快照一致",{"item_id":item,"raw输入":raw},
                       {"list_name":expected_name,"detail_name":expected_name,"detail_item":item},
                       {"list_name":record["item_name_raw"],"detail_name":self.db.get_order(record["id"])["item_name_raw"],"detail_item":record["item_id"]})
            table=self.at.dataframe[0].value
            self.check("页面列表显示快照","读取Streamlit数据表物料快照列",expected_name,table.iloc[0]["物料快照"])
            self.check("保存后编辑区显示快照","读取物料名称快照输入组件",expected_name,self.widget("text_input","物料名称快照（未关联物料时必填）").value)
        else:
            self.check("拒绝错误可见",{"两者皆空":True},["请选择物料或填写物料名称","订单未保存：请选择物料或填写物料名称"],[e.value for e in self.at.error])

    def test_material_item_only(self):
        """C-M1：关联物料、raw空，生成快照。"""
        self._material_create(1,"","夹具物料A",True)

    def test_material_raw_only(self):
        """C-M2：无关联、raw有值，保留自由文本。"""
        self._material_create(None,"自由服务","自由服务",True)

    def test_material_both_present(self):
        """C-M3：关联物料且手填不同名称，列表和详情均保留手填快照。"""
        self._material_create(1,"与档案不同的历史名称","与档案不同的历史名称",True)

    def test_material_both_empty(self):
        """C-M4：关联与raw皆空拒绝。"""
        self._material_create(None,"",None,False)

    def test_material_change_choice(self):
        """D-2、D-3、D-4、D-5：未选拒绝、同步、保留、清空后重新取名。"""
        oid=self.make(item_id=1,item_name_raw="夹具物料A")
        self.start();self.open_order(oid)
        self.set_value("selectbox","关联物料（可空）",2);self.click("保存订单")
        self.check("未选明确拒绝",{"item_id从":1,"改为":2,"快照":"夹具物料A","选择":None},
                   ["已更换关联物料，请明确选择同步为新物料名称或保留当前快照","订单未保存：已更换关联物料，请明确选择同步为新物料名称或保留当前快照"],[e.value for e in self.at.error])
        self.check("未选无写入","数据库item_id",1,self.db.get_order(oid)["item_id"])
        self.set_value("selectbox","物料快照处理（必须选择）","sync");self.click("保存订单")
        self.check("同步成功",{"目标物料":2,"选择":"sync"},"夹具物料B",self.db.get_order(oid)["item_name_raw"])
        self.set_value("selectbox","关联物料（可空）",1)
        self.set_value("selectbox","物料快照处理（必须选择）","keep");self.click("保存订单")
        self.check("保留成功",{"目标物料":1,"选择":"keep"},"夹具物料B",self.db.get_order(oid)["item_name_raw"])
        self.set_value("text_input","物料名称快照（未关联物料时必填）","");self.click("保存订单")
        self.check("清空后重新取名",{"目标物料":1,"raw":""},"夹具物料A",self.db.get_order(oid)["item_name_raw"])

    def test_transaction_save_unique_rollback(self):
        """D-1（事务补充）：唯一冲突回滚，后续连接可继续写。"""
        oid=self.make("A");self.make("B");before=self.db.get_order(oid)
        error=self.capture_error(lambda:self.db.save_order(self.edit_values(oid,order_no="B",quantity="9"),oid))
        self.check("唯一冲突转字段错误",{"订单A改号":"B","数量":"9"},{"field":"order_no","message":"订单号已存在，请使用其他订单号"},error)
        self.check("失败事务完整回滚","原记录全字段",before,self.db.get_order(oid))
        self.db.save_order(self.edit_values(oid,quantity="2"),oid)
        self.check("失败后可新建事务成功写入","随后把数量改2","2",self.db.get_order(oid)["quantity"])

    def test_transaction_action_rejection_and_success(self):
        """E-1、E-7（事务补充）：拒绝跳级不修改，后续合法推进成功。"""
        oid=self.make();before=self.db.get_order(oid)
        ok,reason=self.db.perform_order_action(oid,"advance",self.sm.PRODUCING)
        self.check("跳级拒绝并有原因",{"从":"已下单","到":"生产中"},{"ok":False,"reason":"只允许顺序前进一步，禁止跳级、倒退或原地流转"},{"ok":ok,"reason":reason})
        self.check("拒绝未改库","整行比对",before,self.db.get_order(oid))
        self.check("合法推进",{"从":"已下单","到":"供应商已确认"},(True,"操作成功"),self.db.perform_order_action(oid,"advance",self.sm.CONFIRMED))

    def test_dashboard_boundaries(self):
        """G-1、G-2、G-3：固定today、七笔样本6/2/1及恢复/到货后的变化。"""
        today=date(2026,10,21)
        ids={}
        for number,offset,target in (("T0",0,None),("T7",7,self.sm.PRODUCING),("T8",8,None),("TO",-1,None),
                                      ("TN",None,None),("TA",0,self.sm.ARRIVED),("TC",0,self.sm.COMPLETED)):
            oid=self.make(number,promised_delivery_date=None if offset is None else (today+timedelta(days=offset)).isoformat())
            ids[number]=oid
            for next_status in self.sm.NORMAL_STATUSES[1:]:
                if target is None:break
                self.db.perform_order_action(oid,"advance",next_status)
                if next_status==target:break
        self.db.perform_order_action(ids["T7"],"mark_exception",exception_reason="边界风险")
        rows=[{key:r[key] for key in ("order_no","promised_delivery_date","status","is_exception")} for r in self.db.list_orders()]
        self.check("初始三个数字",{"today":today,"订单":rows},{"active":6,"due":2,"exceptions":1},self.db.dashboard_counts(today))
        self.db.perform_order_action(ids["T7"],"restore")
        self.check("恢复异常后",{"today":today,"操作":"恢复T7"},{"active":6,"due":2,"exceptions":0},self.db.dashboard_counts(today))
        for next_status in self.sm.NORMAL_STATUSES[1:5]:self.db.perform_order_action(ids["T0"],"advance",next_status)
        self.check("T0到货后",{"today":today,"操作":"T0逐级到已到货"},{"active":6,"due":1,"exceptions":0},self.db.dashboard_counts(today))

    def test_decimal_input_and_overflow(self):
        """C-1、C-2、C-3、C-4：金额、脏输入和溢出；逐例记录。"""
        for q,p,total in (("1","1.005",101),("1.5","2.345678",352),("1","0",0),("1",None,None),("10000","10000",10000000000)):
            actual=self.db.encode_numbers(q,p)
            self.check("金额编码",{"quantity":q,"unit_price":p,"rounding":"ROUND_HALF_UP"},total,actual["total_amount"])
        for field in ("quantity","unit_price"):
            for bad in ("1,5","1e3"," 1.5 ","￥1.5","1.0000001"):
                error=self.capture_error(lambda:self.db.encode_numbers(bad if field=="quantity" else "1",bad if field=="unit_price" else "1"))
                self.check("脏输入拒绝",{"field":field,"value":bad},field,None if error is None else error["field"])
        error=self.capture_error(lambda:self.db.encode_numbers("1000000000000","1000000000000"))
        self.check("输入合法但总额超64位",{"quantity":"1000000000000","unit_price":"1000000000000"},
                   {"field":"total_amount","message":"总额超出 SQLite 64 位整数范围，请调整数量或单价"},error)


    def test_state_machine_sequence_and_terminal(self):
        """E-1、E-7、E-8：顺序、跳级、倒退和终态标记异常。"""
        oid=self.make()
        ok,reason=self.db.perform_order_action(oid,"advance",self.sm.PRODUCING)
        self.check("跳级拒绝",{"从":"已下单","到":"生产中"},False,ok)
        self.check("跳级原因",{"动作":"advance"},"只允许顺序前进一步，禁止跳级、倒退或原地流转",reason)
        for target in self.sm.NORMAL_STATUSES[1:]:
            result=self.db.perform_order_action(oid,"advance",target)
            self.check("顺序前进",{"order_id":oid,"target":target},(True,"操作成功"),result)
            self.check("状态落库",{"target":target},target,self.db.get_order(oid)["status"])
        for action,target,reason_input in (("advance",self.sm.ORDERED,None),("mark_exception",None,"终态异常")):
            actual=self.db.perform_order_action(oid,action,target,reason_input)
            self.check("终态拒绝",{"action":action,"target":target,"异常原因":reason_input},
                       (False,"已完成为终态，不可流转或标记异常"),actual)
        self.check("终态无按钮",{"status":"已完成","is_exception":0},[],self.sm.get_available_actions(self.sm.COMPLETED,0))

    def test_exception_restore_invalid_sources(self):
        """E-6（API补充）：异常来源空、非法、已完成、不一致分别拒绝且不写库。"""
        oid=self.make()
        self.db.perform_order_action(oid,"mark_exception",exception_reason="来源边界")
        for source,reason in ((None,"异常来源为空，不能恢复"),("非法","异常来源状态非法，不能恢复"),
                             (self.sm.COMPLETED,"异常来源为已完成，不能恢复终态"),
                             (self.sm.PRODUCING,"异常来源与当前状态不一致，不能恢复")):
            with closing(self.db.get_connection()) as con:
                con.execute("UPDATE orders SET status_before_exception=? WHERE id=?",(source,oid));con.commit()
            before=self.db.get_order(oid)
            self.check("非法来源拒绝",{"status":"已下单","is_exception":1,"source":source},
                       (False,reason),self.db.perform_order_action(oid,"restore"))
            self.check("拒绝保持整行",{"source":source,"操作":"恢复"},before,self.db.get_order(oid))
        with closing(self.db.get_connection()) as con:
            con.execute("UPDATE orders SET status_before_exception=? WHERE id=?",(self.sm.ORDERED,oid));con.commit()
        self.check("恢复合法来源",{"source":"已下单"},(True,"操作成功"),self.db.perform_order_action(oid,"restore"))

    def test_database_unique_and_foreign_keys(self):
        """D-1、D-6、F-1（DB补充）：绕过UI直接插入重复号与不存在外键。"""
        oid=self.make("A")
        with closing(self.db.get_connection()) as con:
            for name,sql,params in (
                    ("重复订单号", "INSERT INTO orders(order_no,supplier_id,item_name_raw,quantity,order_date) VALUES (?,?,?,?,?)",("A",1,"手填",1000000,"2026-10-21")),
                    ("不存在供应商", "UPDATE orders SET supplier_id=? WHERE id=?",(999999,oid)),
                    ("不存在物料", "UPDATE orders SET item_id=? WHERE id=?",(999999,oid))):
                result="未拒绝"
                try:con.execute(sql,params)
                except sqlite3.IntegrityError:result="IntegrityError"
                self.check(name,{"sql":sql,"params":params},"IntegrityError",result)
            self.check("外键实际开启","PRAGMA foreign_keys",1,con.execute("PRAGMA foreign_keys").fetchone()[0])
        for field in ("supplier_id","item_id"):
            error=self.capture_error(lambda:self.db.save_order(self.edit_values(oid,**{field:999999}),oid))
            self.check("引用错误字段定位",{"field":field,"value":999999},field,error["field"] if error else None)

    def test_database_checks(self):
        """C-3、C-M4、E-8（DB补充）：直接SQL验证各CHECK，应用层验证真实日期。"""
        oid=self.make()
        with closing(self.db.get_connection()) as con:
            for column,value in (("quantity",0),("quantity",-1),("quantity",1.2),("unit_price",-1),("unit_price",1.2),
                                 ("total_amount",-1),("total_amount",1.2),("contract_id",1),("order_date","20261021"),
                                 ("promised_delivery_date","bad"),("actual_delivery_date","bad"),("status","异常"),("is_exception",2)):
                result="未拒绝"
                try:con.execute(f"UPDATE orders SET {column}=? WHERE id=?",(value,oid))
                except sqlite3.IntegrityError:result="IntegrityError"
                self.check("DB CHECK拒绝",{"column":column,"value":value},"IntegrityError",result)
            result="未拒绝"
            try:con.execute("UPDATE orders SET item_id=NULL,item_name_raw='  ' WHERE id=?",(oid,))
            except sqlite3.IntegrityError:result="IntegrityError"
            self.check("物料二选一CHECK",{"item_id":None,"raw":"  "},"IntegrityError",result)
        error=self.capture_error(lambda:self.db.save_order(self.edit_values(oid,order_date="2026-02-30"),oid))
        self.check("真实日历拒绝",{"order_date":"2026-02-30"},{"field":"order_date","message":"请输入真实有效的日期"},error)

    def test_transaction_save_sql_failure_rollback(self):
        """D-1（故障注入，仅自动补充）：保存中SQL失败，触发器先写另一订单，检查两行均回滚。"""
        a=self.make("A");b=self.make("B")
        before={oid:self.db.get_order(oid) for oid in (a,b)}
        with closing(self.db.get_connection()) as con:
            con.executescript("""CREATE TRIGGER injected_save_failure AFTER UPDATE OF order_no ON orders
                WHEN NEW.order_no='FAILTEST'
                BEGIN
                    UPDATE orders SET exception_note='注入临时写入' WHERE order_no='B';
                    SELECT RAISE(FAIL,'injected save failure');
                END;""")
        error=self.capture_error(lambda:self.db.save_order(self.edit_values(a,order_no="FAILTEST"),a))
        self.check("SQL失败转form错误",{"故障注入":"AFTER UPDATE写B后RAISE(FAIL)"},
                   {"field":"form","message":"订单未保存，请检查字段约束"},error)
        self.check("两行均回滚",{"A尝试改号":"FAILTEST","B临时备注":"注入临时写入"},before,
                   {oid:self.db.get_order(oid) for oid in (a,b)})

    def test_transaction_action_sql_failure_rollback(self):
        """E-7（故障注入，仅自动补充）：动作中SQL失败，另一订单临时写入也必须回滚。"""
        a=self.make("A");b=self.make("B")
        before={oid:self.db.get_order(oid) for oid in (a,b)}
        with closing(self.db.get_connection()) as con:
            con.executescript("""CREATE TRIGGER injected_action_failure AFTER UPDATE OF status ON orders
                BEGIN
                    UPDATE orders SET exception_note='注入临时写入' WHERE order_no='B';
                    SELECT RAISE(FAIL,'injected action failure');
                END;""")
        actual="未抛异常"
        try:self.db.perform_order_action(a,"advance",self.sm.CONFIRMED)
        except sqlite3.IntegrityError:actual="IntegrityError（页面外层将转通用数据库提示）"
        self.check("捕获注入的SQL错误",{"故障注入":"AFTER UPDATE状态写B后RAISE(FAIL)"},
                   "IntegrityError（页面外层将转通用数据库提示）",actual)
        self.check("两行均回滚",{"A尝试状态":"供应商已确认","B临时备注":"注入临时写入"},before,
                   {oid:self.db.get_order(oid) for oid in (a,b)})


def write_reports(directory, result, selected):
    directory.mkdir(parents=True,exist_ok=True)
    payload={"environment":{"python":platform.python_version(),"os":platform.platform(),"sqlite":sqlite3.sqlite_version,
                            "browser":"未执行：这是AppTest/API测试，不是浏览器"},
             "tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
             "mappings":{name:getattr(SupplementalTests,name).__doc__ for name in selected},
             "assertions":RECORDS,"tracebacks":[{"test":str(test),"traceback":text} for test,text in result.failures+result.errors]}
    (directory/"assertions.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    def cell(value):
        return json.dumps(value,ensure_ascii=False,default=str).replace("|","\\|").replace("\n","<br>")
    lines=["# 补充自动测试逐项记录", "", "环境："+cell(payload["environment"]), "",
           f"函数{result.testsRun}个，断言{len(RECORDS)}条，失败{len(result.failures)}，执行错误{len(result.errors)}。", "",
           "实际列为执行时采集，不根据预期回填。通过不等于真实浏览器验收通过。组件树位置不等于视觉可用性。", ""]
    for name in selected:
        lines += ["## "+name,"",getattr(SupplementalTests,name).__doc__,"",
                  "| 用例 | 输入 | 预期 | 实际 | 结果 |","|---|---|---|---|---|"]
        rows=[row for row in RECORDS if row["test"]==name]
        lines += ["| "+" | ".join(cell(row[key]) for key in ("case","input","expected","actual","result"))+" |" for row in rows]
        if not rows:lines.append("| 执行中断 | 见tracebacks | 未完成 | 未完成 | 未通过 |")
        lines.append("")
    if payload["tracebacks"]:lines += ["## 执行错误","", "```text",str(payload["tracebacks"]),"```"]
    (directory/"assertions.md").write_text("\n".join(lines),encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--app",type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--report-dir",type=Path,default=Path(__file__).resolve().parent/"results")
    parser.add_argument("--pattern",default="test_*")
    args=parser.parse_args()
    APP_ROOT=args.app.resolve()
    selected=[name for name in unittest.defaultTestLoader.getTestCaseNames(SupplementalTests) if fnmatch.fnmatch(name,args.pattern)]
    if not selected:parser.error("没有匹配的测试函数")
    suite=unittest.TestSuite(SupplementalTests(name) for name in selected)
    outcome=unittest.TextTestRunner(verbosity=2).run(suite)
    write_reports(args.report_dir,outcome,selected)
    sys.exit(0 if outcome.wasSuccessful() else 1)
