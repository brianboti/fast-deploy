"""Application delivery: runs an image that already exists on resources the infrastructure layer already created.

A team names an image by digest, a `cloudinfra` request, a group of its machines, a port and where each environment
value comes from; this package checks that the team owns the request and handed this service access to it, verifies the
image, and runs it. It never builds, never takes source, a Dockerfile or a command, and never creates or deletes
infrastructure. It speaks to `cloudinfra` only over HTTP. See docs/DELIVERY-SPEC.md.
"""

__version__ = "0.1.0"
