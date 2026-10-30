"""Application delivery: builds an image in the trusted Google Cloud project, then runs it on resources the
infrastructure layer already created.

A team asks for a build of one commit of a source the operator linked; Cloud Build fetches the source and builds and
pushes the image in the trusted project, and this package records the digest. Images are never built or uploaded
anywhere else: a release may name only a digest recorded here. A release names that image, a `cloudinfra` request, a
group of its machines, a port and where each environment value comes from; this package checks that the team owns the
request and handed this service access to it, verifies the image, and runs it. This package itself runs no build, takes
no build step or command from a caller, and never creates or deletes infrastructure. It speaks to `cloudinfra` only
over HTTP. See docs/DELIVERY-SPEC.md.
"""

__version__ = "0.1.0"
