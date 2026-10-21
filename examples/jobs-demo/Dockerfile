# The service: python:3.12-slim (Debian 13) by digest, the standard library only, a user that is not root.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080
CMD ["python", "app.py"]
