# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Small, dependency-free helpers for YouTube player-response fast paths."""

from __future__ import annotations

from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse


def normalize_unciphered_player_format(
    item: object,
) -> tuple[dict | None, bool]:
    """Return a format with a directly usable URL, without solving JS ciphers.

    YouTube sometimes wraps an already signed URL in ``signatureCipher`` even
    though no encrypted ``s`` challenge is present.  Treating every wrapper as
    encrypted made the bounded player-response lane discard safe itag-18 URLs
    and wait for the full yt-dlp extractor on every request.

    The helper deliberately stays fail-closed: an encrypted ``s`` value, an
    absent URL, or a non-HTTP(S) URL returns ``None`` so the normal extractor
    remains authoritative.  Plain ``sig``/``signature`` values are copied to
    the URL using the wrapper's ``sp`` parameter, matching yt-dlp's safe path.
    """

    if not isinstance(item, dict):
        return None, False

    direct_url = item.get("url")
    if isinstance(direct_url, str) and direct_url.startswith(
        ("http://", "https://")
    ):
        return dict(item), False

    cipher = item.get("signatureCipher") or item.get("cipher")
    if not isinstance(cipher, str) or not cipher:
        return None, False

    parsed_cipher = parse_qs(cipher, keep_blank_values=True)
    if any(str(value).strip() for value in parsed_cipher.get("s", ())):
        return None, False

    wrapped_urls = parsed_cipher.get("url") or ()
    if not wrapped_urls:
        return None, False
    recovered_url = str(wrapped_urls[0] or "").strip()
    if not recovered_url.startswith(("http://", "https://")):
        return None, False

    signature_values = (
        parsed_cipher.get("sig")
        or parsed_cipher.get("signature")
        or ()
    )
    signature = str(signature_values[0] or "").strip() if signature_values else ""
    if signature:
        signature_param = str(
            (parsed_cipher.get("sp") or ("signature",))[0] or "signature"
        ).strip() or "signature"
        parsed_url = urlparse(recovered_url)
        query = parse_qsl(parsed_url.query, keep_blank_values=True)
        if not any(key == signature_param for key, _value in query):
            query.append((signature_param, signature))
            recovered_url = urlunparse(
                parsed_url._replace(query=urlencode(query, doseq=True))
            )

    normalized = dict(item)
    normalized["url"] = recovered_url
    normalized["_micro_cipher_recovered"] = True
    return normalized, True


def summarize_player_response(response: object) -> dict[str, object]:
    """Return URL-free diagnostics for one Innertube player response.

    The summary makes fast-lane misses actionable without ever logging signed
    Googlevideo URLs, cookies, visitor data, or PO tokens.
    """

    if not isinstance(response, dict):
        return {
            "status": "invalid",
            "formats": 0,
            "adaptive": 0,
            "usable": 0,
            "safe_cipher": 0,
            "encrypted_cipher": 0,
        }

    playability = response.get("playabilityStatus") or {}
    status = str(playability.get("status") or "unknown").lower()
    streaming = response.get("streamingData") or {}
    formats = streaming.get("formats") or []
    adaptive = streaming.get("adaptiveFormats") or []
    usable = 0
    safe_cipher = 0
    encrypted_cipher = 0

    for item in (*formats, *adaptive):
        normalized, recovered = normalize_unciphered_player_format(item)
        if normalized is not None:
            usable += 1
            safe_cipher += int(bool(recovered))
            continue
        if not isinstance(item, dict):
            continue
        cipher = item.get("signatureCipher") or item.get("cipher")
        if isinstance(cipher, str) and cipher:
            parsed = parse_qs(cipher, keep_blank_values=True)
            if any(str(value).strip() for value in parsed.get("s", ())):
                encrypted_cipher += 1

    return {
        "status": status,
        "formats": len(formats),
        "adaptive": len(adaptive),
        "usable": usable,
        "safe_cipher": safe_cipher,
        "encrypted_cipher": encrypted_cipher,
    }
