# Private Crew Poker Engine — Execution Plan

## Context
The PRD (`poker_app_prd.md`) asks for a private, real-time Texas Hold'em web app for a fixed friend group, self-hosted on an organizer's machine and exposed via a temporary tunnel (ngrok/cloudflared) instead of paid cloud infra. This is a greenfield repo (currently just the PRD file) — there is no existing code to build on, so this plan defines the project from scratch.

Clarified scope (from user answers):
- **Table size:** single table, up to 9 players, no multi-table lobby.
- **Persistence:** in-memory Redis as source of truth, with Redis persistence (RDB/AOF) enabled so a server crash mid-game doesn't wipe chip stacks/state — still no permanent user accounts or cross-night history.
- **Frontend:** React + Vite, PWA-installable.
- **Hand evaluation:** will evaluate `treys` vs alternatives during implementation and pick the best-maintained/most accurate; `treys` is the likely default given it's pure-Python and dependency-light.
- **Timeout behavior:** auto-fold on 30s action timer expiry or disconnect.
- **Build strategy:** vertical slice first — get 2 players dealt cards, betting, and to showdown end-to-end with minimal UI, then layer in remaining players, side pots, theming, PWA polish, and tunneling.

## Architecture
- **Backend:** Python + FastAPI, `python-socketio` (ASGI mode) mounted into FastAPI for WebSocket transport.
- **State store:** Redis (Docker), with AOF persistence enabled (`appendonly yes`) for crash recovery of live game state.
- **Frontend:** React + Vite PWA (using `vite-plugin-pwa`), `socket.io-client` for realtime.
- **Hand evaluation:** `treys` library (7-card evaluator), swappable if issues surface.
- **Orchestration:** single `docker-compose.yml` running backend, frontend (dev/preview server), and Redis.
- **Tunnel:** ngrok or `cloudflared` pointed at the backend's public port (backend serves the built frontend + WebSocket on one port to keep the tunnel setup simple — avoids exposing two ports through the tunnel).

## Project Structure
```
StraddleUp/
  backend/
    app/
      main.py                # FastAPI app + socket.io mount, serves built frontend
      config.py               # runtime config (SB/BB/buy-in/passcode), set at host startup
      redis_client.py
      auth.py                 # passcode check + token issuance/validation
      game/
        engine.py             # table/session state machine (blinds, streets, turn order)
        dealer.py              # deck, dealing, board reveal
        betting.py             # bet/call/raise/fold validation, pot building
        side_pots.py           # side-pot arbitration per REQ-004 formula
        hand_eval.py            # treys wrapper, winner determination
        timers.py               # per-player action countdown, auto-fold on expiry
      sockets/
        events.py               # socket.io event handlers (join, action, state broadcast)
      models.py                 # pydantic models: Player, Table, Pot, Action
    tests/
      test_betting.py
      test_side_pots.py
      test_hand_eval.py
      test_engine_flow.py       # full hand simulations, e.g. 2p and multi-way all-in scenarios
    Dockerfile
    requirements.txt

  frontend/
    src/
      pages/
        HostSetup.tsx           # REQ-001: SB/BB/buy-in/passcode panel
        JoinRoom.tsx             # REQ-002: passcode entry
        Table.tsx                # main game view
      components/
        Seat.tsx, Board.tsx, PotDisplay.tsx, ActionBar.tsx, TurnTimer.tsx
      lib/
        socket.ts                # socket.io-client setup + reconnection/token handling
        theme.css                # REQ-005: PokerBaazi palette (#0b0e14, #ff2a6d, #05d9e8)
      state/
        gameStore.ts             # client-side state synced from server broadcasts
    vite.config.ts               # vite-plugin-pwa config
    Dockerfile

  docker-compose.yml
  README.md                      # setup + tunnel activation instructions
```

## Execution Phases

### Phase 1 — Vertical Slice (2 players, no side pots, plain UI)
Goal: prove the real-time loop works end-to-end.
- Backend: `models.py`, minimal `engine.py` (single table, deal hole cards, pre-flop/flop/turn/river progression, dealer button rotation), `betting.py` (check/bet/call/raise/fold with basic validation, no side pots yet — heads-up only needs a single pot), `hand_eval.py` using `treys`, socket.io events for join/act/state broadcast.
- Frontend: bare-bones `Table.tsx` with seats, hole cards, board, pot, and action buttons (no theming yet) wired to `socket.ts`.
- Auth: passcode entry (`auth.py`, `JoinRoom.tsx`) and host setup panel (`HostSetup.tsx`) built now since a hand can't start without them.
- Milestone: two browser tabs can join with a passcode, play a full hand pre-flop through showdown, and see correct winner/pot payout.

### Phase 2 — Full Table Mechanics
- Extend `engine.py`/`betting.py` to support up to 9 players, correct blind/button rotation, and turn-order edge cases (folded players skipped, all but one folded ends hand early).
- Implement `side_pots.py` per the PRD's `P_limit = Sum(min(Chips_i, Chips_all_in))` formula; cover with `test_side_pots.py` scenarios (2-way and 3-way all-in with uneven stacks).
- Implement `timers.py`: 30s countdown per turn, auto-fold on expiry, broadcast countdown to all clients.
- Session persistence: reconnection using the localStorage token — on reconnect, server re-associates the socket with the existing player seat/state instead of treating it as a new join.

### Phase 3 — Theming & PWA Polish
- Apply PokerBaazi-inspired theme (`theme.css`): matte black/slate backgrounds, neon pink/cyan accents, glow ring + timer on active seat, responsive seat layout around a felt table.
- Configure `vite-plugin-pwa` (manifest, icons, full-screen standalone display) so the app installs to a phone home screen per REQ-005/PWA requirement.

### Phase 4 — Containerization & Tunnel Deployment
- `docker-compose.yml` wiring backend, frontend, Redis (with `--appendonly yes`), single exposed port.
- README with exact steps: `docker compose up`, then `ngrok http 8000 --domain=...` (or `cloudflared tunnel`).
- Manual end-to-end test: run the full stack, tunnel it, join from a second physical device over the public URL, play a full hand.

## Key Requirement-to-Code Mapping
| Req | Where handled |
| --- | --- |
| REQ-001 | `HostSetup.tsx` → `config.py` |
| REQ-002 | `JoinRoom.tsx` + `auth.py` |
| REQ-003 | `sockets/events.py` + Redis state in `engine.py` |
| REQ-004 | `game/side_pots.py` |
| REQ-005 | `lib/theme.css` + `components/Seat.tsx`/`TurnTimer.tsx` |

## Verification Strategy
- **Unit tests** (`pytest` in `backend/tests/`): betting validation, side-pot math against known scenarios, hand evaluation correctness against fixed 7-card hands with known winners.
- **Integration test**: a scripted multi-client simulation (or manual 2-tab test) driving a full hand from blinds to showdown, verifying broadcast state matches expected pot/stack changes.
- **Manual UI verification**: per project convention, run the dev server and actually play hands in the browser (multiple tabs / devices) before declaring each phase done — checking timer expiry auto-folds, reconnection restores a player's seat, and side-pot payouts are correct when stacks differ.
- **Deployment verification**: bring up `docker-compose`, start a tunnel, and confirm an external device can join and play — this is the acceptance test for the PRD's core deployment strategy.

## Open Follow-ups (not blocking start)
- Exact reconnection UX (e.g., how long a disconnected seat is held open before being auto-removed) can be decided during Phase 2 implementation.
- Final choice between `ngrok` and `cloudflared` can be made in Phase 4 based on whichever the user already has an account/domain for.
