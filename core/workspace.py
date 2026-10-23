"""每日工作空间：标识及按日备份。"""
from datetime import date
from . import config, db


def daily_backup():
    path = config.db_path()
    target = path.parent / 'backups' / f'{path.stem}-{date.today().isoformat()}.db'
    if not target.exists():
        db.snapshot(target)
    return target


def banner(st):
    demo = config.db_path().resolve() == (config.ROOT / 'samples/demo.db').resolve()
    st.info('演示模式 · 当前为模拟数据，可自由试用' if demo else '正式工作区 · 当前修改会保存到服务器数据库')
    try:
        target = daily_backup()
        st.caption(f'今日启动备份：{target.name}（当天首次打开时保存）')
    except Exception as exc:
        st.warning(f'今日备份未完成，请到设置手动备份：{exc}')

    try:
        from . import workflows
        report = workflows.daily_report()
        st.caption(f'今日工作清单已保存：{report.name}')
    except Exception as exc:
        st.warning(f'今日工作清单未生成：{exc}')
