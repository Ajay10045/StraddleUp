# StraddleUp

Private, self-hosted Texas Hold'em for a regular game night. It runs on the host's computer; invited friends open one HTTPS tunnel link in a browser. Chips are a social ledger only—settlement happens outside the app.

## What is included

- One private table for 2–9 players, configurable blinds, buy-in, passcode, and action timer.
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

   Set `POSTGRES_PASSWORD` and `APP_SECRET` to long random values. Never share `.env`.

3. Start the table:

   ```sh
   docker compose up --build -d
   ```

4. Open [http://localhost:8080](http://localhost:8080), configure the table, then take a seat as host.
5. For remote friends, start one tunnel to the frontend port and privately share the HTTPS URL and room passcode:

   ```sh
   cloudflared tunnel --url http://localhost:8080
   ```

   Or, with ngrok:

   ```sh
   ngrok http 8080
   ```

The host machine must remain awake, connected to the internet, and running Docker plus the tunnel. Every remote player needs internet access. No router port forwarding is needed.

## Operating notes

- A disconnected player keeps their seat. Their current turn still expires normally and auto-folds.
- Cash-out during a hand becomes effective after that hand completes.
- Hand/session history is retained in the PostgreSQL Docker volume. Back it up before deleting Docker volumes.
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
