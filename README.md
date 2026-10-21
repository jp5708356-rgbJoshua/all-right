# 个人采购工作台

本地运行、单人使用的中文采购工具，基于 Python、SQLite 和 Streamlit。

管理供应商、物料、订单、比价与货／票／款对账。数据保存在本机，不需要登录，不连接ERP，不开放公网端口。

## 已有功能

- **供应商和物料**：新增、编辑、同名提示；被订单或报价引用的档案不能删除。
- **订单**：顺序推进七个状态，独立标记和恢复异常。
- **仪表盘**：进行中、近期交期、标记异常三个数字。
- **比价**：历史报价、有效期、最低价高亮、有效供应商家数提醒；选中报价删除需二次确认。
- **对账**：独立记录收货、收票、付款；筛选货到未收票、票到未付款、三单未齐。

对账标记不改变订单状态。金额使用定点整数与Decimal；报价目前仅支持人民币。部分框架自带按钮可能显示英文。

尚未实现：待办列表、评分卡、推送、合同、多币种换算及AI导入。配置里保留的相关占位项不会触发这些功能。

## 下载

点击本页面绿色 **Code → Download ZIP**，解压后打开项目文件夹。也可使用Git克隆仓库。

## Mac / Linux首次启动

需要电脑已安装Python。在终端进入项目文件夹，逐行执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

## Windows首次启动

PowerShell中进入项目文件夹：

```powershell
python -m venv .venv
..venvScriptsActivate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

CMD使用`.venvScriptsactivate.bat`激活。若PowerShell禁止激活脚本，可直接运行，无需修改系统策略：

```powershell
..venvScriptspython.exe -m pip install -r requirements.txt
..venvScriptspython.exe -m streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

浏览器打开 **http://127.0.0.1:8501**。使用时保留终端，结束按 **Ctrl+C**。首次安装依赖需要网络，日常操作在本地完成。

以后启动：进入同一文件夹，激活`.venv`，再运行最后一条Streamlit命令即可，不必重复安装。

## 第一次怎么用

1. 在供应商档案新增供应商。
2. 在物料档案新增物料。
3. 创建订单，按按钮推进状态。
4. 选择物料录入供应商报价，查看当前有效最低价。
5. 在对账视图勾选货、票、款并保存。

仓库不包含数据库或演示数据。第一次启动自动创建`data/procurement.db`。

## 数据、备份与升级

- SQLite数据库文件就是全部业务数据。先停止程序，再复制整个项目目录及`config.yaml`和数据目录作备份。
- 不要把数据库放到云同步文件夹或代码仓库。`.gitignore`不影响手动复制备份。
- 升级时先备份，保留自己的`config.yaml`和数据目录，不要用默认配置覆盖它们。
- `db_path`相对项目根目录解析。复制到另一位置或电脑后，保持目录结构并重新建立虚拟环境。
- 当前配置不含真实密钥。以后如填写Webhook/API Key，不要将包含密钥的配置提交到GitHub。
- 联系人、电话、付款账号不出现在列表、报价导出或日志中。

## 配置与测试

`config.yaml`可以修改数据库位置、交期提醒窗口及`quotes.min_suppliers`（默认3）。不满3家是黄色提示，不阻止录入。

已在Python3.9.6、macOS验证。Windows与Python3.12+尚未验证。

测试及边界见[验证说明](docs/验证说明.md)，对账复核见[验收步骤](docs/对账验收步骤.md)。公开仓库用于获取代码，不代表应用已部署到互联网。
