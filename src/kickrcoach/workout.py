"""Immutable, validated ERG definitions. No Bluetooth or runtime control."""
from pathlib import Path
from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

PositiveInt = Annotated[int, Field(strict=True, gt=0, le=2000)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, str_strip_whitespace=True)


class Target(FrozenModel):
    watts: PositiveInt | None = None
    percent_baseline: Annotated[float, Field(strict=True, gt=0, le=200)] | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.watts is None) == (self.percent_baseline is None):
            raise ValueError("specify exactly one of watts or percent_baseline")
        return self

    def resolve(self, baseline_w: int | None) -> int:
        if self.watts is not None:
            return self.watts
        if type(baseline_w) is not int or not 1 <= baseline_w <= 2000:
            raise ValueError("percentage targets require an explicitly configured indoor baseline")
        assert self.percent_baseline is not None
        return int(baseline_w * self.percent_baseline / 100 + 0.5)


class CadenceGuidance(FrozenModel):
    minimum_rpm: Annotated[int, Field(strict=True, ge=10, le=200)]
    maximum_rpm: Annotated[int, Field(strict=True, ge=10, le=200)]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.minimum_rpm > self.maximum_rpm:
            raise ValueError("cadence minimum must not exceed maximum")
        return self


class Block(FrozenModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    duration_sec: Annotated[int, Field(strict=True, gt=0, le=14400)]
    mode: Literal["erg"] = "erg"
    target: Target
    cadence: CadenceGuidance | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None


class Workout(FrozenModel):
    version: Literal[1] = 1
    name: Annotated[str, Field(min_length=1, max_length=120)]
    blocks: Annotated[tuple[Block, ...], Field(min_length=1, max_length=256)]
    notes: Annotated[str, Field(max_length=4000)] | None = None

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("workout version must be integer 1")
        return value

    @property
    def duration_sec(self) -> int:
        return sum(block.duration_sec for block in self.blocks)


def load_workout(path: str | Path) -> Workout:
    """Safe YAML, bounded file size, duplicate keys rejected instead of overwritten."""
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in result:
                raise ValueError(f"duplicate YAML key: {key}")
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    with Path(path).open("rb") as source:
        data = source.read(262145)
    if len(data) > 262144:
        raise ValueError("workout exceeds 256 KiB")
    return Workout.model_validate(yaml.load(data, Loader=UniqueLoader))
