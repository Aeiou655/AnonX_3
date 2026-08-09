# AnonX v3.3.1 Final Release Runbook

## Build and verify

Use Python 3.13 in a clean virtual environment:

```bash
python3.13 -m venv .release-venv
. .release-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt
python -B ops/release_gate.py
```

On Windows PowerShell, activate with:

```powershell
py -3.13 -m venv .release-venv
.\.release-venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt
python -B ops/release_gate.py
```

Publish only when the gate ends with `RELEASE GATE OK`. Distribute the ZIP and
its `.sha256` sidecar together.

## Deploy

1. Back up the current application image or extracted release.
2. Keep the production `.env`, database, cookies, browser profile, sessions,
   downloads, and media outside the extracted release directory.
3. Extract `AnonX-v3.3.1-final.zip` into a new directory.
4. Create a fresh Python 3.13 environment and install `requirements.txt`.
5. Run `python -m pip check` and `python -B ops/verify_structure.py`.
6. Attach the existing runtime configuration and data mounts.
7. Start with `python -m AnonX`.
8. Verify startup, `/play`, `/vplay`, `/song`, `/vsong`, queue controls, and
   `GET /ping` when the Downloader API is enabled.

## Observe and roll back

For the first 30 to 60 minutes, watch process health, MongoDB connectivity,
voice-chat joins, download completion, and bounded provider warnings. To roll
back, stop the `v3.3.1` process, reattach the unchanged runtime configuration
and data to the prior release, then verify startup and one audio/video request.

The release does not migrate the public API, MongoDB schema, or `.env` keys.
