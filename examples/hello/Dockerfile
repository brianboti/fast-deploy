# A tiny web application the runs on Google Cloud build through the delivery service (examples/hello/app.py), as a
# team's source would be: built in the trusted project by Cloud Build at one commit, never on a laptop.
#   python:3.12-slim, the same index digest as Dockerfile.cloudinfra and Dockerfile.appdelivery.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
WORKDIR /app
COPY app.py ./
USER 65532:65532
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
