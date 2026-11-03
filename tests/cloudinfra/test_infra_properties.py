"""Properties of the request contract's validators, held over generated input (hypothesis): what a check accepts is what
its rule says, and no input breaks a check any other way (the security assessment of 2026-10-30, section 7, S3)."""
import ipaddress

import pydantic
from hypothesis import given
from hypothesis import strategies as st

from cloudinfra.models.errors import InfrastructureError
from cloudinfra.models.request import InfrastructureRequest
from cloudinfra.security import validation

PRIVATE = [ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
IAP = ipaddress.ip_network("35.235.240.0/20")
networks = st.builds(lambda address, prefix: ipaddress.ip_network((address, prefix), strict=False),
                     st.integers(min_value=0, max_value=2**32 - 1), st.integers(min_value=0, max_value=32))
JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
                    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=10), children, max_size=3), max_leaves=10)


def refused(check, *args):
    try:
        check(*args)
    except InfrastructureError:
        return True
    return False


@given(network=networks)
def test_a_cidr_is_accepted_exactly_when_private_and_between_8_and_29(network):
    allowed = 8 <= network.prefixlen <= 29 and any(network.subnet_of(private) for private in PRIVATE)
    assert refused(validation.check_cidr, str(network)) != allowed


@given(text=st.text(alphabet="0123456789./ \n", max_size=20))
def test_any_text_as_a_cidr_is_accepted_or_refused_and_accepted_is_a_network(text):
    try:
        cidr = validation.check_cidr(text)
    except InfrastructureError:
        return
    assert str(ipaddress.IPv4Network(cidr)) == cidr


@given(ports=st.lists(st.integers(min_value=1, max_value=65535), min_size=1, max_size=5, unique=True), sources=st.lists(networks, min_size=1, max_size=3))
def test_ssh_and_rdp_are_never_opened_to_a_public_range(ports, sources):
    sources = [str(source) for source in sources]
    public = [source for source in sources if not any(ipaddress.ip_network(source).subnet_of(known) for known in (*PRIVATE, IAP))]
    closed = set(ports) & {22, 3389}
    assert refused(validation.check_public_rule, ports, sources) == bool(closed and public)


@given(name=st.text(alphabet="abz09-_A \n", max_size=25))
def test_a_name_accepted_is_lowercase_short_and_cannot_end_with_a_dash(name):
    if not refused(validation.check_name, name):
        assert 1 <= len(name) <= 20 and name[0].isalpha() and name[0].islower() and not name.endswith("-")
        assert set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")


@given(labels=st.dictionaries(st.text(alphabet="acdgmn-_A \n", max_size=12), st.text(alphabet="az09-_A \n", max_size=8), max_size=4))
def test_a_label_accepted_never_takes_the_services_own_prefixes(labels):
    if not refused(validation.check_labels, labels):
        assert not any(key.startswith(("cloudinfra", "managed-by")) or not key or "\n" in key + value or " " in key + value for key, value in labels.items())


@given(member=st.text(alphabet="usergopviAc:@.-_1 \n", max_size=40))
def test_a_member_accepted_has_one_kind_and_one_address(member):
    if not refused(validation.check_member, member):
        kind, address = member.split(":", 1)
        assert kind in ("user", "group", "serviceAccount") and address.count("@") == 1 and not set(member) & set(" \n")


@given(field=st.sampled_from(["name", "region", "provider", "network", "compute", "database", "access", "labels"]), value=JSON)
def test_any_value_in_a_request_is_accepted_or_refused_and_never_breaks_validation(field, value):
    request = {"provider": "gcp", "region": "us-central1", "name": "shop", "network": {}}
    try:
        InfrastructureRequest.model_validate({**request, field: value})
    except pydantic.ValidationError:
        pass
