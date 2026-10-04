"""Provider readiness inspection for V5 generation.

Reports SET/MISSING without exposing secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMAGE_PROVIDER_VERTEX = "vertex"
IMAGE_MODEL_ENV = "NEWSAGENT_V2_VERTEX_MODEL"


@dataclass
class ProviderStatus:
    """Status for a single provider."""
    provider: str
    status: str  # READY, MISSING_CONFIG, MISSING_CREDENTIALS
    configured_model: str | None
    credential_envs: dict[str, str]  # env_name -> SET/MISSING
    notes: str = ""


class ProviderPreflight:
    """Inspect and report provider readiness."""
    
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.environ = environ or {}
    
    def _check_env(self, name: str) -> str:
        """Check if env var is set (non-empty)."""
        return "SET" if self.environ.get(name, "").strip() else "MISSING"
    
    def check_writer(self) -> ProviderStatus:
        """Check writer provider readiness.
        
        Supports: groq, kimi (when NEWSAGENT_V2_V4_ALLOW_KIMI=true)
        """
        from newsagent_v2.article.writer.v4.provider import (
            resolve_v4_provider_specs,
            PROVIDER_KIMI,
        )
        
        specs = resolve_v4_provider_specs(self.environ)
        primary = specs["primary"]
        
        provider = primary.provider
        model = primary.model
        
        if provider == "groq":
            groq_key = self._check_env("GROQ_API_KEY")
            credentials = {"GROQ_API_KEY": groq_key}
            
            if groq_key == "SET":
                status = "READY"
            else:
                status = "MISSING_CREDENTIALS"
            
            return ProviderStatus(
                provider=provider,
                status=status,
                configured_model=model,
                credential_envs=credentials,
            )
        
        if provider == PROVIDER_KIMI:
            from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV
            kimi_key = self._check_env(KIMI_KEY_ENV)
            allow_kimi = self.environ.get("NEWSAGENT_V2_V4_ALLOW_KIMI", "").lower() == "true"
            credentials = {KIMI_KEY_ENV: kimi_key}
            
            if not allow_kimi:
                status = "NOT_ALLOWED"
                notes = "Set NEWSAGENT_V2_V4_ALLOW_KIMI=true to authorize Kimi"
            elif kimi_key != "SET":
                status = "MISSING_CREDENTIALS"
                notes = f"{KIMI_KEY_ENV} not set"
            else:
                status = "READY"
                notes = "Kimi authorized and ready"
            
            return ProviderStatus(
                provider=provider,
                status=status,
                configured_model=model,
                credential_envs=credentials,
                notes=notes,
            )
        
        return ProviderStatus(
            provider=provider,
            status="UNKNOWN",
            configured_model=model,
            credential_envs={},
            notes=f"Unknown provider: {provider}",
        )
    
    def check_image(self) -> ProviderStatus:
        """Check image provider readiness."""
        # Check Vertex configuration
        project = self._check_env("NEWSAGENT_V2_VERTEX_PROJECT")
        location = self._check_env("NEWSAGENT_V2_VERTEX_LOCATION")
        credentials = self._check_env("GOOGLE_APPLICATION_CREDENTIALS")
        enabled = self.environ.get("NEWSAGENT_V2_VERTEX_ENABLED", "").lower() != "false"
        model = self.environ.get(IMAGE_MODEL_ENV, "imagetext-v1")
        
        envs = {
            "NEWSAGENT_V2_VERTEX_PROJECT": project,
            "NEWSAGENT_V2_VERTEX_LOCATION": location,
            "GOOGLE_APPLICATION_CREDENTIALS": credentials,
        }
        
        if not enabled:
            return ProviderStatus(
                provider=IMAGE_PROVIDER_VERTEX,
                status="DISABLED",
                configured_model=None,
                credential_envs=envs,
                notes="NEWSAGENT_V2_VERTEX_ENABLED is false or missing",
            )
        
        all_set = all(v == "SET" for v in envs.values())
        if not all_set:
            return ProviderStatus(
                provider=IMAGE_PROVIDER_VERTEX,
                status="MISSING_CONFIG",
                configured_model=None,
                credential_envs=envs,
            )

        # Env vars alone are not enough: google-auth + credential file must work.
        try:
            import google.auth  # noqa: F401
        except Exception:
            return ProviderStatus(
                provider=IMAGE_PROVIDER_VERTEX,
                status="MISSING_CONFIG",
                configured_model=None,
                credential_envs=envs,
                notes="python_package:google-auth is not installed",
            )

        creds_path = str(self.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
        if not creds_path or not Path(creds_path).is_file():
            return ProviderStatus(
                provider=IMAGE_PROVIDER_VERTEX,
                status="MISSING_CONFIG",
                configured_model=None,
                credential_envs=envs,
                notes="GOOGLE_APPLICATION_CREDENTIALS path missing or unreadable",
            )

        return ProviderStatus(
            provider=IMAGE_PROVIDER_VERTEX,
            status="READY",
            configured_model=model,
            credential_envs=envs,
        )
    
    def check_wordpress(self) -> ProviderStatus:
        """Check WordPress readiness."""
        base_url = self._check_env("NEWSAGENT_V2_WORDPRESS_BASE_URL")
        username = self._check_env("NEWSAGENT_V2_WORDPRESS_USERNAME")
        password = self._check_env("NEWSAGENT_V2_WORDPRESS_APP_PASSWORD")
        
        envs = {
            "NEWSAGENT_V2_WORDPRESS_BASE_URL": base_url,
            "NEWSAGENT_V2_WORDPRESS_USERNAME": username,
            "NEWSAGENT_V2_WORDPRESS_APP_PASSWORD": password,
        }
        
        all_set = all(v == "SET" for v in envs.values())
        
        if all_set:
            return ProviderStatus(
                provider="wordpress",
                status="READY",
                configured_model=None,
                credential_envs=envs,
            )
        
        return ProviderStatus(
            provider="wordpress",
            status="MISSING_CONFIG",
            configured_model=None,
            credential_envs=envs,
        )
    
    def full_report(self) -> dict[str, Any]:
        """Full readiness report."""
        writer = self.check_writer()
        image = self.check_image()
        wordpress = self.check_wordpress()
        
        return {
            "writer": {
                "provider": writer.provider,
                "status": writer.status,
                "model": writer.configured_model,
                "credentials": writer.credential_envs,
            },
            "image": {
                "provider": image.provider,
                "status": image.status,
                "model": image.configured_model,
                "credentials": image.credential_envs,
            },
            "wordpress": {
                "provider": wordpress.provider,
                "status": wordpress.status,
                "credentials": wordpress.credential_envs,
            },
            "ready_for_generation": writer.status == "READY",
            "ready_for_image": image.status == "READY",
            "ready_for_publishing": wordpress.status == "READY",
        }

    def get_readiness_summary(self) -> dict[str, Any]:
        """Quick summary for generation decision."""
        writer = self.check_writer()
        image = self.check_image()
        return {
            "can_write": writer.status == "READY",
            "can_image": image.status == "READY",
            "writer_provider": writer.provider,
            "image_provider": image.provider,
            "overall": writer.status == "READY" and image.status == "READY",
        }

    def is_controlled_mode(self) -> bool:
        """Check if controlled E2E mode is active."""
        return self.environ.get("NEWSAGENT_V5_CONTROLLED_E2E", "").lower() == "true"


def format_preflight_report(report: dict[str, Any]) -> str:
    """Format report for Telegram display."""
    lines = [
        "<b>⚙️ Generation Preflight</b>",
        "",
        f"Writer: {report['writer']['provider']} ({report['writer']['status']})",
        f"  Model: {report['writer']['model'] or 'N/A'}",
        f"  Credentials: {report['writer']['credentials'].get('GROQ_API_KEY', 'MISSING')}",
        "",
        f"Image: {report['image']['provider']} ({report['image']['status']})",
        f"  Model: {report['image']['model'] or 'N/A'}",
        "",
        f"WordPress: {report['wordpress']['status']}",
        "",
    ]
    
    if report["ready_for_generation"]:
        lines.append("✅ Ready for article generation")
    else:
        lines.append("❌ Writer credentials missing")
    
    if report["ready_for_image"]:
        lines.append("✅ Ready for image generation")
    else:
        lines.append("⚠️ Image generation not configured (article-only mode)")
    
    if report["ready_for_publishing"]:
        lines.append("✅ Ready for WordPress publishing")
    else:
        lines.append("⚠️ WordPress not configured")
    
    return "\n".join(lines)
