"""Properties of the delivery service's validators and redaction, held over generated input (hypothesis): whatever a
contract accepts is what its rule says, and no input makes a validator fail any other way (the security assessment of
2026-10-30, section 7, S3). The first run found every check here accepting a trailing newline (re.match with $)."""
import re

import pydantic
from hypothesis import given, settings
from hypothesis import strategies as st

from appdelivery.events import REDACTED, redact_text
from appdelivery.models import BuildRequest, ReleaseRequest
from appdelivery.releases import scrub

BUILD = {"environment": "dev", "source": "shop", "revision": "a" * 40, "image": "web"}
RELEASE = {"environment": "dev", "request_id": "3f6c0b7d2a9e4c1f8b5d6e7a9c0b1d2e", "target": {"group": "web"},
           "image": "us-central1-docker.pkg.dev/acme/shop/web@sha256:" + "9b" * 32, "port": 8080}
# the characters that matter to a path, an address or a name, and the ones that should never pass
PATHISH = st.text(alphabet="abcAB09._-/:@ \n\t\\%~", max_size=40)
ANY_TEXT = st.text(max_size=80)
LOWER, UPPER, DIGIT = "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789"


def accepted(model, data):
    """The model, or None when it was refused the one way a contract refuses: a validation error."""
    try:
        return model.model_validate(data)
    except pydantic.ValidationError:
        return None


@given(field=st.sampled_from(["context", "dockerfile"]), value=PATHISH)
def test_a_build_path_accepted_stays_inside_the_source(field, value):
    built = accepted(BuildRequest, {**BUILD, field: value})
    if built is not None:
        path = getattr(built, field)
        assert path == value and "\n" not in path and "\\" not in path and " " not in path
        assert not path.startswith(("/", "-")) and "//" not in path and ".." not in path.split("/")
        assert not re.match(r"(?i)[a-z][a-z0-9+.-]*://|git@|github\.com/", path) and ".git/" not in path + "/"


@given(field=st.sampled_from(["environment", "source", "revision", "image", "context", "dockerfile"]), value=ANY_TEXT)
def test_any_text_in_a_build_field_is_accepted_or_refused_and_never_breaks_the_check(field, value):
    built = accepted(BuildRequest, {**BUILD, field: value})
    if built is not None:
        assert getattr(built, field) == value and value.isprintable() and value.strip() == value


@given(field=st.sampled_from(["environment", "source", "revision", "image", "context", "dockerfile"]),
       extra=st.sampled_from(["\n", "\r\n", " ", "\t", "\x00", "\n--force"]), at_end=st.booleans())
def test_a_valid_build_field_with_a_newline_or_a_space_added_is_refused(field, extra, at_end):
    value = BUILD.get(field, "app") + extra if at_end else extra + BUILD.get(field, "app")
    assert accepted(BuildRequest, {**BUILD, field: value}) is None


@given(field=st.sampled_from(["request_id", "image", "environment"]), extra=st.sampled_from(["\n", "\r\n", " ", "\x00"]))
def test_a_valid_release_field_with_a_newline_or_a_space_added_is_refused(field, extra):
    assert accepted(ReleaseRequest, {**RELEASE, field: RELEASE[field] + extra}) is None


@given(revision=st.text(alphabet="0123456789abcdefABCDEF\n ", min_size=38, max_size=42))
def test_a_revision_is_exactly_a_full_lowercase_commit(revision):
    assert (accepted(BuildRequest, {**BUILD, "revision": revision}) is not None) == bool(re.fullmatch(r"[0-9a-f]{40}", revision))


@given(digest=st.text(alphabet="0123456789abcdefABCDEF\n", min_size=62, max_size=66), separator=st.sampled_from(["@sha256:", ":", "@sha512:", "@"]))
def test_an_image_is_released_only_by_a_full_sha256_digest(digest, separator):
    image = "us-central1-docker.pkg.dev/acme/shop/web" + separator + digest
    released = accepted(ReleaseRequest, {**RELEASE, "image": image})
    assert (released is not None) == (separator == "@sha256:" and bool(re.fullmatch(r"[0-9a-f]{64}", digest)))


@given(name=st.text(alphabet="ABZ_09az-\n =", min_size=1, max_size=12))
def test_an_environment_name_accepted_is_one_a_container_and_an_env_file_take(name):
    released = accepted(ReleaseRequest, {**RELEASE, "env": {name: {"value": "x"}}})
    assert (released is not None) == bool(re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", name))


@given(path=st.text(alphabet="/az09._~-?=&% \n#", min_size=1, max_size=30))
def test_a_health_path_accepted_has_no_space_newline_or_fragment(path):
    released = accepted(ReleaseRequest, {**RELEASE, "health": {"path": path}})
    if released is not None:
        assert released.health.path.startswith("/") and not set(released.health.path) & set(" \n#")


@st.composite
def credentials(draw):
    """A token of the shape the redaction promises to catch: 40 or more token characters with both cases and a digit."""
    body = draw(st.text(alphabet=LOWER + UPPER + DIGIT, min_size=37, max_size=80))
    return draw(st.sampled_from(LOWER)) + draw(st.sampled_from(UPPER)) + draw(st.sampled_from(DIGIT)) + body


@given(before=st.text(alphabet=LOWER + " :=,\n", max_size=30), token=credentials(), after=st.text(alphabet=LOWER + " :=,\n", max_size=30))
def test_a_credential_anywhere_in_a_log_line_is_redacted(before, token, after):
    assert token not in redact_text(f"{before} {token} {after}")


# A name's parts are shorter than 20: one unbroken run of 20 letters and digits reads as an API key and is redacted, as the
# rule says (events.py), so a name like that is not promised to stay.
@given(digest=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64), name=st.from_regex(r"[a-z][a-z0-9]{0,15}(?:-[a-z0-9]{1,15}){0,3}", fullmatch=True))
def test_a_digest_and_a_name_stay_readable_in_a_log_line(digest, name):
    line = f"projects/acme/locations/us/repositories/{name}/web@sha256:{digest}"
    assert redact_text(line) == line


@settings(max_examples=200)
@given(values=st.lists(st.text(alphabet=LOWER + UPPER + DIGIT + "-_ ", min_size=4, max_size=20), max_size=4),
       noise=st.lists(st.text(alphabet=LOWER + " \n", max_size=20), max_size=5))
def test_a_secret_value_never_survives_in_a_containers_log(values, noise):
    values = [value for value in values if value not in REDACTED]
    log = "".join(part + (values[index % len(values)] if values else "") for index, part in enumerate(noise or [""]))
    cleaned = scrub(log, values)
    assert not any(value in cleaned for value in values)
