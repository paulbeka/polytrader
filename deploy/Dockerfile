FROM python:3.14-slim
ARG GIT_SHA=unknown
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 POLYTRADER_GIT_SHA=$GIT_SHA
WORKDIR /app
COPY deploy/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app \
    && mkdir /data && chown app:app /data
USER 10001:10001
CMD ["python", "-m", "polytrader.ops", "--help"]
