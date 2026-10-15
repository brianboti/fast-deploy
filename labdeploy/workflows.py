"""Rendering and inspection of the three workflow files every application repository carries."""
import re

TEMPLATES = ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml")
# The parts of a template that differ between the applications of one repository (docs/ROADMAP.md D23, D24).
PLACEHOLDERS = ("__LAB_SUFFIX__", "__LAB_NAME_SUFFIX__", "__LAB_APP_DIR__", "__LAB_APP_PATH__")
SUFFIX = re.compile(r"^(-[a-z][a-z0-9-]{1,19}[a-z0-9])?$")
PATH = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*){0,7}$")


def layout(app=None):
    """The values a template's placeholders take for one application: the plain layout when `app` is None."""
    suffix = getattr(app, "suffix", "") or ""
    path = getattr(app, "path", "") or ""
    if not SUFFIX.fullmatch(suffix) or (path and (not PATH.fullmatch(path) or ".." in path.split("/"))):
        raise ValueError("Unexpected application layout")
    return {"__LAB_SUFFIX__": suffix, "__LAB_NAME_SUFFIX__": f" ({suffix[1:]})" if suffix else "",
            "__LAB_APP_DIR__": f"app/{path}" if path else "app", "__LAB_APP_PATH__": path or "."}


def lay_out(template_text, app=None):
    """A template with its layout placeholders filled for one application (the plain layout when `app` is None)."""
    for placeholder, value in layout(app).items():
        template_text = template_text.replace(placeholder, value)
    return template_text


def render(template_text, platform_repository, platform_sha, app=None):
    """Pin a template to one platform repository and commit, laid out for one application; the placeholders are the only
    variable parts. Without an application, the plain layout of a repository's first service at its root."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", platform_repository) or not re.fullmatch(r"[a-f0-9]{40}", platform_sha):
        raise ValueError("Provide owner/repository and a full 40-character platform commit SHA")
    return lay_out(template_text.replace("PLATFORM_REPOSITORY", platform_repository).replace("PLATFORM_COMMIT", platform_sha), app)


def pinned_commit(workflow_text, platform_repository):
    """The platform commit a rendered workflow pins: the ref that follows the platform repository checkout."""
    lines = workflow_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"repository: {platform_repository}":
            for follow in lines[index + 1:index + 6]:
                match = re.match(r"\s*ref:\s*([a-f0-9]{40})\s*$", follow)
                if match:
                    return match.group(1)
    return None
