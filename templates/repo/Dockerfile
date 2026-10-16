# Drafted by Fast Deploy from docs/DEVELOPER-CONTRACT.md, section 2. Review it, then it is yours: the platform never rewrites it.
# python:__PYTHON__-slim multi-architecture index digest resolved __RESOLVED__. Refresh deliberately after review with
#   docker buildx imagetools inspect python:__PYTHON__-slim
# or merge the pull request Dependabot opens for it; keep the version equal to python in platform.yaml.
FROM python:__PYTHON__-slim@__DIGEST__
# Log lines appear at once. The user below has no home directory: what tools write under ~ (caches, gunicorn's control
# socket, which otherwise logs an error at every start) goes to /tmp, memory on Cloud Run.
ENV PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
# Dependencies before the code: a code change then rebuilds one layer instead of reinstalling every dependency.
COPY __REQUIREMENTS__ ./
RUN pip install --no-cache-dir __INSTALL__
# Only what .dockerignore lets in is copied.
COPY . .
# A numeric user that does not exist in the image: the service never runs as root.
USER 65532:65532
EXPOSE __PORT__
CMD __CMD__
