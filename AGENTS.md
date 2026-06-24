# AGENTS.md

Guidance for Codex and other coding agents working in this repository.

## Project Snapshot

- This repo is a FastAPI dashboard for Bittensor subnet analysis.
- Backend entrypoint: `main.py`.
- Frontend entrypoint: `static/index.html` using vanilla JavaScript and Tailwind CDN styles.
- Redis is optional but important for performance. The app falls back to no-cache mode when Redis is unavailable.
- Historical Streamlit/reference code lives under `backup/`; do not treat it as the active app unless the user asks.

## Run Commands

- Install dependencies: `.venv/bin/python -m pip install -r requirements.txt`
- Start dev server: `.venv/bin/python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000`
- Start via module fallback if no venv exists: `python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000`
- Syntax check: `.venv/bin/python -m py_compile main.py`
- Health check after server start: `curl http://localhost:8000/health`
- PM2 deployment helper: `scripts/deploy_pm2.sh`

## Important Files

- `main.py`: FastAPI app, Bittensor calls, Redis cache helpers, streaming endpoints, diagnostics, cache management, and process stop route.
- `static/index.html`: dashboard UI, stream reader, filters, sorting, formatting, export, and stop button.
- `requirements.txt`: Python dependencies.
- `docs/`: project notes and manual QA checklists. Some docs may describe older behavior; verify against `main.py` and `static/index.html`.
- `server.log`, `dump.rdb`, `__pycache__/`, and local runtime outputs are not source-of-truth files.

## API Surface

Current routes in `main.py` include:

- `GET /`: serves `static/index.html`.
- `GET /api/subnets`: returns a full JSON subnet list, using the snapshot cache when present.
- `GET /api/subnets-stream`: streams NDJSON updates for progressive UI loading.
- `GET /api/subnet/{netuid}`: returns detail for one subnet.
- `GET /health`: health check.
- `POST /api/website/stop`: stops the running website process.
- `GET /api/diagnostic`: diagnostics.
- `GET /api/cache/status`: Redis/cache status.
- `POST /api/cache/clear`: clears cache.
- `POST /api/cache/clear-subnet/{netuid}`: clears one subnet's cache.

## Coding Guidelines

- Keep changes narrow and grounded in the failing layer.
- Prefer the existing single-file backend structure unless a requested change clearly justifies splitting modules.
- Preserve async behavior in request handlers. Bittensor SDK calls may be synchronous, so wrap blocking work with `asyncio.to_thread(...)` when it must run inside async flows.
- Do not replace `asyncio.as_completed(...)` with `async for`; it returns a normal iterator of awaitables.
- Keep Redis cache keys and TTLs explicit near the existing constants in `main.py`.
- Redis values are pickled Python objects. Be careful when changing cached shapes; add cache invalidation or compatibility handling.
- Separate stored numeric precision from display precision. For example, registration cost may be small; avoid rounding backend data just to match UI display.
- When changing UI formatting, inspect `static/index.html` helpers such as `format_tao(...)`, `formatNumber(...)`, filters, and sort logic before changing backend calculations.
- Keep user-facing dashboard text in Traditional Chinese where possible. Avoid introducing Simplified Chinese in new UI copy.
- Do not commit automatically unless the user explicitly asks.

## Performance Notes

- `MAX_CONCURRENT_REQUESTS` controls Bittensor concurrency. Raising it can improve cold-cache throughput but may overload upstream APIs.
- The main metagraph bottleneck is the synchronous `bt.Metagraph(...)` call. It should remain offloaded with `asyncio.to_thread(...)` when used from async code.
- `/api/subnets-stream` intentionally sends fast partial data first and then metagraph updates. Preserve this user experience when editing the loading path.
- Snapshot cache key: `subnets_snapshot:{NETWORK}`. Metagraph cache keys use `metagraph:{netuid}`.
- Cache hits can still cost CPU because metagraph arrays are unpickled and metrics are recomputed.

## Validation

For small backend edits:

1. Run `.venv/bin/python -m py_compile main.py`.
2. If behavior changed, start the server and check `GET /health`.
3. Exercise the relevant route with `curl` or the browser.

For frontend edits:

1. Start the dev server.
2. Open `http://localhost:8000`.
3. Verify stream loading, filters, sorting, export, and mobile layout if the changed area affects them.

For cache or Bittensor flow edits:

1. Check behavior with Redis available and unavailable if feasible.
2. Validate both cold-cache and warm-cache paths.
3. Watch `server.log` for exceptions and slow paths.

## Safety

- Do not delete user data, Redis dumps, logs, or cache files unless explicitly requested.
- Do not run destructive git commands.
- Do not hide Bittensor/network failures by returning misleading successful data.
- If tests are absent, say so and use `py_compile` plus targeted manual checks as the minimum validation.
