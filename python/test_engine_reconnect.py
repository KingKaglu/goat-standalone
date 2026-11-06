"""Claude's engine reconnects without taking the voice down (2026-11-06 audit
B1). A connect that keeps failing is retried with backoff and never raises;
while it is down, jobs don't start and the router lets the voice answer.
Fake engine, no SDK process, no network.
Run: py -3.13 test_engine_reconnect.py"""
import asyncio
import sys

import goat_app as g

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  — {info}" if info and not ok else ""))
    if not ok:
        fails.append(name)


class FakeClient:
    made = []
    plan = []          # one entry per connect: True = works, else an exception

    def __init__(self, options):
        self.options = options
        self.connected = False
        self.queries = []
        FakeClient.made.append(self)

    async def connect(self):
        ok = FakeClient.plan.pop(0) if FakeClient.plan else True
        if ok is not True:
            raise ok
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, text):
        self.queries.append(text)


class FakeWatch:
    def __init__(self):
        self.finished = []

    def start(self, *a):
        pass

    def finish(self, state, text):
        self.finished.append((state, text))

    def running(self):
        return False


class Opts:
    model = "claude-opus-5-5"
    resume = None


def make_app():
    app = g.GoatApp.__new__(g.GoatApp)
    app.events = []
    app.emit = lambda k, d="": app.events.append((k, d))
    app.engine_down = False
    app.claude_out = False
    app.model = ""
    return app


async def main() -> int:
    g._new_client = FakeClient
    g.ENGINE_BACKOFF_S = (0, 0, 0)
    ups = []
    g.self_update.mark_engine_up = lambda: ups.append(1)

    # 1) fails three times, then connects: never raises, ends up connected
    app = make_app()
    app.client = FakeClient(Opts())
    seen_down = []

    class Watcher(FakeClient):
        async def connect(self):
            seen_down.append(app.engine_down)
            await FakeClient.connect(self)
    g._new_client = Watcher
    FakeClient.made.clear()
    FakeClient.plan = [ConnectionError("net down"), OSError("no route"),
                       TimeoutError(), True]
    await asyncio.wait_for(app._connect_engine(Opts()), 5)
    check("keeps retrying, then connects", app.client.connected and len(FakeClient.made) == 4)
    check("engine marked down while failing", seen_down[1:] == [True, True, True], seen_down)
    check("engine up again after", app.engine_down is False)
    check("engine-up file written once, on success", ups == [1])
    check("he is told once, not per retry",
          sum(1 for k, d in app.events if "can't connect" in str(d)) == 1, app.events)
    check("and told when it is back", any("working brain back" in str(d) for _, d in app.events))
    check("model recorded", app.model == Opts.model)

    # 2) fresh=False reuses the client made at boot on the first try
    g._new_client = FakeClient
    FakeClient.made.clear()
    FakeClient.plan = [True]
    app = make_app()
    boot = FakeClient(Opts())
    app.client = boot
    FakeClient.made.clear()
    await app._connect_engine(Opts(), fresh=False)
    check("boot client reused", app.client is boot and boot.connected and not FakeClient.made)

    # 3) cancellation (shutdown) is not swallowed
    FakeClient.plan = [ConnectionError()] * 50
    g.ENGINE_BACKOFF_S = (5,)
    app = make_app()
    app.client = FakeClient(Opts())
    t = asyncio.create_task(app._connect_engine(Opts()))
    await asyncio.sleep(0.05)
    t.cancel()
    try:
        await t
        check("shutdown cancels the retry loop", False)
    except asyncio.CancelledError:
        check("shutdown cancels the retry loop", True)
    g.ENGINE_BACKOFF_S = (0, 0, 0)

    # 4) while down: no job starts, the router lets the voice answer
    app = make_app()
    app.engine_down = True
    app._watch = FakeWatch()
    app._worker_target = lambda: "claude-opus-5-5"
    app._limit_expired = lambda: False
    app.client = FakeClient(Opts())
    await app._work_job("fix the build", "fix the build", "deep")
    check("job refused while the engine is down",
          app._watch.finished and app._watch.finished[0][0] == "failed"
          and "reconnecting" in app._watch.finished[0][1])
    check("nothing sent to the dead engine", app.client.queries == [])
    check("router hands big tasks to the voice while down",
          app._route_big("fix the build and run the tests") is False)

    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
