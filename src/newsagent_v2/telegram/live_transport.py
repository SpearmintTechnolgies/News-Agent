"""Live HTTP transport for Telegram Bot API.

Only enable for explicit user-requested live testing.
"""

from __future__ import annotations

from typing import Any

import requests

from newsagent_v2.telegram.config import TelegramConfig


def create_live_transport(config: TelegramConfig):
    """Create a live HTTP transport for Telegram API calls.
    
    Returns a callable that makes actual HTTP requests
    to api.telegram.org.
    """
    session = requests.Session()
    api_base = "https://api.telegram.org"
    
    def transport(
        redacted_url: str,  # URL with [REDACTED] token
        *,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: int = 30,
        method: str = "",  # Original method name (e.g., "getMe", "sendMessage")
    ) -> Any:
        """Make actual HTTP POST to Telegram API.
        
        Reconstructs the real URL from the redacted one by replacing [REDACTED]
        with the actual bot token.
        """
        # Reconstruct actual URL: replace [REDACTED] with real token
        if "[REDACTED]" in redacted_url:
            actual_url = redacted_url.replace("[REDACTED]", config.bot_token)
        elif method:
            # Fallback: construct URL directly
            actual_url = f"{api_base}/bot{config.bot_token}/{method}"
        else:
            actual_url = redacted_url
        
        try:
            if files:
                response = session.post(
                    actual_url,
                    data=json,
                    files=files,
                    timeout=timeout,
                )
            else:
                response = session.post(
                    actual_url,
                    json=json,
                    timeout=timeout,
                )
            
            return response
        except requests.exceptions.Timeout:
            class FakeResponse:
                status_code = 408
                def json(self): return {"ok": False, "error": "timeout"}
            return FakeResponse()
        except requests.exceptions.ConnectionError:
            class FakeResponse:
                status_code = 0
                def json(self): return {"ok": False, "error": "connection_error"}
            return FakeResponse()
        except Exception as e:
            class FakeResponse:
                status_code = 0
                def json(self): return {"ok": False, "error": str(e)}
            return FakeResponse()
    
    return transport
