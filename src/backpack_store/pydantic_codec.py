"""Optional Pydantic 2 codec; the core package never imports this extension."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Generic, TypeVar

try:
    from pydantic import BaseModel
    from pydantic import ValidationError as PydanticValidationError
    from pydantic import __version__ as pydantic_version
except ImportError:
    raise ImportError(
        "Install outside-labs-backpack with its [pydantic] extra to use this codec."
    ) from None

from .errors import CodecError, RegistrationError, ValidationError
from .records import JSONObject, json_object, positive_integer, validate_type_key

if not pydantic_version.startswith("2."):
    raise ImportError("The Backpack Pydantic codec supports Pydantic 2 only.")

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, init=False)
class PydanticCodec(Generic[T]):
    """Strict JSON validation for one explicitly registered object-shaped model."""

    model_type: type[T]
    type_key: str
    schema_version: int

    def __init__(self, model_type: type[T], *, type_key: str, schema_version: int = 1):
        if (
            not isinstance(model_type, type)
            or not issubclass(model_type, BaseModel)
            or model_type is BaseModel
            or model_type.__pydantic_root_model__
        ):
            raise RegistrationError(
                "model_type must be a concrete object-shaped Pydantic 2 model"
            )
        validate_type_key(type_key)
        positive_integer(schema_version, "schema_version")
        object.__setattr__(self, "model_type", model_type)
        object.__setattr__(self, "type_key", type_key)
        object.__setattr__(self, "schema_version", schema_version)

    def encode(self, value: T) -> JSONObject:
        if type(value) is not self.model_type:
            raise ValidationError(
                "value must have the exact registered Pydantic model type"
            )
        try:
            payload = json_object(
                value.model_dump(
                    mode="json", round_trip=True, by_alias=True, warnings="error"
                )
            )
        except ValidationError:
            raise
        except Exception:  # noqa: BLE001 - serializers may include private model values in errors
            raise CodecError(
                "registered Pydantic model could not produce a JSON object"
            ) from None
        # Revalidate serialized data even when model_construct/mutation bypassed model validation.
        self.decode(payload)
        return payload

    def decode(self, payload: JSONObject) -> T:
        payload = json_object(payload)
        try:
            return self.model_type.model_validate_json(
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
                strict=True,
                extra="forbid",
            )
        except PydanticValidationError:
            raise ValidationError(
                "payload does not satisfy the registered Pydantic model"
            ) from None
        except Exception:  # noqa: BLE001 - trusted validators must not expose payloads in errors
            raise CodecError(
                "registered Pydantic model failed to validate the JSON payload"
            ) from None
