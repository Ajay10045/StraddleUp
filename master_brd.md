# Master BRD — StraddleUp Private Poker

## 1. Product Summary

StraddleUp is a private, real-time, single-table Texas Hold'em web application for a regular group of friends. It replaces in-person home games with a phone-friendly experience while keeping the game night under the organizer's control.

The initial release is deliberately private:

- One host-operated table with 2–9 players.
- No public discovery, registration, payment processing, or persistent public player profiles.
- Social chips only. Friends settle money outside the app.
- Rebuys and cash-outs are recorded in the session ledger so the final settlement is clear.
- The host can run the app on their own laptop or home machine and share a temporary secure link for the duration of a game night.

The design must preserve a clean path to a future public product, without building public-product features prematurely.

## 2. Goals and Success Criteria

### Goals

1. Allow a host to start a private poker session and invite friends in minutes.
2. Run valid Texas Hold'em hands authoritatively on the server, including all-ins, side pots, and split pots.
3. Work well on mobile browsers and as an installable PWA.
4. Preserve an auditable record of completed sessions, hands, actions, rebuys, cash-outs, and final chip balances.
5. Recover a player's table seat after a temporary browser refresh or network interruption.
6. Avoid a mandatory always-on cloud deployment or recurring hosting cost for the private release.

### Success Criteria

- A host can configure stakes and open a passcode-protected lobby.
- Two to nine invited players can join from different networks and complete a full hand.
- Every valid action reaches connected players quickly; server processing should normally be well below 150 ms. End-to-end internet latency is outside the server's control.
- Invalid, out-of-turn, or stale client actions are rejected by the server.
- Timeout auto-fold, reconnection, all-in, side-pot, and split-pot behavior are covered by automated tests.
- A completed session remains viewable after the host stops and restarts the application.

## 3. Scope

### In scope for v1

- No-limit Texas Hold'em, one table, 2–9 seats.
- Host setup: small blind, big blind, starting buy-in, room passcode, action timeout, and maximum seats.
- Anonymous, room-scoped players using a display name and a reconnect token.
- Lobby, seating, ready/start control, dealer button/blinds, four betting streets, showdown, and the next hand.
- Fold, check, call, bet, raise, all-in, automatic timeout fold, side pots, and split pots.
- One complete-buy-in re-buy amount (equal to the configured starting buy-in) and cash-out/sit-out flows.
- Live table state over WebSockets, responsive neon-themed PWA UI, and session/hand history.
- Local hosting with a secure public tunnel.

### Explicitly out of scope for v1

- Real-money payments, wallets, deposits, withdrawals, or in-app settlement.
- Public accounts, player profiles, password recovery, social discovery, multi-table tournaments, and matchmaking.
- Native iOS/Android applications.
- Spectator mode, chat, hand replays, leaderboards, and statistics dashboards.
- Always-on cloud hosting, high availability, or public-scale anti-cheat/fraud systems.

## 4. Product Decisions

| Area | v1 decision |
| --- | --- |
| Table size | One table, 2–9 players. |
| Stakes | Set by host before the first hand; locked once play starts. |
| Chips | Social ledger only; all money settlement happens outside the app. |
| Buy-in | Every initial buy-in and rebuy equals the configured starting buy-in. |
| Rebuy control | Player requests a rebuy; host approves it so chip issuance remains auditable. |
| Cash-out | Player may cash out only between hands. A request during a hand becomes effective after that hand ends. |
| Timeout | Host-configurable, default 30 seconds. The active player auto-folds when it expires. |
| Network loss | The current action timer continues. A returning player resumes their seat if they reconnect before expiry; otherwise they auto-fold when the timer ends. |
| Identity | Display name plus a signed, room-scoped reconnect token. No account or cross-session profile. |
| Game history | Persisted permanently on the host's database. |

## 5. Primary User Flows

### 5.1 Host starts a game night

1. Host starts the local application stack.
2. Host opens the local control screen and enters small blind, big blind, starting buy-in, maximum seats, action timeout, and room passcode.
3. Server creates a new session, stores the configuration, and opens a lobby.
4. Host starts a tunnel and shares the generated HTTPS link plus the room passcode privately.
5. Host seats players, approves rebuys, and starts the first hand when at least two players are ready.

### 5.2 Player joins and plays

1. Player opens the shared link on a phone or desktop browser.
2. Player enters the room passcode and a display name.
3. Server issues a signed reconnect token and the player chooses an available seat.
4. Client receives the current lobby/table snapshot and maintains a WebSocket connection.
5. On their turn, the player sees only legal actions and sends one action to the server.
6. Server validates, persists, updates the authoritative game state, and broadcasts an appropriate view to every player.

### 5.3 Rebuy, sit out, and cash out

1. A seated player requests a complete buy-in rebuy between hands.
2. Host accepts or rejects the request. On acceptance, the server increments that player's stack and records a ledger event.
3. A player choosing to leave during a hand is marked `leave_after_hand`; their current hand continues normally.
4. Once the hand is resolved, the server records the player's final stack as a cash-out, frees the seat, and includes it in the session settlement summary.

## 6. Game Rules and Engine Requirements

### 6.1 Authoritative state machine

All game logic lives on the server. The client never calculates legal moves, deals cards, or decides winners. It only renders its permitted view and submits an intent.

The engine must model: lobby, hand setup, pre-flop, flop, turn, river, showdown, payout, and hand-complete states. It must rotate the dealer button and assign blinds correctly, including heads-up rules.

### 6.2 Betting and turn handling

- Server calculates legal action options and minimum/maximum amounts for the active player.
- The active player is the only player whose action is accepted.
- Fold, check, call, bet, raise, and all-in must obey no-limit betting rules.
- A hand ends immediately when all but one eligible player has folded.
- The timer begins only when an actionable player becomes active. Folded and all-in players are skipped.
- Expiry triggers a server-issued fold. A reconnect does not reset or extend the timer.

### 6.3 Pots and showdown

- The engine calculates main and side pots from each player's hand contribution, including uneven all-ins.
- Only eligible players can win each pot.
- The evaluator selects the best five-card hand from seven cards using a well-tested library such as `treys`, behind a small adapter.
- Tied eligible winners split a pot. Any unavoidable odd chip is allocated by a documented, deterministic dealer-button order.
- The server records cards, contributions, result, payout, and winning hand metadata at completion.

### 6.4 Privacy of cards

- A player receives their own hole cards only.
- Other players receive concealed cards until showdown, then only cards that must be revealed by the game result are exposed.
- Server logs retain the full deal for completed-hand history accessible to the host; player-facing history policy can be added later.

## 7. Network, Hosting, and Disconnect Model

### 7.1 Recommended private-release deployment

No cloud deployment is required for v1. The host runs Docker on their laptop/home computer. PostgreSQL, Redis, backend, and the built frontend run locally, and Docker volumes keep database data on that host machine.

The host starts an outbound encrypted tunnel using Cloudflare Tunnel or ngrok. The tunnel produces an HTTPS URL such as `https://table.example-tunnel.com`. Friends open that link from wherever they are; their browser connects through the tunnel to the host machine. The host does not need to configure router port forwarding.

The host machine must stay powered on, awake, connected to the internet, and running both the application and tunnel. Closing the tunnel, stopping Docker, losing the host's internet, or sleeping the laptop makes the table unavailable until it returns.

### 7.2 Do all players need internet?

Yes, when friends are in different homes, every player needs an internet connection. The host needs internet for the tunnel and each player needs internet to open the public HTTPS URL and maintain the WebSocket.

If everyone is on the same Wi-Fi network, the host could instead share a local-network URL. That is optional and less convenient; the tunnel remains the simplest single-link solution for mixed locations.

### 7.3 Reconnection behavior

Socket.IO reconnects automatically with backoff. The client saves its reconnect token locally and sends it after reconnecting. The server verifies that token, restores the existing seat, and sends a full current-state snapshot; it does not create a new player.

- A disconnected player remains seated with their chips intact.
- If it is their turn, their original timer keeps running and ends in an auto-fold at the configured timeout.
- If it is not their turn, they can reconnect later and resume normally before their next action is due.
- A refresh follows the same reconnect path.
- If a token is unavailable or invalid, the player returns as a new lobby participant and must not be allowed to take an occupied seat.
- The host sees a disconnected indicator and can remove a player only between hands.

### 7.4 Security boundary

The shared URL is protected by HTTPS and an application-level room passcode. Passcodes are stored hashed in the session database; the raw passcode is never persisted. Signed, expiring, room-scoped reconnect tokens prevent simple seat impersonation.

An application passcode does **not** literally block traffic at the tunnel edge—it blocks access after the request reaches the app. True edge access control would require an additional ngrok/Cloudflare access policy and normally some form of participant authentication. For v1, use an unguessable tunnel URL, a strong room passcode, rate-limit failed attempts, and share both only with the invited group.

## 8. Technical Architecture

### 8.1 Components

| Component | Technology | Responsibility |
| --- | --- | --- |
| Web client | React, TypeScript, Vite, `vite-plugin-pwa`, `socket.io-client` | Host setup, join flow, table UI, PWA installation, reconnect UI. |
| API/game server | Python, FastAPI, `python-socketio` in ASGI mode | Authentication, authoritative game engine, REST host/history APIs, WebSocket events. |
| Durable data | PostgreSQL | Sessions, anonymous session players, hands, actions, ledger events, final settlement summaries. |
| Live state | Redis with AOF enabled | Current table state, turn timers, socket presence, transient coordination, restart recovery support. |
| Evaluation | `treys` behind an adapter | Seven-card hand ranking and winner determination. |
| Runtime | Docker Compose | Repeatable local stack and persistent named volumes. |
| Public access | Cloudflare Tunnel preferred; ngrok supported | HTTPS tunnel to a single local public port. |

### 8.2 Persistence strategy

Redis is fast, but it is not the sole record of game history. Redis holds live state and enables quick reconnection. PostgreSQL is the durable source for audit/history. Critical state changes are written transactionally to PostgreSQL and then broadcast. Redis AOF helps restore a live table after a process restart, but the server must reconcile that state against the durable database.

No user profile table is needed. A `session_players` record exists only within a poker session and stores a generated ID, display name, seat, status, chip stack, and token reference. A later public version can add a `users` table without rewriting game-history records.

### 8.3 Core data model

| Record | Key fields |
| --- | --- |
| `sessions` | id, status, blinds, buy-in, max seats, timeout, passcode hash, timestamps. |
| `session_players` | id, session id, display name, seat, status, current stack, reconnect-token hash. |
| `hands` | id, session id, number, dealer seat, board, status, pot total, timestamps. |
| `hand_players` | hand id, session player id, hole cards, starting/ending stack, total contribution, folded/all-in status, payout. |
| `hand_actions` | hand id, sequence, street, actor, action type, amount, resulting pot, timestamp. |
| `ledger_events` | session player id, type (`initial_buyin`, `rebuy`, `cashout`, `payout_adjustment`), amount, approval metadata, timestamp. |
| `session_settlements` | session id, final summary, closed timestamp. |

## 9. User Interface Requirements

- Deep matte black/slate base (`#0b0e14`), neon pink/crimson primary actions (`#ff2a6d`), and electric cyan highlights (`#05d9e8`).
- Responsive felt table with seats dynamically positioned for the viewport and usable on a narrow phone screen.
- Clear active-seat glow, live action countdown, pot, blind level, button/blind markers, player connection status, and stack values.
- Only display actions currently valid for the player; expose bet/raise controls with a clear minimum amount.
- Use optimistic button feedback only; final UI state always follows a server broadcast.
- Provide accessible color contrast, readable type, touch-sized controls, and a visible reconnecting/disconnected state.

## 10. API and Socket Contract (Initial)

### HTTP endpoints

- `POST /api/host/sessions` — create the host session and lobby.
- `GET /api/sessions/{id}/history` — host-authorized session/hand history.
- `POST /api/sessions/{id}/close` — host closes the session and creates its settlement summary.
- `GET /health` — local runtime health check.

### Socket events

- Client to server: `join_room`, `choose_seat`, `ready`, `player_action`, `request_rebuy`, `host_approve_rebuy`, `request_cashout`, `start_hand`, `reconnect`.
- Server to client: `table_snapshot`, `private_hand_update`, `action_rejected`, `turn_started`, `timer_tick`, `player_connection_changed`, `rebuy_updated`, `hand_result`, `session_closed`.

Every state-changing message includes a session/table version. The server rejects stale intents and responds with a fresh snapshot when required.

## 11. Delivery Plan

### Phase 0 — Foundation and decisions

- Scaffold React/TypeScript frontend, FastAPI/socket.io backend, PostgreSQL, Redis, and Docker Compose.
- Define state-machine interfaces, database migrations, secure configuration/secrets handling, and local development workflow.
- Deliverable: stack starts locally, health checks pass, and a tunnel can expose one public port.

### Phase 1 — Private lobby and identity

- Build host configuration, session creation, passcode verification, display-name join, signed reconnect token, lobby, seating, and host start controls.
- Deliverable: 2–9 devices can join the same private lobby through a tunnel and refresh without losing their seat.

### Phase 2 — Two-player vertical slice

- Implement deal, blinds, betting through all streets, fold/check/call/bet/raise, showdown, hand evaluation, snapshots, and basic table UI.
- Deliverable: two browsers complete valid hands end-to-end and receive correct pot payouts.

### Phase 3 — Full table engine and resilience

- Add 3–9 player turn rules, all-ins, side/split pots, timer auto-fold, disconnect behavior, leave-after-hand, rebuy approval, cash-out ledger, and history persistence.
- Deliverable: deterministic automated tests cover uneven all-ins, ties, stale actions, timeout folds, reconnects, and cash-out timing.

### Phase 4 — PWA and visual polish

- Implement the PokerBaazi-inspired responsive design, installable PWA metadata/icons, animations, mobile QA, and host history/settlement view.
- Deliverable: a polished phone-first table that can be installed and played by the group.

### Phase 5 — Game-night validation

- Run a tunnel from the intended host machine and play a real multi-device test session.
- Test home/Wi-Fi and cellular clients, refresh during a hand, timeout, host restart recovery, rebuy, cash-out, side pots, and history export/view.
- Deliverable: documented operating checklist and all acceptance criteria signed off.

## 12. Verification Strategy

- Unit tests: hand evaluator, turn order, betting validation, blind rotation, side pots, split pots, odd-chip handling, and ledger calculations.
- Integration tests: scripted multi-client hands from lobby through settlement, including reconnect and state-version rejection.
- Security tests: passcode rate limiting, invalid/expired token rejection, card-visibility isolation, and authorization of host-only actions.
- Manual mobile tests: Android/iOS browser layout, PWA installation, low-bandwidth reconnect behavior, and actual tunnel access from an external network.
- Data tests: session history remains available after containers restart and database volume persists.

## 13. Operational Requirements

- Use environment variables for secrets; never commit passcodes, signing secrets, or tunnel credentials.
- Provide `docker compose up -d`, database backup, restore, and tunnel startup instructions in `README.md`.
- The host should keep the computer on AC power and disable sleep for the game duration.
- Back up the PostgreSQL Docker volume periodically if long-term history matters.
- The host closes a session only after all desired cash-outs are recorded; the resulting settlement summary is immutable except for an explicitly logged host adjustment.

## 14. Future Extension Path

The v1 boundaries intentionally support later additions: persistent accounts mapped to existing anonymous session-player records, cloud deployment for an always-on table, multi-table rooms, real-time spectator/chat features, player statistics, and—only after legal/compliance review—any payment-related feature.

