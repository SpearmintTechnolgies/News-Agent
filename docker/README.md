# Docker — News Agent OpenClaw + Bifrost

This folder packages **OpenClaw** (news pipeline + gateway + Telegram) and **Bifrost** (LLM gateway) as two Docker containers with persistent volumes. Images are pre-built and warmed for fast startup; transfer to another PC (including **Windows Docker Desktop, no WSL**) is done via the `news-agent-deploy` bundle.

**Full context, architecture, pitfalls, and transfer guidance:** [DOCKER_PREP_AND_TRANSFER.md](DOCKER_PREP_AND_TRANSFER.md)

---

## Quick start — local dev

From this directory (`docker/`):

```bash
./prepare-build-context.sh          # stage build-context/ from ~/.openclaw
./export-data.sh                    # optional: sync openclaw-data/ (see pitfall in full doc)
cp .env.example .env                # edit ports/secrets if needed
docker compose build news-agent     # builds warmed image (~5–10 min first time)
docker compose up -d
./verify.sh
```

- **OpenClaw UI:** http://localhost:18789  
- **Bifrost UI:** http://localhost:8888 (or `BIFROST_UI_PORT` from `.env`)

---

## Quick start — transfer to another PC

After local tests pass (see checklist in [DOCKER_PREP_AND_TRANSFER.md §9](DOCKER_PREP_AND_TRANSFER.md#9-pre-transfer-verification-checklist)):

```bash
./package-for-boss.sh               # creates news-agent-deploy/
zip -r news-agent-deploy.zip news-agent-deploy/   # optional, ~2.1 GB
```

Upload the zip or folder (Drive, USB, etc.). On the recipient Windows PC: extract → copy `.env.example` to `.env` → run **`deploy.bat`**. Details in `news-agent-deploy/README-WINDOWS.txt`.

---

## Script index

| Script | Purpose |
|---|---|
| [prepare-build-context.sh](prepare-build-context.sh) | Copy & patch OpenClaw tree into `build-context/` |
| [Dockerfile](Dockerfile) | Image + plugin warm step |
| [docker-compose.yml](docker-compose.yml) | Local dev stack (build + volumes) |
| [entrypoint.sh](entrypoint.sh) | Container startup |
| [warm-plugins.sh](warm-plugins.sh) | Bake plugin deps at build time |
| [export-data.sh](export-data.sh) | Populate `openclaw-data/` from `~/.openclaw` |
| [package-for-boss.sh](package-for-boss.sh) | Build transfer bundle |
| [verify.sh](verify.sh) | Smoke test running stack |
| [deploy.ps1.template](deploy.ps1.template) | Windows first-install script (copied into bundle) |
| [start.ps1.template](start.ps1.template) | Windows daily start after reboot (`start.bat`) |
| [rebuild-and-package.sh](rebuild-and-package.sh) | Full rebuild + zip for boss PC |

Image tag: **`openclaw-news-agent:2026.4.24`**

---

## Important reminders

- **Bifrost URL** inside the stack is always `http://bifrost:8080/v1` (Docker internal DNS).
- **Plugin deps** are baked in the image—do not mount over `node_modules` / `plugin-runtime-deps`.
- **Bifrost providers** are configured once per machine in the Bifrost UI (`bifrost-data/`).
- **Google Drive auth** is in `openclaw-data/gogcli-config/`; `.env` GOG_* values unlock it.
- **Boss daily:** `start.bat` — not `deploy.bat`.

See [DOCKER_PREP_AND_TRANSFER.md](DOCKER_PREP_AND_TRANSFER.md) for the full picture.
