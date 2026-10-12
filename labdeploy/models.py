import hashlib
import json
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Resources(StrictModel):
    cpu: int = Field(default=1, ge=1, le=4)
    memory_mib: int = Field(default=512, ge=512, le=4096)


class Scaling(StrictModel):
    minimum: int = Field(default=0, ge=0, le=1)
    maximum: int = Field(default=2, ge=1, le=5)


class AppSpec(StrictModel):
    api_version: Literal["lab/v1"] = "lab/v1"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    runtime: Literal["python-http"] = "python-http"
    port: int = Field(default=8080, ge=1024, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$")
    resources: Resources = Field(default_factory=Resources)
    scaling: Scaling = Field(default_factory=Scaling)
    concurrency: int = Field(default=80, ge=1, le=1000)
    exposure: Literal["authenticated"] = "authenticated"
    state: Literal["stateless"] = "stateless"
    dependencies: list[str] = Field(default_factory=list)
    gpu: bool = False
    reviewed: bool = False

    @model_validator(mode="after")
    def compatible(self):
        if self.scaling.minimum > self.scaling.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if self.resources.memory_mib % 256:
            raise ValueError("memory_mib must be a multiple of 256")
        return self


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("YAML requires unique string keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_spec(text: str) -> AppSpec:
    if len(text.encode()) > 32_768:
        raise ValueError("platform.yaml exceeds 32 KiB")
    return AppSpec.model_validate(yaml.load(text, Loader=UniqueLoader))  # noqa: S506 - UniqueLoader subclasses SafeLoader


def spec_hash(spec: AppSpec) -> str:
    return hashlib.sha256(json.dumps(spec.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enforce(spec: AppSpec, cloud="gcp", environment="staging") -> None:
    if cloud != "gcp" or environment != "staging":
        raise ValueError("MVP supports GCP staging only")
    if not spec.reviewed:
        raise ValueError("Review assumptions and merge reviewed: true in platform.yaml first")
    if spec.dependencies or spec.gpu:
        raise ValueError("Database/dependency provisioning and GPUs are not implemented")
    if spec.resources.cpu > 2 or spec.resources.memory_mib > 2048 or spec.scaling.maximum > 3:
        raise ValueError("Staging policy: maximum 2 CPUs, 2048 MiB, 3 instances")

