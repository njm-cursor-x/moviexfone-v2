FROM python:3.12-slim

WORKDIR /app
RUN useradd --create-home --shell /usr/sbin/nologin app
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY static ./static
COPY builder ./builder
RUN pip install --no-cache-dir . \
    && mkdir -p /app/var \
    && chown -R app:app /app
USER app
ENV MOVIEXFONE_BIND=0.0.0.0
ENV MOVIEXFONE_DB=/app/var/moviexfone.db
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "moviexfone"]
