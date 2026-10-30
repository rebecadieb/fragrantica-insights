# Imagem leve usada pelo consumer, pela carga CSV e pelo app Streamlit.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY sql ./sql
COPY scripts ./scripts
COPY data/exemplo ./data/exemplo

EXPOSE 8501
CMD ["streamlit", "run", "src/fraginsights/app/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
