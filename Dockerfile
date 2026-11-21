FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.1 \
 && useradd --create-home --uid 1000 app
WORKDIR /app
RUN chown app:app /app
USER app
ENV UV_PYTHON_DOWNLOADS=never
COPY --chown=app:app pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=app:app src/ ./src/
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never
EXPOSE 8501
