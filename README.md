# 完整采购工作台 · 云端部署分支

Python、Streamlit、SQLite，完整功能及强制登录。main分支保留原本地版本。

配置为render.yaml，Docker使用requirements.lock.txt。持久磁盘挂载/data。不包含默认账号密码、业务数据库或密钥。

请读[正式部署与验收](正式部署与验收.md)及[本次验证报告](本次验证报告.md)。GitHub Actions构建Docker并运行持久化、WebSocket和恢复验收；未见绿色结果前不代表容器验收通过。云端仍需单独验收。
