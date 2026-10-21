# The worker job: one run per request, its result written to the bucket (BUCKET_RUNS, write).
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /job
COPY worker.py .
ENV PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["python", "worker.py"]
