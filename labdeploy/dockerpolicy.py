"""The Dockerfile policy gate (docs/ROADMAP.md 14.1): what a Dockerfile may not do before the platform builds it.

Read as text, never built or run. Each finding names its line, the rule and the fix. Every rule refuses the build, except
that a final image running as root is refused only for code nobody has reviewed (D35: trust `unknown`) and reported for
the lab's own (`reviewed`), and that the two Node.js notes (D45) only report: an image started through npm, and an Alpine
image for dependencies with native code. The build itself never passes `--allow`, so BuildKit's privileged and host-network
entitlements stay off whatever a Dockerfile asks for; this gate refuses the asking.

    python -m labdeploy.dockerpolicy DOCKERFILE --trust unknown|reviewed [--shown PATH] [--trivy REPORT] [--lockfile LOCK] [--json OUT]
        annotations, a line in the step summary; exit 1 on a refusal. --trivy adds Trivy's configuration findings (high and
        critical as warnings), which inform and never stop a build. --lockfile, a Node.js service's npm lockfile, says
        whether its dependencies hold native code.
"""
import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

DOWNLOADERS = r"(?:curl|wget|fetch|aria2c|http(?:ie)?)"
SHELLS = r"(?:sudo\s+)?(?:env\s+(?:\S+=\S*\s+)*)?(?:ba|z|da|k|a)?sh|python[0-9.]*|perl|ruby|node|php"
PIPE_TO_SHELL = re.compile(rf"\b{DOWNLOADERS}\b[^|;&\n]*\|\s*(?:{SHELLS})\b")
SUBSTITUTED = re.compile(rf"(?:(?:ba|z|da|k|a)?sh|python[0-9.]*|source|\.)\s+(?:-c\s+)?[\"']?(?:<\(|\$\()\s*{DOWNLOADERS}\b")
NO_TLS = (
    (re.compile(r"\bcurl\b[^\n|;&]*\s(?:-[A-Za-z]*k[A-Za-z]*|--insecure)\b"), "curl -k / --insecure"),
    (re.compile(r"\bwget\b[^\n|;&]*\s--no-check-certificate\b"), "wget --no-check-certificate"),
    (re.compile(r"--trusted-host\b|\bPIP_TRUSTED_HOST\b"), "pip --trusted-host"),
    (re.compile(r"--allow-unauthenticated\b|--allow-insecure-repositories\b|--force-yes\b|AllowUnauthenticated\s+\"?true"), "apt without signature checks"),
    (re.compile(r"\bapk\b[^\n|;&]*--allow-untrusted\b"), "apk --allow-untrusted"),
    (re.compile(r"--nogpgcheck\b|\bgpgcheck\s*=\s*0\b|--nosignature\b|--nodigest\b"), "yum, dnf or rpm without signature checks"),
    (re.compile(r"\bGIT_SSL_NO_VERIFY\b|http\.sslverify[= ]\s*false", re.I), "git without TLS checks"),
    (re.compile(r"strict-ssl\s*[= ]?\s*false|npm_config_strict_ssl\s*=\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0", re.I), "npm or node without TLS checks"),
    (re.compile(r"\bPYTHONHTTPSVERIFY\s*=\s*['\"]?0\b"), "Python without TLS checks"),
)
SECRET_SOURCE = re.compile(r"(?i)(^|/)(\.git|\.ssh|\.env(\.(?!example$|sample$|template$|dist$)[^/]+)?|[^/]*\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)[^/]*|"
                           r"credentials\.json|service[-_]?account[^/]*\.json|[^/]*\.tfstate)$")
ROOT_USERS = {"root", "0", "0:0", "root:root", "root:0", "0:root"}
INSTRUCTION = re.compile(r"^\s*([A-Za-z]+)(?:\s+(.*))?$", re.S)


class Finding(dict):
    """{rule, line, problem, fix, refuses}"""


def instructions(text):
    """(line number, keyword, arguments) per instruction: continuations joined, comments and blank lines dropped, and a RUN
    heredoc's body (RUN <<EOF ... EOF) kept with its instruction, since that is what runs."""
    escape = "`" if re.match(r"(?i)^\s*#\s*escape\s*=\s*`", text) else "\\"
    lines = text.replace("\r\n", "\n").split("\n")
    result, index = [], 0
    while index < len(lines):
        start, line = index + 1, lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [line]
        while parts[-1].rstrip().endswith(escape) and index < len(lines):
            parts[-1] = parts[-1].rstrip()[:-1]
            following = lines[index]
            index += 1
            if following.lstrip().startswith("#"):
                parts.append("")
                continue
            parts.append(following)
        joined = " ".join(part.strip() for part in parts)
        for marker in re.findall(r"<<-?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", joined):
            body = []
            while index < len(lines) and lines[index].strip() != marker:
                body.append(lines[index])
                index += 1
            index += 1
            joined += "\n" + "\n".join(body)
        match = INSTRUCTION.match(joined)
        if match:
            result.append((start, match.group(1).upper(), (match.group(2) or "").strip()))
    return result


def flags_of(arguments):
    """The --flags at the start of an instruction's arguments, and the rest."""
    flags, rest = [], arguments
    while rest.startswith("--"):
        word, _, rest = rest.partition(" ")
        flags.append(word)
        rest = rest.lstrip()
    return flags, rest


def sources_of(arguments):
    """The sources of a COPY or ADD (exec or shell form), without the destination."""
    _, rest = flags_of(arguments)
    try:
        words = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
    except ValueError:
        words = rest.split()
    return [str(word) for word in words[:-1]]


MANAGERS = ("npm", "npx", "yarn", "pnpm", "bun")
ALPINE_NODE = re.compile(r"(?i)(?:^|\s|/)node:[^\s@]*alpine")


def through_manager(arguments):
    """Whether a CMD or ENTRYPOINT starts a package manager (npm start), directly or through sh -c."""
    try:
        words = [str(word) for word in (json.loads(arguments) if arguments.startswith("[") else shlex.split(arguments))]
    except ValueError:
        return False
    if len(words) > 2 and words[0].rsplit("/", 1)[-1] in ("sh", "bash") and words[1] == "-c":
        try:
            words = shlex.split(words[2])
        except ValueError:
            return False
    words = words[1:] if words[:1] == ["exec"] else words
    return bool(words) and words[0].rsplit("/", 1)[-1] in MANAGERS


def check(text, trust="unknown", native=None, wrapped=None):
    """Every finding in a Dockerfile, in line order; `refuses` says whether it stops the build for this trust level.
    `native` says whether a Node.js service's dependencies hold native code (None when unknown); `wrapped`, the contract's
    build.user (D52), says the platform sets a user on top of the image, so a final image that runs as root is reported."""
    findings = []

    def add(line, rule, problem, fix, refuses=True):
        findings.append(Finding(rule=rule, line=line, problem=problem, fix=fix, refuses=refuses))

    final_user, final_stage_line, final_from, final_start = None, None, "", None
    for line, keyword, arguments in instructions(text):
        if keyword == "FROM":
            final_user, final_stage_line, final_from, final_start = None, line, arguments, None
        if keyword in ("CMD", "ENTRYPOINT"):
            final_start = (line, keyword, arguments)
        if keyword == "USER":
            final_user = arguments.split()[0] if arguments else ""
        if keyword == "RUN":
            flags, command = flags_of(arguments)
            for flag in flags:
                if re.fullmatch(r"--security(=.*)?", flag):
                    add(line, "run-insecure", f"RUN {flag} asks for an insecure sandbox", "remove the flag: the platform never grants it")
                if re.fullmatch(r"--network=host", flag):
                    add(line, "run-host-network", "RUN --network=host asks for the build machine's network", "remove the flag")
            if PIPE_TO_SHELL.search(command) or SUBSTITUTED.search(command):
                add(line, "download-into-shell", "a download is piped straight into a shell or interpreter",
                    "download to a file, check its SHA-256 (sha256sum -c), then run it; or install a pinned package")
            if re.search(r"(?<![\w-])sudo\b", command):
                add(line, "sudo", "RUN uses sudo", "drop sudo: build steps already run with the privileges they need")
        if keyword in ("RUN", "ENV", "ARG"):
            for pattern, what in NO_TLS:
                if pattern.search(arguments):
                    add(line, "verification-disabled", f"TLS or signature checks are turned off ({what})",
                        "remove it, and fix the certificate or the repository key instead")
                    break
        if keyword == "ADD":
            flags, _ = flags_of(arguments)
            remote = [source for source in sources_of(arguments) if re.match(r"(?i)(https?|git)://|git@", source)]
            if remote and not any(flag.startswith("--checksum=") for flag in flags):
                add(line, "add-url-unchecked", f"ADD downloads {remote[0][:80]} without --checksum",
                    "add --checksum=sha256:<digest>, or download in a RUN step and check its SHA-256")
        if keyword in ("ADD", "COPY"):
            flags, _ = flags_of(arguments)
            if not any(flag.startswith("--from") for flag in flags):
                for source in sources_of(arguments):
                    if SECRET_SOURCE.search(source.rstrip("/")):
                        add(line, "copies-secrets", f"{keyword} copies {source[:80]} into the image",
                            "never copy .git, environment files or keys; give secrets at run time (the contract's secrets)")
    if final_stage_line is not None and (final_user is None or final_user.lower() in ROOT_USERS or final_user == ""):
        if wrapped:
            add(final_stage_line, "runs-as-root", f"the final image runs as root; the platform's wrapper stage sets USER {wrapped} on top of it (build.user)",
                "nothing to do unless the service writes where only root may (EACCES at start): then point it at /tmp, or remove build.user to accept root",
                refuses=False)
        else:
            add(final_stage_line, "runs-as-root", "the final image runs as root: it has no USER, or USER root",
                "add USER 65532:65532 (a numeric user that is not root) near the end", refuses=trust == "unknown")
    # The Node.js notes (docs/ROADMAP.md D45): they report and never stop a build.
    if final_start and through_manager(final_start[2]):
        add(final_start[0], "start-through-npm", f"the image starts through a package manager ({final_start[1]} {final_start[2][:60]}), which adds "
            "latency and does not pass SIGTERM on, so Cloud Run's shutdown cuts requests off", "start node itself: CMD [\"node\", \"server.js\"]",
            refuses=False)
    if final_stage_line is not None and native and ALPINE_NODE.search(final_from):
        add(final_stage_line, "alpine-native-code", "the image builds on Alpine (musl), and the dependencies hold native code, whose binaries are "
            "usually built for glibc and may not load", "build on node:<line>-slim (Debian) instead", refuses=False)
    return sorted(findings, key=lambda finding: finding["line"])


def trivy_findings(report):
    """Trivy's configuration findings for a Dockerfile, high and critical: [{id, severity, line, title}]."""
    try:
        data = json.loads(report)
    except ValueError:
        return []
    found = []
    for result in (data.get("Results") or []) if isinstance(data, dict) else []:
        for item in result.get("Misconfigurations") or []:
            if item.get("Severity") in ("HIGH", "CRITICAL"):
                found.append({"id": str(item.get("ID"))[:20], "severity": item.get("Severity"), "title": str(item.get("Title"))[:120],
                              "line": (item.get("CauseMetadata") or {}).get("StartLine")})
    return found


def escape(text, property=False):
    text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if property else text


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.dockerpolicy")
    parser.add_argument("dockerfile")
    parser.add_argument("--trust", choices=("reviewed", "unknown"), required=True)
    parser.add_argument("--shown", help="the Dockerfile's path as the repository names it, for annotations")
    parser.add_argument("--json", help="write the findings here")
    parser.add_argument("--trivy", help="Trivy's `config --format json` report for the same Dockerfile")
    parser.add_argument("--lockfile", help="a Node.js service's npm lockfile, to say whether its dependencies hold native code")
    parser.add_argument("--wrapped-user", type=int, help="the contract's build.user: the platform's wrapper stage runs the image as this user")
    args = parser.parse_args(argv)
    native = None
    if args.lockfile and Path(args.lockfile).is_file():
        from .node import has_native_code, read_lockfile
        native = has_native_code(read_lockfile(Path(args.lockfile).read_text())[0])
    findings = check(Path(args.dockerfile).read_text(), args.trust, native, args.wrapped_user)
    shown = args.shown or args.dockerfile
    extra = trivy_findings(Path(args.trivy).read_text()) if args.trivy and Path(args.trivy).is_file() else []
    for item in extra:
        print(f"::warning file={escape(shown, True)}" + (f",line={item['line']}" if isinstance(item["line"], int) else "")
              + f",title={escape('Trivy ' + item['id'], True)}::{escape(item['title'])}")
    for finding in findings:
        kind = "error" if finding["refuses"] else "warning"
        print(f"::{kind} file={escape(shown, True)},line={finding['line']},title={escape('Dockerfile policy: ' + finding['rule'], True)}::"
              f"{escape(finding['problem'] + '. Fix: ' + finding['fix'])}")
    refused = [finding for finding in findings if finding["refuses"]]
    line = (f"Dockerfile policy ({args.trust}): " + (f"{len(refused)} refused, " if refused else "")
            + (f"{len(findings) - len(refused)} reported" if len(findings) > len(refused) else "") if findings else f"Dockerfile policy ({args.trust}): passed")
    line = line.rstrip(", ") + (f"; Trivy: {len(extra)} high or critical finding{'s' if len(extra) != 1 else ''}, reported" if extra else "")
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(line + "\n")
    if args.json:
        Path(args.json).write_text(json.dumps({"trust": args.trust, "findings": findings, "trivy": extra}, indent=2) + "\n")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
