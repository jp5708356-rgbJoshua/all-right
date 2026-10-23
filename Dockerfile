FROM python:3.12-slim
WORKDIR /app
ENV HOME=/home/appuser PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai \
    PDESK_CONTAINER=1 PDESK_DB_PATH=/data/procurement.db PDESK_CONFIG_PATH=/data/config.yaml
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt && mkdir -p /usr/share/zoneinfo/Asia && python -c "import importlib.resources,shutil; shutil.copyfile(importlib.resources.files('tzdata').joinpath('zoneinfo/Asia/Shanghai'), '/usr/share/zoneinfo/Asia/Shanghai')" && useradd --uid 10001 --create-home appuser && mkdir /data && chown appuser:appuser /data
COPY --chown=appuser:appuser . .
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8501')+'/_stcore/health',timeout=3)"
CMD ["python", "scripts/container_start.py"]
