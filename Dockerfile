FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app/ ./app/
COPY web/ ./web/
COPY data/ ./data/
COPY scripts/ ./scripts/
RUN useradd --create-home --uid 10001 walker
RUN install -d -o walker -g walker /app/.local
USER walker
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"
CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--port", "8000"]
