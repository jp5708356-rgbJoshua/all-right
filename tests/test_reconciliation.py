"""3b独立验收；临时副本与合成3a旧库，不连接业务库。每条先采集实际值。"""
import argparse
import ast
import hashlib
import importlib.util
import inspect
import itertools
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch
import yaml

ROOT=Path(__file__).resolve().parent
APP=ROOT.parent
BASELINE=ROOT/'baseline_round3a'
RECORDS=[]


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec)
    sys.modules[name]=value;spec.loader.exec_module(value);return value


def snapshot(con):
    return {table:{'sql':con.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0],
                   'rows':[list(r) for r in con.execute('SELECT * FROM '+table+' ORDER BY id')]}
            for table in ('suppliers','items','orders','quotes')}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.old={n:sys.modules.get(n) for n in ('db','state_machine')}
        for name in ('app.py','db.py','state_machine.py','config.yaml'):shutil.copy2(APP/name,self.root/name)
        self.config=yaml.safe_load((self.root/'config.yaml').read_text());self.config['db_path']='data/test.db';self.config_write()
        self.sm=module('state_machine',self.root/'state_machine.py');old=module('db',BASELINE/'db.py');old.ROOT=self.root;old.init_db()
        self.supplier=old.save_record('suppliers',{'name':'对账测试供应商'});self.item=old.save_record('items',{'name':'对账测试物料'})
        self.order=old.save_order({'order_no':'OLD-ORDER','supplier_id':self.supplier,'item_id':self.item,'item_name_raw':'历史快照','quantity':'1.5','unit_price':'3.52','order_date':'2026-10-21'})
        old.save_quote({'item_id':self.item,'supplier_id':self.supplier,'quoted_price':'3.52','quote_date':'2026-10-21'})
        with closing(old.get_connection()) as con:self.before=snapshot(con)
        self.db=module('db',self.root/'db.py');self.db.init_db();self.at=None

    def tearDown(self):
        for n,v in self.old.items():
            if v is None:sys.modules.pop(n,None)
            else:sys.modules[n]=v
        self.temp.cleanup()

    def config_write(self):
        (self.root/'config.yaml').write_text(yaml.safe_dump(self.config,allow_unicode=True),encoding='utf-8')

    def check(self,case,inputs,expected,actual):
        RECORDS.append({'test':self._testMethodName,'acceptance':self._testMethodDoc,'case':case,'input':inputs,'expected':expected,'actual':actual,'passed':expected==actual})
        self.assertEqual(actual,expected,case)

    def make(self,number):return self.db.save_order({'order_no':number,'supplier_id':self.supplier,'item_name_raw':'测试服务','quantity':'1','order_date':'2026-10-21'})
    def error(self,call):
        try:call()
        except self.db.OrderError as error:return {'field':error.field,'message':str(error)}
        return None

    def start(self,page='对账视图'):
        from streamlit.testing.v1 import AppTest
        self.at=AppTest.from_file(str(self.root/'app.py'),default_timeout=20).run()
        self.at.sidebar.radio[0].set_value(page).run();self.no_exception()
    def no_exception(self):
        if self.at.exception:raise RuntimeError(str([e.message for e in self.at.exception]))
    def w(self,kind,label):return next(w for w in getattr(self.at,kind) if w.label==label)
    def set(self,kind,label,value):self.w(kind,label).set_value(value).run();self.no_exception()
    def click(self,label):self.w('button',label).click().run();self.no_exception()

    def test_incremental_upgrade(self):
        """A-1：3a带数据增量，仅新增reconciliation，旧四表原样，重复初始化。"""
        self.db.init_db()
        with closing(self.db.get_connection()) as con:
            self.check('旧四表原样','3a1供应商1物料1订单1报价→3b初始化两次',self.before,snapshot(con))
            self.check('业务表范围','sqlite_master',['items','orders','quotes','reconciliation','suppliers'],[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")])
            self.check('不预建对账记录','升级后现有订单',0,con.execute('SELECT COUNT(*) FROM reconciliation').fetchone()[0])
            self.check('无触发器','sqlite_master',[],[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")])
            fk=[dict(r) for r in con.execute('PRAGMA foreign_key_list(reconciliation)')]
            self.check('级联外键','foreign_key_list',['orders','order_id','id','CASCADE'],[fk[0]['table'],fk[0]['from'],fk[0]['to'],fk[0]['on_delete']])

    def test_api_signature_and_field_whitelist(self):
        """B-1：order_id定位参数，values仅五字段，拒绝关联/状态/其他字段。"""
        self.check('签名','inspect.signature',['order_id','values'],list(inspect.signature(self.db.save_reconciliation).parameters))
        for field in ('order_id','id','status','is_exception','supplier_id','total_amount'):
            e=self.error(lambda:self.db.save_reconciliation(self.order,{field:1}))
            self.check('拒绝非业务字段',field,'form',e['field'] if e else None)
        self.check('失败不建记录','非法字段调用后',None,self.db.get_reconciliation(self.order))

    def test_lazy_all_zero_and_upsert(self):
        """B-2：首次全0也建记录，重复保存同一行；取消后保留全0。"""
        self.check('首次无记录','未保存',None,self.db.get_reconciliation(self.order))
        rid=self.db.save_reconciliation(self.order,{})
        record=self.db.get_reconciliation(self.order)
        self.check('全0创建','values空映射',[self.order,0,0,0,None,None],[record[k] for k in ('order_id','goods_received','invoice_received','payment_done','invoice_no','payment_date')])
        again=self.db.save_reconciliation(self.order,{'goods_received':True,'invoice_no':' INV-1 '})
        self.check('重复保存同id','标记货到并发票号',[rid,1,'INV-1'],[again,self.db.get_reconciliation(self.order)['goods_received'],self.db.get_reconciliation(self.order)['invoice_no']])
        self.db.save_reconciliation(self.order,{'goods_received':False,'invoice_no':''})
        record=self.db.get_reconciliation(self.order)
        self.check('取消保留记录','货0发票清空',[rid,0,None],[record['id'],record['goods_received'],record['invoice_no']])
        with closing(self.db.get_connection()) as con:self.check('唯一行','3次保存',1,con.execute('SELECT COUNT(*) FROM reconciliation WHERE order_id=?',(self.order,)).fetchone()[0])

    def test_optional_fields_and_independent_flags(self):
        """B-3：三布尔独立、票款不强制前序，日期/发票可空、清空。"""
        self.db.save_reconciliation(self.order,{'payment_done':1})
        row=self.db.get_reconciliation(self.order)
        self.check('仅款完成可保存','货0票0款1',[0,0,1,None,None],[row[k] for k in ('goods_received','invoice_received','payment_done','invoice_no','payment_date')])
        self.db.save_reconciliation(self.order,{'payment_date':date(2026,10,21),'invoice_no':'发票001'})
        self.check('日期存业务日','付款日期date对象','2026-10-21',self.db.get_reconciliation(self.order)['payment_date'])
        self.db.save_reconciliation(self.order,{'payment_date':None,'invoice_no':'   '})
        self.check('可选字段清空','日期None发票空白',[None,None],[self.db.get_reconciliation(self.order)[k] for k in ('payment_date','invoice_no')])

    def test_invalid_fields_and_order(self):
        """B-4：非法布尔/日期/发票/订单给字段错误，整行不变。"""
        self.db.save_reconciliation(self.order,{'goods_received':1});before=self.db.get_reconciliation(self.order)
        for field,value in [('goods_received',2),('invoice_received',None),('payment_done','1'),('payment_done',1.0),('goods_received',-1),('payment_date','2026-02-30'),('payment_date','2026/10/21'),('invoice_no',123)]:
            e=self.error(lambda:self.db.save_reconciliation(self.order,{field:value}))
            self.check('字段错误',{field:value},field,e['field'] if e else None)
            self.check('失败不改行',{field:value},before,self.db.get_reconciliation(self.order))
        for oid in (None,True,0,-1,999999,2**63):
            e=self.error(lambda:self.db.save_reconciliation(oid,{}));self.check('订单定位错误',oid,'order_id',e['field'] if e else None)

    def test_database_constraints(self):
        """B-5：直接SQL验证order_id非空唯一、外键、布尔及日期格式。"""
        self.db.save_reconciliation(self.order,{})
        cases=[('order_id NOT NULL','INSERT INTO reconciliation(order_id) VALUES (?)',(None,)),('order_id UNIQUE','INSERT INTO reconciliation(order_id) VALUES (?)',(self.order,)),('order_id FK','INSERT INTO reconciliation(order_id) VALUES (?)',(999999,))]
        for field,value in [('goods_received',2),('invoice_received',-1),('payment_done',None),('payment_date','bad')]:
            cases.append((field,'UPDATE reconciliation SET '+field+'=? WHERE order_id=?',(value,self.order)))
        with closing(self.db.get_connection()) as con:
            for label,query,params in cases:
                refused=False
                try:con.execute(query,params);con.commit()
                except sqlite3.IntegrityError:refused=True;con.rollback()
                self.check('DB拒绝',{'case':label,'params':params},True,refused)

    def test_eight_combinations_and_missing_filters(self):
        """C-1：8种布尔组合及无记录，各筛选逐单核对，COALESCE覆盖缺失。"""
        mapping={'未标记':self.order}
        for bits in itertools.product((0,1),repeat=3):
            name=''.join(map(str,bits));oid=self.make(name);self.db.save_reconciliation(oid,dict(zip(('goods_received','invoice_received','payment_done'),bits)));mapping[name]=oid
        expected={'全部':set(mapping),'货到未收票':{'100','101'},'票到未付款':{'010','110'},'三单未齐':set(mapping)-{'111'}}
        for filter_name,names in expected.items():
            rows=self.db.list_reconciliation(filter_name);found={name for name,oid in mapping.items() if oid in {r['order_id'] for r in rows}}
            self.check('筛选完整集合',{'filter':filter_name,'samples':['未标记','000','001','010','011','100','101','110','111']},sorted(names),sorted(found))
            for name in mapping:self.check('逐样本归属',{'filter':filter_name,'sample':name},name in names,mapping[name] in {r['order_id'] for r in rows})
        row=next(r for r in self.db.list_reconciliation() if r['order_id']==self.order)
        self.check('缺失左连接默认','未标记',[None,0,0,0],[row[k] for k in ('reconciliation_id','goods_received','invoice_received','payment_done')])
        e=self.error(lambda:self.db.list_reconciliation('任意SQL'))
        self.check('筛选白名单','非法名称','filter',e['field'] if e else None)

    def test_marking_never_changes_orders(self):
        """D-1：对账新增/更新/取消前后，订单与报价整行完全不变。"""
        for values in ({},{'goods_received':1,'invoice_received':1,'payment_done':1,'invoice_no':'I','payment_date':'2026-10-21'},{'goods_received':0,'invoice_received':0,'payment_done':0,'invoice_no':None,'payment_date':None}):
            self.db.save_reconciliation(self.order,values)
            with closing(self.db.get_connection()) as con:self.check('旧四表无副作用',values,self.before,snapshot(con))

    def test_real_state_transition_preserves_reconciliation(self):
        """D-2：真实已下单→供应商已确认，再直接读reconciliation整行比对。"""
        self.db.save_reconciliation(self.order,{'goods_received':1,'invoice_no':'KEEP','payment_date':'2026-10-21'})
        with closing(self.db.get_connection()) as con:before=dict(con.execute('SELECT * FROM reconciliation WHERE order_id=?',(self.order,)).fetchone())
        before_order=self.db.get_order(self.order)
        ok,message=self.db.perform_order_action(self.order,'advance',self.sm.CONFIRMED)
        # The action name is read from the centralized module; this test must execute, not simulate it.
        with closing(self.db.get_connection()) as con:after=dict(con.execute('SELECT * FROM reconciliation WHERE order_id=?',(self.order,)).fetchone())
        self.check('流转实际成功',{'action':'advance','target':self.sm.CONFIRMED},True,ok)
        self.check('订单确已推进','前后状态',[self.sm.ORDERED,self.sm.CONFIRMED],[before_order['status'],self.db.get_order(self.order)['status']])
        self.check('对账原始行不变','SELECT * reconciliation前后',before,after)

    def test_delete_cascade_only_target(self):
        """E-1：删除订单级联清理，仅删目标对账，其他订单记录保留。"""
        other=self.make('OTHER');self.db.save_reconciliation(self.order,{'invoice_received':1});self.db.save_reconciliation(other,{'payment_done':1})
        before=self.db.get_reconciliation(other);self.db.delete_order(self.order)
        self.check('级联删除','删OLD-ORDER',[None,None],[self.db.get_order(self.order),self.db.get_reconciliation(self.order)])
        self.check('其他记录保留','OTHER',before,self.db.get_reconciliation(other))
        with closing(self.db.get_connection()) as con:self.check('外键检查','PRAGMA foreign_key_check',[],[list(r) for r in con.execute('PRAGMA foreign_key_check')])

    def test_transaction_failure_rollback(self):
        """F-1：仅自动故障注入，UPDATE中触发完整性错误，跨行临时修改回滚。"""
        other=self.make('OTHER');self.db.save_reconciliation(self.order,{'invoice_no':'OLD'});self.db.save_reconciliation(other,{'invoice_no':'OTHER'})
        before=[self.db.get_reconciliation(i) for i in (self.order,other)]
        with closing(self.db.get_connection()) as con:
            con.executescript('''CREATE TRIGGER injected_reconciliation_failure AFTER UPDATE ON reconciliation
                WHEN NEW.invoice_no='FAIL'
                BEGIN
                    UPDATE reconciliation SET invoice_no='TEMP' WHERE order_id='''+str(other)+''';
                    SELECT RAISE(ABORT,'injected integrity error');
                END;''')
        e=self.error(lambda:self.db.save_reconciliation(self.order,{'invoice_no':'FAIL'}))
        self.check('中文非原始异常','触发器RAISE(ABORT)',{'field':'form','message':'对账未保存，请检查字段约束并刷新订单列表'},e)
        self.check('两行都回滚','目标OLD/旁行OTHER',before,[self.db.get_reconciliation(i) for i in (self.order,other)])
        self.db.save_reconciliation(self.order,{'invoice_no':'OK'});self.check('失败后可保存','合法再写','OK',self.db.get_reconciliation(self.order)['invoice_no'])

    def test_transaction_begin_immediate(self):
        """F-2：实际SQL跟踪保存BEGIN IMMEDIATE、COMMIT，无订单UPDATE。"""
        statements=[];original=self.db.get_connection
        def tracked():
            con=original();con.set_trace_callback(statements.append);return con
        with patch.object(self.db,'get_connection',tracked):self.db.save_reconciliation(self.order,{})
        self.check('显式事务','SQL trace',True,any(s=='BEGIN IMMEDIATE' for s in statements))
        self.check('实际提交','SQL trace',True,any(s=='COMMIT' for s in statements))
        self.check('不写订单','SQL trace',[],[s for s in statements if s.lstrip().upper().startswith(('UPDATE ORDERS','INSERT INTO ORDERS','DELETE FROM ORDERS'))])

    def test_ui_three_display_states(self):
        """G-1：未标记/全0/完成三种UI，灰字与绿色对勾样式。"""
        zero=self.make('ZERO');full=self.make('FULL');self.db.save_reconciliation(zero,{});self.db.save_reconciliation(full,dict(goods_received=1,invoice_received=1,payment_done=1))
        self.start();frame=self.at.dataframe[0].value
        for number,expected in [('OLD-ORDER','未标记'),('ZERO','未完成'),('FULL','✓')]:
            self.check('三列显示',number,[expected]*3,frame[frame['订单号']==number][['货','票','款']].iloc[0].tolist())
        self.check('不显示状态','列名',['订单号','供应商','物料','下单日期','货','票','款'],list(frame.columns))
        css=self.at.dataframe[0].proto.styler.styles
        self.check('灰绿样式','Styler CSS',[True,True],['#6b7280' in css,'#16803d' in css])

    def test_ui_create_clear_and_filter_independence(self):
        """G-2：首次全0保存、勾选/取消、可选字段清空，详情独立于筛选。"""
        self.start();self.check('说明可见','对账页',True,any('对账标记不改变订单状态' in i.value for i in self.at.info))
        self.click('保存对账标记');self.check('全0保存后显示','首次保存',['未完成']*3,self.at.dataframe[0].value[['货','票','款']].iloc[0].tolist())
        self.set('checkbox','货已收到',True);self.set('text_input','发票号（可空）','INV-UI');self.set('date_input','付款日期（可空）',date(2026,10,21));self.click('保存对账标记')
        self.check('保存UI值','货1/发票/付款日',[1,'INV-UI','2026-10-21'],[self.db.get_reconciliation(self.order)[k] for k in ('goods_received','invoice_no','payment_date')])
        self.set('checkbox','货已收到',False);self.set('text_input','发票号（可空）','');self.click('清空付款日期');self.click('保存对账标记')
        self.check('取消与清空','货0/空号/空日',[0,None,None],[self.db.get_reconciliation(self.order)[k] for k in ('goods_received','invoice_no','payment_date')])
        self.set('selectbox','对账筛选','货到未收票');self.check('空筛选可见','全0',True,any('当前筛选下暂无订单' in i.value for i in self.at.info))
        self.set('checkbox','货已收到',True);self.click('保存对账标记');self.check('过滤外详情仍可编辑','空筛选中保存货1',['OLD-ORDER'],self.at.dataframe[0].value['订单号'].tolist())

    def test_ui_invalid_date_error(self):
        """G-3：仅自动故障注入，字段错误和保存区摘要均显示，不冒充自然浏览器复现。"""
        self.start()
        with patch.object(self.db,'save_reconciliation',side_effect=self.db.ReconciliationError('payment_date','请输入真实有效的日期')):
            self.click('保存对账标记')
        self.check('两处错误','payment_date注入',2,len([e for e in self.at.error if '真实有效' in e.value]))
        self.check('失败未建记录','注入错误后',None,self.db.get_reconciliation(self.order))

    def test_ui_order_delete_and_state_notice(self):
        """E-2：订单详情有独立状态说明、级联删除提示和原确认流程。"""
        self.db.save_reconciliation(self.order,{});self.start('订单');self.set('selectbox','新增或打开订单',self.order)
        self.check('状态区说明','订单详情',True,any(c.value=='对账标记不改变此处状态' for c in self.at.caption))
        self.check('级联删除提示','确认区',True,any(w.value=='该订单的对账记录将一并删除' for w in self.at.warning))
        self.check('删除仍需确认','未勾确认',True,self.w('button','删除测试订单').disabled)
        self.set('checkbox','确认永久删除这条测试订单',True);self.click('删除测试订单')
        self.check('UI删除级联','确认删除',[None,None],[self.db.get_order(self.order),self.db.get_reconciliation(self.order)])

    def test_ui_switch_records_preserves_saved_values(self):
        """G-5：日期/勾选改动不保存→切另一订单→切回，显示各自已保存值。"""
        other=self.make('OTHER')
        self.db.save_reconciliation(self.order,{'goods_received':1,'invoice_no':'A','payment_date':'2026-10-21'})
        self.db.save_reconciliation(other,{'invoice_received':1,'invoice_no':'B'})
        self.start();self.set('selectbox','打开对账订单',self.order)
        self.set('checkbox','货已收到',False);self.set('text_input','发票号（可空）','UNSAVED');self.set('date_input','付款日期（可空）',date(2029,4,5))
        self.set('selectbox','打开对账订单',other)
        self.check('另一条显示自己的值','切B',[False,True,'B',None],[self.w('checkbox','货已收到').value,self.w('checkbox','票已收到').value,self.w('text_input','发票号（可空）').value,self.w('date_input','付款日期（可空）').value])
        self.set('selectbox','打开对账订单',self.order)
        self.check('切回恢复DB值','回A',[True,False,'A',date(2026,10,21)],[self.w('checkbox','货已收到').value,self.w('checkbox','票已收到').value,self.w('text_input','发票号（可空）').value,self.w('date_input','付款日期（可空）').value])
        self.check('不保存不落库','修改A未保存','A',self.db.get_reconciliation(self.order)['invoice_no'])

    def test_empty_order_page(self):
        """G-4：无订单时页面可打开，默认初始化不自动创建记录。"""
        self.db.delete_order(self.order);self.start()
        self.check('无订单提示','0订单',True,any(i.value=='暂无订单，请先在订单页创建订单' for i in self.at.info))
        self.check('无保存按钮','0订单',False,any(b.label=='保存对账标记' for b in self.at.button))

    def test_persistence_copy_path_and_connections(self):
        """H-1：重载/整目录复制/相对路径/新路径及每连接外键。"""
        self.db.save_reconciliation(self.order,{'invoice_received':1,'invoice_no':'PERSIST'});before=self.db.get_reconciliation(self.order)
        self.db.init_db();self.check('重载保留','重复初始化',before,self.db.get_reconciliation(self.order))
        for n in range(2):
            with closing(self.db.get_connection()) as con:self.check('每连接外键',n,1,con.execute('PRAGMA foreign_keys').fetchone()[0])
        target=self.root/'copied';shutil.copytree(self.root,target,ignore=shutil.ignore_patterns('copied','__pycache__'));old=self.db.ROOT;self.db.ROOT=target
        try:self.check('复制完整目录','目标新位置',before,self.db.get_reconciliation(self.order))
        finally:self.db.ROOT=old
        self.config['db_path']='new-data/test2.db';self.config_write();self.db.init_db();self.check('新相对路径','配置新路径',[],self.db.list_reconciliation())
        self.config['db_path']='data/test.db';self.config_write();self.check('切回原路径','原数据',before,self.db.get_reconciliation(self.order))

    def test_scope_and_function_integrity(self):
        """H-2：状态机/报价/数字函数不变，config无新键，新对账函数不访问订单状态。"""
        for filename in ('state_machine.py','config.yaml'):
            self.check('文件原样',filename,hashlib.sha256((BASELINE/filename).read_bytes()).hexdigest(),hashlib.sha256((APP/filename).read_bytes()).hexdigest())
        for filename,names in [('app.py',['dashboard_page','quote_price_text','show_quote_table','quotes_page']),('db.py',['_count_in_statuses','count_due_within_window','dashboard_counts','quote_settings','_decode_quote','get_quote','save_quote','delete_quote','quote_comparison','quotes_for_order'])]:
            texts=[(p/filename).read_text() for p in (BASELINE,APP)]
            for name in names:
                sources=[ast.get_source_segment(t,next(n for n in ast.parse(t).body if isinstance(n,ast.FunctionDef) and n.name==name)) for t in texts]
                
                if filename == 'app.py':
                    for original, translated in {'\"ID\"':'\"编号\"', '独立的 ID':'独立编号', '异常时间（UTC）':'异常时间（世界协调时）', '币种：CNY':'币种：人民币（CNY）'}.items():
                        sources[0] = sources[0].replace(original, translated)
                self.check('函数原文（仅允许列明的中文显示替换）',filename+':'+name,hashlib.sha256(sources[0].encode()).hexdigest(),hashlib.sha256(sources[1].encode()).hexdigest())
        for filename,names in [('db.py',['get_reconciliation','list_reconciliation','save_reconciliation']),('app.py',['reconciliation_page'])]:
            tree=ast.parse((APP/filename).read_text());nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
            self.check('新逻辑无状态字段',filename,[],[n.name for n in nodes if any(isinstance(x,ast.Constant) and x.value in ('status','is_exception','status_before_exception') for x in ast.walk(n))])


def report(folder,result):
    folder.mkdir(parents=True,exist_ok=True);mapping={n:getattr(ReconciliationTests,n).__doc__ for n in unittest.defaultTestLoader.getTestCaseNames(ReconciliationTests)}
    payload={'environment':{'python':platform.python_version(),'os':platform.platform(),'sqlite':sqlite3.sqlite_version,'method':'unittest/API/AppTest，不是浏览器'},'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'mappings':mapping,'assertions':RECORDS,'tracebacks':[{'test':str(t),'traceback':s} for t,s in result.failures+result.errors]}
    (folder/'assertions.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    def cell(v):return json.dumps(v,ensure_ascii=False,default=str).replace('|','\\|').replace('\n','<br>')
    text=f'# 第3b轮逐项断言记录\n\n函数{result.testsRun}，断言{len(RECORDS)}，失败{len(result.failures)}，错误{len(result.errors)}。\n\n环境：'+cell(payload['environment'])+'\n\n实际值在比较前采集；故障注入单独标注，不是自然浏览器复现。\n'
    for name,doc in mapping.items():
        text+='\n## '+name+'\n\n'+doc+'\n\n| 用例 | 输入 | 预期 | 实际 | 结果 |\n|---|---|---|---|---|\n'
        for row in RECORDS:
            if row['test']==name:text+='| '+' | '.join(cell(row[k]) for k in ('case','input','expected','actual'))+' | '+('通过' if row['passed'] else '失败')+' |\n'
    if payload['tracebacks']:text+='\n## 失败详情\n\n```text\n'+str(payload['tracebacks'])+'\n```\n'
    (folder/'assertions.md').write_text(text,encoding='utf-8')
    (folder.parent/'验收对应表.md').write_text('# 第3b轮验收对应表\n\n| 测试函数 | 验收步骤 |\n|---|---|\n'+'\n'.join('| `'+n+'` | '+doc+' |' for n,doc in mapping.items()),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--app',type=Path,default=APP);parser.add_argument('--baseline',type=Path,default=BASELINE);parser.add_argument('--report-dir',type=Path,default=ROOT/'results');args=parser.parse_args();APP=args.app.resolve();BASELINE=args.baseline.resolve()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReconciliationTests));report(args.report_dir,result);sys.exit(not result.wasSuccessful())
