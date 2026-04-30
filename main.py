import os, json, httpx, re, base64, asyncio, io
from fastapi import FastAPI, Request, UploadFile, File, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── CONFIG ────────────────────────────────────────────────────────
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
SCRAPER_KEY    = os.getenv("SCRAPER_API_KEY", "49c37c3cf7160706fa45cfd94c68b39c")
VAPI_KEY        = os.getenv("VAPI_API_KEY", "6f419597-4f9f-49e2-b213-e338cb9b79ea")
VAPI_PHONE_ID   = os.getenv("VAPI_PHONE_NUMBER_ID", "1dd79fa5-a150-4eb1-a164-936b5c6b5f1a")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID", "06bf4f99-f38a-40b6-b3fb-0b3e55c8c71e")
APIFY_KEY      = os.getenv("APIFY_API_KEY", "")
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY", "")
REVIEWER_EMAIL = os.getenv("REVIEWER_EMAIL", "prathyusha.hyra@gmail.com")
AI_EMAIL       = "prathyusha.hyra@gmail.com"
APP_URL        = os.getenv("APP_URL", "http://localhost:8002")
MODEL          = "claude-sonnet-4-20250514"

HEADERS = {
    "x-api-key": ANTHROPIC_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json"
}
HEADERS_MCP = {**HEADERS, "anthropic-beta": "mcp-client-2025-04-04"}
HEADERS_PDF = {**HEADERS, "anthropic-beta": "pdfs-2024-09-25"}

GMAIL_MCP = {"type": "url", "url": "https://gmailmcp.googleapis.com/mcp/v1", "name": "gmail-mcp"}
GCAL_MCP  = {"type": "url", "url": "https://calendarmcp.googleapis.com/mcp/v1", "name": "gcal-mcp"}

# ── IN-MEMORY STORE ───────────────────────────────────────────────
candidates_db = {}

# ── AUTO-POLLER: checks active VAPI calls every 30s ───────────────
async def _poll_active_calls():
    """Background task — polls VAPI for any calls in phone_interview stage.
    This means webhook is NOT required — works even without ngrok/Railway."""
    while True:
        await asyncio.sleep(30)
        for cid, cand in list(candidates_db.items()):
            if cand.get("stage") != "phone_interview":
                continue
            call_id = cand.get("phone_interview", {}).get("call_id", "")
            if not call_id:
                continue
            # Already has transcript — skip
            if cand["phone_interview"].get("transcript"):
                continue
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(
                        f"https://api.vapi.ai/call/{call_id}",
                        headers={"Authorization": f"Bearer {VAPI_KEY}"}
                    )
                data = resp.json()
                status     = data.get("status", "")
                transcript = (data.get("transcript") or
                              data.get("artifact", {}).get("transcript") or "")
                summary    = data.get("summary", "")
                print(f"[POLLER] cid={cid} call={call_id} status={status} transcript={len(transcript)} summary={len(summary)}")
                if status in ("ended", "completed") and (transcript or summary):
                    cand["phone_interview"]["transcript"] = transcript
                    cand["phone_interview"]["summary"]    = summary
                    cand["stage"] = "phone_complete"
                    add_event(cid, f"Call ended (polled) — transcript: {len(transcript)} chars")
                    asyncio.create_task(_analyze_phone(cid))
            except Exception as e:
                print(f"[POLLER] error for {cid}: {e}")


async def _poll_inbox_for_looms():
    """Background task — checks Gmail every 5 min for Loom submissions from candidates in assignment_sent stage."""
    await asyncio.sleep(60)  # Wait 1 min after startup
    while True:
        try:
            waiting = [cid for cid, c in candidates_db.items() if c.get("stage") == "assignment_sent"]
            if waiting:
                print(f"[INBOX POLLER] Checking inbox for {len(waiting)} candidates awaiting Loom...")
                response = await ask_claude(
                    f"You are monitoring the Gmail inbox for {AI_EMAIL} as part of an AI hiring system.",
                    """Read the inbox and find any emails from candidates that contain a Loom video link (loom.com/share).
For each such email, extract:
- from: sender email address
- loom_url: the full loom.com/share URL

Return a JSON array only, no other text:
[{"from": "candidate@email.com", "loom_url": "https://www.loom.com/share/abc123"}]

If no Loom links found, return: []""",
                    max_tokens=800, mcp_servers=[GMAIL_MCP]
                )
                import re as _re
                match = _re.search(r"\[.*?\]", response, _re.DOTALL)
                submissions = json.loads(match.group(0)) if match else []
                for sub in submissions:
                    sender = sub.get("from", "").lower()
                    loom_url = sub.get("loom_url", "")
                    if not loom_url:
                        continue
                    for cid, cand in candidates_db.items():
                        if (cand.get("email","").lower() == sender and
                                cand.get("stage") == "assignment_sent" and
                                not cand.get("assignment",{}).get("loom_url")):
                            cand["assignment"]["loom_url"] = loom_url
                            cand["stage"] = "loom_submitted"
                            add_event(cid, f"[AUTO] Loom found in inbox: {loom_url}")
                            asyncio.create_task(_analyze_loom(cid, loom_url))
                            print(f"[INBOX POLLER] Found Loom for {cand['name']}: {loom_url}")
        except Exception as e:
            print(f"[INBOX POLLER] error: {e}")
        await asyncio.sleep(300)  # 5 minutes


@app.on_event("startup")
async def startup():
    asyncio.create_task(_poll_active_calls())
    asyncio.create_task(_poll_inbox_for_looms())
    print("[STARTUP] VAPI call poller started (every 30s)")
    print("[STARTUP] Inbox Loom poller started (every 5 min)")
settings_db = {
    "company": "Click Theory Capital",
    "role": "AI/ML Engineer",
    "phone_script": "You are Alex, a senior AI recruiter at Click Theory Capital.\nCandidate: {name} | Role: {role}\n\nSTRUCTURE (15 min):\n1. Warm welcome + Click Theory intro (1 min)\n2. Walk me through your most impressive AI project (3 min)\n3. How do you handle non-deterministic LLM output in production? (3 min)\n4. Describe a multi-step autonomous agent you built (3 min)\n5. Build a lead qualification agent in one week — your approach? (3 min)\n6. Why AI engineering + motivation (2 min)\n7. Close + next steps (1 min)\n\nBe warm but professional. Probe vague answers. End: 'We will be in touch within 48 hours.'\nDo not reveal you are an AI.",
    "assignment_template": "Hi {name},\n\nCongratulations — you have passed the interview stages for the {role} role at Click Theory Capital!\n\nFor the final stage, please complete this assignment within 72 hours:\n\nTASK: Build an AI-powered lead qualification agent\n1. Accept a list of company names as input\n2. Research each company using web search\n3. Score each lead against an ICP (B2B, 50-500 employees, tech-forward)\n4. Draft a personalized outreach email per lead\n5. Return structured JSON output\n\nTech: Python + any LLM API (Claude preferred)\nSubmit via: {submit_url}\n\nWe look forward to seeing what you build!\nThe Click Theory Capital Team",
    "job_profile": {
        "criteria": "3+ years Python. LLM API experience (OpenAI/Anthropic). Production AI deployment. Agentic frameworks (LangChain/CrewAI). RAG + vector databases.",
        "mustHave": "Production AI systems, LLM API hands-on, agent/workflow experience",
        "redFlags": "Tutorial-only experience, no production systems, no LLM API experience"
    }
}


# ══════════════════════════════════════════════════════════════════
# CLAUDE HELPERS
# ══════════════════════════════════════════════════════════════════

async def ask_claude(system: str, user: str, max_tokens: int = 2000, mcp_servers: list = None) -> str:
    """
    Calls Claude with an agentic loop when MCP servers are provided.
    MCP requires multiple turns: Claude emits tool_use → we send tool_result → Claude responds.
    Without the loop, Claude describes the action but never executes it.
    """
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}]
    }
    if mcp_servers:
        payload["mcp_servers"] = mcp_servers
    hdrs = HEADERS_MCP if mcp_servers else HEADERS

    all_text_parts = []

    async with httpx.AsyncClient(timeout=120) as client:
        for _turn in range(8):  # max 8 agentic turns
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=hdrs, json=payload)
            data = resp.json()

            if resp.status_code != 200:
                print(f"[CLAUDE] API error {resp.status_code}: {data}")
                break

            stop_reason = data.get("stop_reason", "")
            content     = data.get("content", [])

            # Collect text from this turn
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    all_text_parts.append(block["text"])
                elif block.get("type") == "mcp_tool_result":
                    for inner in block.get("content", []):
                        if inner.get("type") == "text" and inner.get("text"):
                            all_text_parts.append(inner["text"])

            # If stop_reason is end_turn or no tool use, we're done
            if stop_reason == "end_turn":
                break

            # Check if Claude wants to use tools
            tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]
            if not tool_use_blocks:
                break  # No tools to call, done

            # Build tool results — for MCP the results come back in the next assistant message
            # We append the assistant message and a user message with tool results
            payload["messages"].append({"role": "assistant", "content": content})

            tool_results = []
            for tb in tool_use_blocks:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb.get("id", ""),
                    "content": "Tool executed successfully"
                })

            payload["messages"].append({"role": "user", "content": tool_results})

    return "\n".join(p for p in all_text_parts if p)


@app.post("/api/stream")
async def stream_endpoint(request: Request):
    body = await request.json()
    async def generate():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", "https://api.anthropic.com/v1/messages",
                headers=HEADERS,
                json={"model": MODEL, "max_tokens": 2000, "stream": True,
                      "system": body.get("system", ""),
                      "messages": [{"role": "user", "content": body.get("user", "")}]}
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/ask")
async def ask_endpoint(request: Request):
    body = await request.json()
    text = await ask_claude(body.get("system", ""), body.get("user", ""),
                             mcp_servers=body.get("mcp_servers"))
    return JSONResponse({"text": text})


# ══════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════

@app.get("/api/settings")
async def get_settings():
    return JSONResponse(settings_db)

@app.post("/api/settings")
async def save_settings(request: Request):
    body = await request.json()
    settings_db.update(body)
    return JSONResponse({"ok": True})


# ══════════════════════════════════════════════════════════════════
# PDF EXTRACTION
# ══════════════════════════════════════════════════════════════════

@app.post("/api/extract")
async def extract_pdf(file: UploadFile = File(...)):
    content = await file.read()
    filename = file.filename or "resume.pdf"
    text = ""

    # Method 1: pdfplumber (fast, local, no API cost)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n\n".join(p for p in pages if p).strip()
        print(f"[pdfplumber] extracted {len(text)} chars from {filename}")
    except Exception as e:
        print(f"[pdfplumber] failed: {e}")

    # Method 2: Claude PDF API (fallback for scanned PDFs)
    if len(text) < 100:
        try:
            b64 = base64.b64encode(content).decode()
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=HEADERS_PDF,
                    json={"model": MODEL, "max_tokens": 3000,
                          "messages": [{"role": "user", "content": [
                              {"type": "document", "source": {
                                  "type": "base64",
                                  "media_type": "application/pdf",
                                  "data": b64
                              }},
                              {"type": "text", "text": "Extract ALL text from this resume. Return raw text only, preserve structure."}
                          ]}]}
                )
            data = resp.json()
            text = "".join(c.get("text", "") for c in data.get("content", []))
            print(f"[claude-pdf] extracted {len(text)} chars")
        except Exception as e:
            print(f"[claude-pdf] failed: {e}")

    if len(text) < 20:
        text = f"Could not extract text from {filename}. Please paste the resume text manually."

    return JSONResponse({"text": text.strip(), "filename": filename, "chars": len(text)})


# ══════════════════════════════════════════════════════════════════
# SCREEN RESUME
# ══════════════════════════════════════════════════════════════════

@app.post("/api/screen")
async def screen_resume(request: Request):
    body = await request.json()
    profile = settings_db.get("job_profile", {})
    raw = await ask_claude(
        f"""You are an expert AI/ML hiring manager at Click Theory Capital.
Screen this resume against the job profile below.
Job Profile: {json.dumps(profile)}

Return ONLY valid JSON, no markdown, no extra text:
{{
  "name": "",
  "email": "",
  "phone": "",
  "linkedin": "",
  "github": "",
  "google_scholar": "",
  "score": 0,
  "verdict": "Strong Hire|Hire|Maybe|No Hire",
  "summary": "one sentence",
  "dimensions": [
    {{"name": "AI/ML Experience", "score": 0, "note": ""}},
    {{"name": "Production Systems", "score": 0, "note": ""}},
    {{"name": "LLM/Agent Skills", "score": 0, "note": ""}},
    {{"name": "Python/Engineering", "score": 0, "note": ""}},
    {{"name": "Communication", "score": 0, "note": ""}}
  ],
  "strengths": [],
  "concerns": [],
  "assessment": "2-3 sentences",
  "outreach_email": {{"subject": "", "body": ""}},
  "rejection_email": {{"subject": "", "body": ""}}
}}""",
        f"Resume:\n{body.get('resume', '')}"
    )
    try:
        result = json.loads(raw.replace("```json", "").replace("```", "").strip())
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e), "raw": raw[:300]}, status_code=500)


# ══════════════════════════════════════════════════════════════════
# LINKEDIN / GITHUB / SCHOLAR ENRICHMENT
# ══════════════════════════════════════════════════════════════════

async def scrape_url(url: str, max_chars: int = 5000) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("http://api.scraperapi.com",
                params={"api_key": SCRAPER_KEY, "url": url, "render": "true"})
        clean = re.sub(r"<[^>]+>", " ", resp.text)
        return re.sub(r"\s+", " ", clean).strip()[:max_chars]
    except Exception as e:
        return ""


@app.post("/api/enrich")
async def enrich_candidate(request: Request):
    body = await request.json()
    name     = body.get("name", "")
    linkedin = body.get("linkedin", "")
    github   = body.get("github", "")
    scholar  = body.get("google_scholar", "")

    async def get_linkedin():
        url = linkedin if linkedin.startswith("http") else f"https://www.google.com/search?q={name.replace(' ','+')}+site:linkedin.com/in"
        return await scrape_url(url, 4000)

    async def get_github():
        url = github if github.startswith("http") else f"https://www.google.com/search?q={name.replace(' ','+')}+site:github.com"
        return await scrape_url(url, 4000)

    async def get_scholar():
        url = scholar if scholar.startswith("http") else f"https://scholar.google.com/scholar?q={name.replace(' ','+')}"
        return await scrape_url(url, 3000)

    li_text, gh_text, sch_text = await asyncio.gather(get_linkedin(), get_github(), get_scholar())

    combined = f"LINKEDIN:\n{li_text}\n\nGITHUB:\n{gh_text}\n\nSCHOLAR:\n{sch_text}"

    raw = await ask_claude(
        """Extract structured candidate intelligence from these scraped profiles.
Return ONLY valid JSON:
{
  "linkedin_profile": {"title": "", "company": "", "location": "", "skills": [], "experience": []},
  "github_profile": {"username": "", "top_languages": [], "notable_projects": [], "contribution_level": ""},
  "scholar_profile": {"papers": [], "citations": 0, "research_areas": []},
  "impressive_signals": [],
  "red_flags": [],
  "enrichment_score": 0
}""",
        f"Candidate: {name}\n\n{combined}"
    )
    try:
        return JSONResponse(json.loads(raw.replace("```json", "").replace("```", "").strip()))
    except:
        return JSONResponse({"enrichment_score": 0, "impressive_signals": [], "red_flags": []})


# ══════════════════════════════════════════════════════════════════
# CANDIDATE PIPELINE
# ══════════════════════════════════════════════════════════════════

@app.post("/api/pipeline/register")
async def register(request: Request):
    body = await request.json()
    cid = "c" + str(int(datetime.now().timestamp() * 1000))
    candidates_db[cid] = {
        "id": cid,
        "name": body.get("name", ""),
        "email": body.get("email", ""),
        "phone": body.get("phone", ""),
        "linkedin": body.get("linkedin", ""),
        "github": body.get("github", ""),
        "resume_text": body.get("resume_text", ""),
        "score": body.get("score", 0),
        "verdict": body.get("verdict", ""),
        "assessment": body.get("assessment", ""),
        "dimensions": body.get("dimensions", []),
        "enrichment": body.get("enrichment", {}),
        "stage": "profiled",
        "phone_interview": {"call_id": None, "transcript": None, "score": None},
        "video_interview": {"transcript": None, "score": None, "qa_log": []},
        "assignment": {"sent_at": None, "loom_url": None, "transcript": None, "score": None, "confidence": None},
        "final_score": None,
        "final_verdict": None,
        "human_decision": None,
        "created_at": datetime.now().isoformat(),
        "events": [{"time": datetime.now().isoformat(), "event": "Resume screened", "score": body.get("score", 0)}]
    }
    return JSONResponse({"candidate_id": cid, "candidate": candidates_db[cid]})


def add_event(cid: str, event: str, score=None):
    if cid in candidates_db:
        ev = {"time": datetime.now().isoformat(), "event": event}
        if score is not None:
            ev["score"] = score
        candidates_db[cid]["events"].append(ev)


# ══════════════════════════════════════════════════════════════════
# PHONE INTERVIEW
# ══════════════════════════════════════════════════════════════════

@app.post("/api/pipeline/phone/start")
async def start_phone(request: Request, background_tasks: BackgroundTasks):
    body  = await request.json()
    cid   = body.get("candidate_id", "")
    cand  = candidates_db.get(cid)
    if not cand:
        return JSONResponse({"error": "not found"}, status_code=404)
    phone = body.get("phone") or cand.get("phone", "")
    if not phone:
        return JSONResponse({"error": "no phone number"}, status_code=400)

    # Ensure phone has + prefix
    if not phone.startswith("+"):
        phone = "+" + phone

    script = settings_db["phone_script"].format(
        name=cand["name"], role=settings_db["role"])

    vapi_payload = {
        "phoneNumberId": VAPI_PHONE_ID,
        "customer": {
            "number": phone,
            "name": cand["name"]
        },
        "assistantId": VAPI_ASSISTANT_ID,
        "assistantOverrides": {
            "firstMessage": f"Hi, is this {cand['name']}? This is Alex calling from Click Theory Capital — I'm reaching out about the {settings_db['role']} position you applied for. Do you have about 15 minutes for a quick phone screen?"
        },
        "metadata": {"candidate_id": cid, "stage": "phone"}
    }

    print(f"[VAPI] Placing call to {phone} — payload: {json.dumps(vapi_payload)}")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.vapi.ai/call/phone",
            headers={"Authorization": f"Bearer {VAPI_KEY}", "content-type": "application/json"},
            json=vapi_payload
        )

    data = resp.json()
    print(f"[VAPI] Response: {json.dumps(data)[:400]}")

    if "error" in data or resp.status_code >= 400:
        err = data.get("message") or data.get("error") or str(data)
        add_event(cid, f"VAPI error: {err}")
        return JSONResponse({"error": err, "vapi_response": data}, status_code=400)

    call_id = data.get("id", "")
    candidates_db[cid]["phone_interview"]["call_id"] = call_id
    candidates_db[cid]["phone_interview"]["phone_used"] = phone
    candidates_db[cid]["stage"] = "phone_interview"
    add_event(cid, f"Phone call placed — ID: {call_id} — calling {phone}")
    return JSONResponse({"call_id": call_id, "status": "calling", "phone": phone})


@app.post("/api/pipeline/phone/analyze")
async def analyze_phone(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    cid  = body.get("candidate_id", "")
    tr   = body.get("transcript", "")
    if cid and cid in candidates_db and tr:
        candidates_db[cid]["phone_interview"]["transcript"] = tr
        candidates_db[cid]["stage"] = "phone_complete"
        background_tasks.add_task(_analyze_phone, cid)
    return JSONResponse({"status": "analyzing"})


async def _analyze_phone(cid: str):
    cand = candidates_db.get(cid)
    if not cand:
        return
    transcript = cand["phone_interview"].get("transcript", "")
    summary    = cand["phone_interview"].get("summary", "")
    content    = transcript if len(transcript) >= len(summary) else summary
    if not content:
        add_event(cid, "No transcript or summary to analyze")
        return

    add_event(cid, "Analyzing interview with Claude...")
    raw = await ask_claude(
        "You are a senior hiring manager at Click Theory Capital. Analyze this phone interview for an AI Engineer role. Return ONLY valid JSON: {" + chr(10) +
        '  "overall_score": 0,' + chr(10) +
        '  "verdict": "Advance|Maybe|Reject",' + chr(10) +
        '  "dimensions": [' + chr(10) +
        '    {"name": "Technical Depth", "score": 0, "note": ""},' + chr(10) +
        '    {"name": "Production Experience", "score": 0, "note": ""},' + chr(10) +
        '    {"name": "Communication", "score": 0, "note": ""},' + chr(10) +
        '    {"name": "Problem Solving", "score": 0, "note": ""},' + chr(10) +
        '    {"name": "Cultural Fit", "score": 0, "note": ""}' + chr(10) +
        '  ],' + chr(10) +
        '  "key_strengths": [],' + chr(10) +
        '  "concerns": [],' + chr(10) +
        '  "summary": "2-3 sentences",' + chr(10) +
        '  "recommendation": ""' + chr(10) +
        "}",
        f"Candidate: {cand['name']}\nRole: {settings_db['role']}\nResume score: {cand.get('score',0)}/100\n\nInterview content:\n{content}"
    )
    try:
        result = json.loads(raw.replace("```json", "").replace("```", "").strip())
        cand["phone_interview"]["score"]   = result
        cand["phone_interview"]["summary"] = result.get("summary", summary)
        cand["stage"] = "phone_analyzed"
        score   = result.get("overall_score", 0)
        verdict = result.get("verdict", "")
        add_event(cid, f"Phone analyzed — {score}/100 — {verdict}", score)
        if score >= 65 and verdict != "Reject":
            tmpl = email_templates_db.get("assignment", {})
            subj = tmpl.get("subject", "Next Step — Technical Assignment").format(
                name=cand["name"], role=settings_db["role"])
            body_txt = tmpl.get("body", settings_db["assignment_template"]).format(
                name=cand["name"], role=settings_db["role"],
                submit_url=f"{APP_URL}/submit/{cid}")
            await _send_email(cand["email"], subj, body_txt)
            cand["stage"] = "assignment_sent"
            add_event(cid, f"Assignment email sent to {cand['email']}")
        else:
            await _send_rejection(cid, "phone")
    except Exception as e:
        add_event(cid, f"Phone analysis error: {e}")
        print(f"[ANALYZE] raw: {raw[:200]}")


# ══════════════════════════════════════════════════════════════════
# TEXT + VOICE INTERVIEW (Claude questions + ElevenLabs TTS)
# ══════════════════════════════════════════════════════════════════

INTERVIEW_QUESTIONS = [
    "Tell me about yourself and your background in AI engineering.",
    "Walk me through your most impressive AI project in production.",
    "How do you handle non-deterministic LLM output in a production system?",
    "Describe a multi-step autonomous agent you have built — what was the architecture?",
    "If you had one week to build a lead qualification agent for us, what would your approach be?",
    "What excites you most about AI engineering right now?",
    "What questions do you have for us about the role at Click Theory Capital?"
]

@app.post("/api/interview/start")
async def interview_start(request: Request):
    """Start a text/voice interview session for a candidate"""
    body = await request.json()
    cid  = body.get("candidate_id", "")
    cand = candidates_db.get(cid)
    if not cand:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Reset video interview state
    cand["video_interview"] = {"transcript": None, "score": None, "qa_log": []}
    cand["stage"] = "video_interview"
    add_event(cid, "Text/voice interview session started")

    first_q = INTERVIEW_QUESTIONS[0]
    intro = f"Hi {cand['name']}! I am Alex from Click Theory Capital. I will be conducting your technical interview today for the {settings_db['role']} position. I will ask you {len(INTERVIEW_QUESTIONS)} questions. Take your time with each answer. Let us begin.\n\n{first_q}"

    cand["video_interview"]["qa_log"].append({
        "q_index": 0,
        "question": first_q,
        "answer": None
    })

    return JSONResponse({
        "question_index": 0,
        "question": first_q,
        "intro": intro,
        "total_questions": len(INTERVIEW_QUESTIONS)
    })


@app.post("/api/interview/answer")
async def interview_answer(request: Request, background_tasks: BackgroundTasks):
    """Submit answer to current question, get next question or finish"""
    body     = await request.json()
    cid      = body.get("candidate_id", "")
    answer   = body.get("answer", "").strip()
    q_index  = body.get("question_index", 0)
    cand     = candidates_db.get(cid)
    if not cand:
        return JSONResponse({"error": "not found"}, status_code=404)

    qa_log = cand["video_interview"].get("qa_log", [])

    # Save answer
    if qa_log and len(qa_log) > q_index:
        qa_log[q_index]["answer"] = answer
    else:
        qa_log.append({"q_index": q_index, "question": INTERVIEW_QUESTIONS[q_index] if q_index < len(INTERVIEW_QUESTIONS) else "", "answer": answer})

    next_index = q_index + 1

    # All questions answered — analyze
    if next_index >= len(INTERVIEW_QUESTIONS):
        cand["video_interview"]["qa_log"] = qa_log
        background_tasks.add_task(_analyze_video_interview, cid)
        return JSONResponse({
            "done": True,
            "message": f"Thank you {cand['name']}! That concludes the interview. We will analyze your responses and get back to you within 48 hours. You did great!",
            "total_questions": len(INTERVIEW_QUESTIONS)
        })

    # Next question
    next_q = INTERVIEW_QUESTIONS[next_index]

    # Claude generates a follow-up or transition
    transition = await ask_claude(
        "You are Alex, an AI recruiter. Given the candidate's answer, write ONE short acknowledgment sentence (10-15 words) then move to the next question naturally. Be warm and professional.",
        f"Candidate answer: {answer}\n\nNext question to ask: {next_q}\n\nWrite: [acknowledgment]. [next question]"
    )

    qa_log.append({"q_index": next_index, "question": next_q, "answer": None})
    cand["video_interview"]["qa_log"] = qa_log

    return JSONResponse({
        "done": False,
        "question_index": next_index,
        "question": next_q,
        "transition": transition or next_q,
        "total_questions": len(INTERVIEW_QUESTIONS)
    })


async def _analyze_video_interview(cid: str):
    cand = candidates_db.get(cid)
    if not cand:
        return
    qa_log = cand["video_interview"].get("qa_log", [])
    if not qa_log:
        return

    # Build transcript from QA log
    transcript = "\n\n".join([
        f"Q{i+1}: {qa.get('question','')}\nAnswer: {qa.get('answer','[no answer]')}"
        for i, qa in enumerate(qa_log)
    ])
    cand["video_interview"]["transcript"] = transcript

    raw = await ask_claude(
        f"""You are a senior hiring manager at Click Theory Capital.
Analyze this technical interview for the {settings_db['role']} role.
Return ONLY valid JSON:
{{
  "overall_score": 0,
  "verdict": "Advance|Maybe|Reject",
  "dimensions": [
    {{"name": "Technical Depth", "score": 0, "note": ""}},
    {{"name": "Production Experience", "score": 0, "note": ""}},
    {{"name": "Communication", "score": 0, "note": ""}},
    {{"name": "Problem Solving", "score": 0, "note": ""}},
    {{"name": "Cultural Fit", "score": 0, "note": ""}}
  ],
  "key_strengths": [],
  "concerns": [],
  "standout_answer": "best answer they gave",
  "summary": "2-3 sentence assessment",
  "recommendation": "specific next step"
}}""",
        f"Candidate: {cand['name']}\nRole: {settings_db['role']}\nResume Score: {cand.get('score',0)}/100\n\nInterview Q&A:\n{transcript}"
    )

    try:
        result = json.loads(raw.replace("```json","").replace("```","").strip())
        cand["video_interview"]["score"] = result
        cand["stage"] = "video_analyzed"
        score = result.get("overall_score", 0)
        add_event(cid, f"Video interview analyzed — {score}/100 — {result.get('verdict')}", score)

        # ── Compute consolidated final score ──
        r_score  = cand.get("score", 0)
        ph_score = (cand["phone_interview"].get("score") or {}).get("overall_score", 0)
        lo_score = (cand.get("assignment", {}).get("score") or {}).get("content_score", 0)
        lo_conf  = cand.get("assignment", {}).get("confidence", 0)
        vi_score = score

        if lo_score:
            # Full pipeline: Resume 20% + Phone 25% + Loom 20% + Confidence 5% + Video 30%
            final = int(r_score * 0.20 + ph_score * 0.25 + lo_score * 0.20 + lo_conf * 0.05 + vi_score * 0.30)
        else:
            # No Loom yet: Resume 25% + Phone 35% + Video 40%
            final = int(r_score * 0.25 + ph_score * 0.35 + vi_score * 0.40)

        cand["final_score"]   = final
        cand["final_verdict"] = result.get("verdict", "")
        cand["stage"]         = "ready_for_review"
        add_event(cid, f"Final score computed — {final}/100 — {cand['final_verdict']}", final)
        await _notify_reviewer(cid)

    except Exception as e:
        add_event(cid, f"Video analysis error: {e}")
        print(f"[VIDEO ANALYSIS] error: {e} raw: {raw[:200]}")


@app.post("/api/interview/tts")
async def interview_tts(request: Request):
    """Generate speech from text using ElevenLabs"""
    body = await request.json()
    text = body.get("text", "")
    if not text or not ELEVENLABS_KEY:
        return JSONResponse({"error": "no text or no ElevenLabs key"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.elevenlabs.io/v1/text-to-speech/N2lVS1w4EtoT3dr4eOWO",
                headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
                json={"text": text, "model_id": "eleven_turbo_v2",
                      "voice_settings": {"stability": 0.35, "similarity_boost": 0.80,
                                         "style": 0.45, "use_speaker_boost": True}}
            )
        if resp.status_code == 200:
            audio_b64 = base64.b64encode(resp.content).decode()
            return JSONResponse({"audio_b64": audio_b64, "format": "mp3"})
        else:
            return JSONResponse({"error": f"ElevenLabs error: {resp.status_code}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════════════════
# VAPI WEBHOOK
# ══════════════════════════════════════════════════════════════════

@app.post("/api/webhook/vapi")
async def vapi_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    print(f"[VAPI WEBHOOK] type={body.get('type','')} message_type={body.get('message',{}).get('type','')}")

    # VAPI sends either top-level type or nested message.type
    msg_type = body.get("type") or body.get("message", {}).get("type", "")

    if msg_type == "end-of-call-report":
        # Extract from both possible structures
        call_data = body.get("call") or body.get("message", {}).get("call", {}) or {}
        transcript = (body.get("transcript") or
                      body.get("message", {}).get("transcript") or
                      body.get("artifact", {}).get("transcript") or "")
        summary    = (body.get("summary") or
                      body.get("message", {}).get("summary") or "")
        meta       = call_data.get("metadata") or {}
        cid        = meta.get("candidate_id", "")

        print(f"[VAPI WEBHOOK] call ended — cid={cid} transcript_len={len(transcript)} summary_len={len(summary)}")

        if cid and cid in candidates_db:
            cand = candidates_db[cid]
            cand["phone_interview"]["transcript"] = transcript
            cand["phone_interview"]["summary"]    = summary
            cand["phone_interview"]["call_data"]  = call_data
            cand["stage"] = "phone_complete"
            add_event(cid, f"Call ended — transcript: {len(transcript)} chars — summary: {len(summary)} chars")
            if transcript or summary:
                background_tasks.add_task(_analyze_phone, cid)
            else:
                add_event(cid, "WARNING: No transcript received from VAPI")

    return JSONResponse({"received": True})


@app.get("/api/vapi/debug")
async def vapi_debug():
    """Fetch VAPI assistant + phone number details for debugging"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            asst = await client.get(f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}",
                                    headers={"Authorization": f"Bearer {VAPI_KEY}"})
            phone = await client.get(f"https://api.vapi.ai/phone-number/{VAPI_PHONE_ID}",
                                     headers={"Authorization": f"Bearer {VAPI_KEY}"})
            calls = await client.get("https://api.vapi.ai/call?limit=5",
                                     headers={"Authorization": f"Bearer {VAPI_KEY}"})
        return JSONResponse({
            "assistant": asst.json(),
            "phone_number": phone.json(),
            "recent_calls": calls.json()
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/pipeline/phone/status/{call_id}")
async def check_call_status(call_id: str, background_tasks: BackgroundTasks):
    """Poll VAPI for call status and transcript when webhook is not set up"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.vapi.ai/call/{call_id}",
                headers={"Authorization": f"Bearer {VAPI_KEY}"}
            )
        data = resp.json()
        status     = data.get("status", "")
        transcript = data.get("transcript") or data.get("artifact", {}).get("transcript") or ""
        summary    = data.get("summary", "")

        print(f"[POLL] call={call_id} status={status} transcript={len(transcript)} summary={len(summary)}")

        # Find candidate by call_id
        cid = None
        for c_id, c in candidates_db.items():
            if c.get("phone_interview", {}).get("call_id") == call_id:
                cid = c_id
                break

        if cid and status in ("ended", "completed") and (transcript or summary):
            cand = candidates_db[cid]
            if not cand["phone_interview"].get("transcript"):
                cand["phone_interview"]["transcript"] = transcript
                cand["phone_interview"]["summary"]    = summary
                cand["stage"] = "phone_complete"
                add_event(cid, f"Call status polled — transcript: {len(transcript)} chars")
                background_tasks.add_task(_analyze_phone, cid)

        return JSONResponse({"status": status, "transcript_len": len(transcript),
                             "summary_len": len(summary), "candidate_id": cid})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════════════════
# LOOM SUBMISSION
# ══════════════════════════════════════════════════════════════════

@app.get("/submit/{cid}")
async def submit_page(cid: str):
    cand = candidates_db.get(cid)
    name = cand["name"] if cand else "Candidate"
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>Submit — Click Theory Capital</title>
<style>*{{box-sizing:border-box}}body{{font-family:-apple-system,sans-serif;max-width:480px;margin:4rem auto;padding:2rem;background:#09090b;color:#e4e4e7}}
h1{{font-size:22px;font-weight:600;margin-bottom:.4rem}}p{{color:#a1a1aa;line-height:1.65;margin-bottom:1.5rem;font-size:14px}}
input{{width:100%;padding:.8rem;border:1px solid #27272a;border-radius:8px;font-size:14px;margin-bottom:1rem;background:#18181b;color:#e4e4e7}}
button{{background:#22c55e;color:#000;border:none;padding:.8rem 1.5rem;border-radius:8px;font-size:14px;cursor:pointer;width:100%;font-weight:600}}
button:disabled{{opacity:.4}}.ok{{background:#052e16;border:1px solid #166534;color:#22c55e;padding:1rem;border-radius:8px;display:none;margin-top:1rem;font-size:14px}}</style></head>
<body><h1>Submit Your Assignment</h1>
<p>Hi {name}! Record a Loom video walking through your AI agent build, then paste the link below.</p>
<input type="url" id="u" placeholder="https://www.loom.com/share/..."/>
<button id="b" onclick="sub()">Submit Video</button>
<div class="ok" id="ok">Submitted! You will hear back within 24 hours.</div>
<script>
async function sub(){{
  const u=document.getElementById('u').value.trim();
  if(!u||!u.includes('loom')){{alert('Paste a valid Loom URL');return;}}
  const b=document.getElementById('b');b.disabled=true;b.textContent='Submitting...';
  await fetch('/api/pipeline/loom/submit',{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify({{candidate_id:'{cid}',loom_url:u}})}});
  document.getElementById('ok').style.display='block';b.textContent='Submitted';
}}</script></body></html>""")


@app.post("/api/pipeline/loom/submit")
async def loom_submit(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    cid  = body.get("candidate_id", "")
    url  = body.get("loom_url", "")
    if cid and cid in candidates_db:
        candidates_db[cid]["assignment"]["loom_url"] = url
        candidates_db[cid]["stage"] = "loom_submitted"
        add_event(cid, f"Loom submitted: {url}")
        background_tasks.add_task(_analyze_loom, cid, url)
    return JSONResponse({"status": "received"})


async def _fetch_loom_apify(url: str) -> str:
    if not APIFY_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            start = await client.post(
                "https://api.apify.com/v2/acts/automation-lab~loom-scraper/runs",
                headers={"Authorization": f"Bearer {APIFY_KEY}", "Content-Type": "application/json"},
                json={"videoUrls": [url]}
            )
            run_id = start.json().get("data", {}).get("id", "")
            if not run_id:
                return ""
            for _ in range(18):
                await asyncio.sleep(10)
                poll = await client.get(
                    f"https://api.apify.com/v2/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {APIFY_KEY}"}
                )
                status = poll.json().get("data", {}).get("status", "")
                if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                    break
            if status != "SUCCEEDED":
                return ""
            items = (await client.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
                headers={"Authorization": f"Bearer {APIFY_KEY}"}
            )).json()
            if not items:
                return ""
            item = items[0]
            raw = item.get("transcript", "")
            tr = " ".join(s.get("text", "") for s in raw) if isinstance(raw, list) else str(raw)
            meta = f"[{item.get('title','')} | {item.get('duration','')}]\n\n"
            return (meta + tr).strip()
    except Exception as e:
        print(f"Apify error: {e}")
        return ""


async def _analyze_loom(cid: str, loom_url: str):
    cand = candidates_db.get(cid)
    if not cand:
        return
    transcript = await _fetch_loom_apify(loom_url)
    if not transcript:
        transcript = await scrape_url(loom_url, 6000)
    cand["assignment"]["transcript"] = transcript or "[No transcript available]"

    raw = await ask_claude(
        """Analyze this Loom video assignment for an AI Engineer candidate.
Video analysis is not available — this is based on transcript/content only.
Return ONLY valid JSON:
{
  "content_score": 0,
  "confidence_score": 0,
  "verdict": "Strong Hire|Hire|Maybe|Reject",
  "dimensions": [
    {"name": "Technical Execution", "score": 0, "note": ""},
    {"name": "Agent Quality", "score": 0, "note": ""},
    {"name": "Communication", "score": 0, "note": ""},
    {"name": "Problem Choice", "score": 0, "note": ""},
    {"name": "Code Quality", "score": 0, "note": ""}
  ],
  "what_they_built": "",
  "summary": "",
  "human_review_notes": "VIDEO REVIEW REQUIRED — AI analysis based on transcript only"
}""",
        f"Candidate: {cand['name']}\nResume score: {cand.get('score',0)}/100\n\nTranscript:\n{cand['assignment']['transcript']}"
    )
    try:
        result = json.loads(raw.replace("```json", "").replace("```", "").strip())
        cand["assignment"]["score"] = result
        cand["assignment"]["confidence"] = result.get("confidence_score", 0)
        add_event(cid, f"Loom analyzed — {result.get('content_score',0)}/100 — {result.get('verdict','')}")

        # Generate harder technical interview questions based on what they built in the Loom
        what_they_built = result.get("what_they_built", "")
        loom_summary    = result.get("summary", "")
        tech_qs_raw = await ask_claude(
            f"""You are a senior technical interviewer at Click Theory Capital evaluating an AI Engineer candidate.
The candidate submitted a Loom video of their assignment. Based on what they built, generate 6 challenging follow-up technical questions.
Questions should:
- Probe technical depth on choices they made
- Challenge their architecture decisions
- Test edge cases they may not have considered
- Be harder than standard interview questions
- Be specific to what they built (not generic)

Return ONLY valid JSON:
{{
  "questions": [
    "question 1",
    "question 2",
    "question 3",
    "question 4",
    "question 5",
    "question 6"
  ],
  "focus_areas": ["area1", "area2"]
}}""",
            f"Candidate: {cand['name']}\nRole: {settings_db['role']}\n\nWhat they built: {what_they_built}\nLoom summary: {loom_summary}\nLoom transcript excerpt: {(cand['assignment'].get('transcript',''))[:2000]}"
        )
        try:
            qs_data = json.loads(tech_qs_raw.replace("```json","").replace("```","").strip())
            cand["assignment"]["technical_questions"] = qs_data.get("questions", [])
            cand["assignment"]["focus_areas"] = qs_data.get("focus_areas", [])
            add_event(cid, f"Generated {len(cand['assignment']['technical_questions'])} technical follow-up questions from Loom")
        except Exception as e:
            cand["assignment"]["technical_questions"] = []
            print(f"[LOOM] Failed to generate questions: {e}")

        cand["stage"] = "loom_complete"
        # Send email to candidate to join video interview room
        submit_url = f"{APP_URL}/submit/{cid}"
        interview_url = f"{APP_URL}?iv={cid}"
        await _send_email(cand["email"],
            f"Final Stage — Technical Video Interview — {settings_db['role']} — Click Theory Capital",
            f"""Hi {cand['name']},

Congratulations — your Loom assignment has been reviewed and we're impressed with your work!

You have been selected for the final stage: a Technical Video Interview with our AI interviewer Alex.

This is a more in-depth technical session that will build on your assignment. Expect harder questions about your architectural choices and technical decisions.

To start your video interview, click here:
{interview_url}

The interview takes approximately 20-25 minutes. You can use text or voice to answer.

We look forward to seeing you!
The Click Theory Capital Team""")
        add_event(cid, f"Technical interview invitation sent to {cand['email']}")

    except Exception as e:
        add_event(cid, f"Loom error: {e}")


# ══════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════

async def _send_email(to: str, subject: str, body: str):
    if not to:
        print(f"[EMAIL] Skipped — no recipient address")
        return
    try:
        system = (
            f"You are an automated email assistant for {AI_EMAIL} at Click Theory Capital. "
            f"Your ONLY job is to send emails using the Gmail tool. "
            f"Always call the Gmail send tool immediately with the exact details provided. "
            f"Do not ask for confirmation. Do not modify the content."
        )
        prompt = (
            f"Send an email RIGHT NOW using the Gmail send tool with exactly these details:\n\n"
            f"To: {to}\n"
            f"Subject: {subject}\n"
            f"Body:\n{body}\n\n"
            f"Call the Gmail send tool now."
        )
        result = await ask_claude(system, prompt, max_tokens=500, mcp_servers=[GMAIL_MCP])
        print(f"[EMAIL] ✓ Sent to {to} | Subject: {subject[:60]} | MCP result: {result[:120]}")
    except Exception as e:
        print(f"[EMAIL] ✗ Error sending to {to}: {e}")


async def _send_rejection(cid: str, stage: str):
    cand = candidates_db.get(cid)
    if not cand or not cand.get("email"):
        return
    tmpl = email_templates_db.get("rejection", {})
    subj = tmpl.get("subject", "Click Theory Capital — Application Update").format(
        name=cand["name"], role=settings_db.get("role",""))
    body_txt = tmpl.get("body", "").format(
        name=cand["name"], role=settings_db.get("role",""))
    await _send_email(cand["email"], subj, body_txt)
    candidates_db[cid]["stage"] = f"rejected_{stage}"
    add_event(cid, f"Rejection sent after {stage}")


async def _notify_reviewer(cid: str):
    cand = candidates_db.get(cid)
    if not cand:
        return
    asgn   = cand.get("assignment", {})
    lo_sc  = asgn.get("score", {}) or {}
    vi_sc  = (cand.get("video_interview", {}) or {}).get("score", {}) or {}
    ph_sc  = (cand["phone_interview"].get("score") or {})
    focus  = asgn.get("focus_areas", [])

    dims_phone = "\n".join([f"  {d['name']}: {d['score']}/10 — {d.get('note','')}"
                            for d in ph_sc.get("dimensions", [])]) if ph_sc else "  N/A"
    dims_video = "\n".join([f"  {d['name']}: {d['score']}/10 — {d.get('note','')}"
                            for d in vi_sc.get("dimensions", [])]) if vi_sc else "  N/A"

    body = f"""CANDIDATE READY FOR HUMAN REVIEW
{'='*50}
Name:   {cand['name']}
Email:  {cand['email']}
Phone:  {cand.get('phone','')}

══ CONSOLIDATED SCORES ══
  Resume:         {cand.get('score',0)}/100
  Phone Screen:   {ph_sc.get('overall_score','—')}/100
  Loom Assignment:{lo_sc.get('content_score','—')}/100
  AI Confidence:  {asgn.get('confidence','—')}%
  Video Interview:{vi_sc.get('overall_score','—')}/100
  ─────────────────────
  FINAL SCORE:    {cand.get('final_score','—')}/100
  VERDICT:        {cand.get('final_verdict','')}

══ PHONE INTERVIEW ══
Score: {ph_sc.get('overall_score','—')}/100 | {ph_sc.get('verdict','')}
Summary: {ph_sc.get('summary','')}
Strengths: {', '.join(ph_sc.get('key_strengths',[]))}
Concerns: {', '.join(ph_sc.get('concerns',[]))}
{dims_phone}

══ LOOM ASSIGNMENT ══
Score: {lo_sc.get('content_score','—')}/100
Built: {lo_sc.get('what_they_built','')}
Summary: {lo_sc.get('summary','')}
Focus Areas Tested: {', '.join(focus)}
Loom URL: {asgn.get('loom_url','')}
NOTE: {lo_sc.get('human_review_notes','Video review required — AI analysis from transcript only')}

══ TECHNICAL VIDEO INTERVIEW ══
Score: {vi_sc.get('overall_score','—')}/100 | {vi_sc.get('verdict','')}
Summary: {vi_sc.get('summary','')}
Standout Answer: {vi_sc.get('standout_answer','')}
Strengths: {', '.join(vi_sc.get('key_strengths',[]))}
Concerns: {', '.join(vi_sc.get('concerns',[]))}
{dims_video}

══ RECOMMENDATION ══
{vi_sc.get('recommendation','') or ph_sc.get('recommendation','')}

Dashboard: {APP_URL}
"""
    await _send_email(REVIEWER_EMAIL,
        f"[REVIEW READY] {cand['name']} — Final: {cand.get('final_score','?')}/100 — {cand.get('final_verdict','')}",
        body)
    add_event(cid, "Consolidated report emailed to reviewer")


# ══════════════════════════════════════════════════════════════════
# INBOX CHECK
# ══════════════════════════════════════════════════════════════════

@app.post("/api/check-inbox")
async def check_inbox(background_tasks: BackgroundTasks):
    """Read last 5 emails in Gmail and find Loom links from candidates"""
    try:
        response = await ask_claude(
            f"You are monitoring the Gmail inbox for {AI_EMAIL} as part of an AI hiring system. "
            f"Use the Gmail tool to read the inbox.",
            """Use the Gmail search/list tool to fetch the 5 most recent emails in the inbox.
For each email, check if it contains a loom.com/share link.

Return ONLY a JSON array with this structure (no other text):
[
  {{"from": "sender@email.com", "subject": "email subject", "loom_url": "https://www.loom.com/share/abc123", "snippet": "short preview"}}
]

If an email has no Loom link, still include it with loom_url as null.
If inbox is empty, return: []""",
            max_tokens=1500, mcp_servers=[GMAIL_MCP]
        )
        print(f"[INBOX CHECK] Raw response: {response[:400]}")

        # Parse JSON from response
        import re as _re
        match = _re.search(r"\[.*?\]", response, _re.DOTALL)
        emails = json.loads(match.group(0)) if match else []

        processed = []
        all_cand_emails = {c.get("email","").lower(): cid
                           for cid, c in candidates_db.items() if c.get("email")}

        for email_item in emails:
            loom_url = email_item.get("loom_url")
            sender   = email_item.get("from", "").lower()
            # Strip angle brackets if present e.g. "Name <email@x.com>"
            sender_clean = _re.search(r'[\w.+-]+@[\w.-]+', sender)
            sender_email = sender_clean.group(0).lower() if sender_clean else sender

            if loom_url and "loom.com" in loom_url:
                cid = all_cand_emails.get(sender_email)
                if cid and cid in candidates_db:
                    cand = candidates_db[cid]
                    if not cand.get("assignment", {}).get("loom_url"):
                        cand["assignment"]["loom_url"] = loom_url
                        cand["stage"] = "loom_submitted"
                        add_event(cid, f"Loom found in inbox from {sender_email}: {loom_url}")
                        background_tasks.add_task(_analyze_loom, cid, loom_url)
                        processed.append({"cid": cid, "name": cand["name"], "loom_url": loom_url})
                else:
                    print(f"[INBOX] Loom found from {sender_email} but no matching candidate")

        return JSONResponse({
            "emails_read": len(emails),
            "loom_found": len([e for e in emails if e.get("loom_url")]),
            "processed": processed,
            "raw_emails": emails
        })
    except Exception as e:
        print(f"[INBOX CHECK] Error: {e}")
        return JSONResponse({"emails_read": 0, "error": str(e)})


# ══════════════════════════════════════════════════════════════════
# HUMAN DECISION
# ══════════════════════════════════════════════════════════════════

@app.post("/api/pipeline/decision")
async def human_decision(request: Request):
    body     = await request.json()
    cid      = body.get("candidate_id", "")
    decision = body.get("decision", "")
    cand     = candidates_db.get(cid)
    if not cand:
        return JSONResponse({"error": "not found"}, status_code=404)
    cand["human_decision"] = decision
    cand["human_notes"]    = body.get("notes", "")
    cand["stage"]          = "hired" if decision == "hire" else "passed"
    add_event(cid, f"Human decision: {decision.upper()}")
    if decision == "hire":
        tmpl = email_templates_db.get("offer", {})
        subj = tmpl.get("subject", "Offer — Click Theory Capital").format(
            name=cand["name"], role=settings_db.get("role",""))
        body_txt = tmpl.get("body", "").format(
            name=cand["name"], role=settings_db.get("role",""))
        await _send_email(cand["email"], subj, body_txt)
    return JSONResponse({"status": "ok", "stage": cand["stage"]})


# ══════════════════════════════════════════════════════════════════
# CANDIDATE CRUD
# ══════════════════════════════════════════════════════════════════

@app.get("/api/candidates")
async def get_candidates():
    return JSONResponse(list(candidates_db.values()))

@app.get("/api/candidate/{cid}")
async def get_candidate(cid: str):
    c = candidates_db.get(cid)
    return JSONResponse(c if c else {"error": "not found"})

@app.delete("/api/candidate/{cid}")
async def delete_candidate(cid: str):
    candidates_db.pop(cid, None)
    return JSONResponse({"ok": True})


# ══════════════════════════════════════════════════════════════════
# EMAIL TEMPLATES (editable)
# ══════════════════════════════════════════════════════════════════

email_templates_db = {
    "assignment": {
        "subject": "Next Step — Technical Assignment — {role} — Click Theory Capital",
        "body": "Hi {name},\n\nCongratulations — you have passed the phone interview for the {role} role at Click Theory Capital!\n\nFor the next stage, please complete this technical assignment within 72 hours:\n\nTASK: Build an AI-powered lead qualification agent\n1. Accept a list of company names as input\n2. Research each company using web search\n3. Score each lead against an ICP (B2B, 50-500 employees, tech-forward)\n4. Draft a personalized outreach email per lead\n5. Return structured JSON output\n\nTech: Python + any LLM API (Claude preferred)\nSubmit your Loom walkthrough at: {submit_url}\n\nWe look forward to seeing what you build!\n\nBest,\nAlex\nClick Theory Capital Recruiting"
    },
    "interview_invite": {
        "subject": "Final Stage — Technical Video Interview — {role} — Click Theory Capital",
        "body": "Hi {name},\n\nCongratulations — your Loom assignment has been reviewed and we're impressed!\n\nYou've been selected for the final stage: a Technical Video Interview with our AI interviewer Alex.\n\nThis is a deeper technical session that will build on your assignment. Expect harder questions about your architectural choices and technical decisions.\n\nStart your video interview here:\n{interview_url}\n\nThe interview takes approximately 20-25 minutes. You can use text or voice to answer.\n\nWe look forward to speaking with you!\n\nBest,\nAlex\nClick Theory Capital Recruiting"
    },
    "offer": {
        "subject": "Offer — {role} — Click Theory Capital 🎉",
        "body": "Hi {name},\n\nWe are thrilled to extend you an offer for the {role} role at Click Theory Capital!\n\nYour performance throughout the interview process was outstanding. Someone from our team will reach out within 24 hours to discuss compensation, start date, and next steps.\n\nWelcome to the team!\n\nBest,\nThe Click Theory Capital Team"
    },
    "rejection": {
        "subject": "Click Theory Capital — Application Update",
        "body": "Hi {name},\n\nThank you for your time and interest in the {role} role at Click Theory Capital.\n\nAfter careful consideration, we have decided to move forward with other candidates at this time. This was a competitive process and we appreciate you taking the time to interview with us.\n\nWe will keep your profile on file for future opportunities.\n\nBest of luck,\nAlex\nClick Theory Capital Recruiting"
    },
    "outreach": {
        "subject": "Exciting AI Engineering Role at Click Theory Capital",
        "body": "Hi {name},\n\nI came across your profile and was impressed by your background in AI engineering. We have an exciting {role} role at Click Theory Capital that I think could be a great fit.\n\nWe're building cutting-edge AI systems for B2B sales intelligence. The role involves LLM APIs, agentic frameworks, and production AI deployment.\n\nWould you be open to a quick 15-minute call to learn more?\n\nBest,\nAlex\nClick Theory Capital Recruiting"
    }
}

@app.get("/api/email/templates")
async def get_email_templates():
    return JSONResponse(email_templates_db)

@app.post("/api/email/templates")
async def save_email_templates(request: Request):
    body = await request.json()
    email_templates_db.update(body)
    return JSONResponse({"ok": True})

@app.post("/api/email/send")
async def send_email_direct(request: Request):
    """Direct email send with full agentic loop debug output"""
    body    = await request.json()
    to      = body.get("to", "")
    subject = body.get("subject", "")
    msg     = body.get("body", "")
    if not to or not subject:
        return JSONResponse({"ok": False, "error": "missing to/subject"}, status_code=400)
    try:
        system = (
            f"You are an automated email assistant for {AI_EMAIL} at Click Theory Capital. "
            f"Your ONLY job is to send the email using the Gmail send tool. "
            f"Call the Gmail send tool immediately with the exact details provided."
        )
        prompt = (
            f"Send this email using Gmail:\n\nTo: {to}\nSubject: {subject}\nBody:\n{msg}"
        )
        payload = {
            "model": MODEL,
            "max_tokens": 800,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "mcp_servers": [GMAIL_MCP]
        }
        all_tools_called = []
        all_text = []
        tool_results_received = []

        async with httpx.AsyncClient(timeout=120) as client:
            for turn in range(8):
                resp = await client.post("https://api.anthropic.com/v1/messages",
                                         headers=HEADERS_MCP, json=payload)
                data = resp.json()
                stop_reason = data.get("stop_reason", "")
                content = data.get("content", [])

                print(f"[EMAIL DEBUG] Turn {turn+1} stop={stop_reason} blocks={len(content)}")
                for b in content:
                    btype = b.get("type","")
                    print(f"  block: {btype} name={b.get('name','')} id={b.get('id','')[:12] if b.get('id') else ''}")
                    if btype == "text":
                        all_text.append(b.get("text",""))
                    elif btype == "tool_use":
                        all_tools_called.append({"name": b.get("name",""), "input": b.get("input",{})})
                    elif btype == "mcp_tool_result":
                        for inner in b.get("content",[]):
                            if inner.get("type") == "text":
                                tool_results_received.append(inner.get("text",""))

                if stop_reason == "end_turn" or not any(b.get("type") == "tool_use" for b in content):
                    break

                payload["messages"].append({"role": "assistant", "content": content})
                tool_results = [{"type": "tool_result", "tool_use_id": b["id"], "content": "ok"}
                                for b in content if b.get("type") == "tool_use"]
                payload["messages"].append({"role": "user", "content": tool_results})

        success = len(all_tools_called) > 0
        print(f"[EMAIL DEBUG] Done. Tools called: {[t['name'] for t in all_tools_called]}")
        return JSONResponse({
            "ok": success,
            "sent_to": to,
            "tools_called": all_tools_called,
            "tool_results": tool_results_received,
            "response_text": "\n".join(all_text),
            "warning": "" if success else "No tools were called — Gmail MCP may not be connected or authorized"
        })
    except Exception as e:
        print(f"[EMAIL DIRECT] Error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ══════════════════════════════════════════════════════════════════
# TEST ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.post("/api/test/send-email")
async def test_send_email(request: Request):
    """Send a test email via Gmail MCP — used by interview room to send scorecards"""
    body = await request.json()
    to      = body.get("to", REVIEWER_EMAIL)
    subject = body.get("subject", "Test Email from HiringOS")
    msg     = body.get("body", "This is a test email.")
    try:
        result = await ask_claude(
            f"You are the AI recruiting system for Click Theory Capital using Gmail account {AI_EMAIL}. Send emails exactly as instructed. Call the Gmail send tool immediately.",
            f"Send this email NOW using Gmail:\n\nTo: {to}\nSubject: {subject}\n\nBody:\n{msg}\n\nCall the Gmail send tool now.",
            max_tokens=400, mcp_servers=[GMAIL_MCP]
        )
        print(f"[EMAIL TEST] Sent to {to} | Result: {result[:100]}")
        return JSONResponse({"ok": True, "sent_to": to, "result": result[:300]})
    except Exception as e:
        print(f"[EMAIL TEST] Error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/test/gmail")
async def test_gmail():
    try:
        result = await ask_claude(
            f"You have Gmail access for {AI_EMAIL}.",
            "List subjects of the 3 most recent inbox emails as JSON array of strings.",
            max_tokens=300, mcp_servers=[GMAIL_MCP]
        )
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/api/test/calendar")
async def test_calendar():
    try:
        result = await ask_claude(
            f"You have Google Calendar access for {AI_EMAIL}.",
            "List events in the next 7 days as a short JSON summary.",
            max_tokens=300, mcp_servers=[GCAL_MCP]
        )
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════════
# ELEVENLABS VIDEO INTERVIEW (Claude Q&A + ElevenLabs TTS)
# ══════════════════════════════════════════════════════════════════

ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY", "")
# Elliot — energetic, cheerful, professional male voice
ELEVENLABS_VOICE = "N2lVS1w4EtoT3dr4eOWO"

# Interview questions per stage
IV_QUESTIONS = [
    "To start, can you walk me through your most impressive AI project in production?",
    "How do you handle non-deterministic LLM output in a production system? Walk me through your approach.",
    "Describe the architecture of a multi-step autonomous agent you have built.",
    "If you had one week to build a lead qualification agent for a B2B sales team, what is your exact approach?",
    "What is your experience with RAG systems and vector databases specifically?",
    "Why AI engineering? What excites you most about this field right now?",
]

@app.post("/api/interview/tts")
async def interview_tts(request: Request):
    """Convert text to speech using ElevenLabs — returns audio bytes"""
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)

    if ELEVENLABS_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}",
                    headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
                    json={"text": text, "model_id": "eleven_turbo_v2",
                          "voice_settings": {"stability": 0.35, "similarity_boost": 0.80,
                                             "style": 0.45, "use_speaker_boost": True}}
                )
            if resp.status_code == 200:
                audio_b64 = base64.b64encode(resp.content).decode()
                return JSONResponse({"audio": audio_b64, "type": "audio/mpeg"})
        except Exception as e:
            print(f"ElevenLabs TTS error: {e}")

    # Fallback — return empty, browser will use Web Speech API
    return JSONResponse({"audio": None, "fallback": True, "text": text})


@app.post("/api/interview/evaluate")
async def interview_evaluate(request: Request):
    """Claude evaluates full interview Q&A and returns score"""
    body = await request.json()
    cid     = body.get("candidate_id", "")
    qa_log  = body.get("qa_log", [])  # [{q, a}, ...]
    cand    = candidates_db.get(cid)
    name    = cand["name"] if cand else body.get("name", "Candidate")
    role    = settings_db.get("role", "AI/ML Engineer")

    qa_text = "\n\n".join([f"Q: {item['q']}\nA: {item['a']}" for item in qa_log])

    raw = await ask_claude(
        f"""You are a senior hiring manager at Click Theory Capital evaluating a video interview for a {role} role.
Analyze these question-answer pairs and return ONLY valid JSON:
{{
  "overall_score": 0-100,
  "verdict": "Strong Hire|Hire|Maybe|Reject",
  "dimensions": [
    {{"name": "Technical Depth", "score": 0-10, "note": ""}},
    {{"name": "Production Experience", "score": 0-10, "note": ""}},
    {{"name": "Communication", "score": 0-10, "note": ""}},
    {{"name": "Problem Solving", "score": 0-10, "note": ""}},
    {{"name": "Cultural Fit", "score": 0-10, "note": ""}}
  ],
  "key_strengths": [],
  "concerns": [],
  "standout_answer": "best answer they gave",
  "summary": "2-3 sentence assessment",
  "recommendation": "specific next step"
}}""",
        f"Candidate: {name}\nRole: {role}\n\nInterview Q&A:\n{qa_text}"
    )
    try:
        result = json.loads(raw.replace("```json", "").replace("```", "").strip())

        # Store in candidate profile if cid provided
        if cid and cid in candidates_db:
            candidates_db[cid]["video_interview"] = {
                "qa_log": qa_log,
                "score": result,
                "completed_at": datetime.now().isoformat()
            }
            candidates_db[cid]["stage"] = "video_complete"
            add_event(cid, f"Video interview complete — {result.get('overall_score')}/100 — {result.get('verdict')}",
                      result.get("overall_score"))

        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e), "raw": raw[:300]}, status_code=500)


@app.get("/api/interview/questions")
async def get_questions():
    return JSONResponse({"questions": IV_QUESTIONS})

@app.get("/api/interview/questions/{cid}")
async def get_questions_for_candidate(cid: str):
    """Get technical questions for a specific candidate.
    If they have Loom-generated questions, use those (harder, specific to their build).
    Otherwise fall back to standard questions."""
    cand = candidates_db.get(cid)
    if cand:
        loom_qs = (cand.get("assignment") or {}).get("technical_questions", [])
        if loom_qs and len(loom_qs) >= 4:
            what_built = (cand.get("assignment") or {}).get("score", {}).get("what_they_built", "")
            focus = (cand.get("assignment") or {}).get("focus_areas", [])
            return JSONResponse({
                "questions": loom_qs,
                "mode": "technical",
                "context": f"Based on their assignment: {what_built}",
                "focus_areas": focus
            })
    return JSONResponse({"questions": IV_QUESTIONS, "mode": "standard"})


# ══════════════════════════════════════════════════════════════════
# SERVE
# ══════════════════════════════════════════════════════════════════

@app.get("/interview")
async def interview_page():
    return FileResponse("static/interview.html")


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/health")
async def health():
    return {"status": "ok", "candidates": len(candidates_db), "model": MODEL}
