import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { io, Socket } from "socket.io-client";
import "./styles.css";
import { useEffect, useRef, useState } from "react";

declare global {
  interface HTMLElement { __straddleupRoot?: ReturnType<typeof createRoot>; }
}

type Player = { id: string; name: string; seat: number; stack: number; status: string; connected: boolean; ready: boolean; inHand: boolean; folded: boolean; allIn: boolean; holeCards: string[]; rebuyRequested?: boolean; leaveAfterHand?: boolean; cashoutAmount?: number };
type TableState = { sessionId: string; version: number; status: string; config: { smallBlind: number; bigBlind: number; buyIn: number; maxSeats: number; timeoutSeconds: number; autoNextHandSeconds: number }; players: Player[]; buttonSeat: number | null; handNumber: number; turnDeadline: number | null; nextHandAt: number | null; hand: null | { street: string; board: string[]; smallBlindSeat: number; bigBlindSeat: number; actorSeat: number | null; pot: number; currentBet: number }; lastResult: null | { type: string; board: string[]; pots: { amount: number; winners: string[]; handClass?: string }[] }; legalActions: Record<string, any>; viewerId: string; isHost: boolean; serverTime: number };

const socket: Socket = io({ autoConnect: true, transports: ["websocket", "polling"] });

function storageKey(sessionId: string) { return `straddleup:${sessionId}:token`; }

function App() {
  const params = new URLSearchParams(window.location.search);
  const [sessionId, setSessionId] = useState(params.get("session") || "");
  const [hostToken, setHostToken] = useState("");
  const [findingTable, setFindingTable] = useState(!params.get("session"));
  const [state, setState] = useState<TableState | null>(null);
  const [playerId, setPlayerId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [message, setMessage] = useState("");
  // clockOffset = serverTime - client Date.now() at the moment a snapshot arrived; added to the
  // local clock so countdowns track the server's deadlines despite client clock drift.
  const [clockOffset, setClockOffset] = useState(0);
  const lastVersion = useRef(-1);

  useEffect(() => {
    if (sessionId) {
      setHostToken(params.get("host") || localStorage.getItem(`straddleup:${sessionId}:host`) || "");
      setFindingTable(false);
      return;
    }
    fetch("/api/current-session")
      .then(response => response.json())
      .then(data => { if (data.sessionId) setSessionId(data.sessionId); else setFindingTable(false); })
      .catch(() => setFindingTable(false));
  }, [sessionId]);

  useEffect(() => {
    // New session (or re-subscribe): forget the previous session's version watermark.
    lastVersion.current = -1;
    socket.on("table_snapshot", (next: TableState) => {
      // Transport upgrades/reconnects can deliver snapshots out of order; the server stamps a
      // monotonic `version`, so ignore any snapshot that isn't newer than the last one applied.
      if (typeof next.version === "number" && next.version < lastVersion.current) return;
      lastVersion.current = next.version;
      if (typeof next.serverTime === "number") setClockOffset(next.serverTime - Date.now());
      setState(next);
    });
    socket.on("joined", (data: { playerId: string; reconnectToken: string }) => {
      setPlayerId(data.playerId);
      if (sessionId) localStorage.setItem(storageKey(sessionId), data.reconnectToken);
    });
    socket.on("action_rejected", (data: { message: string }) => setMessage(data.message));
    socket.on("connect", () => setMessage(""));
    return () => { socket.off("table_snapshot"); socket.off("joined"); socket.off("action_rejected"); socket.off("connect"); };
  }, [sessionId]);

  useEffect(() => {
    // Silently restore an existing seat on page refresh or socket reconnect. The server accepts a
    // reconnect token (or host token) without re-asking for name/passcode, so a returning player or
    // host skips the join form entirely. Brand-new visitors have neither token and still see it.
    if (!sessionId) return;
    const reconnectToken = localStorage.getItem(storageKey(sessionId));
    const savedHost = params.get("host") || localStorage.getItem(`straddleup:${sessionId}:host`) || "";
    if (!reconnectToken && !savedHost) return;
    const rejoin = () => socket.emit("join_room", { sessionId, reconnectToken, hostToken: savedHost });
    if (socket.connected) rejoin();
    socket.on("connect", rejoin);
    return () => { socket.off("connect", rejoin); };
  }, [sessionId]);

  const create = async (form: Record<string, string>) => {
    const response = await fetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ smallBlind: Number(form.smallBlind), bigBlind: Number(form.bigBlind), buyIn: Number(form.buyIn), maxSeats: Number(form.maxSeats), timeoutSeconds: Number(form.timeoutSeconds), autoNextHandSeconds: Number(form.autoNextHandSeconds), passcode: form.passcode }) });
    const data = await response.json();
    if (!response.ok) { setMessage(data.detail || "Could not create table."); return; }
    setSessionId(data.sessionId); setHostToken(data.hostToken);
    localStorage.setItem(`straddleup:${data.sessionId}:host`, data.hostToken);
    window.history.replaceState({}, "", `/?session=${data.sessionId}`);
  };

  const join = (name: string, passcode: string) => { setDisplayName(name); socket.emit("join_room", { sessionId, passcode, hostToken, reconnectToken: localStorage.getItem(storageKey(sessionId)), name }); };
  if (findingTable) return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP</p><h2>Finding the private table…</h2></section></main>;
  if (!sessionId) return <HostSetup onCreate={create} message={message} />;
  // A stored token means we're silently restoring a seat — show reconnecting, not the join form,
  // so a refresh mid-game doesn't flash the name/passcode screen while the rejoin is in flight.
  const hasStoredIdentity = sessionId && (localStorage.getItem(storageKey(sessionId)) || hostToken);
  if (!state || !playerId) {
    if (hasStoredIdentity && !message) return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP</p><h2>Reconnecting to your seat…</h2></section></main>;
    return <JoinRoom onJoin={join} sessionId={sessionId} message={message} host={Boolean(hostToken)} />;
  }
  const me = state.players.find(p => p.id === state.viewerId);
  if (!me || me.status !== "seated") return <SeatPicker state={state} initialName={displayName} onChoose={(name, seat) => socket.emit("choose_seat", { name, seat })} message={message} />;
  const history = async () => {
    const response = await fetch(`/api/sessions/${sessionId}/history/public`, { headers: { "X-Player-Token": localStorage.getItem(storageKey(sessionId)) || "" } });
    if (!response.ok) throw new Error("Could not load game history.");
    return (await response.json()).events;
  };
  const close = async () => {
    const response = await fetch(`/api/sessions/${sessionId}/close`, { method: "POST", headers: { "X-Host-Token": hostToken } });
    if (!response.ok) throw new Error("Could not end this table.");
    localStorage.removeItem(`straddleup:${sessionId}:host`);
    window.location.assign("/");
  };
  return <PokerTable state={state} me={me} message={message} clockOffset={clockOffset} onAction={(action, amount?) => socket.emit("player_action", { action, amount })} onStart={() => socket.emit("start_hand")} onReady={() => socket.emit("set_ready", { ready: !me.ready })} onRebuy={() => socket.emit("request_rebuy")} onCashout={() => socket.emit("request_cashout")} onApprove={(id) => socket.emit("host_approve_rebuy", { playerId: id })} onHistory={history} onClose={close} />;
}

function HostSetup({ onCreate, message }: { onCreate: (form: Record<string, string>) => void; message: string }) {
  const [form, setForm] = useState({ smallBlind: "5", bigBlind: "10", buyIn: "1000", maxSeats: "9", timeoutSeconds: "60", autoNextHandSeconds: "8", passcode: "" });
  return <main className="landing"><section className="panel hero"><p className="eyebrow">PRIVATE HOME GAME</p><h1>Straddle<span>Up</span></h1><p>Open a table, share one link, play from anywhere.</p><form onSubmit={e => { e.preventDefault(); onCreate(form); }} className="setup-grid">
    {[ ["Small blind", "smallBlind"], ["Big blind", "bigBlind"], ["Starting buy-in", "buyIn"], ["Seats (2–9)", "maxSeats"], ["Action timer (10–300 sec)", "timeoutSeconds"], ["Next hand delay (sec)", "autoNextHandSeconds"] ].map(([label, key]) => <label key={key}>{label}<input required type="number" min="1" value={form[key as keyof typeof form]} onChange={e => setForm({ ...form, [key]: e.target.value })} /></label>)}
    <label className="wide">Room passcode<input required minLength={4} type="password" placeholder="Share this privately" value={form.passcode} onChange={e => setForm({ ...form, passcode: e.target.value })} /></label><button className="primary wide">Create private table</button>
  </form>{message && <p className="error">{message}</p>}</section></main>;
}

function JoinRoom({ onJoin, sessionId, message, host }: { onJoin: (name: string, passcode: string) => void; sessionId: string; message: string; host: boolean }) {
  const [name, setName] = useState(""); const [passcode, setPasscode] = useState("");
  return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP TABLE</p><h2>{host ? "Host, take a seat" : "You’re invited"}</h2><p className="muted">Table code: {sessionId}</p><form onSubmit={e => { e.preventDefault(); onJoin(name, passcode); }}><label>Display name<input required maxLength={24} autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="Your poker name" /></label>{!host && <label>Room passcode<input required type="password" value={passcode} onChange={e => setPasscode(e.target.value)} /></label>}<button className="primary">Enter lobby</button></form>{message && <p className="error">{message}</p>}</section></main>;
}

function SeatPicker({ state, initialName, onChoose, message }: { state: TableState; initialName: string; onChoose: (name: string, seat: number) => void; message: string }) {
  const [name, setName] = useState(initialName);
  return <main className="landing"><section className="panel join"><p className="eyebrow">TABLE LOBBY</p><h2>Choose your seat</h2><label>Display name<input required value={name} onChange={e => setName(e.target.value)} placeholder="Your poker name" /></label><div className="seats">{Array.from({ length: state.config.maxSeats }, (_, seat) => { const occupant = state.players.find(p => p.seat === seat && p.status === "seated"); return <button key={seat} disabled={Boolean(occupant) || !name.trim()} onClick={() => onChoose(name, seat)} className="seat-choice">{occupant ? `${occupant.name} · occupied` : `Seat ${seat + 1}`}</button>; })}</div>{message && <p className="error">{message}</p>}</section></main>;
}

function PokerTable({ state, me, message, clockOffset, onAction, onStart, onReady, onRebuy, onCashout, onApprove, onHistory, onClose }: { state: TableState; me: Player; message: string; clockOffset: number; onAction: (action: string, amount?: number) => void; onStart: () => void; onReady: () => void; onRebuy: () => void; onCashout: () => void; onApprove: (id: string) => void; onHistory: () => Promise<{ kind: string; payload: any; at: string }[]>; onClose: () => Promise<void> }) {
  const [raiseTo, setRaiseTo] = useState(0);
  const [remaining, setRemaining] = useState(0);
  const [nextHandRemaining, setNextHandRemaining] = useState(0);
  const [history, setHistory] = useState<{ kind: string; payload: any; at: string }[] | null>(null);
  const [stackMode, setStackMode] = useState<"amount" | "blinds">(() => localStorage.getItem("straddleup:stack-mode") === "blinds" ? "blinds" : "amount");
  const [invite, setInvite] = useState<"idle" | "copied" | "manual">("idle");
  // A join link is just this app's URL scoped to the table; an opener with a table already
  // active is routed to the Join page to enter the passcode. Never embed the passcode here.
  const inviteLink = `${window.location.origin}/?session=${state.sessionId}`;
  const onInvite = async () => {
    try {
      await navigator.clipboard.writeText(inviteLink);
      setInvite("copied");
      window.setTimeout(() => setInvite("idle"), 2500);
    } catch {
      // Clipboard blocked (non-HTTPS origin, permissions) — reveal the link to copy by hand.
      setInvite("manual");
    }
  };
  useEffect(() => { const tick = () => { const now = Date.now() + clockOffset; setRemaining(Math.max(0, Math.ceil(((state.turnDeadline || 0) - now) / 1000))); setNextHandRemaining(Math.max(0, Math.ceil(((state.nextHandAt || 0) - now) / 1000))); }; tick(); const id = window.setInterval(tick, 250); return () => clearInterval(id); }, [state.turnDeadline, state.nextHandAt, clockOffset]);
  useEffect(() => { setRaiseTo(state.legalActions?.minRaiseTo || 0); }, [state.legalActions?.minRaiseTo]);
  const current = state.hand?.actorSeat === me.seat;
  const actions = state.legalActions || {};
  const player = (seat: number) => state.players.find(p => p.seat === seat && p.status === "seated");
  return <main className="table-shell"><header><div className="brand">Straddle<span>Up</span><small>Private Table</small></div><div className="header-meta">{state.config.smallBlind}/{state.config.bigBlind} · Buy-in {state.config.buyIn}<span className={socket.connected ? "online" : "offline"}>{socket.connected ? "● Live" : "● Reconnecting"}</span></div><button className="ghost invite" onClick={onInvite}>{invite === "copied" ? "Link copied ✓" : "Invite"}</button></header>
    {invite === "manual" && <div className="invite-manual">Share this link (they’ll need the passcode):<input readOnly aria-label="Invite link" value={inviteLink} onFocus={e => e.target.select()} /></div>}
    {message && <p className="toast error">{message}</p>}<section className="poker-table"><div className="felt"><div className="table-label">{state.status === "running" ? state.hand?.street : state.status === "complete" ? "HAND COMPLETE" : "WAITING FOR PLAYERS"}</div><div className="board">{(state.hand?.board || state.lastResult?.board || []).map((card, i) => <Card key={i} card={card} />)}</div><div className="pot">POT <strong>{state.hand?.pot || 0}</strong></div></div>
      {Array.from({ length: state.config.maxSeats }, (_, seat) => <Seat key={seat} seat={seat} player={player(seat)} active={state.hand?.actorSeat === seat} button={state.buttonSeat === seat} smallBlind={state.hand?.smallBlindSeat === seat} bigBlind={state.hand?.bigBlindSeat === seat} remaining={state.hand?.actorSeat === seat ? remaining : 0} stackMode={stackMode} bigBlindValue={state.config.bigBlind} />)}
    </section>
    <section className="controls"><div className="status-line">{state.status === "running" ? current ? "Your action" : "Waiting for the table" : state.status === "complete" ? `${Result({ state })}${nextHandRemaining ? ` · Next hand in ${nextHandRemaining}s` : ""}` : `${state.players.filter(p => p.status === "seated").length} seated`}</div>
      <div className="view-options"><span>Stacks</span><button className={stackMode === "amount" ? "ghost selected" : "ghost"} onClick={() => { setStackMode("amount"); localStorage.setItem("straddleup:stack-mode", "amount"); }}>Amount</button><button className={stackMode === "blinds" ? "ghost selected" : "ghost"} onClick={() => { setStackMode("blinds"); localStorage.setItem("straddleup:stack-mode", "blinds"); }}>BB</button></div>
      {state.status !== "running" && <div className="lobby-controls"><button onClick={onReady} className={me.ready ? "ghost selected" : "ghost"}>{me.ready ? "Ready ✓" : "I’m ready"}</button>{state.isHost && <button onClick={onStart} className="primary" disabled={state.players.filter(p => p.status === "seated" && p.stack > 0).length < 2}>Deal now</button>}<button onClick={onRebuy} className="ghost" disabled={Boolean(me.rebuyRequested)}> {me.rebuyRequested ? "Rebuy requested" : `Request rebuy +${state.config.buyIn}`}</button><button onClick={onCashout} className="danger">Cash out ({me.stack})</button><button className="ghost" onClick={() => onHistory().then(setHistory).catch(error => setHistory([{ kind: "error", payload: { message: error.message }, at: "" }]))}>History</button>{state.isHost && <button className="danger" onClick={() => onClose().catch(error => setHistory([{ kind: "error", payload: { message: error.message }, at: "" }]))}>End table</button>}</div>}
      {current && <div className="action-bar"><button onClick={() => onAction("fold")} className="danger">Fold</button>{actions.check && <button onClick={() => onAction("check")} className="ghost">Check</button>}{actions.call && <button onClick={() => onAction("call")} className="ghost">Call {actions.toCall}</button>}{actions.allIn && <button onClick={() => onAction("allin")} className="ghost">All-in</button>}{actions.maxRaiseTo > actions.toCall && <div className="raise"><div className="quick-raises">{[1, 2, 4].map(multiplier => { const target = Math.min(actions.maxRaiseTo, Math.max(actions.minRaiseTo, (state.hand?.currentBet || 0) + multiplier * state.config.bigBlind)); return <button key={multiplier} onClick={() => onAction(actions.toCall ? "raise" : "bet", target)} className="ghost">{multiplier}× BB</button>; })}</div><label>Custom<input aria-label="Custom raise to" type="number" min={actions.minRaiseTo} max={actions.maxRaiseTo} value={Math.min(Math.max(raiseTo, actions.minRaiseTo), actions.maxRaiseTo)} onChange={e => setRaiseTo(Number(e.target.value))} /></label><button onClick={() => onAction(actions.toCall ? "raise" : "bet", raiseTo)} className="primary">{actions.toCall ? "Raise to" : "Bet"} {raiseTo}</button></div>}</div>}
      {state.isHost && state.players.filter(p => p.rebuyRequested).length > 0 && <div className="approvals">{state.players.filter(p => p.rebuyRequested).map(p => <button key={p.id} onClick={() => onApprove(p.id)} className="primary">Approve {p.name}’s rebuy</button>)}</div>}
      {history && <section className="history"><button className="ghost" onClick={() => setHistory(null)}>Close history</button><h3>Hand history</h3>{history.length === 0 ? <p>No completed hands yet.</p> : history.slice().reverse().map((event, index) => <HandHistory key={index} event={event} />)}</section>}
    </section></main>;
}

function Result({ state }: { state: TableState }) { const result = state.lastResult; if (!result) return "Ready for the next hand"; const names = result.pots.flatMap(p => p.winners.map(id => state.players.find(player => player.id === id)?.name || "Player")); return `${result.type === "uncontested" ? "Pot awarded to" : "Showdown ·"} ${[...new Set(names)].join(", ")}`; }
function HandHistory({ event }: { event: { kind: string; payload: any; at: string } }) { const payload = event.payload || {}; const names = new Map((payload.players || []).map((player: any) => [player.id, player.name])); const pots = payload.result?.pots || []; return <article className="history-hand"><div className="history-title"><strong>Hand #{payload.handNumber}</strong><span>{event.at ? new Date(event.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}</span></div><div className="history-board">{(payload.board || []).map((card: string, index: number) => <Card key={index} card={card} />)}</div><div className="history-result">{pots.map((pot: any, index: number) => <span key={index}>{pot.amount} → {pot.winners.map((id: string) => names.get(id) || "Player").join(", ")}{pot.handClass ? ` (${pot.handClass})` : ""}</span>)}</div><details><summary>Actions ({(payload.actions || []).length})</summary>{(payload.actions || []).map((action: any) => <div className="history-action" key={action.sequence}>{names.get(action.playerId) || "Player"} · {action.street} · {action.type}{action.amount ? ` ${action.amount}` : ""}</div>)}</details></article>; }
function Card({ card }: { card: string }) { const red = card.endsWith("h") || card.endsWith("d"); const suit: Record<string, string> = { s: "♠", h: "♥", d: "♦", c: "♣" }; return <span className={red ? "card red" : "card"}>{card[0]}<small>{suit[card[1]]}</small></span>; }
function Seat({ seat, player, active, button, smallBlind, bigBlind, remaining, stackMode, bigBlindValue }: { seat: number; player?: Player; active: boolean; button: boolean; smallBlind: boolean; bigBlind: boolean; remaining: number; stackMode: "amount" | "blinds"; bigBlindValue: number }) { const position = `position-${seat}`; const displayedStack = player && (stackMode === "blinds" ? `${(player.stack / bigBlindValue).toFixed(1)} BB` : player.stack); return <div className={`seat ${position} ${active ? "active" : ""} ${!player ? "empty" : ""}`}>{player ? <><div className="seat-name">{player.name}{!player.connected && <i> disconnected</i>}</div><div className="stack">{displayedStack}</div><div className="markers">{button && <b>D</b>}{smallBlind && <b>SB</b>}{bigBlind && <b>BB</b>}{active && <b className="timer">{remaining}s</b>}</div>{player.holeCards?.length > 0 && <div className="hole-cards">{player.holeCards.map((c, i) => <Card card={c} key={i} />)}</div>}{player.folded && <span className="folded">FOLD</span>}{player.allIn && <span className="allin">ALL IN</span>}</> : <span>Open seat</span>}</div>; }

const container = document.getElementById("root")!;
const root = container.__straddleupRoot ??= createRoot(container);
root.render(<StrictMode><App /></StrictMode>);
