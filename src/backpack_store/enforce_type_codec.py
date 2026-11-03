"""Optional strict Enforce Type codec; never imported by the base package."""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass
import sys
from typing import Generic, TypeVar

if sys.version_info < (3, 14):
    raise ImportError("The Backpack Store Enforce Type codec requires Python 3.14 or newer.")

try:
    from enforce_type import TypePlan, compile_type
    from enforce_type import ValidationError as EnforceValidationError
    from enforce_type import __version__ as enforce_version
except ImportError:
    raise ImportError(
        "Install backpack-store with its [enforce-type] extra to use this codec."
    ) from None

from .errors import CodecError, RegistrationError, ValidationError
from .records import JSONObject, json_object, positive_integer, validate_type_key

if not enforce_version.startswith("0.1."):
    raise ImportError("The Backpack Store Enforce Type codec supports Enforce Type 0.1 only.")

T = TypeVar("T")


@dataclass(frozen=True, init=False)
class EnforceTypeCodec(Generic[T]):
    """Compile one trusted dataclass and check/load/dump strict finite JSON."""

    model_type: type[T]
    type_key: str
    schema_version: int
    _plan: TypePlan[T] = field(repr=False, compare=False)

    def __init__(self, model_type: type[T], *, type_key: str, schema_version: int = 1):
        if not isinstance(model_type, type) or not is_dataclass(model_type):
            raise RegistrationError("model_type must be a dataclass class")
        validate_type_key(type_key)
        positive_integer(schema_version, "schema_version")
        try:
            plan = compile_type(model_type)
        except Exception:  # trusted annotation evaluation must not expose application values
            raise RegistrationError("Enforce Type could not compile the registered model") from None
        object.__setattr__(self, "model_type", model_type)
        object.__setattr__(self, "type_key", type_key)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "_plan", plan)

    def encode(self, value: T) -> JSONObject:
        if type(value) is not self.model_type:
            raise ValidationError("value must have the exact registered Enforce Type model type")
        try:
            self._plan.check(value)
            return json_object(self._plan.dump(value, mode="json"))
        except (EnforceValidationError, ValidationError):
            raise ValidationError("value does not satisfy the registered Enforce Type JSON model") from None
        except Exception:  # custom attribute/constructor code can include private values
            raise CodecError("registered Enforce Type model failed to encode") from None

    def decode(self, payload: JSONObject) -> T:
        try:
            value = self._plan.load(json_object(payload), mode="json")
        except (EnforceValidationError, ValidationError):
            raise ValidationError("payload does not satisfy the registered Enforce Type JSON model") from None
        except Exception:
            raise CodecError("registered Enforce Type model failed to decode") from None
        if type(value) is not self.model_type:
            raise CodecError("registered Enforce Type model returned a different Python type")
        return value
