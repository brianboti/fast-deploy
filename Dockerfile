# python:3.12-slim multi-arch index digest resolved 2026-10-11. Refresh deliberately after review with:
#   docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY labdeploy ./labdeploy
RUN mkdir /data && chown 65532:65532 /data
USER 65532:65532
ENV LAB_CONFIG=/config/config.yaml
EXPOSE 8000
CMD ["uvicorn", "labdeploy.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

