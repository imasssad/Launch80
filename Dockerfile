FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY launch80/ ./launch80/
RUN pip install --no-cache-dir . && mkdir -p /app/data && useradd --uid 10001 --create-home trader && chown -R trader:trader /app/data
USER trader
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','8088')+'/health', timeout=4)" || exit 1
CMD ["launch80", "run"]
