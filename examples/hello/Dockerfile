# A tiny web application the runs on Google Cloud build through the delivery service (examples/hello/app.py), as a
# team's source would be: built in the trusted project by Cloud Build at one commit, never on a laptop.
#   python:3.12-slim, the same index digest as Dockerfile.cloudinfra and Dockerfile.appdelivery.
FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e
WORKDIR /app
COPY app.py ./
USER 65532:65532
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
