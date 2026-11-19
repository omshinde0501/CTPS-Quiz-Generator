
import os, io, json, uuid, random, tempfile, time, html
import fitz
from flask import Flask, render_template, request, redirect, url_for, session, send_file
from google import genai
from google.genai import types
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.units import mm

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ctps-quiz-generator-change-this-key")
app.config["MAX_CONTENT_LENGTH"] = 18 * 1024 * 1024

PRIMARY_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-3.1-flash-lite"
RESULTS_STORE = {}

SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "answer": {"type": "string"},
                    "explanation": {"type": "string"}
                },
                "required": ["question", "options", "answer", "explanation"]
            }
        },
        "flashcards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "front": {"type": "string"},
                    "back": {"type": "string"}
                },
                "required": ["front", "back"]
            }
        }
    },
    "required": ["questions", "flashcards"]
}


def read_upload(file):
    name = (file.filename or "").lower()
    data = file.read()

    if not data:
        raise ValueError("The uploaded file is empty.")

    if name.endswith(".txt"):
        text = data.decode("utf-8", errors="ignore").strip()
        if not text:
            raise ValueError("No readable text found in this TXT file.")
        return "text", text

    if name.endswith(".pdf"):
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            if doc.is_encrypted:
                doc.close()
                raise ValueError("Password-protected PDFs are not supported.")
            text = "\n".join(page.get_text() for page in doc).strip()
            doc.close()
            if not text:
                raise ValueError("This PDF has no selectable text. Scanned PDFs need OCR.")
            return "text", text[:70000]
        except ValueError:
            raise
        except Exception:
            raise ValueError("Could not read this PDF. Please try another file.")

    raise ValueError("Please upload a PDF or TXT file.")


def generate_with_retry(client, contents, config):
    models = [PRIMARY_MODEL, FALLBACK_MODEL]
    last_error = None

    for model in models:
        for attempt in range(3):
            try:
                return client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config
                )
            except Exception as e:
                last_error = e
                error = str(e).upper()

                if "503" in error or "UNAVAILABLE" in error:
                    if attempt < 2:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    break

                if "429" in error or "RESOURCE_EXHAUSTED" in error:
                    raise Exception(
                        "Gemini free-tier limit reached. Please wait and try again."
                    )

                if "401" in error or "403" in error or "API_KEY" in error:
                    raise Exception("Gemini API key is invalid or not authorized.")

                if "404" in error or "NOT_FOUND" in error:
                    break

                raise Exception("Gemini error: " + str(e)[:300])

    raise Exception(
        "Gemini is temporarily overloaded. Both models failed. "
        "Please wait a little and try again. Details: " + str(last_error)[:180]
    )


def generate_material(file_type, source, kind):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY is missing from Render environment variables.")

    client = genai.Client(api_key=key)

    if kind == "mcq":
        task = """
Create 10 high-quality multiple-choice questions from the supplied study material.
Each question must have exactly 4 distinct options and one unambiguously correct answer.
Use only facts supported by the material. Avoid vague, repetitive, trivial or illogical questions.
Make distractors realistic but clearly incorrect. Cover different topics in the material.
The answer must exactly match one of the four options.
Provide a short explanation based on the material.
Return questions in the questions array. Leave flashcards empty.
"""
    else:
        task = """
Create 15 useful study flashcards from the supplied study material.
Each front should contain one clear question or concept.
Each back should contain a concise, accurate answer based only on the material.
Avoid duplicate or overly broad cards.
Return cards in the flashcards array. Leave questions empty.
"""

    contents = [
        task,
        "Study material:\n" + source[:70000]
    ]

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=SCHEMA,
        temperature=0.2,
        max_output_tokens=8192
    )

    try:
        response = generate_with_retry(client, contents, config)
        if not response.text:
            raise ValueError("Gemini returned an empty response.")
        return json.loads(response.text)
    finally:
        try:
            client.close()
        except Exception:
            pass


def prepare_mcqs(data):
    output = []
    for q in data.get("questions", []):
        question = str(q.get("question", "")).strip()
        answer = str(q.get("answer", "")).strip()
        options = [str(x).strip() for x in q.get("options", []) if str(x).strip()]

        if not question or not answer or len(options) != 4:
            continue
        if len(set(options)) != 4 or answer not in options:
            continue

        random.shuffle(options)
        output.append({
            "question": question,
            "answer": answer,
            "options": options,
            "explanation": str(q.get("explanation", "")).strip(),
            "selected": None,
            "checked": False
        })

    if not output:
        raise ValueError("No valid MCQs were generated. Try another file.")
    return output


def prepare_cards(data):
    output, seen = [], set()
    for card in data.get("flashcards", []):
        front = str(card.get("front", "")).strip()
        back = str(card.get("back", "")).strip()
        if front and back and front.lower() not in seen:
            seen.add(front.lower())
            output.append({"front": front, "back": back, "shown": False})

    if not output:
        raise ValueError("No valid flashcards were generated. Try another file.")
    return output


def get_results():
    return RESULTS_STORE.get(session.get("result_id"), {})


@app.route("/")
def index():
    return render_template(
        "index.html",
        results=get_results(),
        mode=session.get("mode"),
        status=session.get("status", ""),
        error=session.get("error", "")
    )


@app.route("/generate/<kind>", methods=["POST"])
def generate(kind):
    if kind not in ("mcq", "flashcards"):
        return redirect(url_for("index"))

    try:
        file = request.files.get("file")
        if not file or not file.filename:
            raise ValueError("Please select a PDF or TXT file.")

        file_type, source = read_upload(file)
        data = generate_material(file_type, source, kind)

        if kind == "mcq":
            results = {"questions": prepare_mcqs(data), "cards": []}
        else:
            results = {"questions": [], "cards": prepare_cards(data)}

        result_id = str(uuid.uuid4())
        RESULTS_STORE[result_id] = results
        session["result_id"] = result_id
        session["mode"] = kind
        session["status"] = "Generated successfully!"
        session["error"] = ""

    except Exception as e:
        session["error"] = str(e)
        session["status"] = ""

    return redirect(url_for("index"))


@app.route("/answer", methods=["POST"])
def answer():
    results = get_results()
    questions = results.get("questions", [])

    for i, q in enumerate(questions):
        selected = request.form.get(f"answer_{i}")
        q["selected"] = selected
        q["checked"] = True

    session["status"] = "Answers submitted!"
    return redirect(url_for("index"))


@app.route("/reveal/<int:index>")
def reveal(index):
    results = get_results()
    cards = results.get("cards", [])

    if 0 <= index < len(cards):
        cards[index]["shown"] = not cards[index]["shown"]

    return redirect(url_for("index"))


@app.route("/download-pdf")
def download_pdf():
    results = get_results()
    mode = session.get("mode")
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#1769aa"),
        spaceAfter=16
    )
    question_style = ParagraphStyle(
        "QuestionCustom",
        parent=styles["Normal"],
        fontSize=11,
        leading=16,
        spaceAfter=6
    )
    body_style = ParagraphStyle(
        "BodyCustom",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        spaceAfter=4
    )

    story = [Paragraph("CTPS Study Material", title_style)]

    if mode == "mcq":
        for i, q in enumerate(results.get("questions", []), 1):
            items = [
                Paragraph(f"<b>Q{i}. {html.escape(q['question'])}</b>", question_style)
            ]
            for option in q["options"]:
                mark = "✓ " if option == q["answer"] else ""
                items.append(Paragraph(
                    html.escape(mark + option), body_style
                ))

            items.append(Paragraph(
                "<b>Answer:</b> " + html.escape(q["answer"]), body_style
            ))
            if q.get("explanation"):
                items.append(Paragraph(
                    "<b>Explanation:</b> " + html.escape(q["explanation"]),
                    body_style
                ))
            items.append(Spacer(1, 8))
            story.append(KeepTogether(items))

    else:
        for i, card in enumerate(results.get("cards", []), 1):
            story.append(KeepTogether([
                Paragraph(f"<b>Card {i}: {html.escape(card['front'])}</b>", question_style),
                Paragraph(html.escape(card["back"]), body_style),
                Spacer(1, 10)
            ]))
    doc.build(story)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="CTPS_Study_Material.pdf",
        mimetype="application/pdf"
    )
@app.errorhandler(413)
def too_large(e):
    return "File is too large. Maximum size is 18 MB.", 413
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
