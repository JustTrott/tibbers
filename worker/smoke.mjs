// Drives a running relay through the protocol in LOBBY.md, "The relay".
// scripts/lobby_smoke.sh starts `wrangler dev` and runs this against it;
// on its own it is `node worker/smoke.mjs http://127.0.0.1:8787`.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";

const base = new URL(process.argv[2] ?? "http://127.0.0.1:8787");
const WAIT_MS = 5000;

const hex = (chars) => randomBytes(chars / 2).toString("hex");
const roomUrl = (room) =>
  `${base.origin.replace(/^http/, "ws")}/v1/room?room=${room}`;

class Peer {
  constructor(room) {
    this.inbox = [];
    this.seen = [];
    this.closed = null;
    this.waiters = new Set();
    this.ws = new WebSocket(roomUrl(room));
    this.ws.addEventListener("message", (e) => {
      this.inbox.push(String(e.data));
      this.poke();
    });
    this.ws.addEventListener("close", (e) => {
      this.closed = { code: e.code, reason: e.reason };
      this.poke();
    });
    this.opened = new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error",
        () => reject(new Error("could not connect")), { once: true });
    });
  }

  poke() {
    for (const waiter of this.waiters) waiter();
  }

  send(frame) {
    this.ws.send(typeof frame === "object" && !(frame instanceof Uint8Array)
      ? JSON.stringify(frame) : frame);
  }

  close() {
    this.ws.close(1000);
  }

  /** Resolves with the first non-undefined value `check` returns. */
  until(label, check) {
    return new Promise((resolve, reject) => {
      const done = () => {
        clearTimeout(timer);
        this.waiters.delete(tick);
      };
      const tick = () => {
        const value = check(this);
        if (value !== undefined) {
          done();
          resolve(value);
        }
      };
      const timer = setTimeout(() => {
        done();
        reject(new Error(`timed out waiting for ${label}`));
      }, WAIT_MS);
      this.waiters.add(tick);
      tick();
    });
  }

  /** The next room state that satisfies `want`; earlier ones are consumed. */
  state(label, want) {
    return this.until(label, (p) => {
      while (p.inbox.length) {
        const text = p.inbox.shift();
        if (text === "pong") continue;
        const frame = JSON.parse(text);
        assert.equal(frame.t, "state", `unexpected frame ${text}`);
        p.seen.push(frame.m);
        if (want(frame.m)) return frame.m;
      }
    });
  }

  closedWith(label) {
    return this.until(label, (p) => p.closed ?? undefined);
  }
}

async function step(name, fn) {
  await fn();
  console.log(`  ok  ${name}`);
}

async function refused(room, frames, code) {
  const peer = new Peer(room);
  await peer.opened;
  for (const frame of frames) peer.send(frame);
  const closed = await peer.closedWith(`close ${code}`);
  assert.equal(closed.code, code, `closed ${closed.code} "${closed.reason}"`);
}

async function main() {
  await step("refuses anything but a room upgrade", async () => {
    const status = async (path) => (await fetch(new URL(path, base))).status;
    assert.equal(await status("/"), 404);
    assert.equal(await status("/v1/room?room=abc"), 400);
    assert.equal(await status(`/v1/room?room=${hex(32).toUpperCase()}`), 400);
    assert.equal(await status(`/v1/room?room=${hex(32)}`), 426);
  });

  const room = hex(32);
  const ida = hex(16);
  const idb = hex(16);
  let a = new Peer(room);
  const b = new Peer(room);
  await Promise.all([a.opened, b.opened]);

  await step("answers ping without waking the room", async () => {
    const peer = new Peer(room);
    await peer.opened;
    peer.send("ping");
    await peer.until("pong", (p) => p.inbox.includes("pong") || undefined);
    peer.close();
  });

  await step("a join reaches the whole room", async () => {
    a.send({ t: "join", m: ida });
    await a.state("a sees itself", (m) => ida in m);
    b.send({ t: "join", m: idb });
    const m = await a.state("a sees b", (m) => idb in m);
    assert.deepEqual(m[idb], { c: null, s: null, k: null });
    await b.state("b sees both", (m) => ida in m && idb in m);
  });

  await step("a pick reaches the other member", async () => {
    a.send({ t: "pick", c: 103, s: 103015, k: 103016 });
    const m = await b.state("b sees a's pick", (m) => m[ida]?.k === 103016);
    assert.deepEqual(m[ida], { c: 103, s: 103015, k: 103016 });
  });

  await step("rooms do not hear each other", async () => {
    const other = new Peer(hex(32));
    await other.opened;
    other.send({ t: "join", m: hex(16) });
    other.send({ t: "pick", c: 64, s: 64012, k: null });
    await other.state("the other room's pick",
      (m) => Object.values(m).some((row) => row.s === 64012));
    a.send({ t: "pick", c: 103, s: 103015, k: null });
    await b.state("a's chroma cleared", (m) => m[ida]?.k === null);
    assert.ok(b.seen.every((m) => Object.keys(m).length <= 2),
      "b saw a member from another room");
    other.close();
  });

  await step("refuses a pick before a join", () =>
    refused(room, [{ t: "pick", c: 103, s: 103015, k: null }], 4400));
  await step("refuses a second join on one socket", () =>
    refused(room, [{ t: "join", m: hex(16) }, { t: "join", m: hex(16) }], 4400));
  await step("refuses a member id that is not 16 hex", () =>
    refused(room, [{ t: "join", m: "not-a-member" }], 4400));
  await step("refuses a skin that is not its champion's", () =>
    refused(room, [{ t: "join", m: hex(16) },
      { t: "pick", c: 103, s: 64012, k: null }], 4400));
  await step("refuses a chroma without its skin", () =>
    refused(room, [{ t: "join", m: hex(16) },
      { t: "pick", c: 103, s: null, k: 103016 }], 4400));
  await step("refuses a skin without a champion", () =>
    refused(room, [{ t: "join", m: hex(16) },
      { t: "pick", c: null, s: 103015, k: null }], 4400));
  await step("refuses a row with a field missing", () =>
    refused(room, [{ t: "join", m: hex(16) },
      { t: "pick", c: 103, s: 103015 }], 4400));
  await step("refuses a frame over 128 bytes", () =>
    refused(room, [{ t: "join", m: hex(16) },
      { t: "pick", c: 103, s: 103015, k: null, pad: "x".repeat(100) }], 4400));
  await step("refuses a binary frame", () =>
    refused(room, [new Uint8Array([1, 2, 3])], 4400));

  await step("refused members are not left in the room", async () => {
    a.send({ t: "pick", c: 103, s: 103015, k: 103016 });
    const m = await b.state("a's chroma back", (m) => m[ida]?.k === 103016);
    assert.deepEqual(Object.keys(m).sort(), [ida, idb].sort());
  });

  await step("a reconnect replaces the member's old socket", async () => {
    const again = new Peer(room);
    await again.opened;
    again.send({ t: "join", m: ida });
    const closed = await a.closedWith("the old socket closing");
    assert.equal(closed.code, 4409);
    const m = await b.state("a's row reset", (m) => m[ida]?.s === null);
    assert.deepEqual(Object.keys(m).sort(), [ida, idb].sort());
    a = again;
  });

  await step("a member that leaves is dropped", async () => {
    b.close();
    const m = await a.state("b gone", (m) => !(idb in m));
    assert.deepEqual(Object.keys(m), [ida]);
  });
  a.close();

  await step("sixteen members fill a room", async () => {
    const full = hex(32);
    const peers = Array.from({ length: 16 }, () => new Peer(full));
    await Promise.all(peers.map((p) => p.opened));
    const ids = peers.map((p) => {
      const id = hex(16);
      p.send({ t: "join", m: id });
      return id;
    });
    await peers[0].state("all sixteen",
      (m) => ids.every((id) => id in m));
    await refused(full, [{ t: "join", m: hex(16) }], 4429);
    peers.forEach((p) => p.close());
  });
}

main().then(
  () => {
    console.log("lobby relay: every check passed");
    process.exit(0);
  },
  (err) => {
    console.error(`lobby relay: ${err.stack ?? err}`);
    process.exit(1);
  },
);
