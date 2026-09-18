FROM python:3.11-slim

WORKDIR /app

# Dependencies first so code edits do not invalidate the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Non-root. No secrets baked in: LLM_API_KEY arrives at runtime via -e.
RUN useradd --create-home --uid 1000 gridwise
USER gridwise

ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} expands (Render and similar inject their own port).
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
