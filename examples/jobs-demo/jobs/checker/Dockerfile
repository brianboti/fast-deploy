# The checker job, built on another application's test image (the platform passes it as BASE, docs/ROADMAP.md D69): that
# application's Node.js, npm and packages in /deps, a writable /work, user 65532. Its verdict is its exit code.
ARG BASE
FROM ${BASE}
COPY --chmod=0755 check.mjs /usr/local/bin/check.mjs
# Said again here: the Dockerfile policy reads this file, not the base image, so the user must be named in it.
USER 65532:65532
CMD ["node", "/usr/local/bin/check.mjs"]
