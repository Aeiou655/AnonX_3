# PO Token Provider

AnonX uses yt-dlp's provider framework with the official featured
`bgutil-ytdlp-pot-provider` plugin and its matching long-lived HTTP sidecar.

## Defaults

- Disabled unless explicitly configured
- No Deno start/stop per `/play`
- Cookies remain last-resort and optional

## Enable

```env
PO_TOKEN_PROVIDER_ENABLED=True
PO_TOKEN_PROVIDER_URL=http://127.0.0.1:4416
PO_TOKEN_CLIENT=mweb
PO_TOKEN_CACHE_SEC=300
```

Install `requirements.txt`, then start the matching provider:

```bash
docker compose up -d po-provider
python3 -m AnonX
```

For the complete container deployment:

```bash
docker compose up -d --build
```

Compose pins both the Python plugin and provider image to `1.3.1`. The provider
is published only on `127.0.0.1:4416`; container-to-container traffic uses
`http://po-provider:4416`.

The plugin talks to the provider's `/ping` and `/get_pot` endpoints and supplies
the correct challenge, content binding, proxy, client, and token context. An
arbitrary endpoint returning a token string is not protocol-compatible.

SoundCloud fallback does **not** require this provider.
