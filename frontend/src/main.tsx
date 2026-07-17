import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { io, Socket } from "socket.io-client";
import "./styles.css";
import { useEffect, useRef, useState } from "react";

declare global {
  interface HTMLElement { __straddleupRoot?: ReturnType<typeof createRoot>; }
}

type Player = { id: string; name: string; seat: number; stack: number; status: string; connected: boolean; ready: boolean; inHand: boolean; folded: boolean; allIn: boolean; holeCards: string[]; rebuyRequested?: boolean; leaveAfterHand?: boolean; cashoutAmount?: number; sittingOut?: boolean; sitOutNextHand?: boolean; straddleNextHand?: boolean; timeBankRemaining?: number };
type TableState = { sessionId: string; version: number; status: string; config: { smallBlind: number; bigBlind: number; buyIn: number; maxSeats: number; timeoutSeconds: number; autoNextHandSeconds: number; sessionMinutes: number; timeBankSeconds: number }; players: Player[]; buttonSeat: number | null; handNumber: number; sessionStartedAt: number | null; sessionEndsAt: number | null; timeExtensionProposal?: { minutes: number; votes: Record<string, boolean>; voters: string[] } | null; settlement?: { playerId: string; name: string; boughtIn: number; chipsOut: number; net: number; status: string }[]; chat?: { id: string; playerId: string; name: string; text: string; kind?: string; at: number }[]; turnDeadline: number | null; nextHandAt: number | null; hand: null | { id: string; street: string; board: string[]; smallBlindSeat: number; bigBlindSeat: number; straddleSeat?: number | null; actorSeat: number | null; pot: number; currentBet: number; actions?: { type: string }[] }; lastResult: null | { type: string; board: string[]; pots: { amount: number; winners: string[]; handClass?: string; winnerHands?: { playerId: string; description: string }[] }[] }; legalActions: Record<string, any>; viewerId: string; isHost: boolean; serverTime: number };

const socket: Socket = io({ autoConnect: true, transports: ["websocket", "polling"] });

function storageKey(sessionId: string) { return `straddleup:${sessionId}:token`; }

function playTone(frequency: number) {
  try {
    const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const context = new AudioContextClass();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0.035, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.18);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start(); oscillator.stop(context.currentTime + 0.18);
  } catch { /* Audio is an optional enhancement and may be blocked by the browser. */ }
}

function App() {
  const params = new URLSearchParams(window.location.search);
  const [sessionId, setSessionId] = useState(params.get("session") || "");
  const [hostToken, setHostToken] = useState("");
  const [invitePasscode, setInvitePasscode] = useState(params.get("session") ? localStorage.getItem(`straddleup:${params.get("session")}:invite-passcode`) || "" : "");
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
      setHostToken(params.get("host") || localStorage.getItem(`straddleup:${sessionId}:host`) || localStorage.getItem("straddleup:host-admin") || "");
    }
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
    const rejoin = () => {
      setMessage("");
      if (!sessionId) return;
      const reconnectToken = localStorage.getItem(storageKey(sessionId));
      if (reconnectToken || hostToken) socket.emit("join_room", { sessionId, hostToken, reconnectToken });
    };
    socket.on("connect", rejoin);
    if (socket.connected) rejoin();
    return () => { socket.off("table_snapshot"); socket.off("joined"); socket.off("action_rejected"); socket.off("connect"); };
  }, [sessionId, hostToken]);

  const join = (name: string, passcode: string) => { setDisplayName(name); socket.emit("join_room", { sessionId, passcode, hostToken, reconnectToken: localStorage.getItem(storageKey(sessionId)), name }); };
  if (!sessionId) return <HostPortal />;
  // A stored token means the effect above is silently restoring our seat — show reconnecting, not
  // the join form, so a refresh mid-game doesn't flash the name/passcode screen while it's in flight.
  const hasStoredIdentity = Boolean(sessionId && (localStorage.getItem(storageKey(sessionId)) || hostToken));
  if (!state || !playerId) {
    if (hasStoredIdentity && !message) return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP</p><h2>Reconnecting to your seat…</h2></section></main>;
    return <JoinRoom onJoin={join} sessionId={sessionId} message={message} host={Boolean(hostToken)} invitePasscode={invitePasscode} />;
  }
  const me = state.players.find(p => p.id === state.viewerId);
  if (!me) return <SeatPicker state={state} initialName={displayName} onChoose={(name, seat) => socket.emit("choose_seat", { name, seat })} message={message} />;
  if (me.status === "cashout") return <CashedOut player={me} settlement={state.settlement || []} />;
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
  return <PokerTable state={state} me={me} message={message} clockOffset={clockOffset} onAction={(action, amount?) => socket.emit("player_action", { action, amount })} onStart={() => socket.emit("start_hand")} onReady={() => socket.emit("set_ready", { ready: !me.ready })} onRebuy={() => socket.emit("request_rebuy")} onCashout={() => socket.emit("request_cashout")} onApprove={(id) => socket.emit("host_approve_rebuy", { playerId: id })} onRelease={(id) => socket.emit("host_release_disconnected_player", { playerId: id })} onSitOut={(sittingOut) => socket.emit("set_sit_out", { sittingOut })} onStraddle={(enabled) => socket.emit("set_straddle", { enabled })} onTimeBank={() => socket.emit("use_time_bank")} onProposeExtension={(minutes) => socket.emit("propose_time_extension", { minutes })} onVoteExtension={(approve) => socket.emit("vote_time_extension", { approve })} onChat={(text) => socket.emit("send_chat", { text })} onReaction={(reaction) => socket.emit("send_reaction", { reaction })} onHistory={history} onClose={close} />;
}

type HostTable = { sessionId: string; status: string; createdAt: string; updatedAt: string; config: { smallBlind: number; bigBlind: number; buyIn: number }; handNumber: number; players: { id: string; name: string; status: string; connected: boolean }[] };
type HostSessionSummary = { sessionId: string; status: string; createdAt: string; updatedAt: string; handNumber: number; completedHands: number; config: { smallBlind: number; bigBlind: number; buyIn: number }; players: { id: string; name: string; status: string; boughtIn: number; stack: number }[]; settlement: { playerId: string; name: string; boughtIn: number; chipsOut: number; net: number; status: string }[]; events: { kind: string; payload: any; at: string }[] };

function publicBaseUrl() {
  const stored = localStorage.getItem("straddleup:public-base-url") || window.location.origin;
  try { return new URL(stored).origin; } catch { return window.location.origin; }
}

function inviteUrl(sessionId: string) { return `${publicBaseUrl()}/?session=${encodeURIComponent(sessionId)}`; }

function HostPortal() {
  const [token, setToken] = useState(() => localStorage.getItem("straddleup:host-admin") || "");
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [accessCode, setAccessCode] = useState("");
  const [activeTables, setActiveTables] = useState<HostTable[]>([]);
  const [archivedTables, setArchivedTables] = useState<HostTable[]>([]);
  const [summary, setSummary] = useState<HostSessionSummary | null>(null);
  const [message, setMessage] = useState("");
  const [inviteCodes, setInviteCodes] = useState<Record<string, string>>({});
  const [shareBaseUrl, setShareBaseUrl] = useState(() => localStorage.getItem("straddleup:public-base-url") || window.location.origin);
  const [form, setForm] = useState({ smallBlind: "5", bigBlind: "10", buyIn: "1000", maxSeats: "9", timeoutSeconds: "60", autoNextHandSeconds: "8", sessionMinutes: "180", timeBankSeconds: "60", passcode: "" });
  const headers: Record<string, string> = token ? { "X-Host-Token": token } : {};
  const refresh = async () => {
    const response = await fetch("/api/host/tables", { headers });
    const data = await response.json();
    if (!response.ok) { setMessage(data.detail || "Host access expired. Sign in again."); setToken(""); localStorage.removeItem("straddleup:host-admin"); return; }
    setActiveTables(data.activeTables || []); setArchivedTables(data.archivedTables || []);
  };
  useEffect(() => { fetch("/api/host/status").then(response => response.json()).then(data => setConfigured(Boolean(data.configured))).catch(() => setConfigured(false)); }, []);
  useEffect(() => { if (token) refresh(); }, [token]);
  const authenticate = async () => {
    const response = await fetch("/api/host/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ accessCode }) });
    const data = await response.json();
    if (!response.ok) { setMessage(data.detail || "Could not verify host access."); return; }
    localStorage.setItem("straddleup:host-admin", data.hostToken); setToken(data.hostToken); setAccessCode(""); setMessage("");
  };
  const saveShareBaseUrl = () => {
    try { localStorage.setItem("straddleup:public-base-url", new URL(shareBaseUrl).origin); } catch { setMessage("Enter a full public address, beginning with https://."); return false; }
    return true;
  };
  const createTable = async () => {
    if (!saveShareBaseUrl()) return;
    const response = await fetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ smallBlind: Number(form.smallBlind), bigBlind: Number(form.bigBlind), buyIn: Number(form.buyIn), maxSeats: Number(form.maxSeats), timeoutSeconds: Number(form.timeoutSeconds), autoNextHandSeconds: Number(form.autoNextHandSeconds), sessionMinutes: Number(form.sessionMinutes), timeBankSeconds: Number(form.timeBankSeconds), passcode: form.passcode }) });
    const data = await response.json();
    if (!response.ok) { setMessage(data.detail || "Could not create table."); return; }
    localStorage.setItem(`straddleup:${data.sessionId}:invite-passcode`, form.passcode);
    window.location.assign(`/?session=${data.sessionId}`);
  };
  const rotateInvite = async (sessionId: string) => {
    const response = await fetch(`/api/host/sessions/${sessionId}/invite`, { method: "POST", headers });
    const data = await response.json();
    if (!response.ok) { setMessage(data.detail || "Could not generate an invite code."); return; }
    localStorage.setItem(`straddleup:${sessionId}:invite-passcode`, data.invitePasscode);
    setInviteCodes(codes => ({ ...codes, [sessionId]: data.invitePasscode })); setMessage("New invite code created. Copy the invite before closing this page.");
  };
  const copyInvite = async (sessionId: string) => {
    const passcode = inviteCodes[sessionId] || localStorage.getItem(`straddleup:${sessionId}:invite-passcode`) || "";
    if (!passcode) { setMessage("Generate a fresh invite code first. For security, existing codes cannot be recovered."); return; }
    await navigator.clipboard.writeText(`Join my StraddleUp table: ${inviteUrl(sessionId)}\nPasscode: ${passcode}`);
    setMessage("Invite copied.");
  };
  const closeTable = async (sessionId: string) => { const response = await fetch(`/api/sessions/${sessionId}/close`, { method: "POST", headers }); if (!response.ok) { setMessage("Could not end that table."); return; } await refresh(); };
  const viewHistory = async (sessionId: string) => { const response = await fetch(`/api/host/sessions/${sessionId}/summary`, { headers }); const data = await response.json(); if (!response.ok) { setMessage(data.detail || "Could not load session history."); return; } setSummary(data); };
  const activeEntry = (table: HostTable) => <article className="table-entry" key={table.sessionId}><div><strong>{table.sessionId}</strong><span>{table.status} · Hand {table.handNumber} · {table.players.filter(player => player.status === "seated").length} seated</span></div><div className="directory-actions"><button type="button" className="ghost" onClick={() => window.location.assign(`/?session=${table.sessionId}`)}>Open table</button><button type="button" className="ghost" onClick={() => rotateInvite(table.sessionId)}>New invite code</button><button type="button" className="ghost" onClick={() => copyInvite(table.sessionId)}>Copy invite</button><button type="button" className="danger" onClick={() => closeTable(table.sessionId)}>End</button></div>{inviteCodes[table.sessionId] && <p className="invite-code">New invite code: <strong>{inviteCodes[table.sessionId]}</strong></p>}</article>;
  if (configured === null) return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP HOST</p><h2>Loading host console…</h2></section></main>;
  if (!configured) return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP HOST</p><h2>Host console unavailable</h2><p className="muted">Add the host credential to the ignored local environment file, then restart Docker.</p></section></main>;
  if (!token) return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP HOST</p><h2>Host sign in</h2><p className="muted">Only the locally configured host code can open this console. Player invite links never show host controls.</p><form onSubmit={event => { event.preventDefault(); authenticate(); }}><label>Host access code<input required minLength={8} type="password" autoFocus value={accessCode} onChange={event => setAccessCode(event.target.value)} /></label><button className="primary">Open host console</button></form>{message && <p className="error">{message}</p>}</section></main>;
  const hasSelectedHistory = () => Boolean(summary);
  if (hasSelectedHistory()) return <SessionHistoryView summary={summary!} onClose={() => setSummary(null)} />;
  return <main className="landing host-console"><section className="panel hero"><p className="eyebrow">STRADDLEUP HOST CONSOLE</p><h1>Game <span>Control</span></h1><p>Active games and completed sessions are separate. Tables survive Docker and tunnel restarts.</p><form onSubmit={event => { event.preventDefault(); createTable(); }} className="setup-grid">{[["Small blind", "smallBlind"], ["Big blind", "bigBlind"], ["Starting buy-in", "buyIn"], ["Seats (2–9)", "maxSeats"], ["Action timer", "timeoutSeconds"], ["Next hand delay", "autoNextHandSeconds"], ["Session minutes", "sessionMinutes"], ["Time bank", "timeBankSeconds"]].map(([label, key]) => <label key={key}>{label}<input required type="number" min="0" value={form[key as keyof typeof form]} onChange={event => setForm({ ...form, [key]: event.target.value })} /></label>)}<label className="wide">Public game address<input required type="url" value={shareBaseUrl} onChange={event => setShareBaseUrl(event.target.value)} /><small>Paste the bare https Cloudflare address here once; every invite uses it with a table code.</small></label><label className="wide">New table invite passcode<input required minLength={4} type="password" value={form.passcode} onChange={event => setForm({ ...form, passcode: event.target.value })} /></label><button className="primary wide">Create new table</button></form>{message && <p className="error">{message}</p>}<section className="table-directory"><div className="directory-head"><h2>Live tables</h2><button type="button" className="ghost" onClick={refresh}>Refresh</button></div>{activeTables.length === 0 ? <p className="muted">No live tables. Create one above.</p> : activeTables.map(activeEntry)}<div className="directory-head archived-head"><h2>Completed session history</h2></div>{archivedTables.length === 0 ? <p className="muted">Completed games will appear here with their final session stats.</p> : archivedTables.map(table => <article className="table-entry" key={table.sessionId}><div><strong>{table.sessionId}</strong><span>Completed · {table.handNumber} hands dealt · {table.players.length} players recorded</span></div><div className="directory-actions"><button type="button" className="ghost" onClick={() => viewHistory(table.sessionId)}>View session history</button></div></article>)}</section>{summary && <section className="history host-history"><div className="directory-head"><div><p className="eyebrow">SESSION HISTORY</p><h3>{summary.sessionId}</h3></div><button type="button" className="ghost" onClick={() => setSummary(null)}>Close</button></div><p className="muted">{summary.status === "closed" ? "Completed" : summary.status} · {summary.completedHands} completed hands · blinds {summary.config.smallBlind}/{summary.config.bigBlind}</p>{summary.settlement.length ? <div className="settlement">{summary.settlement.map(entry => <div className="settlement-row" key={entry.playerId}><span>{entry.name}<small>Buy-in {entry.boughtIn} · chips out {entry.chipsOut}</small></span><strong className={entry.net >= 0 ? "positive" : "negative"}>{entry.net >= 0 ? "+" : ""}{entry.net}</strong></div>)}</div> : <div className="history-results">{summary.players.map(player => <span key={player.id}>{player.name} · buy-in {player.boughtIn} · final stack {player.stack}</span>)}</div>}</section>}</section></main>;
}

function SessionHistoryView({ summary, onClose }: { summary: HostSessionSummary; onClose: () => void }) {
  return <main className="landing host-console"><section className="panel hero session-history-page"><p className="eyebrow">COMPLETED SESSION</p><div className="directory-head"><div><h2>{summary.sessionId}</h2><p className="muted">{summary.completedHands} hands · blinds {summary.config.smallBlind}/{summary.config.bigBlind} · social-chip settlement</p></div><button type="button" className="ghost" onClick={onClose}>Back to console</button></div><Settlement settlement={summary.settlement} /><section className="history host-history"><h3>Hand-by-hand audit</h3><p className="muted">Each hand shows the board, winner, exact winning hand, all contributions, payouts, refunds, and action log.</p>{summary.events.length === 0 ? <p>No completed hands were recorded for this older table.</p> : summary.events.slice().reverse().map((event, index) => <HandHistory key={index} event={event} />)}</section></section></main>;
}

function JoinRoom({ onJoin, sessionId, message, host, invitePasscode }: { onJoin: (name: string, passcode: string) => void; sessionId: string; message: string; host: boolean; invitePasscode: string }) {
  const [name, setName] = useState(""); const [passcode, setPasscode] = useState("");
  return <main className="landing"><section className="panel join"><p className="eyebrow">STRADDLEUP TABLE</p><h2>{host ? "Host, take a seat" : "You’re invited"}</h2><p className="muted">Table code: {sessionId}</p>{host && <ShareInvite sessionId={sessionId} passcode={invitePasscode} />}
    <form onSubmit={e => { e.preventDefault(); onJoin(name, passcode); }}><label>Display name<input required maxLength={24} autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="Your poker name" /></label>{!host && <label>Room passcode<input required type="password" value={passcode} onChange={e => setPasscode(e.target.value)} /></label>}<button className="primary">Enter lobby</button></form>{message && <p className="error">{message}</p>}</section></main>;
}

function ShareInvite({ sessionId, passcode }: { sessionId: string; passcode: string }) {
  const [copied, setCopied] = useState(false);
  const link = inviteUrl(sessionId);
  const message = `Join my private StraddleUp poker table.\n\nLink: ${link}\nTable code: ${sessionId}${passcode ? `\nPasscode: ${passcode}` : ""}`;
  const copy = async () => { try { await navigator.clipboard.writeText(message); setCopied(true); window.setTimeout(() => setCopied(false), 1800); } catch { setCopied(false); } };
  return <section className="share-invite"><div className="share-title"><strong>Invite your friends</strong><span>Share this before dealing</span></div><div className="share-link">{link}</div>{passcode ? <div className="share-code">Passcode <strong>{passcode}</strong></div> : <p className="error">Invite code unavailable in this browser. Generate a fresh code from the Host Console.</p>}<div className="share-actions"><button type="button" className="primary" onClick={copy} disabled={!passcode}>{copied ? "Copied ✓" : "Copy invite"}</button><a className="share-button whatsapp" href={passcode ? `https://wa.me/?text=${encodeURIComponent(message)}` : undefined} target="_blank" rel="noreferrer" aria-disabled={!passcode}>WhatsApp</a><a className="share-button gmail" href={passcode ? `https://mail.google.com/mail/?view=cm&fs=1&su=${encodeURIComponent("Join my StraddleUp poker table")}&body=${encodeURIComponent(message)}` : undefined} target="_blank" rel="noreferrer" aria-disabled={!passcode}>Gmail</a></div></section>;
}

function SeatPicker({ state, initialName, onChoose, message }: { state: TableState; initialName: string; onChoose: (name: string, seat: number) => void; message: string }) {
  const [name, setName] = useState(initialName);
  return <main className="landing"><section className="panel join seat-picker"><p className="eyebrow">TABLE LOBBY</p><h2>Pick a seat at the table</h2><p className="muted">Enter your poker name, then tap an open seat on the table.</p><label>Display name<input required value={name} onChange={e => setName(e.target.value)} placeholder="Your poker name" /></label><div className="seat-map"><div className="mini-felt"><span>STRADDLEUP</span><strong>{state.config.smallBlind}/{state.config.bigBlind}</strong></div>{Array.from({ length: state.config.maxSeats }, (_, seat) => { const occupant = state.players.find(p => p.seat === seat && p.status === "seated"); return <button key={seat} disabled={Boolean(occupant) || !name.trim()} onClick={() => onChoose(name, seat)} className={`seat-choice position-${seat} ${occupant ? "occupied" : ""}`}>{occupant ? <><strong>{occupant.name}</strong><small>Occupied</small></> : <><strong>Open seat</strong><small>Tap to sit</small></>}</button>; })}</div>{message && <p className="error">{message}</p>}</section></main>;
}

function CashedOut({ player, settlement }: { player: Player; settlement: { playerId: string; name: string; boughtIn: number; chipsOut: number; net: number; status: string }[] }) {
  const result = settlement.find(entry => entry.playerId === player.id);
  return <main className="landing"><section className="panel join"><p className="eyebrow">CASHED OUT</p><h2>{player.name}, you’re out of the game.</h2><p className="muted">Your chips are recorded for the final settlement. You can keep this tab open to see the final balance.</p>{result && <div className="settlement-card"><span>Chips out <strong>{result.chipsOut}</strong></span><span>Net <strong className={result.net >= 0 ? "positive" : "negative"}>{result.net >= 0 ? "+" : ""}{result.net}</strong></span></div>}</section></main>;
}

function PokerTable({ state, me, message, clockOffset, onAction, onStart, onReady, onRebuy, onCashout, onApprove, onRelease, onSitOut, onStraddle, onTimeBank, onProposeExtension, onVoteExtension, onChat, onReaction, onHistory, onClose }: { state: TableState; me: Player; message: string; clockOffset: number; onAction: (action: string, amount?: number) => void; onStart: () => void; onReady: () => void; onRebuy: () => void; onCashout: () => void; onApprove: (id: string) => void; onRelease: (id: string) => void; onSitOut: (sittingOut: boolean) => void; onStraddle: (enabled: boolean) => void; onTimeBank: () => void; onProposeExtension: (minutes: number) => void; onVoteExtension: (approve: boolean) => void; onChat: (text: string) => void; onReaction: (reaction: string) => void; onHistory: () => Promise<{ kind: string; payload: any; at: string }[]>; onClose: () => Promise<void> }) {
  const [raiseTo, setRaiseTo] = useState(0);
  const [remaining, setRemaining] = useState(0);
  const [nextHandRemaining, setNextHandRemaining] = useState(0);
  const [history, setHistory] = useState<{ kind: string; payload: any; at: string }[] | null>(null);
  const [chatText, setChatText] = useState("");
  const [sessionRemaining, setSessionRemaining] = useState(0);
  const [stackMode, setStackMode] = useState<"amount" | "blinds">(() => localStorage.getItem("straddleup:stack-mode") === "blinds" ? "blinds" : "amount");
  const timerToneRef = useRef("");
  // Correct every countdown with the server-time offset so deadlines track the server despite
  // client clock drift (turnDeadline/nextHandAt/sessionEndsAt are all server epoch-ms).
  useEffect(() => { const tick = () => { const now = Date.now() + clockOffset; setRemaining(Math.max(0, Math.ceil(((state.turnDeadline || 0) - now) / 1000))); setNextHandRemaining(Math.max(0, Math.ceil(((state.nextHandAt || 0) - now) / 1000))); setSessionRemaining(Math.max(0, Math.ceil(((state.sessionEndsAt || 0) - now) / 1000))); }; tick(); const id = window.setInterval(tick, 250); return () => clearInterval(id); }, [state.turnDeadline, state.nextHandAt, state.sessionEndsAt, clockOffset]);
  useEffect(() => { setRaiseTo(state.legalActions?.minRaiseTo || 0); }, [state.legalActions?.minRaiseTo]);
  useEffect(() => { if (state.status === "running" && state.hand?.actorSeat === me.seat) playTone(660); }, [state.status, state.hand?.actorSeat, state.hand?.actions?.length, me.seat]);
  useEffect(() => {
    const isMyTurn = state.status === "running" && state.hand?.actorSeat === me.seat;
    if (!isMyTurn) { timerToneRef.current = ""; return; }
    const warning = remaining <= 3 ? remaining : remaining <= 5 ? 5 : remaining <= 10 ? 10 : 0;
    const marker = `${state.hand?.id}:${warning}`;
    if (warning && timerToneRef.current !== marker) {
      timerToneRef.current = marker;
      playTone(warning <= 3 ? 980 : warning === 5 ? 840 : 720);
    }
  }, [state.status, state.hand?.id, state.hand?.actorSeat, me.seat, remaining]);
  const current = state.hand?.actorSeat === me.seat;
  const actions = state.legalActions || {};
  const player = (seat: number) => state.players.find(p => p.seat === seat && p.status === "seated");
  const sessionMinutes = Math.floor(sessionRemaining / 60);
  const sessionSeconds = sessionRemaining % 60;
  const sessionExpired = Boolean(state.sessionEndsAt && sessionRemaining === 0);
  const proposal = state.timeExtensionProposal;
  const hasVoted = proposal ? Object.prototype.hasOwnProperty.call(proposal.votes, me.id) : false;
  const timerPercent = state.turnDeadline ? Math.min(100, (remaining / state.config.timeoutSeconds) * 100) : 0;
  const invitePasscode = localStorage.getItem(`straddleup:${state.sessionId}:invite-passcode`) || "";
  const sendChat = () => { if (chatText.trim()) { onChat(chatText.trim()); setChatText(""); } };
  return <main className="table-shell"><header><div className="brand">Straddle<span>Up</span><small>Private Table</small></div><div className="header-meta"><span>{state.config.smallBlind}/{state.config.bigBlind} · Buy-in {state.config.buyIn}</span><span className="balance">Your stack {me.stack}</span><span className={socket.connected ? "online" : "offline"}>{socket.connected ? "● Live" : "● Reconnecting"}</span></div></header>
    <div className={`session-bar ${sessionExpired ? "urgent" : ""}`}><span>{state.status === "closed" ? "TABLE CLOSED" : "SESSION TIME"}</span><strong>{sessionExpired ? "Limit reached" : `${sessionMinutes}:${String(sessionSeconds).padStart(2, "0")}`}</strong>{sessionExpired && !proposal && state.status !== "closed" && <button className="ghost" onClick={() => onProposeExtension(30)}>Propose +30 min</button>}{proposal && <span className="vote-copy">Extension vote: +{proposal.minutes} min · {Object.keys(proposal.votes).length}/{proposal.voters.length} votes{!hasVoted && <><button className="primary" onClick={() => onVoteExtension(true)}>Approve</button><button className="danger" onClick={() => onVoteExtension(false)}>Decline</button></>}</span>}</div>
    {message && <p className="toast error">{message}</p>}<section className="poker-table"><div className="felt"><div className="table-label">{state.status === "running" ? state.hand?.street : state.status === "complete" ? "HAND COMPLETE" : state.status === "closed" ? "SETTLEMENT" : "WAITING FOR PLAYERS"}</div><div className="board">{(state.hand?.board || state.lastResult?.board || []).map((card, i) => <Card key={i} card={card} />)}</div><div className="pot"><span>POT</span><strong>{state.hand?.pot || state.lastResult?.pots.reduce((sum, pot) => sum + pot.amount, 0) || 0}</strong><small>Current bet {state.hand?.currentBet || 0}</small></div></div>
      {Array.from({ length: state.config.maxSeats }, (_, seat) => <Seat key={seat} seat={seat} player={player(seat)} active={state.hand?.actorSeat === seat} button={state.buttonSeat === seat} smallBlind={state.hand?.smallBlindSeat === seat} bigBlind={state.hand?.bigBlindSeat === seat} straddle={state.hand?.straddleSeat === seat} remaining={state.hand?.actorSeat === seat ? remaining : 0} timerPercent={state.hand?.actorSeat === seat ? timerPercent : 0} stackMode={stackMode} bigBlindValue={state.config.bigBlind} />)}
    </section>
    <section className="controls"><div className="status-line">{state.status === "running" ? current ? <><strong>Your action</strong><span className="turn-hint">{remaining}s remaining</span></> : "Waiting for the table" : state.status === "complete" ? `${Result({ state })}${nextHandRemaining ? ` · Next hand in ${nextHandRemaining}s` : ""}` : state.status === "closed" ? "Final settlement" : `${state.players.filter(p => p.status === "seated").length} seated`}</div>
      {state.status === "running" && <div className={`turn-bar ${remaining <= 10 ? "urgent" : ""}`}><span style={{ width: `${timerPercent}%` }} /></div>}
      <div className="view-options"><span>Stacks</span><button className={stackMode === "amount" ? "ghost selected" : "ghost"} onClick={() => { setStackMode("amount"); localStorage.setItem("straddleup:stack-mode", "amount"); }}>Amount</button><button className={stackMode === "blinds" ? "ghost selected" : "ghost"} onClick={() => { setStackMode("blinds"); localStorage.setItem("straddleup:stack-mode", "blinds"); }}>BB</button></div>
      {state.isHost && state.status !== "closed" && <details className="host-invite" open={state.status === "lobby"}><summary>Invite players</summary><ShareInvite sessionId={state.sessionId} passcode={invitePasscode} /></details>}
      {state.status !== "running" && state.status !== "closed" && <div className="lobby-controls"><button onClick={onReady} className={me.ready ? "ghost selected" : "ghost"}>{me.ready ? "Ready ✓" : "I’m ready"}</button>{state.isHost && <button onClick={onStart} className="primary" disabled={state.players.filter(p => p.status === "seated" && p.stack > 0 && !p.sittingOut).length < 2}>Deal now</button>}<button onClick={onRebuy} className="ghost" disabled={Boolean(me.rebuyRequested)}> {me.rebuyRequested ? "Rebuy requested" : `Request rebuy +${state.config.buyIn}`}</button><button onClick={() => onSitOut(!me.sittingOut)} className={me.sittingOut ? "selected" : "ghost"}>{me.sittingOut ? "Sit back in" : "Sit out next hand"}</button><button onClick={() => onStraddle(!me.straddleNextHand)} className={me.straddleNextHand ? "selected" : "ghost"}>{me.straddleNextHand ? "Straddle ✓" : "Straddle next hand"}</button><button onClick={onCashout} className="danger">Cash out ({me.stack})</button></div>}
      {state.status === "running" && <div className="lobby-controls"><button onClick={() => onSitOut(!me.sitOutNextHand)} className={me.sitOutNextHand ? "selected" : "ghost"}>{me.sitOutNextHand ? "Sitting out next" : "Sit out next hand"}</button>{current && (me.timeBankRemaining || 0) > 0 && <button onClick={onTimeBank} className="ghost">Time bank +30s ({me.timeBankRemaining}s)</button>}<button onClick={onCashout} className="danger">{me.inHand ? "Leave after hand" : `Cash out (${me.stack})`}</button></div>}
      {state.status !== "running" && <div className="table-tools"><button className="ghost" onClick={() => onHistory().then(setHistory).catch(error => setHistory([{ kind: "error", payload: { message: error.message }, at: "" }]))}>History</button>{state.isHost && state.status !== "closed" && <button className="danger" onClick={() => onClose().catch(error => setHistory([{ kind: "error", payload: { message: error.message }, at: "" }]))}>End table</button>}</div>}
      {current && <div className="action-bar"><button onClick={() => onAction("fold")} className="danger">Fold</button>{actions.check && <button onClick={() => onAction("check")} className="ghost">Check</button>}{actions.call && <button onClick={() => onAction("call")} className="ghost">Call {actions.toCall}</button>}{actions.allIn && <button onClick={() => onAction("allin")} className="ghost">All-in</button>}{actions.canRaise && actions.maxRaiseTo > actions.toCall && <div className="raise"><div className="quick-raises">{[1, 2, 4].map(multiplier => { const target = Math.min(actions.maxRaiseTo, Math.max(actions.minRaiseTo, (state.hand?.currentBet || 0) + multiplier * state.config.bigBlind)); return <button key={multiplier} onClick={() => onAction(actions.toCall ? "raise" : "bet", target)} className="ghost">{multiplier}× BB</button>; })}</div><label>Custom<input aria-label="Custom raise to" type="number" min={actions.minRaiseTo} max={actions.maxRaiseTo} value={Math.min(Math.max(raiseTo, actions.minRaiseTo), actions.maxRaiseTo)} onChange={e => setRaiseTo(Number(e.target.value))} /></label><button onClick={() => onAction(actions.toCall ? "raise" : "bet", raiseTo)} className="primary">{actions.toCall ? "Raise to" : "Bet"} {raiseTo}</button></div>}</div>}
      {state.isHost && state.players.filter(p => p.rebuyRequested).length > 0 && <div className="approvals">{state.players.filter(p => p.rebuyRequested).map(p => <button key={p.id} onClick={() => onApprove(p.id)} className="primary">Approve {p.name}’s rebuy</button>)}</div>}
      {state.isHost && state.players.some(p => p.status === "seated" && !p.connected) && <div className="approvals">{state.players.filter(p => p.status === "seated" && !p.connected).map(p => <button key={p.id} onClick={() => onRelease(p.id)} className="ghost">Release {p.name}’s disconnected seat</button>)}</div>}
      {state.status === "closed" && <Settlement settlement={state.settlement || []} />}
      <section className="chat"><div className="chat-head"><h3>Table chat</h3><span>{state.status === "running" ? "Paused during a live hand" : "Keep strategy and live-hand talk off chat"}</span></div><div className="chat-log">{(state.chat || []).length === 0 ? <span className="muted">No messages yet.</span> : (state.chat || []).slice(-30).map(entry => <div className={`chat-message ${entry.kind === "reaction" ? "reaction" : ""}`} key={entry.id}><strong>{entry.name}</strong><span>{entry.text}</span></div>)}</div>{state.status !== "closed" && <div className="reactions">{["👏", "🔥", "😎", "🃏", "♠️", "💰", "GG"].map(reaction => <button key={reaction} className="ghost" onClick={() => onReaction(reaction)}>{reaction}</button>)}</div>}{state.status !== "running" && state.status !== "closed" && <form className="chat-form" onSubmit={event => { event.preventDefault(); sendChat(); }}><input value={chatText} maxLength={240} onChange={event => setChatText(event.target.value)} placeholder="Say hello to the table…" /><button className="primary">Send</button></form>}</section>
      {history && <section className="history"><button className="ghost" onClick={() => setHistory(null)}>Close history</button><h3>Hand history</h3>{history.length === 0 ? <p>No completed hands yet.</p> : history.slice().reverse().map((event, index) => <HandHistory key={index} event={event} />)}</section>}
    </section></main>;
}

function Result({ state }: { state: TableState }) { const result = state.lastResult; if (!result) return "Ready for the next hand"; const names = result.pots.flatMap(p => p.winners.map(id => state.players.find(player => player.id === id)?.name || "Player")); const hands = result.pots.flatMap(p => (p.winnerHands || []).map(hand => hand.description)); return `${result.type === "uncontested" ? "🏆 Pot awarded to" : "🏆 Showdown winner"} ${[...new Set(names)].join(", ")}${hands.length ? ` — ${[...new Set(hands)].join(" / ")}` : ""}`; }
function Settlement({ settlement }: { settlement: { playerId: string; name: string; boughtIn: number; chipsOut: number; net: number; status: string }[] }) { return <section className="settlement"><h3>Session settlement</h3><p className="muted">Social chips only — settle these balances outside the app.</p>{settlement.map(entry => <div className="settlement-row" key={entry.playerId}><span>{entry.name}<small>{entry.status === "cashout" ? " cashed out" : " still seated"}</small></span><span>out {entry.chipsOut} · <strong className={entry.net >= 0 ? "positive" : "negative"}>{entry.net >= 0 ? "+" : ""}{entry.net}</strong></span></div>)}</section>; }
function HandHistory({ event }: { event: { kind: string; payload: any; at: string } }) {
  const payload = event.payload || {}; const result = payload.result || {}; const names = new Map((payload.players || []).map((player: any) => [player.id, player.name])); const pots = result.pots || []; const refunds = result.refunds || [];
  const describeAction = (action: any) => { const committed = action.chipsCommitted ?? action.amount ?? 0; if (action.type === "raise") return `raised to ${action.streetTotal} (added ${committed}; pot ${action.potAfter})`; if (action.type === "bet") return `bet ${action.streetTotal} (pot ${action.potAfter})`; if (action.type === "call") return `called ${committed} (pot ${action.potAfter})`; if (action.type === "allin") return `all-in: added ${committed}, total ${action.handTotal} (pot ${action.potAfter})`; return action.type; };
  return <article className="history-hand"><div className="history-title"><strong>Hand #{payload.handNumber}</strong><span>{event.at ? new Date(event.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}</span></div><div className="history-board">{(payload.board || []).map((card: string, index: number) => <Card key={index} card={card} />)}</div><div className="history-total">Contested pot {result.potTotal ?? pots.reduce((sum: number, pot: any) => sum + pot.amount, 0)} · Total committed {result.totalCommitted ?? "—"}</div><div className="history-result">{pots.map((pot: any, index: number) => <span key={index}>{pot.amount} → {(pot.winnerHands || []).length ? pot.winnerHands.map((winner: any) => `${names.get(winner.playerId) || "Player"} (${winner.description})`).join(", ") : `${pot.winners.map((id: string) => names.get(id) || "Player").join(", ")}${pot.handClass ? ` (${pot.handClass})` : ""}`}</span>)}</div>{refunds.length > 0 && <div className="history-results">{refunds.map((refund: any, index: number) => <span key={index}>Unmatched bet returned: {refund.amount} to {names.get(refund.playerId) || "Player"}</span>)}</div>}{(result.playerResults || []).length > 0 && <div className="history-results">{result.playerResults.map((entry: any) => <span key={entry.playerId}>{names.get(entry.playerId) || "Player"}: started {entry.startingStack} · committed {entry.committed} · payout {entry.payout} · ended {entry.endingStack} · net {entry.net >= 0 ? "+" : ""}{entry.net}</span>)}</div>}<details><summary>Actions ({(payload.actions || []).length})</summary>{(payload.actions || []).map((action: any) => <div className="history-action" key={action.sequence}>{names.get(action.playerId) || "Player"} · {action.street} · {describeAction(action)}</div>)}</details></article>;
}
function Card({ card }: { card: string }) { const red = card.endsWith("h") || card.endsWith("d"); const suit: Record<string, string> = { s: "♠", h: "♥", d: "♦", c: "♣" }; return <span className={red ? "card red" : "card"}>{card[0]}<small>{suit[card[1]]}</small></span>; }
function Seat({ seat, player, active, button, smallBlind, bigBlind, straddle, remaining, timerPercent, stackMode, bigBlindValue }: { seat: number; player?: Player; active: boolean; button: boolean; smallBlind: boolean; bigBlind: boolean; straddle: boolean; remaining: number; timerPercent: number; stackMode: "amount" | "blinds"; bigBlindValue: number }) { const position = `position-${seat}`; const displayedStack = player && (stackMode === "blinds" ? `${(player.stack / bigBlindValue).toFixed(1)} BB` : player.stack); return <div className={`seat ${position} ${active ? "active" : ""} ${!player ? "empty" : ""}`}>{player ? <><div className="seat-name">{player.name}{!player.connected && <i> disconnected</i>}</div><div className="stack">{displayedStack}</div>{active && <div className={`seat-timer ${remaining <= 10 ? "urgent" : ""}`}><span style={{ width: `${timerPercent}%` }} /></div>}<div className="markers">{button && <b>D</b>}{smallBlind && <b>SB</b>}{bigBlind && <b>BB</b>}{straddle && <b>STR</b>}{active && <b className="timer">{remaining}s</b>}</div>{player.holeCards?.length > 0 && <div className="hole-cards">{player.holeCards.map((c, i) => <Card card={c} key={i} />)}</div>}{player.sittingOut && <span className="folded">SIT OUT</span>}{player.folded && <span className="folded">FOLD</span>}{player.allIn && <span className="allin">ALL IN</span>}</> : <span>Open seat</span>}</div>; }

const container = document.getElementById("root")!;
const root = container.__straddleupRoot ??= createRoot(container);
root.render(<StrictMode><App /></StrictMode>);
