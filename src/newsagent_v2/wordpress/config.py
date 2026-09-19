"""WordPress REST config. Explicit environ only. Never logs secrets."""

from __future__ import annotations

from dataclasses import dataclass

BASE_ENV = "NEWSAGENT_V2_WORDPRESS_BASE_URL"
USER_ENV = "NEWSAGENT_V2_WORDPRESS_USERNAME"
PASSWORD_ENV = "NEWSAGENT_V2_WORDPRESS_APP_PASSWORD"


class WordPressConfigError(ValueError):
    """Missing or invalid WordPress configuration."""


@dataclass(frozen=True)
class WordPressConfig:
    base_url: str
    username: str
    app_password: str

    def __post_init__(self) -> None:
        base = self.base_url.strip().rstrip("/")
        user = self.username.strip()
        password = self.app_password.strip()
        if not base.startswith(("http://", "https://")):
            raise WordPressConfigError(f"{BASE_ENV} must be an http(s) URL")
        if not user:
            raise WordPressConfigError(f"{USER_ENV} is empty")
        if not password:
            raise WordPressConfigError(f"{PASSWORD_ENV} is empty")
        object.__setattr__(self, "base_url", base)
        object.__setattr__(self, "username", user)
        object.__setattr__(self, "app_password", password)

    def secrets(self) -> tuple[str, ...]:
        return (self.app_password, self.username)


def load_wordpress_config(environ: dict[str, str] | None) -> WordPressConfig:
    if environ is None:
        raise WordPressConfigError("environ must be provided explicitly")
    missing = [name for name in (BASE_ENV, USER_ENV, PASSWORD_ENV) if name not in environ]
    if missing:
        raise WordPressConfigError("WordPress env not set: " + ", ".join(missing))
    return WordPressConfig(
        base_url=str(environ[BASE_ENV]),
        username=str(environ[USER_ENV]),
        app_password=str(environ[PASSWORD_ENV]),
    )
