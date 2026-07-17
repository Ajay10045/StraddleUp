# StraddleUp

Private, self-hosted Texas Hold'em for a regular game night. It runs on the host's computer; invited friends open one HTTPS tunnel link in a browser. Chips are a social ledger only—settlement happens outside the app.

## What is included

- Host-only table creation for 2–9 players, configurable blinds, buy-in, invite passcode, and action timer.
- Mobile-friendly PWA table with real-time Socket.IO updates.
- Server-authoritative betting, blind rotation, all-ins, side pots, split pots, and showdown evaluation.
- Automatic timeout folds and reconnect tokens that restore a player’s existing seat after refresh/network loss.
- Host-approved full rebuys, cash-out-after-hand, and durable event history.
- PostgreSQL persists history; Redis keeps fast live state with AOF enabled.

## First game-night setup

1. Install Docker Desktop and make sure it is running.
2. Copy the environment template and choose strong secrets:

   ```sh
   cp .env.example .env
   ```

   Set `POSTGRES_PASSWORD` and `APP_SECRET` to long random values. Set `HOST_ACCESS_CODE` only in this local file. Never share or commit `.env`.

3. Start the table:

   ```sh
   docker compose up --build -d
   ```

4. Open [http://localhost:8080](http://localhost:8080), sign in with the host code from your local `.env`, and use the Host Console. Only that console can create, share, or end tables; a player link only opens that table's lobby.
5. Start one tunnel to the frontend port:

   ```sh
   cloudflared tunnel --url http://localhost:8080
   ```

   Or, with ngrok:

   ```sh
   ngrok http 8080
   ```

6. Copy the bare public HTTPS address printed by the tunnel (without `?session=...`) into **Public game address** in the Host Console. Create a table, take a seat as host, then use the in-app Copy/WhatsApp/Gmail invite controls. A table does not create another Cloudflare URL: its invitation is the same public base address plus `?session=<table-code>`.

The host machine must remain awake, connected to the internet, and running Docker plus the tunnel. Every remote player needs internet access. No router port forwarding is needed.

## Operating notes

- A disconnected player keeps their seat. Their current turn still expires normally and auto-folds.
- A returning player reconnects with the same browser token, name, stack, and seat. If that browser identity is lost, the host can release the disconnected seat from the table controls.
- Players who sit during a live hand wait until the next hand; they cannot alter the active hand's chip accounting.
- Cash-out during a hand becomes effective after that hand completes.
- Hand/session history is retained in the PostgreSQL Docker volume. Back it up before deleting Docker volumes.
- The Host Console separates live tables from completed sessions. Completed entries open final stacks, buy-ins, net settlement, and completed-hand totals.
- Stop the app with `docker compose down`. Do **not** add `-v` unless you intentionally want to erase the game history.

## Development checks

Run backend tests locally after creating a Python environment and installing `backend/requirements.txt`:

```sh
cd backend
pytest -q
```

The end-to-end container build is:

```sh
docker compose up --build
```
