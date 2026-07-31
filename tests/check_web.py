#!/usr/bin/env python3
"""Web-layer checks — no API calls, no model spend.

The FastAPI layer had no tests at all: input caps, rate limits, and the error
scrubbing that keeps a submitted API key out of a validation message were all
unverified, and every one of them is a thing you only find out about in production.

Uses FastAPI's TestClient, so nothing binds a port and nothing reaches a provider.
The streaming endpoints are exercised only up to the point where they would call a
model — enough to prove validation and limits behave.
"""
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

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        sys.exit(1)
    print("all web checks passed")


main()
