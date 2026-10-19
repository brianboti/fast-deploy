"""Rendering and inspection of the three workflow files every application repository carries."""
import re

TEMPLATES = ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml")
# The platform repository's own pipeline for applications whose contract it keeps (docs/ROADMAP.md D33).
CENTRAL = {"deploy": "deploy.yml", "recover": "recover.yml", "rightsize": "rightsize.yml"}
INSTALLATION_ACTION = "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3.3.0"
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
    """The platform commit a rendered workflow pins: the ref that follows the platform repository checkout. Several names
    may be given (the repository's own, then the ones it had before a rename); GitHub compares them without case."""
    names = [platform_repository] if isinstance(platform_repository, str) else list(platform_repository)
    wanted = {f"repository: {name}".lower() for name in names}
    lines = workflow_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() in wanted:
            for follow in lines[index + 1:index + 6]:
                match = re.match(r"\s*ref:\s*([a-f0-9]{40})\s*$", follow)
                if match:
                    return match.group(1)
    return None


def _swap(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"The deploy template no longer holds the central pipeline's anchor: {old[:70]!r}")
    return text.replace(old, new)


APP_DIR_EXPRESSION = "${{ inputs.app_path != '' && format('app/{0}', inputs.app_path) || 'app' }}"
APP_READER = "${{ steps.app-token.outputs.token || secrets.APPS_READ_TOKEN || github.token }}"
TOKEN_STEPS = """      # A short-lived token for the application repository: the platform's GitHub App when it is installed (the variable
      # APPS_APP_ID and the secret APPS_APP_PRIVATE_KEY), else the operator's read-only token APPS_READ_TOKEN, else this
      # job's own token, which reaches public repositories only.
      - name: Name the application repository
        id: source
        shell: bash
        run: |
          set -euo pipefail
          echo "owner=${SOURCE_REPOSITORY%%/*}" >> "$GITHUB_OUTPUT"
          echo "name=${SOURCE_REPOSITORY#*/}" >> "$GITHUB_OUTPUT"
      - name: Token for the application repository
        id: app-token
        if: vars.APPS_APP_ID != ''
        uses: @INSTALLATION_ACTION@
        with:
          app-id: ${{ vars.APPS_APP_ID }}
          private-key: ${{ secrets.APPS_APP_PRIVATE_KEY }}
          owner: ${{ steps.source.outputs.owner }}
          repositories: ${{ steps.source.outputs.name }}
          permission-contents: read
      - name: The commit is on the application's main
        env:
          GH_TOKEN: @APP_READER@
        shell: bash
        run: |
          set -euo pipefail
          STATUS=$(gh api "repos/$SOURCE_REPOSITORY/compare/$COMMIT_SHA...main" --jq .status)
          [ "$STATUS" = "ahead" ] || [ "$STATUS" = "identical" ]
""".replace("@INSTALLATION_ACTION@", INSTALLATION_ACTION).replace("@APP_READER@", APP_READER)
READ_CONTRACT = """      # The contract the plan was made from: apps/<service>/platform.yaml on this run's commit of main (the plan's contract
      # commit is an ancestor). It must name this repository, folder and commit, and hash to the plan's spec hash; merged,
      # it is reviewed (docs/ROADMAP.md D32).
      - name: Read the contract
        env:
          PYTHONPATH: ${{ github.workspace }}/platform
        shell: bash
        run: |
          set -euo pipefail
          python - <<'PY'
          import os, pathlib, re
          from labdeploy.models import parse_contract, spec_hash, spec_text
          service = os.environ['APPLICATION']
          assert re.fullmatch(r'[a-z][a-z0-9-]{1,19}[a-z0-9]', service), 'not a service name'
          spec, source = parse_contract(pathlib.Path('platform/apps', service, 'platform.yaml').read_text())
          folder = '' if os.environ['APP_PATH'] == '.' else os.environ['APP_PATH']
          assert spec.name == service, 'the contract names another service'
          assert source.repository.lower() == os.environ['SOURCE_REPOSITORY'].lower(), 'the contract names another repository'
          assert source.path == folder, 'the contract names another folder'
          assert source.commit == os.environ['COMMIT_SHA'], 'the contract names another commit'
          assert spec_hash(spec) == os.environ['SPEC_HASH'], 'Spec hash mismatch'
          pathlib.Path('contract').mkdir(exist_ok=True)
          pathlib.Path('contract/platform.yaml').write_text(spec_text(spec))
          PY
"""


def central_deploy(template_text):
    """The platform repository's deploy.yml, made from templates/deploy-gcp.yml: the same steps, but the application and its
    commit arrive as inputs, the application is checked out read-only with a narrowed token, the contract is read from
    apps/<id>/platform.yaml, and the provenance names the application's source (docs/ROADMAP.md D33)."""
    t = template_text
    t = _swap(t, "name: Deploy GCP staging__LAB_NAME_SUFFIX__\n", "name: Deploy (platform pipeline)\n")
    t = _swap(t, """      plan_id:
        required: true
        type: string
""", """      plan_id:
        required: true
        type: string
      application:
        description: The service, whose contract is apps/<service>/platform.yaml and whose environment is staging-<service>
        required: true
        type: string
      repository:
        description: The application's repository (owner/name), checked out read-only at commit_sha
        required: true
        type: string
      app_path:
        description: The service's folder in that repository; empty for the root
        required: false
        default: ''
        type: string
      contract_commit:
        description: The platform commit whose apps/<service>/platform.yaml the plan was made from; must be on main
        required: true
        type: string
""")
    t = _swap(t, "  group: gcp-staging__LAB_SUFFIX__\n", "  group: gcp-staging-${{ inputs.application }}\n")
    t = _swap(t, """  APP_DIR: __LAB_APP_DIR__
  APP_PATH: __LAB_APP_PATH__
  DEPLOY_WORKFLOW: deploy-gcp__LAB_SUFFIX__.yml
""", """  APP_DIR: @APP_DIR@
  APP_PATH: ${{ inputs.app_path || '.' }}
  DEPLOY_WORKFLOW: deploy.yml
  APPLICATION: ${{ inputs.application }}
  SOURCE_REPOSITORY: ${{ inputs.repository }}
  CONTRACT_COMMIT: ${{ inputs.contract_commit }}
""".replace("@APP_DIR@", APP_DIR_EXPRESSION))
    t = _swap(t, "  SPEC_SOURCE: __LAB_APP_DIR__/platform.yaml\n", "  SPEC_SOURCE: contract/platform.yaml\n")
    # Repositories taken as they are: code nobody has reviewed unless the contract says otherwise (docs/ROADMAP.md D35).
    t = _swap(t, "  TRUST_DEFAULT: reviewed\n", "  TRUST_DEFAULT: unknown\n")
    t = _swap(t, "  PLATFORM_LOCK: ''\n  PLATFORM_TEST_LOCK: ''\n  PLATFORM_NODE_LOCK: ''\n",
              "  PLATFORM_LOCK: platform/apps/${{ inputs.application }}/requirements.lock\n"
              "  PLATFORM_TEST_LOCK: platform/apps/${{ inputs.application }}/requirements-test.lock\n"
              "  PLATFORM_NODE_LOCK: platform/apps/${{ inputs.application }}/package-lock.json\n")
    t = _swap(t, """          test "$GITHUB_REF" = "refs/heads/main"
          # The commit must be the tip of main or already part of its history: never an unmerged branch.
          if [ "$COMMIT_SHA" != "$GITHUB_SHA" ]; then
            STATUS=$(gh api "repos/$GITHUB_REPOSITORY/compare/$COMMIT_SHA...$GITHUB_SHA" --jq .status)
            [ "$STATUS" = "ahead" ] || [ "$STATUS" = "identical" ]
          fi
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ inputs.commit_sha }}
          persist-credentials: false
          path: app
""", """          test "$GITHUB_REF" = "refs/heads/main"
          [[ "$APPLICATION" =~ ^[a-z][a-z0-9-]{1,19}[a-z0-9]$ ]]
          [[ "$SOURCE_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
          [[ "$CONTRACT_COMMIT" =~ ^[a-f0-9]{40}$ ]]
          [[ "$APP_PATH" = "." || "$APP_PATH" =~ ^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*){0,7}$ ]]
          [[ "$APP_PATH" != *..* ]]
          # The contract commit is this run's commit on main or one of its ancestors: never an unmerged branch.
          if [ "$CONTRACT_COMMIT" != "$GITHUB_SHA" ]; then
            STATUS=$(gh api "repos/$GITHUB_REPOSITORY/compare/$CONTRACT_COMMIT...$GITHUB_SHA" --jq .status)
            [ "$STATUS" = "ahead" ] || [ "$STATUS" = "identical" ]
          fi
""" + TOKEN_STEPS + """      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          repository: ${{ inputs.repository }}
          ref: ${{ inputs.commit_sha }}
          token: @APP_READER@
          persist-credentials: false
          path: app
""".replace("@APP_READER@", APP_READER))
    # The platform checkout is this repository at this run's commit, twice (build and deploy jobs).
    if t.count("""          repository: PLATFORM_REPOSITORY
          ref: PLATFORM_COMMIT
          persist-credentials: false
          path: platform
""") != 2:
        raise ValueError("The deploy template no longer checks the platform out twice")
    t = t.replace("""          repository: PLATFORM_REPOSITORY
          ref: PLATFORM_COMMIT
          persist-credentials: false
          path: platform
          # For a private platform repo: read-only fine-grained token or App token.
          token: ${{ secrets.PLATFORM_READ_TOKEN || github.token }}
""", """          repository: ${{ github.repository }}
          ref: ${{ github.sha }}
          persist-credentials: false
          path: platform
""")
    t = t.replace("""          repository: PLATFORM_REPOSITORY
          ref: PLATFORM_COMMIT
          persist-credentials: false
          path: platform
          token: ${{ secrets.PLATFORM_READ_TOKEN || github.token }}
""", """          repository: ${{ github.repository }}
          ref: ${{ github.sha }}
          persist-credentials: false
          path: platform
""")
    t = _swap(t, """      - name: Install trusted platform dependencies
        run: python -m pip install --require-hashes -r platform/requirements.lock
""", """      - name: Install trusted platform dependencies
        run: python -m pip install --require-hashes -r platform/requirements.lock
""" + READ_CONTRACT)
    # The deploy job: the contract is read again (a reused image comes with no bundle); the application is not checked out.
    t = _swap(t, """      - run: python -m pip install --require-hashes -r platform/requirements.lock
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4
        if: inputs.image_digest == ''
        with:
          name: release-bundle
          path: bundle
      # Reuse: the reviewed configuration comes from the commit itself; the image was built and attested by an earlier run.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        if: inputs.image_digest != ''
        with:
          ref: ${{ inputs.commit_sha }}
          persist-credentials: false
          path: app
      - name: Validate dispatch inputs for reuse
        if: inputs.image_digest != ''
        env:
          GH_TOKEN: ${{ github.token }}
        shell: bash
        run: |
          set -euo pipefail
          [[ "$COMMIT_SHA" =~ ^[a-f0-9]{40}$ ]]
          [[ "$SPEC_HASH" =~ ^[a-f0-9]{64}$ ]]
          [[ "$PLAN_ID" =~ ^[a-f0-9]{32}$ ]]
          [[ "$BAKE_SECONDS" =~ ^[0-9]{1,3}$ ]] && [ "$BAKE_SECONDS" -le 900 ]
          test "$GITHUB_REF" = "refs/heads/main"
          if [ "$COMMIT_SHA" != "$GITHUB_SHA" ]; then
            STATUS=$(gh api "repos/$GITHUB_REPOSITORY/compare/$COMMIT_SHA...$GITHUB_SHA" --jq .status)
            [ "$STATUS" = "ahead" ] || [ "$STATUS" = "identical" ]
          fi
""", """      - run: python -m pip install --require-hashes -r platform/requirements.lock
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4
        if: inputs.image_digest == ''
        with:
          name: release-bundle
          path: bundle
""" + READ_CONTRACT + """      # Reuse: the reviewed configuration is the contract; the image was built and attested by an earlier run.
      - name: Validate dispatch inputs for reuse
        if: inputs.image_digest != ''
        env:
          GH_TOKEN: ${{ github.token }}
        shell: bash
        run: |
          set -euo pipefail
          [[ "$COMMIT_SHA" =~ ^[a-f0-9]{40}$ ]]
          [[ "$SPEC_HASH" =~ ^[a-f0-9]{64}$ ]]
          [[ "$PLAN_ID" =~ ^[a-f0-9]{32}$ ]]
          [[ "$BAKE_SECONDS" =~ ^[0-9]{1,3}$ ]] && [ "$BAKE_SECONDS" -le 900 ]
          test "$GITHUB_REF" = "refs/heads/main"
          [[ "$CONTRACT_COMMIT" =~ ^[a-f0-9]{40}$ ]]
          if [ "$CONTRACT_COMMIT" != "$GITHUB_SHA" ]; then
            STATUS=$(gh api "repos/$GITHUB_REPOSITORY/compare/$CONTRACT_COMMIT...$GITHUB_SHA" --jq .status)
            [ "$STATUS" = "ahead" ] || [ "$STATUS" = "identical" ]
          fi
""")
    # Provenance: the platform repository is the builder; the application's source is named in the statement, signed with
    # the application's own key, whatever the repositories' visibility.
    t = _swap(t, """          elif [ "$(gh api "repos/$GITHUB_REPOSITORY" --jq .visibility)" = "public" ]; then
            MODE=github
          else
            MODE=cosign-kms
          fi
""", """          else
            # The platform pipeline always signs with the application's Cloud KMS key: the statement names the application's
            # repository and commit and the contract, which GitHub's own attestation of this repository would not.
            MODE=cosign-kms
          fi
""")
    t = _swap(t, "      REPOSITORY: ${{ github.repository }}\n    steps:\n      - uses: actions/download-artifact",
              "      REPOSITORY: ${{ inputs.repository }}\n    steps:\n      - uses: actions/download-artifact")
    t = _swap(t, """    environment:
      name: staging__LAB_SUFFIX__
""", """    environment:
      name: staging-${{ inputs.application }}
""")
    t = t.replace("__LAB_APP_DIR__", APP_DIR_EXPRESSION)
    for placeholder in PLACEHOLDERS:
        if placeholder in t:
            raise ValueError(f"{placeholder} is left in the central deploy workflow")
    if "PLATFORM_REPOSITORY" in t or "PLATFORM_COMMIT" in t:
        raise ValueError("The central deploy workflow must not pin a platform commit: it is the platform")
    return "# Generated from templates/deploy-gcp.yml by labdeploy.workflows.central_deploy; a test keeps them equal. Never edit by hand.\n" + t
