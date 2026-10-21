"""第3a轮隔离验收。仅临时目录；每项断言先采集实际值，再比较。"""
import argparse
import ast
import hashlib
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
import yaml

ROOT = Path(__file__).resolve().parent
APP = ROOT.parent
BASELINE = ROOT / 'baseline_round2'
RECORDS = []
TODAY = date(2026,10,21)


def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value)
    return value


def snapshot(con):
    return {t:{'sql':con.execute('SELECT sql FROM sqlite_master WHERE name=?',(t,)).fetchone()[0],
               'rows':[list(r) for r in con.execute('SELECT * FROM '+t+' ORDER BY id')]}
            for t in ('suppliers','items','orders')}


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.old={n:sys.modules.get(n) for n in ('db','state_machine')}
        for filename in ('db.py','app.py','state_machine.py','config.yaml'):shutil.copy2(APP/filename,self.root/filename)
        self.config=yaml.safe_load((self.root/'config.yaml').read_text()); self.config['db_path']='data/test.db';self.config_write()
        module('state_machine',self.root/'state_machine.py')
        base=module('db',BASELINE/'db.py');base.ROOT=self.root;base.init_db()
        self.suppliers=[base.save_record('suppliers',{'name':'供应商'+str(n)}) for n in (1,2,3)]
        self.items=[base.save_record('items',{'name':'物料'+str(n)}) for n in (1,2)]
        self.order=base.save_order({'order_no':'OLD-1','supplier_id':1,'item_id':1,'item_name_raw':'历史名称','quantity':'1','order_date':TODAY})
        with closing(base.get_connection()) as con:self.before=snapshot(con)
        self.db=module('db',self.root/'db.py'); self.db.init_db();self.at=None

    def tearDown(self):
        for n,v in self.old.items():
            if v is None:sys.modules.pop(n,None)
            else:sys.modules[n]=v
        self.temp.cleanup()

    def config_write(self):
        (self.root/'config.yaml').write_text(yaml.safe_dump(self.config,allow_unicode=True),encoding='utf-8')

    def check(self,case,inputs,expected,actual):
        RECORDS.append({'test':self._testMethodName,'acceptance':self._testMethodDoc,'case':case,'input':inputs,
                        'expected':expected,'actual':actual,'passed':expected==actual})
        self.assertEqual(actual,expected,case)

    def quote(self,**kwargs):
        values={'item_id':1,'supplier_id':1,'quoted_price':'3.52','quote_date':TODAY}
        values.update(kwargs);return self.db.save_quote(values)

    def error(self,call):
        try:call()
        except self.db.OrderError as e:return {'field':e.field,'message':str(e)}
        return None

    def start(self,page='比价视图'):
        from streamlit.testing.v1 import AppTest
        self.at=AppTest.from_file(str(self.root/'app.py'),default_timeout=20).run()
        self.at.sidebar.radio[0].set_value(page).run()
        if self.at.exception:raise RuntimeError(str(self.at.exception))
        if page=='比价视图':self.w('selectbox','选择比价物料').set_value(1).run()

    def w(self,kind,label):return next(w for w in getattr(self.at,kind) if w.label==label)
    def set(self,kind,label,value):self.w(kind,label).set_value(value).run()
    def click(self,label):self.w('button',label).click().run()

    def test_incremental_schema(self):
        """A-1：旧三表结构数据原样保留、仅新增quotes、重复初始化。"""
        self.db.init_db()
        with closing(self.db.get_connection()) as con:
            self.check('旧表快照','第2轮带1订单3供应商2物料→3a初始化两次',self.before,snapshot(con))
            tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            self.check('仅新增quotes','sqlite_master',['items','orders','quotes','reconciliation','suppliers'],tables)
            indexes={r[0]:r[1] for r in con.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='quotes'")}
            self.check('指定索引','查询索引',{'idx_quotes_item_date':'CREATE INDEX idx_quotes_item_date ON quotes(item_id, quote_date)', 'idx_quotes_supplier':'CREATE INDEX idx_quotes_supplier ON quotes(supplier_id)'},indexes)
            self.check('每连接外键','PRAGMA foreign_keys',1,con.execute('PRAGMA foreign_keys').fetchone()[0])

    def test_price_roundtrip(self):
        """B-1：单价零、六位精度、64位边界及编辑精确往返，不舍入。"""
        for value,expected in [('0',0),('3.52',3520000),('1.234567',1234567),('1.2300000',1230000),('9223372036854.775807',2**63-1)]:
            qid=self.quote(quoted_price=value)
            with closing(self.db.get_connection()) as con:row=con.execute('SELECT quoted_price,typeof(quoted_price) FROM quotes WHERE id=?',(qid,)).fetchone()
            self.check('整数编码',value,[expected,'integer'],list(row))
            self.db.save_quote({'quoted_price':value},qid)
            self.check('编辑往返',value,self.db.format_fixed(expected,self.db.SCALE),self.db.get_quote(qid)['quoted_price'])

    def test_invalid_numeric_inputs(self):
        """B-2：脏输入、超精度、负数、空值、超64位直接拒绝。"""
        for value in ['1,5','1e3',' 1.5 ','￥1.5','1.2345678','-1','','9223372036854.775808',1.5]:
            e=self.error(lambda:self.quote(quoted_price=value))
            self.check('可读字段错误',{'quoted_price':value},True,bool(e and e['field']=='quoted_price' and e['message']))
        self.check('没有写入','以上9次错误',[],self.db.quote_comparison(1,TODAY)['quotes'])

    def test_dates_and_lead(self):
        """B-3：日期真实日历日、必填及交期非负整数，可选值留空。"""
        for field,value in [('quote_date',None),('quote_date','2026-02-30'),('valid_until','2026-02-30'),('valid_until','2026/10/21'),('lead_time_days','1.5'),('lead_time_days',-1),('lead_time_days',True),('lead_time_days',' 1'),('lead_time_days',2**63)]:
            e=self.error(lambda:self.quote(**{field:value}));self.check('字段拒绝',{field:value},field,e['field'] if e else None)
        q=self.quote(lead_time_days='0',valid_until='2026-10-20',selection_reason=' ',note='')
        row=self.db.get_quote(q)
        self.check('可选空值及已过期允许',{'lead':'0','valid':'2026-10-20'},[0,None,None,'CNY'],[row['lead_time_days'],row['selection_reason'],row['note'],row['currency']])
        q=self.quote(quote_date='2099-01-01');self.check('未来报价日允许','2099-01-01','2099-01-01',self.db.get_quote(q)['quote_date'])

    def test_database_checks(self):
        """B-4：直接SQL绕过UI验证CHECK和外键，非应用层替代。"""
        q=self.quote()
        invalid=[('quoted_price',-1),('quoted_price',1.2),('quoted_price',None),('currency','USD'),('currency',None),('quote_date','bad'),('quote_date',None),('valid_until','bad'),('lead_time_days',-1),('lead_time_days',1.5),('is_selected',2),('is_selected',None),('item_id',999),('supplier_id',999)]
        with closing(self.db.get_connection()) as con:
            for field,value in invalid:
                refused=False
                try:con.execute('UPDATE quotes SET '+field+'=? WHERE id=?',(value,q));con.commit()
                except sqlite3.IntegrityError:refused=True;con.rollback()
                self.check('DB约束拒绝',{field:value},True,refused)

    def test_currency_and_references(self):
        """B-5：仅CNY、必填引用、不可编辑字段均给中文错误。"""
        for field,value in [('currency','USD'),('item_id',999),('supplier_id',999),('item_id',None),('supplier_id',None),('is_selected',2),('base_amount','1')]:
            e=self.error(lambda:self.quote(**{field:value}));self.check('API可读拒绝',{field:value},True,bool(e and e['message']))

    def test_validity_boundaries(self):
        """C-1：valid_until空/今天/未来有效、昨天过期，today可传。"""
        ids=[self.quote(valid_until=d) for d in (None,TODAY,TODAY+timedelta(days=1),TODAY-timedelta(days=1))]
        result=self.db.quote_comparison(1,TODAY)
        self.check('逐条有效标记',{'today':str(TODAY),'valid_until':['空','今天','明天','昨天']},[1,1,1,0],[next(r for r in result['quotes'] if r['id']==i)['is_valid'] for i in ids])
        self.check('后一天固定口径',{'today':str(TODAY+timedelta(days=2))},1,sum(r['is_valid'] for r in self.db.quote_comparison(1,TODAY+timedelta(days=2))['quotes']))

    def test_lowest_distinct_supplier(self):
        """C-2：同一供应商两条最低价只算1家，第二家加入算2，过期低价排除。"""
        a=self.quote();b=self.quote();expired=self.quote(supplier_id=3,quoted_price='0',valid_until='2026-10-20')
        r=self.db.quote_comparison(1,TODAY)
        self.check('同供应商两条最低','供应商1两条3.52、供应商3过期0',['3.52',1,1,[b,a]],[r['lowest_price'],r['tied_supplier_count'],r['valid_supplier_count'],[q['id'] for q in r['quotes'] if q['is_lowest']]])
        c=self.quote(supplier_id=2);r=self.db.quote_comparison(1,TODAY)
        self.check('两家三条并列','供应商2新增3.52',[2,2,3],[r['tied_supplier_count'],r['valid_supplier_count'],sum(q['is_lowest'] for q in r['quotes'])])
        self.db.save_quote({'valid_until':'2026-10-20'},c)
        self.check('编辑影响比价','将供应商2报价改过期',1,self.db.quote_comparison(1,TODAY)['tied_supplier_count'])

    def test_history_ordering(self):
        """C-3：报价日期倒序，同日id倒序；全部历史均保留。"""
        a=self.quote(quote_date='2026-10-20');b=self.quote(quote_date='2026-10-21');c=self.quote(quote_date='2026-10-21');d=self.quote(quote_date='2026-10-19')
        self.check('稳定排序','日期20/21/21/19',[c,b,a,d],[r['id'] for r in self.db.quote_comparison(1,TODAY)['quotes']])

    def test_immutable_references(self):
        """D-1：API修改物料/供应商均拒绝，原数据不变。"""
        q=self.quote();before=self.db.get_quote(q)
        for field in ('item_id','supplier_id'):
            e=self.error(lambda:self.db.save_quote({field:2},q));self.check('引用不可变',{field:2},field,e['field'])
            self.check('失败原行不变',field,before,self.db.get_quote(q))
        self.db.save_quote({'item_id':1,'supplier_id':1,'note':'可改备注'},q)
        self.check('原引用可重复提交','相同ID及新备注','可改备注',self.db.get_quote(q)['note'])

    def test_selection_and_delete(self):
        """D-2：多条选中并存；二次确认；删除不改另一选中标记。"""
        a=self.quote(is_selected=1,selection_reason='理由A');b=self.quote(is_selected=1,selection_reason='理由B')
        self.check('同时选中','同物料同供应商两条选中',[1,1],[self.db.get_quote(q)['is_selected'] for q in (a,b)])
        self.start();frame=self.at.dataframe[0].value
        self.check('选中状态均展示','两条选中报价表格',['✓','✓'],[frame[frame['编号']==q].iloc[0]['选中'] for q in (a,b)])
        for confirmed,second in [(False,False),(True,False)]:
            e=self.error(lambda:self.db.delete_quote(a,confirmed,second));self.check('缺确认拒绝',[confirmed,second],True,bool(e));self.check('记录仍在',a,True,self.db.get_quote(a) is not None)
        self.db.delete_quote(a,True,True)
        self.check('二次确认删除','a两项确认',[None,1,'理由B'],[self.db.get_quote(a),self.db.get_quote(b)['is_selected'],self.db.get_quote(b)['selection_reason']])
        self.at.run();frame=self.at.dataframe[0].value
        self.check('删除不改另一条显示','删除a后报价b','✓',frame[frame['编号']==b].iloc[0]['选中'])
        c=self.quote();self.db.delete_quote(c,True);self.check('普通删除','仅普通确认',None,self.db.get_quote(c))

    def test_archive_delete_protection(self):
        """D-3：订单引用/报价引用/无引用删除回归，统一中文。"""
        q=self.quote(item_id=2,supplier_id=2)
        for table,key,source in [('suppliers',1,'订单'),('items',1,'订单'),('suppliers',2,'报价'),('items',2,'报价')]:
            try:self.db.delete_record(table,key);actual=None
            except ValueError as e:actual=str(e)
            self.check('引用保护',{'table':table,'id':key,'source':source},'该记录已被订单或报价引用，不能删除',actual)
        self.db.delete_quote(q,True);self.db.delete_record('items',2);self.db.delete_record('suppliers',2)
        self.check('解除引用可删','删报价后删物料2供应商2',[None,None],[self.db.get_record('items',2),self.db.get_record('suppliers',2)])

    def test_order_link_by_id(self):
        """E-1：订单 item_name_raw 与物料档案名不同时，报价只按 item_id 查，不按名称匹配。"""
        a=self.quote();b=self.quote(supplier_id=2);other=self.quote(item_id=2)
        self.check('按关联ID全部报价',{'order_item_id':1,'item_name_raw':'历史名称','other_item_id':2},[b,a],[q['id'] for q in self.db.quotes_for_order(self.order,TODAY)['quotes']])
        with closing(self.db.get_connection()) as con:self.check('报价不改订单','创建三条报价后',self.before,snapshot(con))
        noitem=self.db.save_order({'order_no':'RAW','supplier_id':1,'item_name_raw':'物料1','quantity':'1','order_date':TODAY})
        self.check('名称相同不猜关联','raw=物料1,item_id空',None,self.db.quotes_for_order(noitem,TODAY))

    def test_config_threshold_and_fallback(self):
        """F-1：阈值刷新、缺键默认3及无全局警告；非法配置拒绝。"""
        self.check('默认阈值','完整配置',(3,False),self.db.quote_settings())
        self.config['quotes']['min_suppliers']=4;self.config_write();self.check('重读配置','改4无需重启',(4,False),self.db.quote_settings())
        del self.config['quotes'];self.config_write();self.check('缺整个quotes','删除quotes',(3,True),self.db.quote_settings())
        self.config['quotes']={};self.config_write();self.check('缺叶键','quotes为空映射',(3,True),self.db.quote_settings())
        for value in [0,-1,True,'3',None]:
            self.config['quotes']={'min_suppliers':value};self.config_write();e=self.error(self.db.quote_settings)
            self.check('非法阈值',value,'config',e['field'] if e else None)

    def test_ui_warning_threshold_refresh(self):
        """F-2：0/1/2家警告3家不警告，缺键每次比价渲染一处，不影响其他页。"""
        self.start()
        self.check('无有效报价不显示虚假最低价','0条报价',True,any(s.value=='暂无有效报价' for s in self.at.info))
        for count in range(4):
            if count:self.quote(supplier_id=count);self.at.run()
            warning='有效报价不足 3 家，请确认是否符合比价要求'
            self.check('有效家数警告',count,count<3,warning in [w.value for w in self.at.warning])
        self.config['quotes']['min_suppliers']=4;self.config_write();self.at.run()
        self.check('改配置刷新UI','3家配置改4',True,any('不足 4 家' in w.value for w in self.at.warning))
        del self.config['quotes'];self.config_write()
        msg='config.yaml 缺少 quotes.min_suppliers，已使用默认值 3'
        for iteration in range(2):
            self.at.run();self.check('每次渲染一处缺键警告',iteration,1,[w.value for w in self.at.warning].count(msg))
        self.at.sidebar.radio[0].set_value('仪表盘').run();self.check('其他页无缺键警告','切仪表盘',False,msg in [w.value for w in self.at.warning])

    def test_ui_minimum_and_table(self):
        """C-4：最低价无符号、1家/2家并列，空有效期—、过期标记、稳定行顺序。"""
        a=self.quote();b=self.quote();expired=self.quote(quoted_price='0',valid_until='2026-10-20')
        # UI uses local today, so make expired relative to runtime date, not fixture day.
        self.db.save_quote({'valid_until':(date.today()-timedelta(days=1)).isoformat()},expired)
        self.start();self.check('1家并列UI','同供应商两条最低',True,'最低价 3.52（1 家并列）' in [s.value for s in self.at.success])
        c=self.quote(supplier_id=2);self.at.run();self.check('2家并列UI','第二供应商同最低',True,'最低价 3.52（2 家并列）' in [s.value for s in self.at.success])
        frame=self.at.dataframe[0].value
        self.check('空有效期文案','未填有效期','—',frame[frame['编号']==a].iloc[0]['有效期至'])
        self.check('过期标记','昨天到期','已过期',frame[frame['编号']==expired].iloc[0]['有效性'])
        self.check('两条最低均标记','a、b同供应商',[True,True],[frame[frame['编号']==q].iloc[0]['价格标记']=='最低价' for q in (a,b)])

    def test_ui_create_edit_delete(self):
        """G-1：手工新增/编辑、日期预填/清空、字段摘要、双确认及不可编辑引用。"""
        self.start();self.check('预填本地今天','新增报价',date.today(),self.w('date_input','报价日期（必填）').value)
        self.set('selectbox','报价供应商（必选）',1);self.set('text_input','报价单价（必填，最多六位小数）','1e3');self.click('保存报价')
        self.check('字段及保存区域错误','1e3',2,len([e for e in self.at.error if '标准十进制' in e.value]))
        self.set('text_input','报价单价（必填，最多六位小数）','3.52');self.set('checkbox','标记为选中报价',True);self.click('保存报价')
        self.check('成功无异常','保存后',[],[e.message for e in self.at.exception])
        q=self.db.quote_comparison(1)['quotes'][0]['id'];self.check('选中保存','新增选中',1,self.db.get_quote(q)['is_selected'])
        self.check('引用UI只读','编辑已有报价',False,any(w.label=='报价供应商（必选）' for w in self.at.selectbox))
        self.click('清空报价日期');self.click('保存报价');self.check('日期清空拒绝','清空必填日',True,any('报价日期必填' in e.value for e in self.at.error))
        self.set('date_input','报价日期（必填）',date.today());self.set('date_input','报价有效期至（可空）',date.today());self.click('保存报价');self.click('清空报价有效期至');self.click('保存报价')
        self.check('可空日期清空','有效期至清空保存',None,self.db.get_quote(q)['valid_until'])
        self.set('checkbox','确认永久删除这条报价',True);self.check('选中需二次确认','只勾普通确认',True,self.w('button','删除报价').disabled)
        self.set('checkbox','该报价已标记为选中，删除后选择理由将一并丢失，确定删除？',True);self.check('双确认可删','两项确认',False,self.w('button','删除报价').disabled)
        self.click('删除报价');self.check('删除后空列表','点击删除报价',[],self.db.quote_comparison(1)['quotes'])
        self.check('回到新增','删除后','新增报价',self.w('selectbox','新增或打开报价').format_func(self.w('selectbox','新增或打开报价').value))

    def test_ui_linked_quotes(self):
        """E-2：历史名称快照订单只显示item_id=1全部报价，不显示物料2。"""
        a=self.quote();b=self.quote(supplier_id=2);other=self.quote(item_id=2)
        self.start('订单');self.set('selectbox','新增或打开订单',self.order)
        self.check('只读关联表','item1/raw历史名称',[b,a],self.at.dataframe[1].value['编号'].tolist())
        self.check('详情无报价编辑','订单详情',False,any(w.label=='保存报价' for w in self.at.button))

    def test_scope_and_unchanged_functions(self):
        """H-1：状态机/仪表盘聚合逐字节不变、配置仅新增指定键、新报价函数不访问status。"""
        self.check('状态机文件','SHA对比',hashlib.sha256((BASELINE/'state_machine.py').read_bytes()).hexdigest(),hashlib.sha256((APP/'state_machine.py').read_bytes()).hexdigest())
        for filename,names in [('app.py',['dashboard_page']),('db.py',['_count_in_statuses','count_due_within_window','dashboard_counts'])]:
            texts=[(x/filename).read_text() for x in (BASELINE,APP)]; trees=[ast.parse(t) for t in texts]
            for name in names:
                sources=[ast.get_source_segment(t,next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)) for t,tree in zip(texts,trees)]
                self.check('函数原文不变',filename+':'+name,sources[0],sources[1])
        old=yaml.safe_load((BASELINE/'config.yaml').read_text());new=yaml.safe_load((APP/'config.yaml').read_text());added=new.pop('quotes')
        self.check('唯一配置扩展','删quotes后对比',[old,{'min_suppliers':3}],[new,added])
        tree=ast.parse((APP/'db.py').read_text());new_nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and (n.name in ('quote_settings','_decode_quote','get_quote','save_quote','delete_quote','quote_comparison','quotes_for_order'))]
        self.check('新增比价函数不读状态','查AST常量',[],[n.name for n in new_nodes if any(isinstance(x,ast.Constant) and x.value in ('status','is_exception','status_before_exception') for x in ast.walk(n))])

    def test_persistence_and_connection(self):
        """H-2：重载/复制后报价保留，每连接外键=1，相对db_path从项目定位。"""
        q=self.quote();before=self.db.get_quote(q);self.db.init_db();self.check('重复初始化保留报价',q,before,self.db.get_quote(q))
        for i in range(2):
            with closing(self.db.get_connection()) as con:self.check('独立连接FK',i,1,con.execute('PRAGMA foreign_keys').fetchone()[0])
        target=self.root/'copied';shutil.copytree(self.root,target,ignore=shutil.ignore_patterns('copied','__pycache__'));old=self.db.ROOT;self.db.ROOT=target
        try:self.check('整个目录复制读取',str(target),before,self.db.get_quote(q))
        finally:self.db.ROOT=old


def report(folder,result):
    folder.mkdir(parents=True,exist_ok=True)
    mappings={n:getattr(QuoteTests,n).__doc__ for n in unittest.defaultTestLoader.getTestCaseNames(QuoteTests)}
    payload={'environment':{'python':platform.python_version(),'os':platform.platform(),'sqlite':sqlite3.sqlite_version,'method':'unittest + AppTest，不是浏览器'},'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'mappings':mappings,'assertions':RECORDS,'tracebacks':[{'test':str(t),'traceback':v} for t,v in result.failures+result.errors]}
    (folder/'assertions.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    def cell(v):return json.dumps(v,ensure_ascii=False,default=str).replace('|','\\|').replace('\n','<br>')
    text=f'# 第3a轮逐项自动测试记录\n\n函数{result.testsRun}，断言{len(RECORDS)}，失败{len(result.failures)}，错误{len(result.errors)}。\n\n环境：'+cell(payload['environment'])+'\n\n实际值在断言比较前采集，不由预期回填。\n'
    for name,mapping in mappings.items():
        text+='\n## '+name+'\n\n'+mapping+'\n\n| 用例 | 输入 | 预期 | 实际 | 结果 |\n|---|---|---|---|---|\n'
        for row in RECORDS:
            if row['test']==name:text+='| '+' | '.join(cell(row[k]) for k in ('case','input','expected','actual'))+' | '+('通过' if row['passed'] else '失败')+' |\n'
    if payload['tracebacks']:text+='\n## 错误\n\n```text\n'+str(payload['tracebacks'])+'\n```\n'
    (folder/'assertions.md').write_text(text,encoding='utf-8')
    (folder.parent/'第3a轮回归对应表.md').write_text('# 验收与测试对应表\n\n| 函数 | 验收 |\n|---|---|\n'+'\n'.join('| `'+n+'` | '+m+' |' for n,m in mappings.items()),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--app',type=Path,default=APP);parser.add_argument('--baseline',type=Path,default=BASELINE);parser.add_argument('--report-dir',type=Path,default=ROOT/'results');args=parser.parse_args();APP=args.app.resolve();BASELINE=args.baseline.resolve()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(QuoteTests));report(args.report_dir,result);sys.exit(not result.wasSuccessful())
