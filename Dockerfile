FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY prompts ./prompts

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 persona \
    && mkdir -p /app/data \
    && chown -R persona:persona /app

USER persona

EXPOSE 18080

CMD ["persona-hub"]
