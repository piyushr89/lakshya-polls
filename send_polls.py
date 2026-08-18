"""
send_polls.py  —  Arjuna JEE 2.0 Automation
===============================================
Modes:
  --mode=motivation  (8 AM daily)        → motivation quote text → all groups
  --mode=quiz        (1 PM Mon-Fri)      → intro + 5 PYQ polls → all groups
  --mode=checkin     (5 PM daily)        → daily checkin / Saturday weekly review
  --mode=solution    (10 PM Mon-Fri)     → 5 solution messages → all groups
  --mode=college     (3 PM Mon-Wed-Fri)  → random IIT campus photo → all groups

All student-facing text is now generated/written in Hindi, typed in the
Roman/English alphabet (i.e. Hinglish-texting style, NOT Devanagari, NOT
plain English). Internal admin email alerts are left in English since
those are only for you.

GitHub Secrets required:
  PW_TOKEN, GROQ_API_KEY, ALERT_EMAIL, GMAIL_APP_PWD
  GDRIVE_SA_JSON    — full JSON of Google service account key
  GDRIVE_FOLDER_ID  — ID of the Drive folder containing college photos
"""

import os, sys, json, random, time, argparse, smtplib, traceback, re
from datetime import date, datetime
from pathlib import Path
from email.mime.text import MIMEText

import requests
from groq import Groq

# ─── SECRETS ──────────────────────────────────────────────────────────────────

PW_TOKEN         = os.environ["PW_TOKEN"]
GROQ_API_KEY     = os.environ["GROQ_API_KEY"]
ALERT_EMAIL      = os.environ.get("ALERT_EMAIL", "")
GMAIL_APP_PWD    = os.environ.get("GMAIL_APP_PWD", "")
GDRIVE_SA_JSON   = os.environ.get("GDRIVE_SA_JSON", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")

# ─── PW API CONFIG ────────────────────────────────────────────────────────────

BASE_URL  = "https://api.penpencil.co"
CLIENT_ID = "5eb393ee95fab7468a79d189"
BATCH_ID = "6a3b6bd9b08a189f3e37cb59"

HEADERS = {
    "Authorization": f"Bearer {PW_TOKEN}",
    "client-id":     CLIENT_ID,
    "client-type":   "WEB",
    "origin":        "https://www.pw.live",
    "referer":       "https://www.pw.live/",
    "x-sdk-version": "0.0.28",
    "randomid":      "2f81cbed-4d22-4f57-994e-3f78dbf6e309",
}

JSON_HEADERS = {**HEADERS, "Content-Type": "application/json"}

# ─── GROUPS ───────────────────────────────────────────────────────────────────

GROUPS = [
    {"name": "Group 1", "groupId": "6a6c68608e0d0ed8af94391e", "conversationId": "6a6c68731835c5e9d05b6ee9"},
    {"name": "Group 2", "groupId": "6a6c68669f98d00a9f573d76", "conversationId": "6a6c68764555218f708ecc82"},
    {"name": "Group 3", "groupId": "6a703e7927d4e967fe9107ef", "conversationId": "6a703e7a8779ade78b5f7348"},
]

# ─── SUBJECT ROTATION ─────────────────────────────────────────────────────────

SUBJECT_MIXES = [
    ("Physics",   "Physics",   "Chemistry", "Chemistry", "Maths"),
    ("Maths",     "Maths",     "Physics",   "Chemistry", "Chemistry"),
    ("Chemistry", "Chemistry", "Maths",     "Maths",     "Physics"),
    ("Physics",   "Maths",     "Chemistry", "Physics",   "Maths"),
    ("Maths",     "Physics",   "Physics",   "Chemistry", "Maths"),
]

# ─── FILE PATHS ───────────────────────────────────────────────────────────────

HISTORY_FILE     = Path("history.json")
TODAY_Q_FILE     = Path("todays_questions.json")
SENT_PHOTOS_FILE = Path("sent_photos.json")
PDF_DIR          = Path("pdfs")
BANK_DIR         = Path("pdfs/banks")

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def send_alert(subject, body):
    if not ALERT_EMAIL or not GMAIL_APP_PWD:
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = ALERT_EMAIL
        msg["To"]      = ALERT_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(ALERT_EMAIL, GMAIL_APP_PWD)
            s.send_message(msg)
        log("Alert email sent.")
    except Exception as e:
        log(f"[WARN] Email alert failed: {e}")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ─── GROQ JSON PARSING ────────────────────────────────────────────────────────

def clean_latex(text: str) -> str:
    text = re.sub(r'\\([a-zA-Z]+)', r' \1 ', text)
    text = re.sub(r'\\(?!["\\/bfnrtu])', r' ', text)
    return text


def extract_questions_from_groq(raw: str) -> list:
    """
    Robustly extract and VALIDATE a list of question dicts from Groq response.
    Handles: plain array, wrapped object, question1/question2 keys, markdown fences.
    """
    # Strip markdown fences
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("[") or stripped.startswith("{"):
                raw = stripped
                break

    raw = clean_latex(raw).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"[WARN] JSON parse failed: {e} — Raw: {raw[:200]}")
        return []

    questions = []

    if isinstance(parsed, list):
        # Plain array — ideal case
        questions = parsed

    elif isinstance(parsed, dict):
        # Case 1: wrapped array e.g. {"questions": [...]}
        for val in parsed.values():
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                questions = val
                break

        # Case 2: Groq used "question1", "question2"... keys
        # e.g. {"question1": {...}, "question2": {...}}
        if not questions:
            numbered = []
            for key, val in parsed.items():
                if isinstance(val, dict) and (
                    key.startswith("question") or
                    key.startswith("q") or
                    key[0].isdigit()
                ):
                    numbered.append(val)
            if numbered:
                log(f"[INFO] Detected numbered question keys — extracted {len(numbered)} items")
                questions = numbered

        # Case 3: fallback — collect any dict values that look like questions
        if not questions:
            for val in parsed.values():
                if isinstance(val, dict) and (
                    "question" in val or "question_text" in val
                ):
                    questions.append(val)
            if questions:
                log(f"[INFO] Extracted {len(questions)} questions from dict values")

    # Validate each question — only keep fully valid ones
    valid = []
    for q in questions:
        if not isinstance(q, dict):
            continue

        # Fix alternate key names
        if "question_text" in q and "question" not in q:
            q["question"] = q.pop("question_text")
        if "answer_options" in q and "options" not in q:
            q["options"] = q.pop("answer_options")
        if "answer" in q and "correct" not in q:
            q["correct"] = q.pop("answer")

        # Validate question text
        if not q.get("question") or not str(q["question"]).strip():
            log(f"[WARN] Rejected: missing question text")
            continue

        # Validate options
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) < 4:
            log(f"[WARN] Rejected: bad options → {str(q.get('question',''))[:50]}")
            continue
        # Check options are not empty placeholders
        if any(not str(o).strip() for o in opts[:4]):
            log(f"[WARN] Rejected: empty option → {str(q.get('question',''))[:50]}")
            continue

        # Validate correct
        correct = q.get("correct")
        if not isinstance(correct, int) or not (1 <= correct <= 4):
            log(f"[WARN] Rejected: bad correct={correct} → {str(q.get('question',''))[:50]}")
            continue

        # Validate solution
        if not q.get("solution") or not str(q["solution"]).strip():
            q["solution"] = "Standard JEE solution dekh lo."

        q["options"] = [str(o) for o in opts[:4]]
        valid.append(q)

    return valid


# ─── PW: SEND TEXT MESSAGE ────────────────────────────────────────────────────

def send_message(group, text) -> bool:
    """Returns True if message sent successfully, False otherwise."""
    if not text or not text.strip():
        return False
    payload = {
        "batchId":   BATCH_ID,
        "groupId":   group["groupId"],
        "role":      "Mentor",
        "type":      "text",
        "text":      text,
        "filePages": 0,
    }
    try:
        r = requests.post(
            f"{BASE_URL}/v1/conversation/{group['conversationId']}/chat",
            headers=JSON_HEADERS, json=payload, timeout=15
        )
        if r.status_code in (200, 201):
            log(f"  ✅ Message → {group['name']}")
            time.sleep(1)
            return True
        elif r.status_code == 401:
            log(f"  ❌ Token expired → {group['name']} — update PW_TOKEN in GitHub Secrets!")
            time.sleep(1)
            return False
        else:
            log(f"  ⚠️  Message failed → {group['name']}: {r.status_code} {r.text[:150]}")
            time.sleep(1)
            return False
    except Exception as e:
        log(f"  ❌ Message error → {group['name']}: {e}")
        time.sleep(1)
        return False


# ─── PW: UPLOAD + SEND IMAGE ──────────────────────────────────────────────────

def upload_image(image_path: str) -> str:
    path = Path(image_path)
    log(f"Uploading image: {path.name} ({path.stat().st_size // 1024} KB)")
    with open(path, "rb") as f:
        files = {"file": (path.name, f, "image/jpeg")}
        r = requests.post(
            f"{BASE_URL}/v1/files",
            headers=HEADERS, files=files, timeout=30
        )
    if r.status_code in (200, 201):
        data     = r.json()
        image_id = (
            data.get("data", {}).get("_id")
            or data.get("data", {}).get("imageId")
            or data.get("_id")
            or data.get("imageId")
        )
        if not image_id:
            raise Exception(f"imageId not found in response: {data}")
        log(f"✅ Image uploaded → imageId: {image_id}")
        return image_id
    raise Exception(f"Upload failed: {r.status_code} {r.text[:300]}")


def send_image_message(group, image_id, file_size_kb) -> bool:
    payload = {
        "batchId":   BATCH_ID,
        "groupId":   group["groupId"],
        "role":      "Mentor",
        "type":      "image",
        "imageId":   image_id,
        "filePages": 0,
        "fileSize":  file_size_kb,
    }
    success = False
    try:
        r = requests.post(
            f"{BASE_URL}/v1/conversation/{group['conversationId']}/chat",
            headers=JSON_HEADERS, json=payload, timeout=15
        )
        if r.status_code in (200, 201):
            log(f"  ✅ Image sent → {group['name']}")
            success = True
        else:
            log(f"  ⚠️  Image failed → {group['name']}: {r.status_code} {r.text[:150]}")
    except Exception as e:
        log(f"  ❌ Image error → {group['name']}: {e}")
    time.sleep(1)
    return success


# ─── PW: SEND POLL (TWO-STEP) ─────────────────────────────────────────────────

def send_poll(group, question) -> bool:
    """Returns True if the poll was successfully posted, False otherwise."""
    options = question.get("options", [])
    correct = question.get("correct")

    # Safety check
    if not options or len(options) < 4 or not correct or not (1 <= correct <= 4):
        log(f"  ⚠️  Skipping malformed poll: {str(question.get('question',''))[:50]}")
        return False
    if not question.get("question", "").strip():
        log(f"  ⚠️  Skipping poll with empty question text")
        return False

    # ── STEP 1: Create poll → get pollId ──────────────────────
    create_payload = {
        "type":          "SINGLE",
        "entityType":    "mentorship",
        "entityId":      group["groupId"],
        "poll_question": question["question"],
        "correctOption": [correct],
        "pollOptions": [
            {"option_id": i + 1, "option_label": opt, "option_description": opt}
            for i, opt in enumerate(options)
        ],
    }
    try:
        r1 = requests.post(
            f"{BASE_URL}/v2/poll/create-poll",
            headers=JSON_HEADERS, json=create_payload, timeout=15
        )
        log(f"  [DEBUG] create-poll → status={r1.status_code} body={r1.text[:500]}")
        if r1.status_code not in (200, 201):
            log(f"  ⚠️  Poll create failed → {group['name']}: {r1.status_code} {r1.text[:200]}")
            return False
        poll_data = r1.json().get("data", {})
        poll_id   = poll_data.get("pollId")
        if not poll_id:
            log(f"  ⚠️  No pollId in response → {group['name']}: {r1.text[:150]}")
            return False
    except Exception as e:
        log(f"  ❌ Poll create error → {group['name']}: {e}")
        return False

    time.sleep(0.5)

    # ── STEP 2: Post poll into group chat ─────────────────────
    poll_options_str = json.dumps({
        "pollId":        poll_id,
        "type":          "SINGLE",
        "pollOptions": [
            {"option_id": i + 1, "option_label": opt, "option_description": opt}
            for i, opt in enumerate(options)
        ],
        "correctOption": [correct],
    })
    chat_payload = {
        "batchId":     BATCH_ID,
        "groupId":     group["groupId"],
        "role":        "Mentor",
        "text":        question["question"],
        "type":        "poll",
        "pollOptions": poll_options_str,
    }
    success = False
    try:
        r2 = requests.post(
            f"{BASE_URL}/v1/conversation/{group['conversationId']}/chat",
            headers=JSON_HEADERS, json=chat_payload, timeout=15
        )
        log(f"  [DEBUG] chat post → status={r2.status_code} body={r2.text[:500]}")
        if r2.status_code in (200, 201):
            log(f"  ✅ Poll sent → {group['name']}: {question['question'][:55]}...")
            success = True
        else:
            log(f"  ⚠️  Poll chat failed → {group['name']}: {r2.status_code} {r2.text[:200]}")
    except Exception as e:
        log(f"  ❌ Poll chat error → {group['name']}: {e}")

    time.sleep(1.5)
    return success


# ─── GOOGLE DRIVE HELPERS ─────────────────────────────────────────────────────

def get_questions_drive_filename() -> str:
    """Filename includes today's date — e.g. arjuna_questions_2026-08-07.json"""
    return f"arjuna_questions_{date.today()}.json"

def get_drive_service(readonly=True):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_info = json.loads(GDRIVE_SA_JSON)
    scope   = "https://www.googleapis.com/auth/drive.readonly" if readonly else "https://www.googleapis.com/auth/drive"
    creds   = service_account.Credentials.from_service_account_info(
        sa_info, scopes=[scope]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_json_to_drive(data: dict):
    """Save questions JSON to a fixed file in Drive — overwrites each day."""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    service  = get_drive_service(readonly=False)
    content  = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    media    = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/json")

    # Check if file already exists in the folder
    filename = get_questions_drive_filename()
    results  = service.files().list(
        q=f"'{GDRIVE_FOLDER_ID}' in parents and name='{filename}' and trashed=false",
        fields="files(id, name)"
    ).execute()
    existing = results.get("files", [])

    if existing:
        service.files().update(
            fileId=existing[0]["id"],
            media_body=media
        ).execute()
        log(f"✅ Updated {filename} in Drive")
    else:
        metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
        service.files().create(
            body=metadata,
            media_body=media,
            fields="id"
        ).execute()
        log(f"✅ Created {filename} in Drive")


def download_json_from_drive() -> dict:
    """Read today's questions JSON from Drive."""
    import io

    service = get_drive_service(readonly=True)
    filename = get_questions_drive_filename()
    results  = service.files().list(
        q=f"'{GDRIVE_FOLDER_ID}' in parents and name='{filename}' and trashed=false",
        fields="files(id, name)"
    ).execute()
    files = results.get("files", [])

    if not files:
        raise FileNotFoundError(f"{filename} not found in Drive — did quiz mode run today?")

    from googleapiclient.http import MediaIoBaseDownload
    request  = service.files().get_media(fileId=files[0]["id"])
    fh       = io.BytesIO()
    dl       = MediaIoBaseDownload(fh, request)
    done     = False
    while not done:
        _, done = dl.next_chunk()
    fh.seek(0)
    return json.loads(fh.read().decode("utf-8"))


def list_drive_photos(service):
    results = service.files().list(
        q=f"'{GDRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
        fields="files(id, name)",
        pageSize=500
    ).execute()
    return results.get("files", [])


def download_drive_photo(service, file_id, dest_path):
    from googleapiclient.http import MediaIoBaseDownload
    import io
    request = service.files().get_media(fileId=file_id)
    fh      = io.FileIO(dest_path, "wb")
    dl      = MediaIoBaseDownload(fh, request)
    done    = False
    while not done:
        _, done = dl.next_chunk()
    fh.close()


# ─── VERIFIED PYQ BANK (hand-transcribed from official PW PDFs) ──────────────

def load_question_bank(subject: str) -> list:
    """Loads verified PYQ questions for a subject from pdfs/banks/<subject>.json
    (transcribed + answer-key-checked from the official Arjuna JEE Hindi 2.0
    PDFs). Returns [] if no bank file exists yet for that subject."""
    fname = BANK_DIR / f"{subject.lower()}.json"
    if not fname.exists():
        return []
    try:
        data = json.loads(fname.read_text(encoding="utf-8"))
        return data.get("questions", [])
    except Exception as e:
        log(f"[WARN] Failed to load bank for {subject}: {e}")
        return []


def pick_from_bank(subject: str, history: dict, already_picked: set) -> dict:
    """Picks one not-yet-used question for `subject` from the local verified
    bank. Returns None if the bank is empty or every question in it has
    already been used (checked against history.json + this run's picks)."""
    bank = load_question_bank(subject)
    used = set(history.get("used", []))
    random.shuffle(bank)
    for q in bank:
        qhash = str(hash(q.get("question", "")[:50]))
        if qhash not in used and qhash not in already_picked:
            q = {**q, "subject": subject}
            already_picked.add(qhash)
            return q
    return None


def generate_explanation(question: dict) -> str:
    """For bank questions that don't have a written solution yet: asks Groq
    to EXPLAIN the already-verified correct answer, not determine it. The
    correct option itself comes from the official PW answer key, not from
    Groq — this call can only get the explanation wording wrong, not the
    answer itself."""
    opts    = question.get("options", [])
    correct = question.get("correct", 1)
    letters = ["A", "B", "C", "D"]
    correct_letter = letters[correct - 1] if 1 <= correct <= 4 else "?"
    correct_text   = opts[correct - 1] if opts else ""

    prompt = f"""This is a verified JEE {question.get('subject','')} PYQ — the answer is already confirmed from the official answer key. Write only a short, clear explanation of how this answer is reached.

Question: {question.get('question','')}
Options: {opts}
Correct Answer: ({correct_letter}) {correct_text}

Rules:
- LANGUAGE: Write in Hindi using Devanagari script (देवनागरी), matching the question's own script — not Roman/English letters
- 3-5 steps, plain text
- Do not change or second-guess the answer — only explain why it is correct
- No backslashes or LaTeX

Return ONLY the explanation text (no JSON, no markdown, no backticks)."""

    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log(f"[WARN] Explanation generation failed: {e}")
        return "Solution jald hi add hoga."


# ─── GROQ CLIENT ──────────────────────────────────────────────────────────────

groq_client = Groq(api_key=GROQ_API_KEY)

# Groq deprecated llama-3.3-70b-versatile (and llama-3.1-8b-instant) on
# 17 Jun 2026. openai/gpt-oss-120b is Groq's recommended replacement.
# If this model ever gets deprecated too, this is the only line to change.
GROQ_MODEL = "openai/gpt-oss-120b"


def sample_pyq_text(subject, chars=3000):
    fname = PDF_DIR / f"{subject.lower()}_pyq.txt"
    if not fname.exists():
        return f"[No PYQ file for {subject} — use general JEE knowledge]"
    text  = fname.read_text(encoding="utf-8", errors="ignore")
    if len(text) <= chars:
        return text
    seed  = date.today().toordinal() * 100 + hash(subject) % 100
    random.seed(seed)
    start = random.randint(0, len(text) - chars)
    chunk = text[start: start + chars]
    nl    = chunk.find("\n")
    return chunk[nl:].strip() if nl > 0 else chunk.strip()


# ─── GROQ: GENERATE QUESTIONS ─────────────────────────────────────────────────

def generate_questions(subjects):
    subject_list  = "\n".join(f"Q{i+1}: {s}" for i, s in enumerate(subjects))
    pyq_samples   = {s: sample_pyq_text(s) for s in set(subjects)}
    context_block = "\n\n".join(
        f"=== {s} PYQ SAMPLE ===\n{t}" for s, t in pyq_samples.items()
    )
    prompt = f"""You are a JEE question expert. Generate exactly 5 JEE PYQ questions.

Subject assignment:
{subject_list}

PYQ MATERIAL:
{context_block}

RULES:
- LANGUAGE: Write the "question", "options", and "solution" fields in Hindi
  using Devanagari script (देवनागरी) — matching how the official JEE Hindi-medium
  papers are written. NOT Roman/English letters, NOT plain English.
  Keep numbers, units, chemical symbols, formulas, and variable names unchanged —
  only the surrounding sentence structure should be Hindi.
- Each question MUST include exam year and session tag e.g. [JEE Main 2022 June S1] or [JEE Adv 2019 P2] — keep this tag as-is in standard English format
- 4 options per question (A B C D) — specific values, not placeholders
- correct is 1-4 (1=A, 2=B, 3=C, 4=D)
- solution: 3-5 step working, written in Hindi (Devanagari) as described above
- CRITICAL: Do NOT use LaTeX backslashes like \\alpha \\frac \\theta \\sqrt
- DO NOT USE QUESTIONS WHERE IMAGES ARE REFERRED OR PRESENT
- Write math in plain text: "alpha" not "\\alpha", "x^2" not "x squared"
- Backslashes break JSON parsing — plain text only

Return ONLY a JSON array of exactly 5 objects, no markdown, no backticks:
[
  {{
    "subject": "Physics",
    "year_tag": "[JEE Main 2023 Jan S2]",
    "question": "[JEE Main 2023 Jan S2] full question text here, in Hindi (Devanagari)",
    "options": ["A text", "B text", "C text", "D text"],
    "correct": 2,
    "solution": "Step 1: ...\\nStep 2: ...\\nAnswer: B"
  }}
]"""

    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        log(f"[DEBUG] Groq raw (first 150): {raw[:150]}")
        return extract_questions_from_groq(raw)
    except Exception as e:
        log(f"[WARN] Groq call failed: {e}")
        return []


# ─── GROQ: INTRO MESSAGE ──────────────────────────────────────────────────────

def generate_intro_message(subjects):
    subject_str = ", ".join(subjects)
    prompt = f"""Write a short energetic motivational message before a JEE daily quiz.

Today's subjects: {subject_str}
Today's date: {date.today().strftime('%A, %d %B %Y')}

Rules:
- 1-2 lines max
- Mention today's subjects naturally
- End with hype to answer the polls
- Sound like a real caring teacher/mentor
- LANGUAGE: Hindi written in Roman/English script only (like WhatsApp Hinglish texting) — NOT Devanagari, NOT plain English. Fresh and different every day
- Don't use any quotes "" or ''

Return ONLY the message text."""
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=150,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log(f"[WARN] Intro message generation failed: {e}")
        return f"Aaj ka quiz taiyar hai — {subject_str}! Chalo shuru karte hain 💪"


# ─── GROQ: MOTIVATION QUOTE ───────────────────────────────────────────────────

def generate_motivation_quote():
    system = """You write deeply authentic motivational quotes for JEE/IIT aspirants
in HINDI, typed using the Roman/English alphabet — the way Indian students text
Hindi on WhatsApp (Hinglish script). NOT Devanagari script. NOT plain English.

Raw, real — like something a topper or struggling student actually thinks while studying late.

RULES:
- Hindi (Roman script) only — e.g. style like "Har mock mein rank girta hai, lekin himmat nahi girni chahiye"
- Specific to JEE: mock ranks, rank drops, late night studying, Kota pressure, PCM, parents sacrifices
- 1-4 lines max. Punchy.
- Make the student FEEL seen, not lectured
- BANNED: generic cliches — "Kabhi haar mat maano", "Khud par vishwas rakho", "Mehnat karo", or their English equivalents like "Never give up", "Believe in yourself", "Work hard"
- AVOID these words entirely: doubt, quit, fail, die, kill, blood, 3 AM, midnight, alone, hopeless
- Keep it intense but clean — PW has a content filter

Return ONLY JSON: {"quote": "quote text"}"""

    categories = [
        "discipline_and_consistency", "exam_pressure_and_fear",
        "parents_sacrifice", "comeback_after_failure",
        "late_night_study_grind", "mock_test_mindset",
        "iit_dream_visualization", "competition_mindset",
        "time_management", "mental_toughness",
    ]
    cat = categories[date.today().toordinal() % len(categories)]
    resp = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": f"Category: {cat}\nSeed: {date.today().toordinal()}"},
        ],
        temperature=0.88,
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content.strip()
    try:
        return json.loads(raw).get("quote", raw)
    except Exception:
        return raw


# ─── GROQ: COLLEGE CAPTION ────────────────────────────────────────────────────

def generate_college_caption(photo_name):
    prompt = f"""Write a short punchy caption to send with an IIT campus photo to JEE aspirants.

Photo filename hint: {photo_name}
Seed for variety: {date.today().toordinal()}

Rules:
- 1-2 lines max
- Make the student WANT to be there
- LANGUAGE: Hindi written in Roman/English script only (Hinglish texting style) — not plain English
- No hashtags

Return ONLY the caption text."""
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.92,
            max_tokens=100,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log(f"[WARN] Caption generation failed: {e}")
        return "Ek din yahaan padhoge tum bhi 🎓"


# ─── GROQ: DAILY CHECKIN ──────────────────────────────────────────────────────

def generate_daily_checkin_message():
    prompt = f"""Write a warm engaging message to JEE aspirants at 5 PM asking:
1. How their day is going
2. Whether they covered today's study target

Today is {date.today().strftime('%A, %d %B %Y')}.
Seed: {date.today().toordinal()}

Rules:
- Sound like a caring mentor
- Casual and warm tone
- 1-2 lines max
- LANGUAGE: Hindi written in Roman/English script only (like WhatsApp Hinglish texting) — not plain English
- End with invitation to reply

Return ONLY the message text."""
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.92,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log(f"[WARN] Checkin message generation failed: {e}")
        return "Aaj ka din kaisa raha? Aaj ka target cover ho gaya? Batao! 😊"


# ─── GROQ: WEEKLY REVIEW ──────────────────────────────────────────────────────

def generate_weekly_review_message():
    prompt = f"""Write an engaging message to JEE aspirants at 5 PM on Saturday asking them to:
1. Rate their week out of 10
2. Share how their week went

Week ending: {date.today().strftime('%d %B %Y')}
Seed: {date.today().toordinal()}

Rules:
- Warm reflective tone
- Make students feel safe to share honestly
- 2-3 lines max
- LANGUAGE: Hindi written in Roman/English script only (like WhatsApp Hinglish texting) — not plain English

Return ONLY the message text."""
    try:
        resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.92,
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log(f"[WARN] Weekly review message generation failed: {e}")
        return "Hafta kaisa raha? 1-10 mein rate karo aur batao kya achha raha, kya improve karna hai 🙂"


# ─── MODE: MOTIVATION (8 AM daily) ───────────────────────────────────────────

def run_motivation():
    log("=== MODE: MOTIVATION (8 AM) ===")

    # Try generating a quote, with fallback if PW blocks it
    quote = None
    for attempt in range(3):
        try:
            q = generate_motivation_quote()
            # Pre-screen: avoid words PW commonly blocks
            blocked_words = ["3 AM", "3AM", "doubt", "quit", "fail", "die", "kill", "blood"]
            if any(w.lower() in q.lower() for w in blocked_words):
                log(f"[WARN] Quote contains potentially blocked word, retrying... (attempt {attempt+1})")
                time.sleep(1)
                continue
            quote = q
            break
        except Exception as e:
            log(f"[WARN] Quote generation failed attempt {attempt+1}: {e}")
            time.sleep(1)

    if not quote:
        quote = "Aaj ek aur mauka hai apne IIT dream ke aur kareeb jaane ka. Focused raho, consistent raho. Tum kar sakte ho! 💪"
        log("[WARN] Using fallback quote")

    log(f"Quote: {quote[:80]}...")
    msg = f"🌅 Suprabhat, Arjuna JEE 2.0!\n\n{quote}\n\n— Lage raho. Tumhara IIT tumhara wait kar raha hai. 💪"

    success = 0
    fail    = 0
    token_expired = False

    for group in GROUPS:
        log(f"Sending to {group['name']}...")
        ok = send_message(group, msg)
        if ok:
            success += 1
        else:
            # Check if it's a token issue
            fail += 1
            # Try fallback if prohibited word
            fallback = "🌅 Suprabhat, Arjuna JEE 2.0!\n\nAaj strong start karo. Jo bhi problem solve karoge, wo tumhe IIT rank ke ek kadam aur kareeb le jayega. Lage raho! 💪"
            ok2 = send_message(group, fallback)
            if ok2:
                success += 1
                fail -= 1

    log(f"Motivation results: {success}/5 sent, {fail}/5 failed")

    if success == 0:
        msg_alert = (
            f"❌ Morning Motivation FAILED — 0/5 groups received the message.\n"
            f"Most likely cause: PW_TOKEN expired.\n\n"
            f"Fix: pw.live → group → create poll → copy Authorization header → "
            f"GitHub Secrets → PW_TOKEN → Update\n\n"
            f"Quote attempted: {quote}"
        )
        log(f"❌ {msg_alert}")
        send_alert("❌ Morning Motivation FAILED — Token likely expired", msg_alert)
        sys.exit(1)
    elif fail > 0:
        log("⚠️  Motivation sent to some groups.")
        send_alert(
            f"⚠️ Morning Motivation — {fail} groups failed",
            f"Sent: {success}/5\nFailed: {fail}/5\nDate: {date.today()}\nQuote: {quote}"
        )
    else:
        log("✅ Motivation mode complete.")
        send_alert(
            "✅ Morning Motivation Sent",
            f"Sent to all 5 groups.\nDate: {date.today()}\nQuote: {quote}"
        )


# ─── MODE: QUIZ (1 PM Mon-Fri) ───────────────────────────────────────────────

def run_quiz():
    log("=== MODE: QUIZ (1 PM) ===")

    history  = load_json(HISTORY_FILE, {"used": []})
    weekday  = date.today().weekday()
    subjects = list(SUBJECT_MIXES[weekday % len(SUBJECT_MIXES)])
    log(f"Today's subjects: {subjects}")

    questions = []
    attempts  = 0

    # ── Step 1: prefer verified questions from the local PYQ bank ──────────
    already_picked = set()
    needed_from_groq = []
    for subj in subjects:
        q = pick_from_bank(subj, history, already_picked)
        if q:
            questions.append(q)
            log(f"  📚 Bank [{subj}]: {q.get('question','')[:60]}...")
        else:
            needed_from_groq.append(subj)

    # ── Step 2: Groq fills whatever the bank couldn't cover ────────────────
    if needed_from_groq:
        log(f"Need {len(needed_from_groq)} more via Groq (bank empty/exhausted for: {needed_from_groq})...")

    while len(questions) < 5 and attempts < 8 and needed_from_groq:
        attempts += 1
        needed = 5 - len(questions)
        log(f"Attempt {attempts}: need {needed} more question(s)...")

        qs = generate_questions(needed_from_groq)

        # Add only valid questions not already collected
        existing_texts = {q["question"] for q in questions}
        for q in qs:
            if len(questions) >= 5:
                break
            if q["question"] not in existing_texts:
                questions.append(q)
                existing_texts.add(q["question"])

        log(f"  Got {len(qs)} valid this attempt — total so far: {len(questions)}/5")

        if len(questions) < 5:
            time.sleep(2)

    # Save whatever we have (even partial) so solution mode can still run
    if questions:
        partial = {
            "date": str(date.today()),
            "questions": questions[:5]
        }
        save_json(TODAY_Q_FILE, partial)
        try:
            upload_json_to_drive(partial)
        except Exception:
            pass
        log(f"💾 Saved {min(len(questions),5)} questions")

    if len(questions) < 3:
        msg = f"Quiz failed — only {len(questions)}/5 valid questions after {attempts} attempts."
        log(f"❌ {msg}")
        send_alert("❌ Arjuna Quiz FAILED", msg)
        sys.exit(1)

    if len(questions) < 5:
        log(f"⚠️  Only {len(questions)}/5 questions — proceeding with what we have")

    questions = questions[:5]

    log(f"✅ Got {len(questions)} questions.")
    for i, q in enumerate(questions):
        log(f"   Q{i+1} [{q.get('subject','')}]: {q.get('question','N/A')[:65]}...")

    intro = generate_intro_message(subjects)
    log(f"Intro: {intro[:80]}...")

    total_sent = 0
    total_fail = 0

    for group in GROUPS:
        log(f"\n── {group['name']} ──")
        ok = send_message(group, f"📢 {intro}")
        total_sent += 1 if ok else 0
        total_fail += 0 if ok else 1
        time.sleep(1)
        for i, q in enumerate(questions):
            log(f"  Poll {i+1}/5 [{q.get('subject','')}]")
            ok = send_poll(group, q)
            total_sent += 1 if ok else 0
            total_fail += 0 if ok else 1

    # Save questions locally — daily.yml will upload as artifact
    questions_data = {"date": str(date.today()), "questions": questions}
    save_json(TODAY_Q_FILE, questions_data)
    log(f"💾 Questions saved to {TODAY_Q_FILE}")

    # Update history
    for q in questions:
        qhash = str(hash(q.get("question", "")[:50]))
        if qhash not in history["used"]:
            history["used"].append(qhash)
    history["used"] = history["used"][-500:]
    save_json(HISTORY_FILE, history)
    log(f"History updated ({len(history['used'])} entries).")

    if total_sent == 0:
        msg_alert = (
            f"❌ Quiz FAILED — 0/{total_sent + total_fail} messages/polls sent.\n"
            f"Most likely cause: PW_TOKEN expired.\n\n"
            f"Fix: pw.live → any group chat → DevTools → Network tab → copy Authorization header → "
            f"GitHub Secrets → PW_TOKEN → Update"
        )
        log(f"❌ {msg_alert}")
        send_alert("❌ Quiz FAILED — Token likely expired", msg_alert)
        sys.exit(1)
    elif total_fail > 0:
        log(f"⚠️  Quiz sent with {total_fail} failures.")
        send_alert(
            f"⚠️ Quiz — {total_fail} sends failed",
            f"Sent: {total_sent}\nFailed: {total_fail}\nSubjects: {subjects}\nDate: {date.today()}"
        )
    else:
        log("✅ Quiz mode complete.")
        send_alert(
            "✅ Polls Sent",
            f"5 polls sent to all groups.\nSubjects: {subjects}\nDate: {date.today()}\n\n"
            + "\n".join(
                f"Q{i+1} [{q.get('subject','')}]: {q.get('question','')[:80]}"
                for i, q in enumerate(questions)
            )
        )


# ─── MODE: SOLUTION (10 PM Mon-Fri) ──────────────────────────────────────────

def run_solution():
    log("=== MODE: SOLUTION (10 PM) ===")

    if not TODAY_Q_FILE.exists():
        msg = "todays_questions.json not found — quiz mode may have failed today."
        log(f"❌ {msg}")
        send_alert("❌ Solution mode failed — no questions file", msg)
        sys.exit(1)

    raw_data = load_json(TODAY_Q_FILE, {})

    # Handle both formats: plain list (old) and {date, questions} (new)
    if isinstance(raw_data, list):
        questions = raw_data
    elif isinstance(raw_data, dict):
        questions = raw_data.get("questions", [])
        saved_date = raw_data.get("date", "unknown")
        log(f"Questions from: {saved_date}")
    else:
        questions = []

    if not questions:
        log("❌ Questions file empty or missing.")
        send_alert("❌ Solution mode failed — no questions file",
                   "todays_questions.json missing. Did quiz mode run today?")
        sys.exit(1)

    log(f"Loaded {len(questions)} questions.")
    letters = ["A", "B", "C", "D"]

    total_sent = 0
    total_fail = 0

    for group in GROUPS:
        log(f"\n── {group['name']} ──")
        ok = send_message(group, "🎯 Aaj ke quiz ke solutions aa gaye hain! Apne answers check karo 👇")
        total_sent += 1 if ok else 0
        total_fail += 0 if ok else 1
        time.sleep(1)

        for i, q in enumerate(questions):
            subject        = q.get("subject", "")
            year_tag       = q.get("year_tag", "")
            opts           = q.get("options", [])
            correct        = q.get("correct", 1)
            soln = q.get("solution", "").strip()
            if not soln:
                soln = generate_explanation(q)
            correct_letter = letters[correct - 1] if 1 <= correct <= 4 else "?"
            correct_text   = opts[correct - 1] if opts else ""

            sol_msg = (
                f"Q{i+1} Solution [{subject}] {year_tag}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{q.get('question','')}\n\n"
                f"✅ सही जवाब: ({correct_letter}) {correct_text}\n\n"
                f"📝 व्याख्या:\n{soln}"
            )
            ok = send_message(group, sol_msg)
            total_sent += 1 if ok else 0
            total_fail += 0 if ok else 1
            time.sleep(1.5)

    if total_sent == 0:
        msg_alert = (
            f"❌ Solutions FAILED — 0/{total_sent + total_fail} messages sent.\n"
            f"Most likely cause: PW_TOKEN expired.\n\n"
            f"Fix: pw.live → any group chat → DevTools → Network tab → copy Authorization header → "
            f"GitHub Secrets → PW_TOKEN → Update"
        )
        log(f"❌ {msg_alert}")
        send_alert("❌ Solutions FAILED — Token likely expired", msg_alert)
        sys.exit(1)
    elif total_fail > 0:
        log(f"⚠️  Solutions sent with {total_fail} failures.")
        send_alert(
            f"⚠️ Solutions — {total_fail} messages failed",
            f"Sent: {total_sent}\nFailed: {total_fail}\nDate: {date.today()}"
        )
    else:
        log("✅ Solution mode complete.")
        send_alert("✅ Solutions Sent", f"All solutions sent to all groups.\nDate: {date.today()}")


# ─── MODE: CHECKIN (5 PM daily) ───────────────────────────────────────────────

def run_checkin():
    log("=== MODE: CHECKIN (5 PM) ===")
    is_saturday = date.today().weekday() == 5

    if is_saturday:
        log("Saturday — generating weekly review...")
        message       = generate_weekly_review_message()
        header        = "📊 Hafte ka Review Time!"
        email_subject = "✅ Weekly Review Sent"
    else:
        log("Generating daily checkin...")
        message       = generate_daily_checkin_message()
        header        = ""
        email_subject = "✅ Daily Checkin Sent"

    log(f"Message: {message[:80]}...")

    total_sent = 0
    total_fail = 0

    for group in GROUPS:
        log(f"Sending to {group['name']}...")
        if header:
            ok = send_message(group, header)
            total_sent += 1 if ok else 0
            total_fail += 0 if ok else 1
            time.sleep(0.5)
        ok = send_message(group, message)
        total_sent += 1 if ok else 0
        total_fail += 0 if ok else 1

    if total_sent == 0:
        msg_alert = (
            f"❌ Checkin FAILED — 0/{total_sent + total_fail} messages sent.\n"
            f"Most likely cause: PW_TOKEN expired.\n\n"
            f"Fix: pw.live → any group chat → DevTools → Network tab → copy Authorization header → "
            f"GitHub Secrets → PW_TOKEN → Update"
        )
        log(f"❌ {msg_alert}")
        send_alert("❌ Checkin FAILED — Token likely expired", msg_alert)
        sys.exit(1)
    elif total_fail > 0:
        log(f"⚠️  Checkin sent with {total_fail} failures.")
        send_alert(
            f"⚠️ Checkin — {total_fail} sends failed",
            f"Sent: {total_sent}\nFailed: {total_fail}\nDate: {date.today()}"
        )
    else:
        log("✅ Checkin mode complete.")
        send_alert(email_subject, f"Checkin sent to all groups.\nDate: {date.today()}")


# ─── MODE: COLLEGE (3 PM Mon-Wed-Fri) ────────────────────────────────────────

def run_college():
    log("=== MODE: COLLEGE PHOTO (3 PM) ===")

    if not GDRIVE_SA_JSON or not GDRIVE_FOLDER_ID:
        raise Exception("GDRIVE_SA_JSON or GDRIVE_FOLDER_ID secret is missing.")

    log("Connecting to Google Drive...")
    service    = get_drive_service()
    all_photos = list_drive_photos(service)
    log(f"Found {len(all_photos)} photos in Drive folder.")

    if not all_photos:
        send_alert("⚠️ College Photo — No photos in Drive", "Add photos to the Drive folder.")
        sys.exit(1)

    sent_data = load_json(SENT_PHOTOS_FILE, {"sent": []})
    sent_ids  = set(sent_data.get("sent", []))
    all_ids   = {p["id"] for p in all_photos}
    unsent    = [p for p in all_photos if p["id"] not in sent_ids]
    log(f"Unsent photos: {len(unsent)} / {len(all_photos)}")

    if not unsent:
        log("All photos sent — resetting cycle.")
        send_alert(
            "📸 College Photos — Cycle complete, restarting",
            f"All {len(all_photos)} photos sent. Starting cycle again."
        )
        sent_data["sent"] = []
        unsent = all_photos

    photo = random.choice(unsent)
    log(f"Selected: {photo['name']}")

    ext      = Path(photo["name"]).suffix or ".jpg"
    tmp_path = f"college_photo{ext}"
    download_drive_photo(service, photo["id"], tmp_path)
    file_size_kb = Path(tmp_path).stat().st_size // 1024
    log(f"Downloaded: {tmp_path} ({file_size_kb} KB)")

    log("Generating caption via Groq...")
    caption = generate_college_caption(photo["name"])
    log(f"Caption: {caption}")

    log("Uploading to PW...")
    image_id = upload_image(tmp_path)

    total_sent = 0
    total_fail = 0

    for group in GROUPS:
        log(f"Sending to {group['name']}...")
        ok = send_image_message(group, image_id, file_size_kb)
        total_sent += 1 if ok else 0
        total_fail += 0 if ok else 1
        time.sleep(0.5)
        ok = send_message(group, caption)
        total_sent += 1 if ok else 0
        total_fail += 0 if ok else 1

    sent_data["sent"].append(photo["id"])
    sent_data["sent"] = [i for i in sent_data["sent"] if i in all_ids]
    save_json(SENT_PHOTOS_FILE, sent_data)
    log(f"Marked sent. Remaining: {len(all_photos) - len(sent_data['sent'])}/{len(all_photos)}")

    Path(tmp_path).unlink(missing_ok=True)

    if total_sent == 0:
        msg_alert = (
            f"❌ College Photo FAILED — 0/{total_sent + total_fail} sends succeeded.\n"
            f"Most likely cause: PW_TOKEN expired.\n\n"
            f"Fix: pw.live → any group chat → DevTools → Network tab → copy Authorization header → "
            f"GitHub Secrets → PW_TOKEN → Update"
        )
        log(f"❌ {msg_alert}")
        send_alert("❌ College Photo FAILED — Token likely expired", msg_alert)
        sys.exit(1)
    elif total_fail > 0:
        log(f"⚠️  College photo sent with {total_fail} failures.")
        send_alert(
            f"⚠️ College Photo — {total_fail} sends failed",
            f"Sent: {total_sent}\nFailed: {total_fail}\nPhoto: {photo['name']}\nDate: {date.today()}"
        )
    else:
        log("✅ College photo mode complete.")
        send_alert(
            "✅ College Photo Sent",
            f"Photo: {photo['name']}\nCaption: {caption}\nDate: {date.today()}"
        )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["quiz", "solution", "motivation", "checkin", "college"],
        required=True,
    )
    args = parser.parse_args()
    log(f"Starting in mode: {args.mode.upper()}")

    try:
        if args.mode == "motivation":
            run_motivation()
        elif args.mode == "quiz":
            run_quiz()
        elif args.mode == "solution":
            run_solution()
        elif args.mode == "checkin":
            run_checkin()
        elif args.mode == "college":
            run_college()
    except Exception as e:
        err = traceback.format_exc()
        log(f"❌ FATAL ERROR:\n{err}")
        send_alert(
            f"❌ Arjuna Automation CRASHED ({args.mode} mode)",
            f"Error: {e}\n\nTraceback:\n{err}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
