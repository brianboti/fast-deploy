# A tiny web application the runs on Google Cloud build through the delivery service (examples/hello/app.py), as a
# team's source would be: built in the trusted project by Cloud Build at one commit, never on a laptop.
#   python:3.12-slim, the same index digest as Dockerfile.cloudinfra and Dockerfile.appdelivery.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY app.py ./
USER 65532:65532
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
