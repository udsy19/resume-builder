#!/usr/bin/env python3
"""Web-layer checks — no API calls, no model spend.

The FastAPI layer had no tests at all: input caps, rate limits, and the error
scrubbing that keeps a submitted API key out of a validation message were all
unverified, and every one of them is a thing you only find out about in production.

Uses FastAPI's TestClient, so nothing binds a port and nothing reaches a provider.
The streaming endpoints are exercised only up to the point where they would call a
model — enough to prove validation and limits behave.
"""
import os
import sys
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

    print("routes:")

    def health():
        r = client.get("/api/health")
        assert r.status_code == 200, r.status_code
    check("GET /api/health", health)

    def index():
        r = client.get("/")
        assert r.status_code == 200 and "<html" in r.text.lower()
    check("GET / serves the UI", index)

    def templates():
        r = client.get("/api/templates")
        assert r.status_code == 200
        ids = {t["id"] for t in r.json()["templates"]}
        assert {"udaya", "jakes", "mst", "sb2nov"} <= ids, ids
        assert sum(1 for t in r.json()["templates"] if t.get("default")) == 1, "exactly one default"
    check("GET /api/templates lists all four with one default", templates)

    def unknown_template():
        r = client.get("/api/templates/does-not-exist/preview.pdf")
        assert r.status_code in (404, 400), r.status_code
    check("unknown template preview is rejected, not a 500", unknown_template)

    print("input caps:")

    def jd_cap():
        r = client.post("/api/tailor/stream", data={
            "job_description": "x" * (webapp.MAX_JD_CHARS + 1), "dump_text": "hi"})
        assert r.status_code >= 400, f"oversized JD accepted ({r.status_code})"
    check("oversized job description rejected", jd_cap)

    def missing_jd():
        r = client.post("/api/tailor/stream", data={"dump_text": "hi"})
        assert r.status_code >= 400, "missing job description accepted"
    check("missing job description rejected", missing_jd)

    def latex_cap():
        r = client.post("/api/export/pdf", json={"latex": "x" * (webapp.MAX_LATEX_CHARS + 1)})
        assert r.status_code >= 400, "oversized LaTeX accepted"
    check("oversized LaTeX rejected", latex_cap)

    def instruction_cap():
        r = client.post("/api/edit/stream", json={
            "latex": "\\documentclass{article}\\begin{document}x\\end{document}",
            "instruction": "x" * (webapp.MAX_INSTRUCTION_CHARS + 1)})
        assert r.status_code >= 400, "oversized instruction accepted"
    check("oversized edit instruction rejected", instruction_cap)

    print("secret hygiene:")

    def no_key_echo():
        """A validation error must never quote back what was submitted.

        The tailor endpoint takes an api_key field, so an error that echoes input
        would put a live key into a response body, a log, and probably a bug report.
        """
        planted = "sk-ant-api03-PLANTEDSECRETVALUE0000000000"
        r = client.post("/api/tailor/stream", data={
            "job_description": "x" * (webapp.MAX_JD_CHARS + 1),
            "dump_text": "hi",
            "api_key": planted,
        })
        assert planted not in r.text, "an API key was echoed back in an error response"
        assert "PLANTEDSECRETVALUE" not in r.text, "submitted secret leaked into the response"
    check("submitted API key never echoed in errors", no_key_echo)

    print("rate limiting:")

    def limits_configured():
        for bucket in ("tailor", "edit"):
            limit, window = webapp.RATE_LIMITS[bucket]
            assert limit > 0 and window > 0, f"{bucket} limit is not positive"
    check("rate limit buckets configured", limits_configured)

    def limit_enforced():
        """Drive one bucket past its limit and confirm a 429 arrives."""
        limit, _ = webapp.RATE_LIMITS["edit"]
        body = {"latex": "\\documentclass{article}\\begin{document}x\\end{document}",
                "instruction": "x" * (webapp.MAX_INSTRUCTION_CHARS + 1)}
        seen_429 = False
        for _ in range(limit + 2):
            if client.post("/api/edit/stream", json=body).status_code == 429:
                seen_429 = True
                break
        assert seen_429, f"no 429 after {limit + 2} requests against a limit of {limit}"
    check("rate limit actually returns 429", limit_enforced)

    print("access gate:")

    def gate():
        """The gate is a security boundary, so it is tested as one.

        Without it a deployed instance hands its own API key to every visitor: the
        provider falls back to the environment whenever a request carries no key.
        """
        import importlib
        import hashlib
        import hmac as _hmac
        import time as _time

        # A throwaway PIN invented for this test. The deployment's real PIN lives
        # only in the hosting environment — putting it here would publish it.
        test_pin = "918273"
        os.environ["ACCESS_PIN"] = test_pin
        try:
            gated = importlib.reload(webapp)
            gc = TestClient(gated.app)

            assert gated.ACCESS_PIN, "gate did not pick up ACCESS_PIN"
            assert gc.get("/api/auth/status").json() == {"gate": "enabled", "unlocked": False}

            # A wrong PIN is refused, and the response never contains the real one.
            bad = gc.post("/api/auth", json={"pin": "000000"})
            assert bad.status_code == 401, bad.status_code
            assert test_pin not in bad.text, "the PIN leaked in an error response"

            # The endpoints that spend money are closed while locked.
            r = gc.post("/api/tailor/stream", data={"job_description": "Engineer", "dump_text": "me"})
            assert r.status_code == 401, f"locked tailor endpoint returned {r.status_code}"

            # The right PIN opens them, and hands back no key.
            ok = gc.post("/api/auth", json={"pin": test_pin})
            assert ok.status_code == 200, ok.status_code
            token = ok.json()["token"]
            assert token, "no token issued"
            assert "sk-" not in ok.text, "an API key leaked into the auth response"
            assert gc.get("/api/auth/status", headers={"X-Access-Token": token}).json()["unlocked"]

            # A forged signature and an expired token are both rejected.
            forged = token.split(".")[0] + "." + "0" * 64
            assert not gc.get("/api/auth/status", headers={"X-Access-Token": forged}).json()["unlocked"], \
                "a forged token was accepted"
            past = str(int(_time.time()) - 10)
            sig = _hmac.new(gated._SESSION_SECRET.encode(), past.encode(), hashlib.sha256).hexdigest()
            assert not gc.get("/api/auth/status",
                              headers={"X-Access-Token": f"{past}.{sig}"}).json()["unlocked"], \
                "an expired token was accepted"
        finally:
            os.environ.pop("ACCESS_PIN", None)
            importlib.reload(webapp)
    check("PIN gate protects the server's API key", gate)

    def gate_off_by_default():
        """A local checkout with no ACCESS_PIN must behave exactly as before."""
        assert not webapp.ACCESS_PIN
        assert client.get("/api/auth/status").json() == {"gate": "disabled", "unlocked": True}
    check("gate is disabled when no PIN is configured", gate_off_by_default)

    def pin_selects_provider():
        """A PIN-unlocked run spends the operator's credits, so the operator picks
        the provider. A visitor's own key still routes to that key's own provider."""
        import importlib
        os.environ["ACCESS_PIN"] = "918273"
        try:
            gated = importlib.reload(webapp)
            gc = TestClient(gated.app)
            assert gated.PIN_PROVIDER == "openai", f"PIN provider is {gated.PIN_PROVIDER!r}"

            token = gc.post("/api/auth", json={"pin": "918273"}).json()["token"]

            class Req:
                def __init__(self, tok): self.headers = {"x-access-token": tok}

            key, provider = gated._resolve_credentials(Req(token), "")
            assert key == "" and provider == "openai", (key, provider)

            # A supplied key must not be overridden — it routes by its own prefix.
            key, provider = gated._resolve_credentials(Req(token), "sk-ant-user-key")
            assert key == "sk-ant-user-key" and provider is None, (key, provider)

            # Still refused without a token.
            try:
                gated._resolve_credentials(Req("bogus"), "")
                raise AssertionError("locked request was allowed the server key")
            except Exception as e:
                assert getattr(e, "status_code", None) == 401, e
        finally:
            os.environ.pop("ACCESS_PIN", None)
            importlib.reload(webapp)
    check("PIN-unlocked runs default to the configured provider", pin_selects_provider)

    def provider_choice():
        """A caller may only pick a provider whoever is paying actually has."""
        import importlib
        os.environ["ACCESS_PIN"] = "918273"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            g = importlib.reload(webapp)
            gc = TestClient(g.app)

            # Locked: no choice is offered at all.
            assert gc.get("/api/providers").json() == {"server": [], "default": None, "unlocked": False}

            token = gc.post("/api/auth", json={"pin": "918273"}).json()["token"]
            hdr = {"X-Access-Token": token}
            body = gc.get("/api/providers", headers=hdr).json()
            assert body["server"] == ["anthropic"], body
            assert body["unlocked"] is True

            class Req:
                def __init__(self, tok): self.headers = {"x-access-token": tok}

            # Only one provider configured: the default resolves to it even though
            # PIN_PROVIDER asks for openai, rather than billing a key that isn't there.
            key, prov = g._resolve_credentials(Req(token), "")
            assert (key, prov) == ("", "anthropic"), (key, prov)

            # Asking for one the server cannot pay for is refused, not silently swapped.
            try:
                g._resolve_credentials(Req(token), "", "openai")
                raise AssertionError("unconfigured provider was accepted")
            except Exception as e:
                assert getattr(e, "status_code", None) == 400, e

            # With both configured, an explicit choice is honoured.
            os.environ["OPENAI_API_KEY"] = "sk-proj-test"
            g2 = importlib.reload(webapp)
            tok2 = TestClient(g2.app).post("/api/auth", json={"pin": "918273"}).json()["token"]
            assert g2._resolve_credentials(Req(tok2), "", "openai") == ("", "openai")
            assert g2._resolve_credentials(Req(tok2), "", "anthropic") == ("", "anthropic")

            # A user's own key always wins and routes by its own prefix.
            assert g2._resolve_credentials(Req(tok2), "sk-ant-mine", "openai") == ("sk-ant-mine", None)
        finally:
            for k in ("ACCESS_PIN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                os.environ.pop(k, None)
            importlib.reload(webapp)
    check("provider choice is validated against who is paying", provider_choice)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        sys.exit(1)
    print("all web checks passed")


main()
