
import io
import json
import logging
import os
import random
import re
import uuid
import urllib.error
import urllib.request
from html import escape

import fitz
from flask import Flask, render_template, request, redirect, url_for, session, send_file
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.units import mm


# -----------------------------------------------------------------------------
# APP CONFIGURATION
# -----------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "ctps-quiz-generator-local-secret-change-me"
)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.logger.setLevel(logging.INFO)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get(
    "GROQ_MODEL",
    "openai/gpt-oss-20b"
).strip()

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

CHUNK_SIZE = 4500
MAX_CHUNKS = 6
QUESTIONS_PER_CHUNK = 5
CARDS_PER_CHUNK = 6

RESULTS_STORE = {}


# -----------------------------------------------------------------------------
# SESSION STORAGE
# -----------------------------------------------------------------------------

def get_saved_results():
    return RESULTS_STORE.get(session.get("data_id"), {})


def save_results(results, mode):
    data_id = str(uuid.uuid4())

    RESULTS_STORE[data_id] = {
        "results": results,
        "mode": mode
    }

    session["data_id"] = data_id


# -----------------------------------------------------------------------------
# FILE READING
# -----------------------------------------------------------------------------

def read_file(uploaded_file):

    filename = (uploaded_file.filename or "").lower()
    raw_data = uploaded_file.read()

    if filename.endswith(".txt"):
        return raw_data.decode("utf-8", errors="replace")

    if filename.endswith(".pdf"):

        try:
            with fitz.open(
                stream=raw_data,
                filetype="pdf"
            ) as document:

                if document.is_encrypted and not document.authenticate(""):
                    raise ValueError(
                        "This PDF is password-protected. "
                        "Please upload an unlocked PDF."
                    )

                page_text = []

                for page_number, page in enumerate(document, start=1):

                    page_content = page.get_text(
                        "text",
                        sort=True
                    ).strip()

                    if page_content:
                        page_text.append(
                            f"\n\n[Page {page_number}]\n{page_content}"
                        )

                if not page_text:
                    raise ValueError(
                        "No selectable text was found. "
                        "This may be a scanned PDF; OCR is needed first."
                    )

                return "\n".join(page_text)

        except fitz.FileDataError:
            raise ValueError(
                "The PDF could not be opened. Please try a valid PDF file."
            )

    raise ValueError("Please upload a PDF or TXT file.")


# -----------------------------------------------------------------------------
# TEXT CLEANING
# -----------------------------------------------------------------------------

def clean_study_text(text):

    cleaned_lines = []

    for raw_line in text.replace("\x00", " ").replace("\uf0b7", "•").splitlines():

        line = re.sub(r"[ \t]+", " ", raw_line).strip()

        if not line:
            cleaned_lines.append("")
            continue

        if re.fullmatch(
            r"(?:page\s*)?\d+(?:\s*of\s*\d+)?",
            line,
            re.I
        ):
            continue

        if re.fullmatch(r"[-_=•·\s]{3,}", line):
            continue

        if re.match(
            r"^(table of contents|bibliography|references|acknowledgements?)\s*$",
            line,
            re.I
        ):
            continue

        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


# -----------------------------------------------------------------------------
# TEXT CHUNKING
# -----------------------------------------------------------------------------

def split_into_chunks(
    text,
    chunk_size=CHUNK_SIZE,
    max_chunks=MAX_CHUNKS
):

    paragraphs = [
        p.strip()
        for p in re.split(r"\n\s*\n", text)
        if p.strip()
    ]

    chunks = []
    current = ""

    def add_piece(piece):

        nonlocal current

        piece = piece.strip()

        if not piece:
            return

        if len(piece) > chunk_size:

            sentences = re.split(
                r"(?<=[.!?])\s+",
                piece
            )

            for sentence in sentences:

                if len(sentence) > chunk_size:

                    for start in range(
                        0,
                        len(sentence),
                        chunk_size
                    ):
                        add_piece(
                            sentence[start:start + chunk_size]
                        )

                elif not current:
                    current = sentence

                elif len(current) + len(sentence) + 1 <= chunk_size:
                    current += " " + sentence

                else:
                    chunks.append(current)
                    current = sentence

            return

        if not current:
            current = piece

        elif len(current) + len(piece) + 2 <= chunk_size:
            current += "\n\n" + piece

        else:
            chunks.append(current)
            current = piece

    for paragraph in paragraphs:
        add_piece(paragraph)

    if current:
        chunks.append(current)

    if not chunks:
        return []

    if len(chunks) <= max_chunks:
        return chunks

    # Sample across the beginning, middle and end of long documents.
    indexes = [
        round(i * (len(chunks) - 1) / (max_chunks - 1))
        for i in range(max_chunks)
    ]

    return [chunks[i] for i in indexes]


def normalize_for_match(text):

    text = str(text or "").lower()

    text = re.sub(
        r"[^a-z0-9+#]+",
        " ",
        text
    )

    return re.sub(r"\s+", " ", text).strip()


# -----------------------------------------------------------------------------
# GROQ JSON SCHEMAS
# -----------------------------------------------------------------------------

MCQ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "evidence": {"type": "string"}
                },
                "required": [
                    "question",
                    "answer",
                    "options",
                    "evidence"
                ],
                "additionalProperties": False
            }
        }
    },
    "required": ["questions"],
    "additionalProperties": False
}


FLASHCARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                    "evidence": {"type": "string"}
                },
                "required": [
                    "front",
                    "back",
                    "evidence"
                ],
                "additionalProperties": False
            }
        }
    },
    "required": ["cards"],
    "additionalProperties": False
}


# -----------------------------------------------------------------------------
# GROQ API CONNECTION
# -----------------------------------------------------------------------------

def call_groq(
    system_prompt,
    user_prompt,
    schema,
    schema_name
):

    if not GROQ_API_KEY:
        raise ValueError(
            "The Groq API key is missing. "
            "Add GROQ_API_KEY in Render Environment Variables and redeploy."
        )

    payload = {
        "model": GROQ_MODEL,

        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ],

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema
            }
        },

        "temperature": 0.2,
        "max_completion_tokens": 3500
    }

    request_data = json.dumps(payload).encode("utf-8")

    api_request = urllib.request.Request(
        GROQ_API_URL,
        data=request_data,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            api_request,
            timeout=120
        ) as response:

            response_body = response.read().decode("utf-8")

        response_json = json.loads(response_body)

        content = response_json["choices"][0]["message"].get("content")

        if not content:
            raise ValueError(
                "Groq returned an empty response. Please try again."
            )

        return json.loads(content)

    except urllib.error.HTTPError as error:

        details = error.read().decode(
            "utf-8",
            errors="replace"
        )

        app.logger.error(
            "Groq API HTTP %s: %s",
            error.code,
            details[:1500]
        )

        if error.code == 401:
            raise ValueError(
                "Groq rejected the API key. Check GROQ_API_KEY in Render."
            )

        if error.code == 429:
            raise ValueError(
                "Groq's free-tier rate limit was reached. "
                "Please wait a little and try again."
            )

        if error.code == 400:
            raise ValueError(
                "Groq rejected the request or model setting. "
                "Check the GROQ_MODEL value in Render."
            )

        raise ValueError(
            f"Groq API error ({error.code}). Please try again later."
        )

    except urllib.error.URLError as error:

        app.logger.error(
            "Groq connection error: %s",
            error
        )

        raise ValueError(
            "Could not connect to Groq. Please try again in a moment."
        )

    except (
        KeyError,
        IndexError,
        json.JSONDecodeError,
        TypeError
    ):

        app.logger.exception("Unexpected Groq response")

        raise ValueError(
            "Groq returned an unreadable response. Please try again."
        )


# -----------------------------------------------------------------------------
# AI PROMPTS
# -----------------------------------------------------------------------------

MCQ_SYSTEM_PROMPT = """You are an expert university-level question setter.

Create accurate, useful multiple-choice questions using ONLY the supplied study text.

Rules:

- Test meaningful concepts, principles, applications, comparisons, causes,
  effects, steps, properties, and definitions.
- Do not make every question a simple definition.
- Every correct answer must be directly supported by the supplied text.
- Each question must have exactly four distinct, plausible options.
- All four options must be real terms, concepts, values, or processes
  that appear in the supplied text.
- Do not invent distractors or use silly/generic options.
- Exactly one option must be correct.
- Avoid ambiguous questions and trick wording.
- Do not ask about page numbers, slide numbers, document layout,
  or missing information.
- Do not repeat the same question in different wording.
- The evidence field MUST be copied verbatim from the supplied text.
- Evidence should be a continuous excerpt of roughly 8-35 words
  and must support the correct answer.
- If the text does not contain enough material for a good question,
  return fewer questions.
- Never use outside knowledge to fill gaps.
"""


FLASHCARD_SYSTEM_PROMPT = """You are an expert study-material creator.

Create concise, useful flashcards using ONLY the supplied study text.

Rules:

- Front should be a meaningful concept, question, formula name,
  process, or term.
- Back should explain it accurately and clearly in 1-3 sentences.
- Prefer important examinable concepts.
- Avoid trivial words and headings.
- Do not invent facts or add outside knowledge.
- Do not create duplicate cards.
- The evidence field MUST be copied verbatim from the supplied text.
- Evidence should be a continuous excerpt of roughly 8-35 words
  and must support the back of the card.
- If the text has little useful content, return fewer cards.
"""


# -----------------------------------------------------------------------------
# EVIDENCE VALIDATION
# -----------------------------------------------------------------------------

def valid_evidence(evidence, source):

    evidence_norm = normalize_for_match(evidence)
    source_norm = normalize_for_match(source)

    if len(evidence_norm.split()) < 7:
        return False

    return evidence_norm in source_norm


# -----------------------------------------------------------------------------
# AI MCQ GENERATION
# -----------------------------------------------------------------------------

def generate_mcqs(chunks):

    collected = []
    seen_questions = set()
    errors = []

    for chunk_number, chunk in enumerate(chunks, start=1):

        prompt = (
            f"Create up to {QUESTIONS_PER_CHUNK} high-quality MCQs "
            "from this section. Return fewer if the section does not "
            "support enough distinct questions.\n\n"
            f"STUDY TEXT (section {chunk_number}):\n{chunk}"
        )

        try:

            result = call_groq(
                MCQ_SYSTEM_PROMPT,
                prompt,
                MCQ_SCHEMA,
                "quiz_questions"
            )

        except ValueError as error:

            if not collected:
                raise

            errors.append(str(error))
            break

        for item in result.get("questions", []):

            question = re.sub(
                r"\s+",
                " ",
                str(item.get("question", ""))
            ).strip()

            answer = re.sub(
                r"\s+",
                " ",
                str(item.get("answer", ""))
            ).strip()

            evidence = str(
                item.get("evidence", "")
            ).strip()

            options = [
                re.sub(
                    r"\s+",
                    " ",
                    str(option)
                ).strip()
                for option in item.get("options", [])
            ]

            if not (
                25 <= len(question) <= 450
                and 1 <= len(answer) <= 100
            ):
                continue

            if len(options) != 4:
                continue

            if any(
                not option or len(option) > 100
                for option in options
            ):
                continue

            if len({
                normalize_for_match(option)
                for option in options
            }) != 4:
                continue

            if answer.casefold() not in {
                option.casefold()
                for option in options
            }:
                continue

            if not valid_evidence(evidence, chunk):
                continue

            source_norm = normalize_for_match(chunk)

            # Reject options that are not grounded in the uploaded text.
            if any(
                normalize_for_match(option) not in source_norm
                for option in options
            ):
                continue

            if (
                normalize_for_match(answer)
                not in normalize_for_match(evidence)
            ):
                continue

            question_key = normalize_for_match(question)

            if question_key in seen_questions:
                continue

            seen_questions.add(question_key)

            shuffled_options = options[:]
            random.shuffle(shuffled_options)

            collected.append({
                "question": question,
                "answer": answer,
                "options": shuffled_options,
                "selected": None,
                "checked": False
            })

    return collected, errors


# -----------------------------------------------------------------------------
# AI FLASHCARD GENERATION
# -----------------------------------------------------------------------------

def generate_flashcards(chunks):

    collected = []
    seen_fronts = set()
    errors = []

    for chunk_number, chunk in enumerate(chunks, start=1):

        prompt = (
            f"Create up to {CARDS_PER_CHUNK} useful flashcards "
            "from this section. Return fewer if there are not "
            "enough important concepts.\n\n"
            f"STUDY TEXT (section {chunk_number}):\n{chunk}"
        )

        try:

            result = call_groq(
                FLASHCARD_SYSTEM_PROMPT,
                prompt,
                FLASHCARD_SCHEMA,
                "study_flashcards"
            )

        except ValueError as error:

            if not collected:
                raise

            errors.append(str(error))
            break

        for item in result.get("cards", []):

            front = re.sub(
                r"\s+",
                " ",
                str(item.get("front", ""))
            ).strip()

            back = re.sub(
                r"\s+",
                " ",
                str(item.get("back", ""))
            ).strip()

            evidence = str(
                item.get("evidence", "")
            ).strip()

            key = normalize_for_match(front)

            if not (
                2 <= len(front) <= 100
                and 15 <= len(back) <= 600
            ):
                continue

            if (
                not key
                or key in seen_fronts
                or not valid_evidence(evidence, chunk)
            ):
                continue

            seen_fronts.add(key)

            collected.append({
                "front": front,
                "back": back,
                "shown": False
            })

    return collected, errors


# -----------------------------------------------------------------------------
# HOME PAGE
# -----------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():

    saved = get_saved_results()

    return render_template(
        "index.html",
        results=saved.get("results"),
        mode=saved.get("mode"),
        status=None
    )


# -----------------------------------------------------------------------------
# GENERATION ROUTE
# -----------------------------------------------------------------------------

@app.route("/generate/<kind>", methods=["POST"])
def generate(kind):

    if kind not in ("mcq", "flashcards"):
        return redirect(url_for("home"))

    uploaded = request.files.get("notes")

    if not uploaded or not uploaded.filename:

        return render_template(
            "index.html",
            status="Please upload your notes first.",
            error=True
        )

    try:

        study_text = clean_study_text(
            read_file(uploaded)
        )

        if len(study_text) < 100:

            raise ValueError(
                "Too little readable text was extracted. "
                "If this is a scanned PDF, OCR is needed first."
            )

        chunks = split_into_chunks(study_text)

        if not chunks:

            raise ValueError(
                "No usable study content was found in this file."
            )

        if kind == "mcq":

            questions, generation_errors = generate_mcqs(chunks)

            if not questions:

                raise ValueError(
                    "The AI could not find enough well-supported MCQs "
                    "in this PDF. Try a clearer or more detailed file. "
                    "No weak questions were added."
                )

            saved_results = {
                "questions": questions[:30],
                "cards": []
            }

            message = (
                f"Generated {len(saved_results['questions'])} "
                "AI-based MCQs from your PDF."
            )

        else:

            cards, generation_errors = generate_flashcards(chunks)

            if not cards:

                raise ValueError(
                    "The AI could not identify enough reliable "
                    "flashcards in this PDF. Try a clearer or "
                    "more detailed file."
                )

            saved_results = {
                "questions": [],
                "cards": cards[:50]
            }

            message = (
                f"Generated {len(saved_results['cards'])} "
                "AI-based flashcards from your PDF."
            )

        if generation_errors:

            message += (
                " (Generation stopped early because of an API "
                "limit or connection issue.)"
            )

        save_results(saved_results, kind)

        return render_template(
            "index.html",
            results=saved_results,
            mode=kind,
            status=message
        )

    except ValueError as error:

        return render_template(
            "index.html",
            status=str(error),
            error=True
        )

    except Exception:

        app.logger.exception("Quiz generation failed")

        return render_template(
            "index.html",
            status=(
                "Something went wrong while processing this file. "
                "Please try again."
            ),
            error=True
        )


# -----------------------------------------------------------------------------
# ANSWER CHECKING
# -----------------------------------------------------------------------------

@app.route("/answer", methods=["POST"])
def answer():

    saved = get_saved_results()
    data = saved.get("results", {})
    questions = data.get("questions", [])

    try:
        index = int(
            request.form.get("question_index", -1)
        )
    except (TypeError, ValueError):
        index = -1

    if 0 <= index < len(questions):

        chosen = request.form.get("choice", "")

        if chosen in questions[index].get("options", []):

            questions[index]["selected"] = chosen
            questions[index]["checked"] = True

            RESULTS_STORE[
                session.get("data_id")
            ]["results"] = data

    return redirect(url_for("home"))


# -----------------------------------------------------------------------------
# FLASHCARD REVEAL
# -----------------------------------------------------------------------------

@app.route("/reveal/<int:index>", methods=["POST"])
def reveal(index):

    saved = get_saved_results()
    data = saved.get("results", {})
    cards = data.get("cards", [])

    if 0 <= index < len(cards):

        cards[index]["shown"] = not cards[index].get(
            "shown",
            False
        )

        RESULTS_STORE[
            session.get("data_id")
        ]["results"] = data

    return redirect(url_for("home"))


# -----------------------------------------------------------------------------
# PDF DOWNLOAD
# -----------------------------------------------------------------------------

@app.route("/download-pdf")
def download_pdf():

    saved = get_saved_results()

    data = saved.get("results", {})
    mode = saved.get("mode")

    if not data or mode not in ("mcq", "flashcards"):
        return redirect(url_for("home"))

    buffer = io.BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "QuizTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#174b86"),
        spaceAfter=14
    )

    body_style = styles["BodyText"]

    story = [
        Paragraph(
            "CTPS Quiz Generator",
            title_style
        ),
        Spacer(1, 5)
    ]

    if mode == "mcq":

        for number, question in enumerate(
            data.get("questions", []),
            1
        ):

            parts = [

                Paragraph(
                    f"<b>Q{number}. "
                    f"{escape(question['question'])}</b>",
                    body_style
                ),

                Spacer(1, 5)
            ]

            for letter, option in zip(
                "ABCD",
                question["options"]
            ):

                parts.append(
                    Paragraph(
                        f"{letter}. {escape(str(option))}",
                        body_style
                    )
                )

            parts.extend([

                Paragraph(
                    f"<font color='#287653'><b>"
                    f"Correct answer: "
                    f"{escape(str(question['answer']))}"
                    f"</b></font>",
                    body_style
                ),

                Spacer(1, 12)
            ])

            story.append(
                KeepTogether(parts)
            )

    else:

        for number, card in enumerate(
            data.get("cards", []),
            1
        ):

            story.append(
                KeepTogether([

                    Paragraph(
                        f"<b>{number}. "
                        f"{escape(str(card['front']))}</b>",
                        body_style
                    ),

                    Paragraph(
                        escape(str(card["back"])),
                        body_style
                    ),

                    Spacer(1, 12)
                ])
            )

    document.build(story)

    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="CTPS_Practice_Material.pdf",
        mimetype="application/pdf"
    )


# -----------------------------------------------------------------------------
# FILE SIZE ERROR
# -----------------------------------------------------------------------------

@app.errorhandler(413)
def file_too_large(_error):

    return render_template(
        "index.html",
        status=(
            "The uploaded file is too large. "
            "Please upload a file smaller than 20 MB."
        ),
        error=True
    ), 413


# -----------------------------------------------------------------------------
# LOCAL DEVELOPMENT
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False)
