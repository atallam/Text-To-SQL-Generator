# One image, two commands: the API (uvicorn) and the UI (streamlit). See docker-compose.yml.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Add ",embeddings" to use local sentence-transformers (pulls in PyTorch, ~2 GB) and set
# EMBEDDING_PROVIDER=sentence-transformers. The default image uses the lightweight lexical embedder.
ARG EXTRAS="api,ui"

# Editable install on purpose: text2sql finds sources/ and .env relative to the project root (/app).
COPY pyproject.toml README.md ./
COPY text2sql ./text2sql
RUN pip install -e ".[${EXTRAS}]"

COPY api ./api
COPY app ./app
COPY scripts ./scripts
COPY sources ./sources

RUN useradd --create-home appuser && mkdir -p data evals/feedback && chown -R appuser /app
USER appuser

EXPOSE 8000 8501
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
