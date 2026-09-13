/**
 * The lobby relay: one Durable Object per party room, passing rows of ids
 * between the tibbers installs in that party and nothing else. LOBBY.md,
 * "The relay", is the design; this is what the code holds to.
 *
 * - A room id is 32 hex and a member id 16 hex. Both are hashes each client
 *   derives from its own League client, so the relay never sees a party id,
 *   an account id or a name, and has no way to work one out.
 * - A row is champion, skin, chroma. Skin and chroma ids are
 *   championId * 1000 + n, and a row whose ids do not name its own champion
 *   is refused, so a room cannot be made to carry anything else.
 * - No storage and no alarm. A room is its open sockets and ends with the
 *   last of them. Each socket keeps its member id and row as its attachment,
 *   which survives hibernation, so a room woken by a pick has lost nothing.
 *
 * The per-IP rate limit is a zone rule on /v1/room rather than code here,
 * which is why workers.dev is switched off in wrangler.toml.
 */
import { DurableObject } from "cloudflare:workers";

const ROOM_ID = /^[0-9a-f]{32}$/;
const MEMBER_ID = /^[0-9a-f]{16}$/;
const MAX_FRAME_BYTES = 128;
const MAX_MEMBERS = 16;
// Sockets that have not joined count against this, or a room could be
// filled with connections that never say who they are.
const MAX_SOCKETS = 24;

// Refusals, in the application range so a client can tell them from a
// dropped connection. None of them is worth reconnecting straight into.
const BAD_FRAME = 4400;
const REPLACED = 4409;
const FULL = 4429;

type Row = { c: number | null; s: number | null; k: number | null };
type Member = { m: string; row: Row };
type Frame = { t: "join"; m: string } | { t: "pick"; row: Row };

const EMPTY: Row = { c: null, s: null, k: null };

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/v1/room") {
      return new Response("not found", { status: 404 });
    }
    const room = url.searchParams.get("room") ?? "";
    if (!ROOM_ID.test(room)) {
      return new Response("room must be 32 lowercase hex", { status: 400 });
    }
    if (request.method !== "GET"
        || request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("expected a websocket upgrade", { status: 426 });
    }
    return env.ROOM.getByName(room).fetch(request);
  },
} satisfies ExportedHandler<Env>;

export class Room extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // Keepalives are answered by the runtime and never wake the room.
    ctx.setWebSocketAutoResponse(
      new WebSocketRequestResponsePair("ping", "pong"));
  }

  async fetch(): Promise<Response> {
    if (this.ctx.getWebSockets().length >= MAX_SOCKETS) {
      return new Response("room full", { status: 429 });
    }
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    const frame = parse(message);
    if (!frame) {
      return this.refuse(ws, BAD_FRAME, "bad frame");
    }
    const me = member(ws);
    if (frame.t === "pick") {
      if (!me) {
        return this.refuse(ws, BAD_FRAME, "join first");
      }
      ws.serializeAttachment({ m: me.m, row: frame.row } satisfies Member);
      return this.broadcast();
    }
    if (me) {
      return this.refuse(ws, BAD_FRAME, "already joined");
    }
    const members = this.members();
    const previous = members.find(([, other]) => other.m === frame.m);
    if (previous) {
      // The same member on a new socket is a reconnect that beat the old
      // socket's close. The new socket is that member from here on.
      previous[0].serializeAttachment(null);
      previous[0].close(REPLACED, "replaced by a newer connection");
    } else if (members.length >= MAX_MEMBERS) {
      return this.refuse(ws, FULL, "room full");
    }
    ws.serializeAttachment({ m: frame.m, row: EMPTY } satisfies Member);
    this.broadcast();
  }

  async webSocketClose(ws: WebSocket) {
    this.leave(ws);
  }

  async webSocketError(ws: WebSocket) {
    this.leave(ws);
  }

  private refuse(ws: WebSocket, code: number, reason: string) {
    // Detached before closing, so the room stops counting the socket now
    // rather than whenever the client answers the close.
    this.leave(ws);
    ws.serializeAttachment(null);
    ws.close(code, reason);
  }

  private leave(ws: WebSocket) {
    const me = member(ws);
    if (me) {
      this.broadcast(me.m);
    }
  }

  private members(except?: string): [WebSocket, Member][] {
    const out: [WebSocket, Member][] = [];
    for (const ws of this.ctx.getWebSockets()) {
      const m = member(ws);
      if (m && m.m !== except) {
        out.push([ws, m]);
      }
    }
    return out;
  }

  /** Everyone gets the whole room, so a late joiner needs no history. */
  private broadcast(except?: string) {
    const members = this.members(except);
    const state = JSON.stringify({
      t: "state",
      m: Object.fromEntries(members.map(([, m]) => [m.m, m.row])),
    });
    for (const [ws] of members) {
      try {
        ws.send(state);
      } catch {
        // Closing under us. Its own close handler rebroadcasts without it.
      }
    }
  }
}

function member(ws: WebSocket): Member | null {
  return (ws.deserializeAttachment() as Member | null | undefined) ?? null;
}

function parse(message: string | ArrayBuffer): Frame | null {
  if (typeof message !== "string"
      || new TextEncoder().encode(message).byteLength > MAX_FRAME_BYTES) {
    return null;
  }
  let frame: unknown;
  try {
    frame = JSON.parse(message);
  } catch {
    return null;
  }
  if (typeof frame !== "object" || frame === null) {
    return null;
  }
  const { t, m, c, s, k } = frame as Record<string, unknown>;
  if (t === "join") {
    return typeof m === "string" && MEMBER_ID.test(m) ? { t: "join", m } : null;
  }
  if (t === "pick") {
    const row = toRow(c, s, k);
    return row && { t: "pick", row };
  }
  return null;
}

/**
 * A row, or null when its ids do not hang together: a skin or chroma id
 * carries its champion as id // 1000, a chroma needs its skin, and there is
 * no skin without a champion.
 */
function toRow(c: unknown, s: unknown, k: unknown): Row | null {
  if (!isId(c) || !isId(s) || !isId(k)) {
    return null;
  }
  if (c === null) {
    return s === null && k === null ? EMPTY : null;
  }
  if (s !== null && Math.floor(s / 1000) !== c) {
    return null;
  }
  if (k !== null && (s === null || Math.floor(k / 1000) !== c)) {
    return null;
  }
  return { c, s, k };
}

function isId(v: unknown): v is number | null {
  return v === null || (Number.isSafeInteger(v) && (v as number) > 0);
}
