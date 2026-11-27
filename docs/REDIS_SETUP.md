# Redis setup (Windows)

CallIntel needs **Redis 6.2 or newer** (Redis Streams `XAUTOCLAIM`). Redis 8 is recommended. Without a server, `run` and `process` fall back to an in-process stand-in (fakeredis) that is single-process and loses all state when the process exits. That is fine for a quick test, but not for real runs.

Pick **one** option.

| Option | Best for | Effort | Notes |
|---|---|---|---|
| A. Memurai Developer | Fastest start on Windows | 2 min | Native Windows service, Redis 8.2 compatible. Dev/test only: restarts every 10 days, uses at most 50% of RAM, production use is prohibited |
| B. WSL2 + Ubuntu 24.04 | Closest to production (Linux), no limits | 10-15 min, may need a reboot | Real Redis. Ubuntu 24.04 ships Redis 7.0. **Do not use Ubuntu 22.04**, whose Redis 6.0 is too old |
| C. Docker Desktop | If you already use Docker | 15+ min | Uses `docker-compose.yml` in this repo (Redis 8.2) |

Avoid hosted "free Redis" services for this project. Even masked transcript text should stay on-prem.

---

## Option A: Memurai Developer (native Windows)

Run this in PowerShell and approve the Windows admin prompt:

```powershell
winget install --id Memurai.MemuraiDeveloper -e
```

**If it fails with MSI error 1603** (the log shows `SFXCA: Failed to create temp directory. Error code 5`): this is a known Windows Installer issue with installers built using WiX's managed custom actions. A security change moved the custom-action temp folder into a protected system location, so checks running as a non-admin user are denied. The fix is to start the installer itself elevated. winget leaves the downloaded, signed MSI in your temp folder:

```powershell
Start-Process msiexec.exe -Verb RunAs -Wait -ArgumentList '/i "C:\Users\<you>\AppData\Local\Temp\WinGet\Memurai.MemuraiDeveloper.4.1.2\Memurai-Developer-v4.1.2.msi" /l*v "C:\Temp\memurai-install.log"'
```

Memurai installs as an auto-start Windows service on `127.0.0.1:6379`. Memurai Developer 4.1.2 reports Redis API 7.2.5, which meets the 6.2+ requirement. Check it; the reply should be `PONG`:

```powershell
& "C:\Program Files\Memurai\memurai-cli.exe" ping
```

Then apply the [recommended settings](#recommended-settings-for-callintel). On Memurai, `CONFIG REWRITE` fails with `ERR Rewriting config file: Invalid argument`, so the runtime `CONFIG SET` values would be lost at the next restart (and the Developer edition restarts every 10 days). Persist them with the included script instead, which backs up `memurai.conf`, edits it and restarts the service (one admin prompt):

```powershell
Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList '-NoProfile -File "<path-to>\banking-call-intel\scripts\memurai_persist_settings.ps1"'
```

## Option B: WSL2 + Ubuntu 24.04 (recommended for a production-like setup)

1. In PowerShell, install Ubuntu and approve the admin prompt. Reboot if Windows asks, then open "Ubuntu 24.04" from the Start menu and create a Linux user.

   ```powershell
   wsl --install -d Ubuntu-24.04
   ```

2. Inside the Ubuntu terminal, install and start Redis:

   ```bash
   sudo apt update && sudo apt install -y redis-server
   sudo systemctl enable --now redis-server
   redis-server --version     # must show v=7.x or newer
   redis-cli ping             # PONG
   ```

   For Redis 8, add the official repository first (from https://redis.io/docs/latest/operate/oss_and_stack/install/), then run `sudo apt install redis`.

3. WSL2 forwards local ports, so Windows apps reach it at `redis://127.0.0.1:6379`. Check from PowerShell:

   ```powershell
   wsl -d Ubuntu-24.04 -- redis-cli ping
   ```

4. WSL can stop the Linux VM after you close all Ubuntu windows. If CallIntel later reports Redis as unreachable, start it again from PowerShell:

   ```powershell
   wsl -d Ubuntu-24.04 -u root -- service redis-server start
   ```

## Option C: Docker Desktop

Install Docker Desktop (it uses WSL2), then from the `banking-call-intel` folder:

```powershell
docker compose up -d redis
```

The compose file already enables AOF persistence, a 2 GB memory cap, and `volatile-lru` eviction.

---

## Recommended settings for CallIntel

Run these once (`redis-cli` for options B/C inside the Linux/container shell, or `memurai-cli` for option A):

```bash
redis-cli CONFIG SET appendonly yes                  # queue + manifest survive restarts
redis-cli CONFIG SET maxmemory 2gb
redis-cli CONFIG SET maxmemory-policy volatile-lru   # evict only TTL'd cache keys, never queues/manifest
redis-cli CONFIG REWRITE                             # persist to the config file
```

Why `volatile-lru`: LLM, embedding and alias caches have TTLs and can be evicted safely. The work queue, DLQ, review queue and manifest have no TTL, so they are never evicted.

If Redis runs on another machine, set a password (`CONFIG SET requirepass <strong-password>` or an ACL user), enable TLS, and put the credentials only in `.env`.

## Point CallIntel at Redis

1. Create `.env` from the template (in `banking-call-intel`) if you haven't yet:

   ```powershell
   copy .env.example .env
   ```

   It already contains `REDIS_URL=redis://127.0.0.1:6379/0`. With a password, use `redis://:PASSWORD@127.0.0.1:6379/0`, or `rediss://...` for TLS.

   **Use `127.0.0.1`, not `localhost`, on Windows.** `localhost` resolves to IPv6 `::1` first. Memurai and Ollama listen on IPv4 only, so every new connection waits about 2 s before falling back (measured: Redis 2,035 ms vs 21 ms; Ollama 2,363 ms vs 183 ms). With a 2 s connect timeout, that also causes intermittent "Redis unreachable" errors.

2. `config/config.yaml` ships with `redis.allow_fakeredis_fallback: false`, so a Redis outage fails loudly instead of quietly running in memory. Set it to `true` only for laptop experiments without a server. The test suite always uses fakeredis and never needs a server.

3. Verify:

   ```powershell
   .\.venv\Scripts\python -m callintel doctor
   ```

   Expected: `redis : redis://127.0.0.1:6379/0 (redis 7.2.5) -> OK` (the version depends on your server).

4. Run with real multi-process workers:

   ```powershell
   .\.venv\Scripts\python -m callintel run --processes 2 --concurrency 2
   ```

   Check queue depth, DLQ, the review queue, manifest counts and today's metrics:

   ```powershell
   .\.venv\Scripts\python -m callintel status
   ```

   For a long-running setup, keep one process enqueuing new files every 10 seconds:

   ```powershell
   .\.venv\Scripts\python -m callintel ingest --watch 10
   ```

   And, in another terminal, the workers:

   ```powershell
   .\.venv\Scripts\python -m callintel worker --processes 2 --concurrency 2
   ```

On a single 6 GB GPU, more processes will not make the LLM faster (Ollama serves one request at a time). Multiple processes pay off for CPU-heavy masking (Presidio/GLiNER) and with vLLM or OpenAI.

## Inspecting what CallIntel stores

```bash
redis-cli --scan --pattern "ci:*" | head            # keys
redis-cli XLEN ci:stream:ingest                      # waiting jobs
redis-cli XRANGE ci:stream:dlq - + COUNT 5           # failed jobs and their errors
redis-cli HGETALL ci:stats:$(date +%F)               # today's counters
redis-cli HLEN ci:alias                              # learned product aliases
```

Redis only ever holds **masked** text and metadata. Raw transcripts and PII never reach it, except the optional encrypted vault, which is off by default.
