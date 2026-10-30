"""A web application firewall behind the generic waf switch (Cloud Armor): a security policy on a load balancer's backend,
or on the backends of a cluster's front door, that refuses listed sources, limits each client address to a rate, and
refuses requests Google's preconfigured OWASP rules recognise (https://docs.cloud.google.com/armor/docs/waf-rules), at
sensitivity 1, the level Google recommends to start with for its few false positives. In preview, rules only log what
they would refuse. The last rule allows everything else."""
from ...models.errors import ErrorCode, InfrastructureError
from . import translate
from .api import COMPUTE

OWASP = ("sqli-v33-stable", "xss-v33-stable", "lfi-v33-stable", "rfi-v33-stable", "rce-v33-stable", "scannerdetection-v33-stable", "protocolattack-v33-stable",
         "sessionfixation-v33-stable")
DEFAULT_PRIORITY = 2147483647
IN_USE = (6, 20)  # attempts, seconds apart: a load balancer GKE deletes may still name the policy for a while


def rules(spec) -> list[dict]:
    found = []
    if spec.deny_sources:
        found.append({"priority": 1000, "description": "listed sources", "action": "deny(403)",
                      "match": {"versionedExpr": "SRC_IPS_V1", "config": {"srcIpRanges": list(spec.deny_sources)}}})
    if spec.rate_limit_per_minute:
        found.append({"priority": 2000, "description": "rate per client address", "action": "throttle",
                      "match": {"versionedExpr": "SRC_IPS_V1", "config": {"srcIpRanges": ["*"]}},
                      "rateLimitOptions": {"conformAction": "allow", "exceedAction": "deny(429)", "enforceOnKey": "IP",
                                           "rateLimitThreshold": {"count": spec.rate_limit_per_minute, "intervalSec": 60}}})
    if spec.owasp:
        found.extend({"priority": 3000 + index, "description": rule, "action": "deny(403)",
                      "match": {"expr": {"expression": f"evaluatePreconfiguredWaf('{rule}', {{'sensitivity': 1}})"}}} for index, rule in enumerate(OWASP))
    for rule in found:
        rule["preview"] = spec.preview
    return [*found, {"priority": DEFAULT_PRIORITY, "description": "everything else", "action": "allow",
                     "match": {"versionedExpr": "SRC_IPS_V1", "config": {"srcIpRanges": ["*"]}}}]


class GcpArmor:
    def __init__(self, api):
        self.api = api

    def url(self, name: str = "") -> str:
        return f"{COMPUTE}/projects/{self.api.project}/global/securityPolicies" + (f"/{name}" if name else "")

    def body(self, name: str, spec, ctx) -> dict:
        return {"name": name, "type": "CLOUD_ARMOR", "description": translate.description(ctx), "rules": rules(spec)}

    def create(self, name: str, spec, ctx) -> str:
        """The policy (one an earlier attempt made is kept); its link."""
        try:
            self.api.wait_compute(self.api.post(self.url(), json=self.body(name, spec, ctx)))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.PROVIDER_CONFLICT:
                raise
        return self.url(name)

    def delete(self, name: str) -> None:
        for attempt in range(IN_USE[0]):
            try:
                self.api.wait_compute(self.api.delete(self.url(name)))
                return
            except InfrastructureError as exc:
                if exc.code == ErrorCode.NOT_FOUND:
                    return
                if "in use" not in exc.message.lower() and "being used" not in exc.message.lower() or attempt == IN_USE[0] - 1:
                    raise
                self.api.sleep(IN_USE[1])
