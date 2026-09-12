from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)


DEFAULT_BASE_URL = (
    "https://jacob-programmer-viewpicture-method.trycloudflare.com"
)

MODEL_ID = "flux-2-klein-9b-local"


class LocalFluxImageGenProvider(ImageGenProvider):

    @property
    def name(self) -> str:
        return "local-flux"

    @property
    def display_name(self) -> str:
        return "Local FLUX.2 Klein 9B"

    def _base_url(self) -> str:
        return os.environ.get(
            "LOCAL_FLUX_BASE_URL",
            DEFAULT_BASE_URL,
        ).rstrip("/")

    def is_available(self) -> bool:
        try:
            req = Request(
                f"{self._base_url()}/health",
                headers={"User-Agent": "Hermes-Local-Flux/1.0"},
            )

            with urlopen(req, timeout=8) as response:
                if response.status != 200:
                    return False

                payload = json.loads(
                    response.read().decode("utf-8")
                )

                return (
                    payload.get("status") == "ok"
                    and "flux-2-klein-9b"
                    in str(payload.get("model", "")).lower()
                )

        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": MODEL_ID,
                "display": "FLUX.2 Klein 9B — Local GPU",
                "speed": "GPU / local tunnel",
                "strengths": "Low-cost text-to-image",
                "price": "local compute",
            }
        ]

    def default_model(self) -> Optional[str]:
        return MODEL_ID

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Local FLUX.2 Klein 9B",
            "badge": "local",
            "tag": "Private GPU FLUX endpoint",
            "env_vars": [],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text"],
            "max_reference_images": 0,
        }

    def _size_for_aspect(
        self,
        aspect_ratio: str,
        kwargs: Dict[str, Any],
    ) -> str:

        # Explicit size wins.
        size = kwargs.get("size")
        if isinstance(size, str) and "x" in size:
            return size

        # Our production default is square 512×512
        # to keep compute and output size low.
        if aspect_ratio == "portrait":
            return "512x768"

        if aspect_ratio == "landscape":
            return "768x512"

        return "512x512"

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:

        prompt = (prompt or "").strip()
        aspect_ratio = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required",
                error_type="invalid_input",
                provider=self.name,
                model=MODEL_ID,
                prompt="",
                aspect_ratio=aspect_ratio,
            )

        # This backend is text-to-image only.
        if image_url or reference_image_urls:
            return error_response(
                error="Local FLUX backend supports text-to-image only",
                error_type="unsupported_modality",
                provider=self.name,
                model=MODEL_ID,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        size = self._size_for_aspect(
            aspect_ratio,
            kwargs,
        )

        # Keep the inference configuration deliberately cheap.
        steps = int(
            kwargs.get(
                "num_inference_steps",
                4,
            )
        )

        guidance = float(
            kwargs.get(
                "guidance_scale",
                1.0,
            )
        )

        seed = kwargs.get("seed")

        payload: Dict[str, Any] = {
            "prompt": prompt,
            "size": size,
            "num_inference_steps": steps,
            "guidance_scale": guidance,
            "n": 1,
        }

        if seed is not None:
            payload["seed"] = seed

        body = json.dumps(payload).encode("utf-8")

        request = Request(
            f"{self._base_url()}/v1/images/generations",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Hermes-Local-Flux/1.0",
            },
            method="POST",
        )

        try:
            with urlopen(
                request,
                timeout=300,
            ) as response:

                raw = response.read().decode("utf-8")

                result = json.loads(raw)

            data = result.get("data") or []

            if not data:
                raise RuntimeError(
                    "Local FLUX API returned no data"
                )

            item = data[0]

            b64 = item.get("b64_json")

            if not b64:
                raise RuntimeError(
                    "Local FLUX API returned no b64_json"
                )

            image_path = save_b64_image(
                b64,
                prefix="local-flux",
                extension="png",
            )

            # Apply the exact MemeCoinist logo locally.
            try:
                branding = (
                    Path.home()
                    / ".hermes"
                    / "branding"
                    / "brand_image.py"
                )

                if branding.exists():
                    import importlib.util

                    spec = (
                        importlib.util
                        .spec_from_file_location(
                            "memecoinist_branding",
                            branding,
                        )
                    )

                    if spec and spec.loader:
                        module = (
                            importlib.util.module_from_spec(
                                spec
                            )
                        )

                        spec.loader.exec_module(module)

                        branded = module.apply_logo(
                            str(image_path)
                        )

                        if branded:
                            image_path = Path(branded)

            except Exception as branding_error:
                # Branding failure must not destroy
                # successful image generation.
                print(
                    "Local branding warning:",
                    branding_error,
                )

            return success_response(
                image=str(image_path),
                model=MODEL_ID,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                provider=self.name,
                modality="text",
            )

        except HTTPError as exc:
            try:
                detail = exc.read().decode(
                    "utf-8",
                    errors="replace",
                )
            except Exception:
                detail = str(exc)

            return error_response(
                error=(
                    f"Local FLUX HTTP {exc.code}: "
                    f"{detail[:500]}"
                ),
                error_type="local_flux_http_error",
                provider=self.name,
                model=MODEL_ID,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        except URLError as exc:
            return error_response(
                error=f"Local FLUX connection failed: {exc}",
                error_type="local_flux_connection_error",
                provider=self.name,
                model=MODEL_ID,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        except Exception as exc:
            return error_response(
                error=str(exc),
                error_type=type(exc).__name__,
                provider=self.name,
                model=MODEL_ID,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )


def register(ctx) -> None:
    ctx.register_image_gen_provider(
        LocalFluxImageGenProvider()
    )
