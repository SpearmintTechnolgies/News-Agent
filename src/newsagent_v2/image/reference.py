"""
Optional story/source reference images for provider requests.

The source file is never mutated. Providers that cannot accept a reference
must declare that capability; a required reference then fails before network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from newsagent_v2.image.contract import (
    REFERENCE_IMAGE_INSTRUCTIONS,
    normalize_reference_role as contract_normalize_role,
)
from newsagent_v2.image.provider import ImageProvider, ImageProviderError, ProviderRequest
from newsagent_v2.image.validate import ImageValidationError, file_sha256, validate_reference_image

REFERENCE_PROMPT_BLOCK = " ".join(REFERENCE_IMAGE_INSTRUCTIONS)


def normalize_reference_role(role: str | None) -> str:
    try:
        return contract_normalize_role(role)
    except ValueError as exc:
        raise ImageProviderError("invalid_reference_role", str(exc)) from exc


def with_reference_prompt(prompt: str, *, role: str | None = None) -> str:
    normalize_reference_role(role)
    return f"{prompt}\n\n{REFERENCE_PROMPT_BLOCK}"


def empty_reference_telemetry() -> dict[str, Any]:
    return {
        "reference_used": False,
        "reference_role": None,
        "reference_sha256": None,
        "reference_width": None,
        "reference_height": None,
        "reference_path": None,
        "provider_reference_supported": None,
        "reference_required": False,
        "reference_copied": False,
    }


def inspect_reference_file(path: Path) -> dict[str, Any]:
    before = file_sha256(path)
    inspected = validate_reference_image(path)
    after = file_sha256(path)
    if before != after:
        raise ImageValidationError("reference_mutated", "reference validation must not mutate the source file")
    inspected["source_unchanged"] = True
    return inspected


def apply_reference_policy(
    provider: ImageProvider,
    request: ProviderRequest,
) -> dict[str, Any]:
    """
    Validate an optional/required reference before provider.generate.

    Does not copy the reference. Does not open a network connection.
    """
    supported = bool(provider.reference_images_supported)
    telemetry = empty_reference_telemetry()
    telemetry["provider_reference_supported"] = supported
    telemetry["reference_required"] = bool(request.reference_required)

    path_value = request.reference_image_path
    if not path_value:
        if request.reference_required:
            raise ImageProviderError(
                "missing_reference",
                "reference_required is true but reference_image_path is empty",
            )
        return telemetry

    if request.reference_required and not supported:
        raise ImageProviderError(
            "reference_unsupported",
            f"{provider.identity().provider_name} does not support reference images",
        )

    inspected = inspect_reference_file(Path(path_value))
    role = normalize_reference_role(request.reference_image_role)
    telemetry.update(
        {
            "reference_role": role,
            "reference_sha256": inspected["sha256"],
            "reference_width": inspected["width"],
            "reference_height": inspected["height"],
            "reference_path": inspected["path"],
            "reference_format": inspected["format"],
            "reference_bytes": inspected["size_bytes"],
            "reference_used": supported,
            "reference_copied": False,
        }
    )
    return telemetry
