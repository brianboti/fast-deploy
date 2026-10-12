"""Rendering and inspection of the three workflow files every application repository carries."""
import re

TEMPLATES = ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml")


def render(template_text, platform_repository, platform_sha):
    """Pin a template to one platform repository and commit; the placeholders are the only variable parts."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", platform_repository) or not re.fullmatch(r"[a-f0-9]{40}", platform_sha):
        raise ValueError("Provide owner/repository and a full 40-character platform commit SHA")
    return template_text.replace("PLATFORM_REPOSITORY", platform_repository).replace("PLATFORM_COMMIT", platform_sha)


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
