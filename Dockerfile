FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app
RUN useradd --create-home --shell /usr/sbin/nologin app
COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY static ./static
COPY builder ./builder
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && mkdir -p /app/var \
    && chown -R app:app /app
USER app
ENV MOVIEXFONE_BIND=0.0.0.0
ENV MOVIEXFONE_DB=/app/var/moviexfone.db
EXPOSE 8000
HEALTHCHECK CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"]
CMD ["python", "-m", "moviexfone"]
