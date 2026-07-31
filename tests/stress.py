#!/usr/bin/env python3
"""Stress and abuse checks for the web layer — no model calls, no cost.

Every case here is something a real deployment will meet: a client that hangs up
mid-stream, a run that goes silent long enough to trip an idle timeout, concurrent
users, hostile input, and malformed bodies. The happy path is already covered by
check_web.py; this file is about what happens when things go wrong.
"""
import asyncio
import concurrent.futures
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok    {name}")
    except Exception as e:
        print(f"  FAIL  {name}: {e}")
        failures.append(name)


def main():
    from fastapi.testclient import TestClient
    import web.app as webapp

    client = TestClient(webapp.app)

    print("sse resilience:")

    def heartbeat_on_silence():
        """A silent generator must still produce bytes, or proxies drop the connection.

        This is the defect behind a live 'network error': generation reasons for over a
        minute without emitting an event, and any idle timeout in between kills a stream
        that is producing nothing.
        """
        async def silent():
            await asyncio.sleep(webapp.SSE_HEARTBEAT_SECONDS * 1.6)
            yield "data: {}\n\n"

        async def drive():
            out, started = [], time.monotonic()
            async for chunk in webapp._keepalive(silent()):
                out.append(chunk)
                if time.monotonic() - started > webapp.SSE_HEARTBEAT_SECONDS * 3:
                    break
            return out

        chunks = asyncio.run(drive())
        assert any(c.startswith(":") for c in chunks), \
            "no heartbeat emitted during silence — a proxy would drop this stream"
        assert any(c.startswith("data:") for c in chunks), "real events were lost"
    check("heartbeat keeps a silent stream alive", heartbeat_on_silence)

    def heartbeat_forwards_errors():
        """A failure inside the run must reach the client, not hang it forever."""
        async def boom():
            yield "data: {}\n\n"
            raise RuntimeError("upstream exploded")

        async def drive():
            seen = []
            try:
                async for chunk in webapp._keepalive(boom()):
                    seen.append(chunk)
            except RuntimeError as e:
                return seen, str(e)
            return seen, None

        seen, err = asyncio.run(drive())
        assert err == "upstream exploded", f"error was swallowed: {err!r}"
        assert seen, "events before the failure were lost"
    check("heartbeat propagates upstream errors", heartbeat_forwards_errors)

    def heartbeat_stops_cleanly():
        """A finite source must terminate rather than heartbeat forever."""
        async def short():
            yield "data: 1\n\n"
            yield "data: 2\n\n"

        async def drive():
            return [c async for c in webapp._keepalive(short())]

        chunks = asyncio.run(asyncio.wait_for(drive(), timeout=10))
        assert len([c for c in chunks if c.startswith("data:")]) == 2, chunks
    check("heartbeat terminates with its source", heartbeat_stops_cleanly)

    print("hostile input:")

    hostile = [
        ("latex injection", {"latex": r"\documentclass{a}\input{/etc/passwd}\begin{document}x\end{document}"}),
        ("write18 shell escape", {"latex": r"\documentclass{a}\write18{rm -rf /}\begin{document}x\end{document}"}),
        ("empty body", {"latex": ""}),
        ("not latex at all", {"latex": "just some text"}),
        ("null bytes", {"latex": "\\documentclass{a}\x00\\begin{document}x\\end{document}"}),
    ]

    def hostile_latex():
        for label, body in hostile:
            r = client.post("/api/export/pdf", json=body)
            assert r.status_code != 500, f"{label} caused a 500 — unhandled"
            assert "/etc/passwd" not in r.text, f"{label} leaked file contents"
            assert "root:" not in r.text, f"{label} leaked /etc/passwd"
    check("hostile LaTeX is rejected, never 500s", hostile_latex)

    def malformed_bodies():
        cases = [
            ("not json", {"content": "{{{", "headers": {"Content-Type": "application/json"}}),
            ("wrong types", {"json": {"latex": 123, "instruction": ["a"]}}),
            ("missing fields", {"json": {}}),
            ("huge history", {"json": {"latex": "\\documentclass{a}\\begin{document}x\\end{document}",
                                       "instruction": "hi",
                                       "history": [{"role": "user", "content": "x"}] * 500}}),
        ]
        for label, kw in cases:
            r = client.post("/api/edit/stream", **kw)
            assert r.status_code != 500, f"{label} caused a 500"
            assert r.status_code >= 400, f"{label} was accepted ({r.status_code})"
    check("malformed request bodies are rejected, never 500", malformed_bodies)

    def unicode_and_control():
        """Real dumps carry emoji, RTL text and zero-width characters."""
        nasty = "Zoë 北京 \u202eRTL\u202c \u200b\u200b bullet • em—dash"
        r = client.post("/api/tailor/stream", data={"job_description": nasty, "dump_text": nasty})
        assert r.status_code != 500, "unicode input caused a 500"
    check("unicode and control characters do not crash the API", unicode_and_control)

    print("concurrency:")

    def concurrent_reads():
        """Many simultaneous clients must not corrupt shared state or error."""
        def hit(_):
            return client.get("/api/templates").status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(hit, range(64)))
        assert all(c == 200 for c in codes), f"non-200 under load: {sorted(set(codes))}"
    check("64 concurrent requests all succeed", concurrent_reads)

    def rate_limiter_thread_safety():
        """The limiter mutates a shared dict of deques from many threads."""
        webapp._hits.clear()
        limit, _ = webapp.RATE_LIMITS["edit"]
        body = {"latex": "x", "instruction": "y"}

        def hit(_):
            return client.post("/api/edit/stream", json=body).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(hit, range(limit + 30)))
        assert 429 in codes, "limiter never fired under concurrent load"
        assert 500 not in codes, "limiter raced and 500'd"
        webapp._hits.clear()
    check("rate limiter holds under concurrent load", rate_limiter_thread_safety)

    print("gate under attack:")

    def pin_bruteforce_is_bounded():
        """Six digits is a million guesses; the limiter is the only thing stopping them."""
        import importlib
        os.environ["ACCESS_PIN"] = "424242"
        try:
            gated = importlib.reload(webapp)
            gc = TestClient(gated.app)
            gated._hits.clear()
            codes = [gc.post("/api/auth", json={"pin": f"{i:06d}"}).status_code for i in range(30)]
            assert 429 in codes, "PIN endpoint allowed 30 guesses without limiting"
            attempts_before_limit = codes.index(429)
            assert attempts_before_limit <= 12, \
                f"{attempts_before_limit} guesses allowed before limiting — too loose"
        finally:
            os.environ.pop("ACCESS_PIN", None)
            importlib.reload(webapp)
    check("PIN brute force is rate limited", pin_bruteforce_is_bounded)

    def token_cannot_be_forged():
        """Try a range of malformed and forged tokens against the gate."""
        import importlib
        os.environ["ACCESS_PIN"] = "424242"
        try:
            gated = importlib.reload(webapp)
            gc = TestClient(gated.app)
            forgeries = [
                "", ".", "abc", "9999999999.", "9999999999." + "f" * 64,
                "0.0", "-1.x", "9999999999.deadbeef",
                "9999999999.%s" % ("a" * 63), "not-a-number.sig",
            ]
            for tok in forgeries:
                got = gc.get("/api/auth/status", headers={"X-Access-Token": tok}).json()
                assert got["unlocked"] is False, f"forged token accepted: {tok!r}"
        finally:
            os.environ.pop("ACCESS_PIN", None)
            importlib.reload(webapp)
    check("forged session tokens are all rejected", token_cannot_be_forged)

    def lockout_escalates():
        """Each wrong PIN must cost more than the last, and a success must clear it."""
        import importlib
        os.environ["ACCESS_PIN"] = "424242"
        try:
            gated = importlib.reload(webapp)
            gc = TestClient(gated.app)
            gated._hits.clear(); gated._failures.clear()

            # Free attempts first, then a lockout that grows.
            codes = []
            for i in range(gated._LOCKOUT_AFTER + 2):
                codes.append(gc.post("/api/auth", json={"pin": f"{i:06d}"}).status_code)
            assert 429 in codes, f"no lockout after {len(codes)} wrong PINs: {codes}"

            ip = next(iter(gated._failures))
            first = gated._lockout_remaining(ip)
            assert first > 0, "lockout recorded no wait"

            gated._note_failure(ip)
            second = gated._lockout_remaining(ip)
            assert second > first, f"lockout did not escalate: {first} -> {second}"

            # The correct PIN is refused while locked — the lock is not bypassable.
            assert gc.post("/api/auth", json={"pin": "424242"}).status_code == 429, \
                "lockout was bypassed by supplying the correct PIN"

            # Once the lock expires, the right PIN works and clears the record.
            gated._failures[ip]["until"] = 0.0
            ok = gc.post("/api/auth", json={"pin": "424242"})
            assert ok.status_code == 200, ok.status_code
            assert ip not in gated._failures, "a success did not clear the failure record"
        finally:
            os.environ.pop("ACCESS_PIN", None)
            importlib.reload(webapp)
    check("wrong PINs trigger escalating lockout", lockout_escalates)

    def lockout_never_logs_the_pin():
        src = (ROOT / "web" / "app.py").read_text()
        block = src.split("def _note_failure")[1].split("@app.post")[0]
        assert "ACCESS_PIN" not in block and "body.pin" not in block, \
            "the failure logger references the PIN — it must never be logged"
    check("failed-attempt logging never includes the PIN", lockout_never_logs_the_pin)

    def thinking_renderer_is_safe():
        """The reasoning stream renders model markdown, so it must not render model HTML.

        Reasoning summaries are markdown — OpenAI's lead with bold section headers, which
        is why the panel was showing literal "**Assessing security measures**". Rendering
        them means model output reaches innerHTML, so escaping must come first.
        """
        import json, shutil, subprocess

        src = (ROOT / "web" / "static" / "app.js").read_text()
        assert "function renderThinking" in src, "renderThinking is missing"
        fn = src[src.index("function renderThinking"):]
        fn = fn[:fn.index("\n}\n") + 3]

        # Escaping must be the first thing that touches the input.
        body = fn[fn.index("{"):]
        assert body.index("esc(md)") < body.index(".replace("), \
            "renderThinking transforms before escaping — model output could inject markup"

        if not shutil.which("node"):
            return                                   # source check above still applied

        harness = (
            'function esc(s){return String(s).replace(/[&<>"]/g,'
            'c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));}\n'
            + fn +
            '\nconst out = ['
            '  renderThinking("<img src=x onerror=alert(1)>"),'
            '  renderThinking("**bold <script>alert(1)</script>**"),'
            '  renderThinking("**Assessing security measures**"),'
            '  renderThinking("Use **SIEM** and `grep`")'
            '];\nconsole.log(JSON.stringify(out));'
        )
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, f"renderer threw: {res.stderr[:200]}"
        xss1, xss2, header, inline = json.loads(res.stdout)

        for out in (xss1, xss2):
            assert "<img" not in out and "<script" not in out, f"markup survived: {out}"
            assert "&lt;" in out, f"input was not escaped: {out}"
        assert "**" not in header, f"markdown left raw: {header}"
        assert "<b" in header, f"header not rendered: {header}"
        assert "<b>SIEM</b>" in inline and "<code>grep</code>" in inline, inline
    check("reasoning stream renders markdown without rendering HTML", thinking_renderer_is_safe)

    def compiles_do_not_block_the_loop():
        """A compile must not freeze every other user's stream.

        compile_pdf is async but shelled out synchronously, so each of the 15-30 compiles
        in a run froze the whole event loop — on a single-worker server that stalls other
        users' SSE streams, including the keepalives holding their connections open.
        """
        import asyncio as aio
        from src.latex import compile_pdf, find_pdflatex
        from src.templates import get_template

        if not find_pdflatex():
            return                                   # nothing to measure without TeX
        tex = get_template("udaya").read()

        async def drive():
            ticks = 0
            stop = False

            async def ticker():
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await aio.sleep(0.02)

            tk = aio.create_task(ticker())
            await compile_pdf(tex)
            stop = True
            await aio.sleep(0)
            tk.cancel()
            return ticks

        ticks = aio.run(drive())
        # A ~0.5s compile leaves room for many 20ms ticks; a blocked loop yields ~1.
        assert ticks > 5, f"event loop was blocked during compilation ({ticks} ticks)"
    check("LaTeX compilation does not block the event loop", compiles_do_not_block_the_loop)

    def runs_survive_a_dropped_client():
        """A run is fifteen minutes of paid work; a closed tab must not destroy it."""
        import asyncio as aio

        async def drive():
            rid = webapp._new_run()
            for i in range(3):
                webapp._record(rid, {"step": "phase", "n": i})

            class Req:
                async def is_disconnected(self): return False

            # Rejoin having already seen the first event: replay must resume at 1.
            resp = await webapp.run_replay(rid, Req(), offset=1)
            seen = []

            async def pump():
                async for chunk in resp.body_iterator:
                    if chunk.startswith("data:"):
                        seen.append(chunk)
                        if len(seen) == 2:
                            webapp._record(rid, {"step": "result"})
                        if len(seen) >= 3:
                            return
            await aio.wait_for(pump(), timeout=15)
            return seen, webapp._runs[rid]["done"]

        seen, done = aio.run(drive())
        assert len(seen) == 3, f"replay returned {len(seen)} events"
        assert '"n": 1' in seen[0], f"replay did not honour the offset: {seen[0]}"
        assert done, "the run was not marked finished"

        # An unknown run is a clean 404, not a hang.
        r = client.get("/api/runs/does-not-exist/stream")
        assert r.status_code == 404, r.status_code

        # Memory stays bounded.
        for _ in range(webapp.MAX_RUNS + 8):
            webapp._new_run()
        assert len(webapp._runs) <= webapp.MAX_RUNS, f"run store grew to {len(webapp._runs)}"
        webapp._runs.clear()
    check("runs survive a dropped client and stay bounded", runs_survive_a_dropped_client)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        sys.exit(1)
    print("all stress checks passed")


main()
