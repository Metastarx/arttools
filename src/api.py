"""HTTP clients for the third-party relay endpoints (GPT image models first).

Two calling conventions are supported, matching the platform documentation:

* ``gemini`` - ``POST {base}/v1beta/models/{model}:generateContent``
* ``openai`` - ``POST {base}/v1/chat/completions``

Relay platforms are not perfectly consistent about how they hand back image
bytes, so responses are parsed defensively: base64 parts (``inlineData`` /
``b64_json``), ``data:`` URLs, markdown image links and plain image URLs are
all understood and downloaded when necessary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import requests

from .config import Settings, normalize_base_url

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}

_MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}

_DATA_URI_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=_\-\s]+)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+?)\s*\)")
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>()\[\]]+?\.(?:png|jpe?g|webp|gif|bmp)(?:\?[^\s\"'<>()\[\]]*)?",
    re.IGNORECASE,
)


class ApiError(RuntimeError):
    """Transport failure or non-2xx response."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        body: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


class NoImageError(ApiError):
    """The endpoint answered with 200 but no image could be found."""


@dataclass
class GeneratedImage:
    data: bytes
    mime_type: str = "image/png"
    source: str = "unknown"

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.data).hexdigest()

    @property
    def extension(self) -> str:
        return _EXT_BY_MIME.get((self.mime_type or "").lower(), "png")


@dataclass
class GenerationResult:
    images: List[GeneratedImage] = field(default_factory=list)
    text: str = ""
    model: str = ""
    endpoint: str = ""
    api_style: str = ""
    request: Dict[str, Any] = field(default_factory=dict)
    response: Any = None
    elapsed: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return bool(self.images)


def cap_size_string(size: str, max_side: int) -> str:
    """``"1536x1024"`` + cap 1024 -> ``"1024x683"`` (longest side clamped)."""
    if not size or "x" not in str(size).lower():
        return size
    try:
        width, height = (int(part) for part in str(size).lower().split("x", 1))
    except ValueError:
        return size
    longest = max(width, height)
    if longest <= 0 or longest <= max_side:
        return size
    ratio = max_side / float(longest)
    new_width = max(1, int(round(width * ratio)))
    new_height = max(1, int(round(height * ratio)))
    if width >= height:
        new_width = max_side
    else:
        new_height = max_side
    return str(new_width) + "x" + str(new_height)


def shrink_to_max_side(data: bytes, max_side: int) -> Optional[bytes]:
    """Re-encode ``data`` so its longest side is ``max_side``.

    Returns ``None`` when the image already fits (or cannot be read), so callers
    keep the original bytes untouched whenever possible.
    """
    try:
        import io as _io

        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(_io.BytesIO(data)) as image:
            width, height = image.size
            longest = max(width, height)
            if longest <= 0 or longest <= max_side:
                return None
            ratio = max_side / float(longest)
            new_width = max(1, int(round(width * ratio)))
            new_height = max(1, int(round(height * ratio)))
            if width >= height:
                new_width = max_side
            else:
                new_height = max_side
            resized = image.convert("RGBA" if "A" in image.mode else "RGB").resize(
                (new_width, new_height), _resample()
            )
            buffer = _io.BytesIO()
            resized.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:  # pragma: no cover - malformed image
        log.warning("Could not shrink an oversized image: %s", exc)
        return None


def _resample():
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS


def sniff_mime(data: bytes) -> Optional[str]:
    """Guess the image type from its magic bytes."""
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            if magic == b"RIFF" and data[8:12] != b"WEBP":
                continue
            return mime
    return None


def decode_base64(text: str) -> Optional[bytes]:
    """Decode base64 that may be url-safe, padded or line-wrapped."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
    cleaned = cleaned.rstrip("=")
    try:
        return base64.b64decode(cleaned + "=" * ((-len(cleaned)) % 4))
    except (binascii.Error, ValueError):
        return None


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _signature(data: bytes) -> Optional[bytes]:
    """A 16x16 greyscale fingerprint used to spot re-encoded duplicates."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("L").resize((16, 16)).tobytes()
    except Exception:  # pragma: no cover - malformed image
        return None


def _is_near_duplicate(signature: bytes, known: List[bytes], tolerance: int = 2) -> bool:
    for other in known:
        if all(abs(left - right) <= tolerance for left, right in zip(signature, other)):
            return True
    return False


def _looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 128:
        return False
    return sniff_mime(data) is not None or len(data) > 1024


def _add_url(url: str, urls: List[str], seen: set) -> None:
    candidate = (url or "").strip().rstrip(".,;)\\\"'")
    if not candidate.startswith("http") or candidate in seen:
        return
    seen.add(candidate)
    urls.append(candidate)


def _collect_from_string(text: str, images: List[GeneratedImage], urls: List[str], seen: set) -> None:
    if not text or len(text) < 16:
        return
    if "data:image/" in text:
        for match in _DATA_URI_RE.finditer(text):
            data = decode_base64(match.group(2))
            if data:
                mime = (match.group(1) or "").lower() or sniff_mime(data) or "image/png"
                images.append(GeneratedImage(data, mime, "data-uri"))
    if "http" in text:
        for match in _MD_IMAGE_RE.finditer(text):
            _add_url(match.group(1), urls, seen)
        for match in _IMAGE_URL_RE.finditer(text):
            _add_url(match.group(0), urls, seen)


def _strip_images_from_text(text: str) -> str:
    text = _DATA_URI_RE.sub(" ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _download(url: str, session: requests.Session, settings: Optional[Settings]) -> Optional[GeneratedImage]:
    timeout = 120
    proxies = None
    if settings is not None:
        timeout = min(float(settings.timeout), 120.0)
        proxies = settings.proxies()
    try:
        response = session.get(url, timeout=timeout, proxies=proxies)
    except requests.RequestException as exc:
        log.warning("Could not download image url %s: %s", url, exc)
        return None
    if response.status_code != 200:
        log.warning("Could not download image url %s (HTTP %s)", url, response.status_code)
        return None
    data = response.content
    if not _looks_like_image(data):
        log.warning("URL %s did not return image bytes.", url)
        return None
    return GeneratedImage(data, sniff_mime(data) or "image/png", url)


def extract_images(
    payload: Any,
    *,
    session: Optional[requests.Session] = None,
    settings: Optional[Settings] = None,
    fetch_urls: bool = True,
) -> Tuple[List[GeneratedImage], str]:
    """Pull every image (and any prose) out of an arbitrary API response."""
    images: List[GeneratedImage] = []
    texts: List[str] = []
    urls: List[str] = []
    seen_urls: set = set()

    for node in _walk(payload):
        if isinstance(node, dict):
            inline = node.get("inlineData") or node.get("inline_data")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                data = decode_base64(inline["data"])
                if data:
                    mime = inline.get("mimeType") or inline.get("mime_type") or sniff_mime(data) or "image/png"
                    images.append(GeneratedImage(data, str(mime), "inlineData"))
                    continue
            if isinstance(node.get("b64_json"), str):
                data = decode_base64(node["b64_json"])
                if data:
                    images.append(GeneratedImage(data, sniff_mime(data) or "image/png", "b64_json"))
                    continue
            image_url = node.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str):
                if image_url.startswith("data:image/"):
                    data = decode_base64(image_url.partition("base64,")[2])
                    if data:
                        images.append(GeneratedImage(data, sniff_mime(data) or "image/png", "data-uri"))
                elif image_url.startswith("http"):
                    _add_url(image_url, urls, seen_urls)
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        elif isinstance(node, str):
            _collect_from_string(node, images, urls, seen_urls)

    if fetch_urls and urls:
        session = session or requests.Session()
        for url in urls:
            image = _download(url, session, settings)
            if image is not None:
                images.append(image)

    unique: List[GeneratedImage] = []
    seen_hashes: set = set()
    fingerprints: List[bytes] = []
    for image in images:
        digest = image.sha1
        if digest in seen_hashes:
            continue
        signature = _signature(image.data)
        if signature is not None and _is_near_duplicate(signature, fingerprints):
            log.info("Dropped a pixel-identical copy returned by the platform (%s).", image.source)
            continue
        seen_hashes.add(digest)
        if signature is not None:
            fingerprints.append(signature)
        unique.append(image)

    prose = dict.fromkeys(
        cleaned
        for cleaned in (_strip_images_from_text(item) for item in texts)
        if len(cleaned) > 1
    )
    return unique, " ".join(prose)


def strip_image_config(payload: Dict[str, Any]) -> bool:
    """Remove aspect-ratio / size hints; some relays reject them with HTTP 400."""
    removed = False
    generation_config = payload.get("generationConfig")
    if isinstance(generation_config, dict) and generation_config.pop("imageConfig", None) is not None:
        removed = True
    if payload.pop("image_config", None) is not None:
        removed = True
    return removed


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``extra`` into ``base`` (mutates and returns ``base``)."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _describe_http_error(response: requests.Response) -> str:
    hints = {
        400: " - the platform rejected the payload (try --api-style openai)",
        401: " - check RELAY_API_KEY",
        403: " - key is not allowed to use this model",
        404: " - check RELAY_BASE_URL and the model name",
        429: " - rate limited or out of quota",
    }
    try:
        detail = json.dumps(response.json(), ensure_ascii=False)[:600]
    except ValueError:
        detail = (response.text or "").strip()[:600]
    return (
        "HTTP "
        + str(response.status_code)
        + hints.get(response.status_code, "")
        + (": " + detail if detail else "")
    )


def _cap_image(image: "GeneratedImage", max_side: int) -> "GeneratedImage":
    """Downscale one response image so its longest side never exceeds ``max_side``."""
    shrunk = shrink_to_max_side(image.data, max_side)
    if shrunk is None:
        return image
    log.info(
        "Response image was larger than %s px - shrunk locally (%.1f KB -> %.1f KB).",
        max_side,
        len(image.data) / 1024.0,
        len(shrunk) / 1024.0,
    )
    return GeneratedImage(shrunk, "image/png", image.source + "+capped")


def _load_reference(item: Any) -> Tuple[str, bytes, str]:
    """Normalise one reference image into ``(filename, bytes, mime)``."""
    if isinstance(item, GeneratedImage):
        return ("reference." + item.extension, item.data, item.mime_type or "image/png")
    if isinstance(item, (bytes, bytearray)):
        blob = bytes(item)
        return ("reference.png", blob, sniff_mime(blob) or "image/png")
    file_path = Path(item)
    blob = file_path.read_bytes()
    return (file_path.name or "reference.png", blob, sniff_mime(blob) or "image/png")


def load_references(items: Sequence[Any]) -> List[Tuple[str, bytes, str]]:
    """Load reference images (paths, raw bytes or :class:`GeneratedImage`)."""
    references = [_load_reference(item) for item in items]
    if not references:
        raise ApiError("Reference-based generation needs at least one reference image.")
    return references


def data_uri(mime: str, blob: bytes) -> str:
    """``image/png`` + bytes -> ``data:image/png;base64,...`` (for chat payloads)."""
    return "data:" + (mime or "image/png") + ";base64," + base64.b64encode(blob).decode("ascii")


class BaseClient:
    """Shared request/response plumbing for both calling conventions."""

    style = "images"

    # Safety overrides are only meaningful on the native Gemini convention, but the
    # list lives here so both builders can share one helper.
    SAFETY_CATEGORIES = (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )

    def __init__(self, settings: Settings, session: Optional[requests.Session] = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.drop_image_config = not settings.send_image_config

    def build_payload(
        self,
        prompt: str,
        *,
        model: str,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
        max_side: Optional[int] = None,
    ) -> GenerationResult:
        resolved_model = model or self.settings.model
        payload = self.build_payload(
            prompt,
            model=resolved_model,
            count=count,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            temperature=temperature,
            extra=extra,
            system=system,
        )
        if self.drop_image_config:
            strip_image_config(payload)
        if max_side:
            # Ask for a smaller render when the platform honours the hint...
            hinted = payload.get("size")
            if isinstance(hinted, str):
                payload["size"] = cap_size_string(hinted, int(max_side))

        url = self.settings.url_for(resolved_model, self.style)
        log.debug("POST %s", url)
        started = time.time()
        try:
            raw = self._post(url, payload)
        except ApiError as exc:
            if exc.status != 400 or not strip_image_config(payload):
                raise
            log.warning(
                "The platform rejected the aspect-ratio / size hint (HTTP 400). "
                "Retrying without imageConfig - describe the composition in the prompt instead."
            )
            self.drop_image_config = True
            raw = self._post(url, payload)
        return self._finish(
            raw,
            url=url,
            model=resolved_model,
            payload=payload,
            started=started,
            max_side=max_side,
        )

    def _post(self, url: str, payload: Dict[str, Any]) -> Any:
        try:
            response = self.session.post(
                url,
                headers=self.settings.headers(),
                json=payload,
                timeout=self.settings.timeout,
                proxies=self.settings.proxies(),
            )
        except requests.Timeout as exc:
            raise ApiError(
                "Request timed out after " + str(self.settings.timeout) + "s",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise ApiError("Network error: " + str(exc), retryable=True) from exc

        if response.status_code >= 400:
            raise ApiError(
                _describe_http_error(response),
                status=response.status_code,
                body=(response.text or "")[:2000],
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                "Response was not JSON (HTTP " + str(response.status_code) + "): "
                + (response.text or "")[:400],
                status=response.status_code,
            ) from exc

    def _post_multipart(
        self,
        url: str,
        data: Dict[str, Any],
        files: List[Tuple[str, Tuple[str, bytes, str]]],
    ) -> Any:
        """POST a ``multipart/form-data`` body: reference image(s) plus text fields."""
        try:
            response = self.session.post(
                url,
                headers=self.settings.headers(json_body=False),
                data=data,
                files=files,
                timeout=self.settings.timeout,
                proxies=self.settings.proxies(),
            )
        except requests.Timeout as exc:
            raise ApiError(
                "Request timed out after " + str(self.settings.timeout) + "s",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise ApiError("Network error: " + str(exc), retryable=True) from exc

        if response.status_code >= 400:
            raise ApiError(
                _describe_http_error(response),
                status=response.status_code,
                body=(response.text or "")[:2000],
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                "Response was not JSON (HTTP " + str(response.status_code) + "): "
                + (response.text or "")[:400],
                status=response.status_code,
            ) from exc

    def _finish(
        self,
        raw: Any,
        *,
        url: str,
        model: str,
        payload: Dict[str, Any],
        started: float,
        max_side: Optional[int] = None,
    ) -> GenerationResult:
        """Parse a response into images + prose and enforce the local pixel cap."""
        images, text = extract_images(raw, session=self.session, settings=self.settings)
        if max_side:
            # The gateway silently ignores size hints, so cap it locally instead.
            images = [_cap_image(image, int(max_side)) for image in images]
        if not images:
            log.warning("No image in response. Model text: %s", (text or "(empty)")[:400])
        return GenerationResult(
            images=images,
            text=text,
            model=model,
            endpoint=url,
            api_style=self.style,
            request=payload,
            response=raw,
            elapsed=time.time() - started,
        )

    def generate_with_reference(
        self,
        prompt: str,
        references: Sequence[Any],
        *,
        model: Optional[str] = None,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        system: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        max_side: Optional[int] = None,
    ) -> GenerationResult:
        """Generate *from* reference images instead of from text alone.

        Implemented by each calling convention, because the platform exposes
        img2img on a different endpoint per convention.
        """
        raise NotImplementedError

    def _safety_settings(self) -> List[Dict[str, str]]:
        return [
            {"category": category, "threshold": "BLOCK_NONE"}
            for category in self.SAFETY_CATEGORIES
        ]


class GeminiClient(BaseClient):
    """Native style: ``/v1beta/models/{model}:generateContent``."""

    style = "gemini"

    def build_payload(
        self,
        prompt: str,
        *,
        model: str,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        generation_config: Dict[str, Any] = {
            "responseModalities": ["TEXT", "IMAGE"],
            "candidateCount": 1,
        }
        if temperature is not None:
            generation_config["temperature"] = temperature

        image_config: Dict[str, Any] = {}
        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        if image_size:
            image_config["imageSize"] = image_size
        if image_config:
            generation_config["imageConfig"] = image_config

        payload: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        if self.settings.send_safety_settings:
            payload["safetySettings"] = self._safety_settings()
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if self.settings.extra_payload:
            deep_merge(payload, self.settings.extra_payload)
        if extra:
            deep_merge(payload, extra)
        return payload

    def generate_with_reference(
        self,
        prompt: str,
        references: Sequence[Any],
        *,
        model: Optional[str] = None,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        system: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        max_side: Optional[int] = None,
    ) -> GenerationResult:
        """Reference images as ``inlineData`` parts on ``:generateContent``."""
        resolved_model = model or self.settings.model
        loaded = load_references(references)

        parts: List[Dict[str, Any]] = [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(blob).decode("ascii")}}
            for _name, blob, mime in loaded
        ]
        parts.append({"text": prompt})

        generation_config: Dict[str, Any] = {
            "responseModalities": ["TEXT", "IMAGE"],
            "candidateCount": 1,
        }
        image_config: Dict[str, Any] = {}
        if aspect_ratio:
            image_config["aspectRatio"] = aspect_ratio
        if image_size:
            image_config["imageSize"] = image_size
        if image_config:
            generation_config["imageConfig"] = image_config

        payload: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }
        if self.settings.send_safety_settings:
            payload["safetySettings"] = self._safety_settings()
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if self.settings.extra_payload:
            deep_merge(payload, self.settings.extra_payload)
        if extra:
            deep_merge(payload, extra)

        url = self.settings.url_for(resolved_model, self.style)
        log.debug("POST %s (%s reference image(s))", url, len(loaded))
        started = time.time()
        raw = self._post(url, payload)
        return self._finish(
            raw,
            url=url,
            model=resolved_model,
            payload=payload,
            started=started,
            max_side=max_side,
        )


class OpenAIClient(BaseClient):
    """OpenAI-compatible style: ``/v1/chat/completions``."""

    style = "openai"

    def build_payload(
        self,
        prompt: str,
        *,
        model: str,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if self.settings.openai_modalities:
            payload["modalities"] = ["text", "image"]

        image_config: Dict[str, Any] = {}
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        if image_size:
            image_config["image_size"] = image_size
        if image_config:
            payload["image_config"] = image_config

        if self.settings.extra_payload:
            deep_merge(payload, self.settings.extra_payload)
        if extra:
            deep_merge(payload, extra)
        return payload

    def generate_with_reference(
        self,
        prompt: str,
        references: Sequence[Any],
        *,
        model: Optional[str] = None,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        system: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        max_side: Optional[int] = None,
    ) -> GenerationResult:
        """Reference images as ``image_url`` data URIs on ``/v1/chat/completions``."""
        resolved_model = model or self.settings.model
        loaded = load_references(references)

        content: List[Dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": data_uri(mime, blob)}}
            for _name, blob, mime in loaded
        ]
        content.append({"type": "text", "text": prompt})

        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        payload: Dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "stream": False,
        }
        if self.settings.openai_modalities:
            payload["modalities"] = ["text", "image"]

        image_config: Dict[str, Any] = {}
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        if image_size:
            image_config["image_size"] = image_size
        if image_config:
            payload["image_config"] = image_config

        if self.settings.extra_payload:
            deep_merge(payload, self.settings.extra_payload)
        if extra:
            deep_merge(payload, extra)

        url = self.settings.url_for(resolved_model, self.style)
        log.debug("POST %s (%s reference image(s))", url, len(loaded))
        started = time.time()
        raw = self._post(url, payload)
        return self._finish(
            raw,
            url=url,
            model=resolved_model,
            payload=payload,
            started=started,
            max_side=max_side,
        )


class ImagesClient(BaseClient):
    """OpenAI Images style: ``POST {base}/v1/images/generations``.

    This is the endpoint the ``gpt-image-*`` models live on. Transparency is not
    supported by those models, so the pipeline asks for a fixed pure-green
    background and keys it out afterwards (see :mod:`src.postprocess`).
    """

    style = "images"

    SIZE_BY_RATIO = {
        "1:1": "1024x1024",
        "4:3": "1536x1024",
        "3:2": "1536x1024",
        "16:9": "1536x1024",
        "3:4": "1024x1536",
        "2:3": "1024x1536",
        "9:16": "1024x1536",
    }

    def build_payload(
        self,
        prompt: str,
        *,
        model: str,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        text = prompt
        if system:
            text = system.strip() + "\n\n" + prompt

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": text,
            "n": max(1, int(count or 1)),
            "output_format": "png",
        }

        size = str(image_size).strip() if image_size else ""
        if size and "x" not in size.lower():
            size = ""
        if not size and aspect_ratio:
            size = self.SIZE_BY_RATIO.get(str(aspect_ratio).strip(), "")
        if size:
            payload["size"] = size

        if self.settings.extra_payload:
            deep_merge(payload, self.settings.extra_payload)
        if extra:
            deep_merge(payload, extra)
        return payload

    def generate_with_reference(
        self,
        prompt: str,
        references: Sequence[Any],
        *,
        model: Optional[str] = None,
        count: int = 1,
        aspect_ratio: Optional[str] = None,
        image_size: Optional[str] = None,
        system: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        max_side: Optional[int] = None,
    ) -> GenerationResult:
        """Continue or restyle from reference images: ``POST {base}/v1/images/edits``.

        The first reference is the character portrait and any further ones are the
        frames already drawn of the same animation. Multipart text fields have to
        be flat strings, so nested ``extra_payload`` entries are skipped here.
        """
        resolved_model = model or self.settings.model
        loaded = load_references(references)

        text = prompt
        if system:
            text = system.strip() + "\n\n" + prompt

        data: Dict[str, Any] = {
            "model": resolved_model,
            "prompt": text,
            "n": str(max(1, int(count or 1))),
            "output_format": "png",
        }
        size = str(image_size).strip() if image_size else ""
        if size and "x" not in size.lower():
            size = ""
        if not size and aspect_ratio:
            size = self.SIZE_BY_RATIO.get(str(aspect_ratio).strip(), "")
        if size:
            data["size"] = cap_size_string(size, int(max_side)) if max_side else size

        for source in (self.settings.extra_payload or {}, extra or {}):
            for key, value in source.items():
                if isinstance(value, (str, int, float, bool)):
                    data[str(key)] = str(value)

        url = normalize_base_url(self.settings.base_url) + "/v1/images/edits"
        files = [("image", (name, blob, mime)) for name, blob, mime in loaded]
        log.debug("POST %s (%s reference image(s))", url, len(files))
        started = time.time()
        raw = self._post_multipart(url, data, files)
        return self._finish(
            raw,
            url=url,
            model=resolved_model,
            payload=dict(data),
            started=started,
            max_side=max_side,
        )


def client_for(
    settings: Settings,
    api_style: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> BaseClient:
    """Return the client matching ``api_style`` (defaults to ``settings``)."""
    style = (api_style or settings.api_style or "images").lower()
    if style == "images":
        return ImagesClient(settings, session)
    if style == "openai":
        return OpenAIClient(settings, session)
    return GeminiClient(settings, session)


def list_models(
    settings: Settings, session: Optional[requests.Session] = None
) -> Tuple[int, Any, str]:
    """``GET {base}/v1/models`` -> ``(status_code, payload, url)``."""
    session = session or requests.Session()
    url = normalize_base_url(settings.base_url) + "/v1/models"
    try:
        response = session.get(
            url,
            headers=settings.headers(),
            timeout=min(float(settings.timeout), 60.0),
            proxies=settings.proxies(),
        )
    except requests.RequestException as exc:
        raise ApiError("Could not reach " + url + ": " + str(exc)) from exc
    try:
        payload: Any = response.json()
    except ValueError:
        payload = (response.text or "")[:1000]
    return response.status_code, payload, url


def _run_with_retries(
    settings: Settings, call: Any, *, retry_empty: bool = True
) -> GenerationResult:
    """Retry transport errors / 429 / 5xx / empty responses around one call."""
    attempts = max(1, int(settings.max_retries) + 1)
    backoff = max(0.0, float(settings.retry_backoff))
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            result = call()
            result.attempts = attempt
            if result.images or not retry_empty:
                return result
            last_error = NoImageError(
                "The endpoint returned no image (model said: "
                + (result.text or "(nothing)")[:200]
                + ")"
            )
        except ApiError as exc:
            if not exc.retryable:
                raise
            last_error = exc

        if attempt < attempts:
            wait = backoff * attempt + random.uniform(0.0, 1.5)
            log.warning(
                "Attempt %s/%s failed: %s -- retrying in %.1fs",
                attempt,
                attempts,
                str(last_error)[:300],
                wait,
            )
            time.sleep(wait)

    if last_error is not None:
        raise last_error
    raise ApiError("Generation failed for an unknown reason")


def generate_with_retry(
    client: BaseClient,
    prompt: str,
    *,
    retry_empty: bool = True,
    **kwargs: Any,
) -> GenerationResult:
    """Call the API, retrying transport errors / 429 / 5xx / empty responses."""
    return _run_with_retries(
        client.settings, lambda: client.generate(prompt, **kwargs), retry_empty=retry_empty
    )


# A route that cannot serve image edits is worth retrying on the other convention.
ROUTE_FALLBACK_STATUS = {400, 404, 405, 415, 501}


def generate_reference_with_retry(
    settings: Settings,
    prompt: str,
    references: Sequence[Any],
    *,
    api_style: Optional[str] = None,
    retry_empty: bool = True,
    **kwargs: Any,
) -> GenerationResult:
    """Image-to-image generation, with a route fallback.

    The relay exposes img2img differently per calling convention (multipart
    ``/v1/images/edits`` for ``images``, ``image_url`` data URIs for ``openai``,
    ``inlineData`` parts for ``gemini``). The route matching ``api_style`` is
    tried first; if the platform does not implement it, the next one is tried, so
    a spec keeps working whichever convention its model prefers.
    """
    primary = (api_style or settings.api_style or "images").lower()
    routes = [primary] + [route for route in ("images", "openai") if route != primary]

    errors: List[str] = []
    for position, route in enumerate(routes):
        client = client_for(settings, route)
        try:
            return _run_with_retries(
                settings,
                lambda: client.generate_with_reference(prompt, references, **kwargs),
                retry_empty=retry_empty,
            )
        except ApiError as exc:
            if exc.status in ROUTE_FALLBACK_STATUS and position + 1 < len(routes):
                log.warning(
                    "The %s route did not accept reference images (HTTP %s) - trying %s.",
                    route,
                    exc.status,
                    routes[position + 1],
                )
                errors.append(route + ": " + str(exc)[:200])
                continue
            raise

    raise ApiError("No route accepted the reference image. " + " | ".join(errors)[:600])
