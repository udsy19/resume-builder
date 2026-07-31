"""
ATS Resume Builder API — one-page agentic resume tailoring.

Input: the candidate's information dump (LaTeX/PDF/Word/Markdown/text),
a job description, an optional LaTeX template choice, and an aggressiveness
level. Output: a recursively evaluated, ATS-optimized one-page LaTeX resume.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from typing import List, Literal, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from fastapi import FastAPI, Form, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agent import AgentError, ResumeAgent
from src.ingest import Dump, ingest_file
from src.latex import DANGEROUS as _LATEX_DANGEROUS, compile_pdf, find_pdflatex
from src.templates import TEMPLATES_DIR, get_template, list_templates, validate_custom_template

app = FastAPI(title="ATS Resume Builder", version="4.0.0")

static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# ── Input limits ──
MAX_DUMP_BYTES = 4 * 1024 * 1024
MAX_DUMP_TEXT = 300_000
MAX_JD_CHARS = 50_000
MAX_LATEX_CHARS = 200_000
MAX_INSTRUCTION_CHARS = 4_000

# ── Per-IP rate limiting (in-memory; per serverless instance — a floor, not a wall) ──
RATE_LIMITS = {"tailor": (10, 3600), "edit": (60, 3600),
               # Six digits is a million guesses; keep attempts scarce.
               "auth": (8, 900)}
_hits: dict = defaultdict(deque)


def _rate_limit(bucket: str, request: Request):
    limit, window = RATE_LIMITS[bucket]
    ip = (request.headers.get("x-forwarded-for", "") or (request.client.host if request.client else "?")).split(",")[0].strip()
    q = _hits[f"{bucket}:{ip}"]
    now = time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit reached — try again in a while.")
    q.append(now)


# ── PIN gate for the server's own API key ────────────────────────────
#
# Without this, a deployed instance hands its own API key to every visitor: the
# provider falls back to the environment whenever the request carries no key, so
# anyone who found the URL could spend the owner's credits.
#
# The PIN lives in the environment, never in this repository — the repository is
# public. With ACCESS_PIN unset the gate is disabled and the old behaviour applies,
# which is what you want for local development.
#
# The API key itself is never sent to the browser. The client proves it knows the
# PIN, receives a short-lived signed token, and the server uses its own key on that
# token's behalf.
ACCESS_PIN = os.environ.get("ACCESS_PIN", "").strip()
SESSION_HOURS = int(os.environ.get("ACCESS_SESSION_HOURS", "720"))  # 30 days
# Deterministic across serverless instances so a token issued by one validates on
# another. An explicit SESSION_SECRET is better; without one, derive from the PIN.
_SESSION_SECRET = (os.environ.get("SESSION_SECRET", "").strip()
                   or hashlib.sha256(f"resume-builder:{ACCESS_PIN}".encode()).hexdigest())


def _issue_token() -> str:
    expires = int(time.time()) + SESSION_HOURS * 3600
    sig = hmac.new(_SESSION_SECRET.encode(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _token_valid(token: str) -> bool:
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = hmac.new(_SESSION_SECRET.encode(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _server_key_allowed(request: Request) -> bool:
    """May this request fall back to the server's own API key?"""
    if not ACCESS_PIN:
        return True                                  # gate disabled (local dev)
    return _token_valid(request.headers.get("x-access-token", ""))


class PinRequest(BaseModel):
    pin: str = Field(max_length=64)


@app.post("/api/auth")
async def auth(request: Request, body: PinRequest):
    """Exchange the PIN for a signed session token. The API key never leaves the server."""
    if not ACCESS_PIN:
        return {"unlocked": True, "token": "", "gate": "disabled"}
    # Constant-time compare so the response cannot be used as an oracle, and the
    # attempt is rate limited in middleware since six digits is brute-forceable.
    if not hmac.compare_digest(body.pin.strip(), ACCESS_PIN):
        raise HTTPException(status_code=401, detail="That PIN is not right.")
    return {"unlocked": True, "token": _issue_token(), "gate": "enabled"}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Whether this deployment gates its key, and whether the caller is already in."""
    return {"gate": "enabled" if ACCESS_PIN else "disabled",
            "unlocked": _server_key_allowed(request)}


# Which bucket guards which path, for the middleware below.
_RATE_LIMITED_PATHS = {"/api/tailor/stream": "tailor", "/api/edit/stream": "edit",
                       "/api/auth": "auth"}


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Apply the rate limit before routing, not inside the handler.

    Endpoints taking a Pydantic body are validated before their handler runs, so a
    malformed request returned 422 without ever reaching the in-handler limiter —
    leaving invalid requests effectively unlimited. Checking here covers both the
    valid and invalid paths with one rule.
    """
    bucket = _RATE_LIMITED_PATHS.get(request.url.path)
    if bucket and request.method == "POST":
        try:
            _rate_limit(bucket, request)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # Strip submitted values from validation errors (they can contain API keys).
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


def _resolve_key(request: Request, supplied: str) -> str:
    """Pick the key for this request, or refuse.

    A key the user supplied always wins — it is theirs to spend. Otherwise the
    server's own key is used only when the caller has unlocked the gate; without
    that, falling back to the environment would let any visitor spend the owner's
    credits, which is exactly what the gate exists to prevent.
    """
    supplied = (supplied or "").strip()
    if supplied:
        return supplied
    if _server_key_allowed(request):
        return ""                                   # provider falls back to the env key
    raise HTTPException(
        status_code=401,
        detail="Enter the access PIN at the bottom of the sidebar, or add your own API key in Settings.",
    )


def _friendly_error(e: Exception) -> str:
    if isinstance(e, anthropic.RateLimitError):
        return "The model is rate-limited right now — wait a minute and try again."
    if isinstance(e, anthropic.AuthenticationError):
        return "The API key was rejected — check it in Settings."
    if isinstance(e, anthropic.APIStatusError) and e.status_code >= 500:
        return "The model service had a hiccup — try again shortly."
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the model service — check connectivity and retry."
    if isinstance(e, (AgentError, ValueError)):
        return str(e)
    traceback.print_exc()
    return "Something unexpected went wrong — try again."


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class EditRequest(BaseModel):
    latex: str = Field(max_length=MAX_LATEX_CHARS)
    instruction: str = Field(max_length=MAX_INSTRUCTION_CHARS)
    history: List[ChatTurn] = Field(default_factory=list, max_length=24)
    job_description: str = Field(default="", max_length=MAX_JD_CHARS)
    api_key: str = Field(default="", max_length=300)


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=(static_path / "index.html").read_text())


# Serving the UI here too is deliberate. Vercel changed internal rewrites to route by
# the rewritten destination path, so a catch-all rewrite delivered every request to the
# app as "/api/index" — nothing matched, and the site came up blank with no clue why.
# vercel.json now uses routes, which preserve the original path, and this keeps the page
# reachable if any host ever rewrites that way again.
@app.get("/api/index", response_class=HTMLResponse)
async def root_rewritten():
    return HTMLResponse(content=(static_path / "index.html").read_text())


@app.get("/api/health")
async def health_check(request: Request):
    return {
        "status": "healthy",
        "version": "4.0.0",
        # Echoed so a routing misconfiguration is visible from outside: if this is not
        # "/api/health", something between the client and the app is rewriting paths.
        "received_path": request.url.path,
        "pdflatex": bool(find_pdflatex()),
        "gate": "enabled" if ACCESS_PIN else "disabled",
    }


@app.get("/api/templates")
async def templates():
    return {"templates": list_templates()}


_preview_cache: dict = {}


@app.get("/api/templates/{template_id}/preview.pdf")
async def template_preview(template_id: str):
    """Compiled preview of a built-in template, so users can see it before choosing."""
    template = get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Unknown template.")
    if template_id not in _preview_cache:
        result = await compile_pdf(template.read())
        if not result.ok or not result.pdf:
            raise HTTPException(status_code=502, detail="Could not render a preview for this template.")
        _preview_cache[template_id] = result.pdf
    return Response(
        content=_preview_cache[template_id],
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{template_id}-preview.pdf"',
                 "Cache-Control": "public, max-age=86400"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# A run goes quiet for long stretches — generation reasons for well over a minute before
# emitting anything. Any idle timeout between the browser and this process (proxy, load
# balancer, or the browser itself) will drop a silent connection, and the page reports it
# as "network error" with no trace on the server, because the server did nothing wrong.
# A comment line every few seconds keeps the connection provably alive and costs nothing:
# SSE clients ignore lines beginning with a colon.
SSE_HEARTBEAT_SECONDS = 15


async def _keepalive(source):
    """Yield the source's events, injecting a heartbeat whenever it falls silent."""
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    async def pump():
        try:
            async for item in source:
                await queue.put(item)
        except Exception as e:                       # surface, don't strand the client
            await queue.put(e)
        finally:
            await queue.put(DONE)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()


@app.post("/api/tailor/stream")
async def tailor_stream(
    request: Request,
    job_description: str = Form(...),
    aggressiveness: int = Form(2),
    template_id: str = Form("udaya"),
    dump_text: Optional[str] = Form(None),
    dump: Optional[UploadFile] = File(None),
    custom_template: Optional[UploadFile] = File(None),
    api_key: str = Form(""),
    cover_letter: bool = Form(False),
):
    """Run the agentic tailoring loop, streaming progress as SSE."""
    # Rate limiting happens in middleware, before routing — see rate_limit_middleware.

    job_description = job_description.strip()
    if not job_description:
        raise HTTPException(status_code=400, detail="Job description is required.")
    if len(job_description) > MAX_JD_CHARS:
        raise HTTPException(status_code=400, detail="Job description is too long — trim it to the actual posting.")

    # Resolve the dump
    if dump is not None:
        raw = await dump.read()
        if len(raw) > MAX_DUMP_BYTES:
            raise HTTPException(status_code=400, detail="Dump file is too large (max 4MB) — export a lighter file.")
        try:
            the_dump = ingest_file(dump.filename or "dump", raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif dump_text and dump_text.strip():
        if len(dump_text) > MAX_DUMP_TEXT:
            raise HTTPException(status_code=400, detail="Pasted dump is too long (max 300k characters).")
        the_dump = Dump(text=dump_text, filename="pasted")
    else:
        raise HTTPException(status_code=400, detail="Provide your resume/information dump (file or text).")

    # Resolve the template (custom upload wins; LaTeX only)
    if custom_template is not None:
        raw = await custom_template.read()
        try:
            template_latex = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Custom template must be a plain-text LaTeX (.tex) file.")
        error = validate_custom_template(template_latex)
        if error:
            raise HTTPException(status_code=400, detail=error)
    else:
        template = get_template(template_id)
        if template is None:
            raise HTTPException(status_code=400, detail=f"Unknown template '{template_id}'.")
        template_latex = template.read()

    # Resolved before the stream opens: an HTTPException raised inside the generator
    # cannot set a status code, because the headers are already on the wire.
    resolved_key = _resolve_key(request, api_key)

    async def events():
        try:
            agent = ResumeAgent(user_api_key=resolved_key)
            final = None
            async for update in agent.run(
                dump=the_dump,
                job_description=job_description,
                template_latex=template_latex,
                aggressiveness=aggressiveness,
            ):
                if await request.is_disconnected():
                    return  # stop burning tokens for a closed tab
                if update.get("step") == "result" and cover_letter:
                    # Held back so the letter can be attached to it: the UI treats the
                    # result event as the end of the run, and a letter that arrived
                    # afterwards would land on a screen that had already finished.
                    final = update
                    continue
                yield _sse(update)

            if final is not None:
                letter = None
                async for ev in agent.cover_letter(
                    dump=the_dump,
                    job_description=job_description,
                    jd_analysis=final["result"].get("jd_analysis") or {},
                    resume_latex=final["result"]["latex"],
                    template_latex=(TEMPLATES_DIR / "cover-letter.tex").read_text(),
                    today=date.today().strftime("%B %-d, %Y"),
                ):
                    if await request.is_disconnected():
                        return
                    letter = ev.pop("letter", None) or letter
                    yield _sse(ev)
                final["result"]["cover_letter"] = letter
                yield _sse(final)
        except Exception as e:
            yield _sse({"step": "error", "message": _friendly_error(e)})

    return StreamingResponse(
        _keepalive(events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/edit/stream")
async def edit_stream(request: Request, edit: EditRequest):
    """Conversational resume editing — streams thinking, then the updated document."""
    if not edit.latex.strip() or "\\documentclass" not in edit.latex:
        raise HTTPException(status_code=400, detail="No valid resume to edit.")
    if not edit.instruction.strip():
        raise HTTPException(status_code=400, detail="Tell me what to change.")

    resolved_key = _resolve_key(request, edit.api_key)

    async def events():
        try:
            agent = ResumeAgent(user_api_key=resolved_key)
            async for update in agent.edit(
                latex=edit.latex,
                instruction=edit.instruction,
                chat_history=[t.model_dump() for t in edit.history],
                job_description=edit.job_description,
            ):
                if await request.is_disconnected():
                    return
                yield _sse(update)
        except Exception as e:
            yield _sse({"step": "error", "message": _friendly_error(e)})

    return StreamingResponse(
        _keepalive(events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── Exports ──

def _check_latex_exportable(latex_content: str):
    if len(latex_content) > MAX_LATEX_CHARS:
        raise HTTPException(status_code=400, detail="Document is too large to export.")
    if _LATEX_DANGEROUS.search(latex_content):
        raise HTTPException(status_code=400, detail="Document contains file-access commands that aren't allowed.")


@app.post("/api/export/pdf")
async def export_pdf(latex_content: str = Form(...)):
    """Compile the LaTeX to PDF and report the page count.

    Uses local pdflatex when present; otherwise the hosted compiler
    (latex.ytotech.com), which means the resume content leaves this server.
    """
    _check_latex_exportable(latex_content)

    result = await compile_pdf(latex_content)
    if not result.ok or not result.pdf:
        raise HTTPException(
            status_code=400,
            detail=result.error or "LaTeX compilation failed — check the document for syntax errors.",
        )
    return Response(
        content=result.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=tailored_resume.pdf",
            "X-Page-Count": str(result.pages or ""),
            "Access-Control-Expose-Headers": "X-Page-Count",
        },
    )


@app.post("/api/export/tex")
async def export_tex(latex_content: str = Form(...)):
    return Response(
        content=latex_content,
        media_type="application/x-tex",
        headers={"Content-Disposition": "attachment; filename=tailored_resume.tex"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
