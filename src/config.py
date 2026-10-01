"""Configuration for the GPT image art-asset pipeline.

Resolution order for every value (first hit wins):
    1. explicit CLI argument / ``load_settings(**overrides)``
    2. environment variable (including anything read from the ``.env`` file)
    3. the defaults declared in this module
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .paths import PROJECT_ROOT as _PATHS_ROOT, load_paths

# The layout is resolved in exactly one place - ``src.paths`` - which also lets the
# environment point the pipeline at another resource or harness folder. The names below
# are kept because the rest of the project already imports them.
PROJECT_ROOT = _PATHS_ROOT
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_HARNESS_DIR = PROJECT_ROOT / "hareness"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "resource"

CHARACTER_DIR_NAME = "characters"
STYLE_DIR_NAME = "style"
ANIMATION_FILE_NAME = "animation.yaml"

DEFAULT_BASE_URL = "http://token.wd.com"
DEFAULT_MODEL = "gpt-image-2.5-sunburst"
DEFAULT_API_STYLE = "images"

# Models this project is set up for (see the platform model list).
IMAGE_MODELS = (
    "gpt-image-2",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
)
API_STYLES = ("images", "openai", "gemini")
AUTH_STYLES = ("bearer", "x-goog-api-key")

_QUOTES = "\"" + chr(39)
_TRUTHY = {"1", "true", "yes", "y", "on"}


def parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal ``.env`` reader (used when python-dotenv is not installed)."""
    data: Dict[str, str] = {}
    if not Path(path).is_file():
        return data
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        data[key.strip()] = value
    return data


def load_env(env_file: Optional[Path] = None) -> Dict[str, str]:
    """Return the ``.env`` values overlaid with the real process environment."""
    env: Dict[str, str] = dict(parse_env_file(env_file or DEFAULT_ENV_FILE))
    env.update({k: v for k, v in os.environ.items() if v not in (None, "")})
    return env


def _first(env: Mapping[str, str], names: tuple, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = env.get(name)
        if value not in (None, ""):
            return value
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in _TRUTHY


def _resolve_path(value: Any, base: Path = PROJECT_ROOT) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def normalize_base_url(raw: str) -> str:
    """``token.wd.com`` / ``http://token.wd.com/`` -> ``http://token.wd.com``."""
    base = (raw or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE_URL
    if "://" not in base:
        base = "https://" + base
    for suffix in ("/v1/chat/completions", "/v1beta", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


@dataclass
class Settings:
    """Everything the clients need in order to talk to the relay platform."""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_style: str = DEFAULT_API_STYLE
    auth_style: str = "bearer"
    timeout: float = 300.0
    max_retries: int = 2
    retry_backoff: float = 5.0
    harness_dir: Path = DEFAULT_HARNESS_DIR
    output_dir: Path = DEFAULT_OUTPUT_DIR
    common_name: str = "common.yaml"
    send_image_config: bool = True
    proxy: Optional[str] = None
    write_metadata: bool = True
    send_safety_settings: bool = False
    openai_modalities: bool = True
    extra_payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def characters_dir(self) -> Path:
        return Path(self.harness_dir) / CHARACTER_DIR_NAME

    @property
    def common_path(self) -> Path:
        """The shared "sent with every request" block inside the harness folder."""
        return Path(self.harness_dir) / self.common_name

    @property
    def styles_dir(self) -> Path:
        return Path(self.harness_dir) / STYLE_DIR_NAME

    @property
    def animation_path(self) -> Path:
        """The clip library (``hareness/animation.yaml``) read by ``anim``."""
        return Path(self.harness_dir) / ANIMATION_FILE_NAME

    @property
    def masked_key(self) -> str:
        key = self.api_key or ""
        if len(key) > 14:
            return key[:7] + "..." + key[-4:]
        return "(unset)" if not key else "(too short)"

    def url_for(self, model: Optional[str] = None, api_style: Optional[str] = None) -> str:
        model = model or self.model
        style = (api_style or self.api_style or DEFAULT_API_STYLE).lower()
        base = normalize_base_url(self.base_url)
        if style == "images":
            return base + "/v1/images/generations"
        if style == "openai":
            return base + "/v1/chat/completions"
        return base + "/v1beta/models/" + model + ":generateContent"

    def headers(self, *, json_body: bool = True) -> Dict[str, str]:
        """Auth headers for one request.

        ``json_body=False`` is for ``multipart/form-data`` uploads: ``requests``
        only writes the multipart boundary when *it* sets the Content-Type, so an
        image-edit upload must not claim to be JSON.
        """
        headers = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if (self.auth_style or "bearer").lower() == "x-goog-api-key":
            headers["x-goog-api-key"] = self.api_key
        else:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def proxies(self) -> Optional[Dict[str, str]]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def require_key(self) -> str:
        if not self.api_key or len(self.api_key) < 8:
            raise SystemExit(
                "No API key found. Put it in .env as RELAY_API_KEY=... "
                "or pass --key on the command line."
            )
        return self.api_key


def load_settings(env_file: Optional[Path] = None, **overrides: Any) -> Settings:
    """Build :class:`Settings` from the environment plus CLI overrides."""
    env = load_env(env_file)

    settings = Settings(
        api_key=_first(
            env, ("RELAY_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"), ""
        )
        or "",
        base_url=normalize_base_url(
            _first(env, ("RELAY_BASE_URL", "OPENAI_BASE_URL", "GEMINI_BASE_URL"), DEFAULT_BASE_URL)
        ),
        model=_first(env, ("DEFAULT_MODEL", "OPENAI_MODEL", "GEMINI_MODEL", "MODEL"), DEFAULT_MODEL),
        api_style=_first(env, ("DEFAULT_API_STYLE", "API_STYLE"), DEFAULT_API_STYLE).lower(),
        auth_style=_first(env, ("AUTH_STYLE", "AUTH_HEADER"), "bearer").lower(),
        timeout=_as_float(_first(env, ("REQUEST_TIMEOUT", "TIMEOUT")), 300.0),
        max_retries=_as_int(_first(env, ("MAX_RETRIES",)), 2),
        retry_backoff=_as_float(_first(env, ("RETRY_BACKOFF",)), 5.0),
        proxy=_first(env, ("HTTP_PROXY_URL", "HTTPS_PROXY_URL"), None),
        write_metadata=_as_bool(_first(env, ("WRITE_METADATA",)), True),
        send_safety_settings=_as_bool(_first(env, ("SEND_SAFETY_SETTINGS",)), False),
        openai_modalities=_as_bool(_first(env, ("OPENAI_MODALITIES",)), True),
        common_name=_first(env, ("COMMON_FILE", "COMMON_BLOCK"), "common.yaml") or "common.yaml",
        send_image_config=_as_bool(_first(env, ("SEND_IMAGE_CONFIG",)), True),
    )

    # Where the project keeps its folders is decided in src.paths, once, so the CLI and
    # the GUI cannot disagree about it. Anything passed in explicitly still wins, below.
    paths = load_paths(env)
    settings.harness_dir = paths.harness
    settings.output_dir = paths.resource

    for key, value in overrides.items():
        if value is None:
            continue
        if not hasattr(settings, key):
            raise KeyError("Unknown setting override: " + str(key))
        setattr(settings, key, value)

    settings.base_url = normalize_base_url(settings.base_url)
    settings.harness_dir = Path(settings.harness_dir)
    settings.output_dir = Path(settings.output_dir)

    if settings.api_style not in API_STYLES:
        raise SystemExit(
            "Unknown api_style '" + str(settings.api_style) + "'. Use one of: "
            + ", ".join(API_STYLES) + "."
        )
    if settings.auth_style not in AUTH_STYLES:
        raise SystemExit(
            "Unknown auth_style '" + str(settings.auth_style) + "'. Use one of: "
            + ", ".join(AUTH_STYLES) + "."
        )
    return settings
