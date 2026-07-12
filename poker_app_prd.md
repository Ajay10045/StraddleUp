# Product Requirement Document (PRD): Private Crew Poker Engine
**Inspired by PokerBaazi UI Theme & Local Tunneling Deployment Strategy**

---

## 1. Executive Summary & Purpose
The objective of this project is to build a private, real-time, web-based Texas Hold'em Poker application designed exclusively for a fixed group of friends. The app replaces the physical constraints, scheduling friction, and travel overhead of home games while preserving the competitive, premium digital club experience.

To ensure total privacy and eliminate recurring monthly infrastructure hosting fees, the application follows a **Local Tunneling deployment strategy**. The application is hosted on an organizer's local machine or a private home device whenever a game session is active, exposing a secure, temporary public URL to the crew via an encrypted tunnel.

---

## 2. Architectural & Tech Stack Blueprint
The agent must implement a lean, efficient, and real-time state architecture using the following specific technologies:

*   **Frontend Framework:** React.js or Vue.js configured as a **Progressive Web App (PWA)**. This allows mobile players to run the app full-screen from their smartphone home screen without intrusive browser URL bars.
*   **Backend Engine:** Python using **FastAPI**. FastAPI's native asynchronous support is ideal for maintaining highly concurrent, persistent state connections.
*   **Real-Time Transport:** **WebSockets** via `socket.io` protocol implementations to push and pull game states instantly under 150ms.
*   **In-Memory State Store:** **Redis** (running locally via Docker) to manage live table actions, active player connections, and quick session tracking.
*   **Tunneling Layer:** **ngrok** or **Cloudflare Tunnels (`cloudflared`)** to securely proxy the local port to a public HTTPS URL.

---

## 3. Core Product Features

### 3.1 Session Creation & Stakes Configuration (Host Controls)
At launch, the host must be presented with a startup configuration panel to establish the specific parameters for the night's game. These values are committed to the application context before any player sits down:
*   **Small Blind (SB) Size:** Configurable integer input field (e.g., 5, 10, 25 chips).
*   **Big Blind (BB) Size:** Configurable integer input field (typically 2x Small Blind).
*   **Starting Buy-in Stack:** Configurable starting chip allocation per player (e.g., 1000 chips or 100 Big Blinds).
*   **Room Passcode:** An alphanumeric passcode generated or chosen by the host to secure the tunnel endpoint.

### 3.2 Entry Guard & Session Privacy
Because the application runs via a public tunnel link, unauthorized traffic must be strictly blocked at the routing edge:
*   **Zero-DB Passcode Authentication:** No public user registration or signup database is required. Users navigate to the link and must enter the exact Room Passcode established by the host during startup. The server verifies this against the running instance variables.
*   **Session Persistence:** Upon successful passcode entry, the frontend stores a secure token in local storage so players aren't disconnected or kicked if they accidentally refresh their browser.

### 3.3 Game Engine Mechanics
The backend engine must handle core Texas Hold'em rules explicitly:
*   **Blinds & Structure:** Automated Small Blind / Big Blind assignment (using the stakes specified at startup) rotating clockwise with the dealer button.
*   **Action Loop:** Sequential round handling (Pre-flop, Flop, Turn, River) with valid options dynamically pushed to active players (Check, Bet, Raise, Call, Fold).
*   **Side Pots & Split Pots:** Robust evaluation math to handle scenarios where multiple players go All-In with varying chip stacks. Total pot collected from player i must be limited by their max contribution: P_limit = Sum(min(Chips_i, Chips_all_in))
*   **Hand Evaluation:** Automated calculation using a verified lookup algorithm (e.g., `treys` or `DeucesWild` libraries) to find the winning 5-card combination at showdown.

### 3.4 PokerBaazi-Inspired UI Theme
The visual design must mimic modern premium digital poker lobbies:
*   **Color Palette:** Deep matte black/slate backgrounds (`#0b0e14`), vibrant neon pink/crimson actions (`#ff2a6d`), and electric cyan highlights (`#05d9e8`).
*   **Table Layout:** Centered responsive felt table with seats positioned dynamically around it based on the client viewport (optimized for mobile).
*   **State Indicators:** Glow rings around the active player whose turn it is, complete with a ticking digital countdown timer (e.g., 30 seconds to act).

---

## 4. Step-by-Step Deployment & Execution Flow
The agent must structure the application to boot smoothly using a single orchestration file.

### Step 1: Containerized Environment Setup
Define a local `docker-compose.yml` file containing the FastAPI backend server, the frontend PWA server, and the Redis instance. This ensures the environment remains identical across any host laptop or computer.

### Step 2: Activating the Secure Tunnel
The developer runs the local stack on port `8000`, then initiates the tunnel via CLI:
```bash
ngrok http 8000 --domain=your-chosen-subdomain.ngrok-free.app
```

### Step 3: Client Handshake and WebSocket Broadcast
Friends hit the public URL. The tunneling client safely relays the traffic to the local machine, establishing persistent WebSocket pipes for real-time game state distribution based on the initialized stakes.

---

## 5. Product Requirement Matrix for the AI Agent

| Module ID | Requirement Detail | Priority | Acceptance Criteria |
| :--- | :--- | :--- | :--- |
| **REQ-001** | Game Initialization Panel | **P0** | Host can input custom Small Blind, Big Blind, and Buy-in chip sizes upon launching the application before lobby generation. |
| **REQ-002** | Zero-DB Passcode Auth | **P0** | User types room password; server verifies against initial runtime configuration variable and grants room access token. |
| **REQ-003** | WebSocket Sync Engine | **P0** | All player actions update the central Redis state and broadcast to all connected clients under 150ms. |
| **REQ-004** | Side-Pot Arbitrator | **P1** | Accurately calculates split/side pots automatically when shorter stacks push All-In. |
| **REQ-005** | Neon Theme CSS Layout | **P1** | Matches PokerBaazi aesthetic using fluid layout grids with responsive table positioning. |
