
import os, io, json, uuid, random, tempfile, html
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
RESULTS_STORE = {}
GEMINI_MODEL = "gemini-3.8-flash"

SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
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

def get_saved_results():
    return RESULTS_STORE.get(session.get("data_id"), {})

def read_upload(file):
    name, data = (file.filename or "").lower(), file.read()
    if not data:
        raise ValueError("The uploaded file is empty.")
    if name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        if len(text.strip()) < 40:
            raise ValueError("The TXT file contains too little study material.")
        return {"kind": "txt", "content": text}
    if name.endswith(".pdf"):
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            if doc.is_encrypted or not doc.page_count:
                raise ValueError("The PDF is empty or password-protected.")
            doc.close()
        except ValueError:
            raise
        except Exception:
            raise ValueError("The uploaded PDF is invalid or damaged.")
        return {"kind": "pdf", "content": data}
    raise ValueError("Please upload a PDF or TXT file.")

def generate_material(upload, kind):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("Gemini API key is missing. Add GEMINI_API_KEY in Render.")

    if kind == "mcq":
        prompt = """Create up to 20 high-quality MCQs strictly from the uploaded material.
Each question must have exactly four distinct options and one correct answer.
Use plausible, relevant distractors. Questions must be clear, logical and non-repetitive.
Include a short explanation. Do not invent facts or use outside knowledge.
Generate fewer questions rather than poor ones. Return an empty flashcards array."""
    else:
        prompt = """Create up to 40 useful flashcards from the uploaded material.
Each card must have a clear question or term on the front and an accurate,
concise answer on the back. Cover important concepts without repetition.
Do not invent facts or use outside knowledge. Return an empty questions array."""

    client = genai.Client(api_key=key)
    uploaded, temp = None, None

    try:
        if upload["kind"] == "pdf":
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(upload["content"])
                temp = f.name
            uploaded = client.files.upload(file=temp)
            contents = [prompt, uploaded]
        else:
            contents = [prompt, "\nSTUDY MATERIAL:\n", upload["content"][:70000]]

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=SCHEMA,
                temperature=0.2,
                max_output_tokens=8192
            )
        )
        if not response.text:
            raise ValueError("Gemini returned an empty response.")
        return json.loads(response.text)

    except ValueError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if any(x in msg for x in ["429", "quota", "resource_exhausted"]):
            raise ValueError("Gemini free usage limit reached. Please try again later.")
        if any(x in msg for x in ["401", "403", "api key", "permission"]):
            raise ValueError("Gemini authentication failed. Check your API key.")
        if "404" in msg:
            raise ValueError("Gemini model unavailable. Check the model name or API access.")
        print("GEMINI ERROR:", e)
        raise ValueError("Generation failed. Please try another file.")
    finally:
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        if temp and os.path.exists(temp):
            os.remove(temp)
        try:
            client.close()
        except Exception:
            pass

def prepare_mcqs(items):
    result, seen = [], set()
    for q in items[:20]:
        try:
            question = str(q["question"]).strip()
            answer = str(q["answer"]).strip()
            options = [str(x).strip() for x in q["options"] if str(x).strip()]
            if len(options) != 4 or len({x.casefold() for x in options}) != 4:
                continue
            correct = next((x for x in options if x.casefold() == answer.casefold()), None)
            if not correct or len(question) < 10 or question.casefold() in seen:
                continue
            seen.add(question.casefold())
            random.shuffle(options)
            result.append({
                "question": question, "answer": correct, "options": options,
                "explanation": str(q.get("explanation", "")),
                "selected": None, "checked": False
            })
        except (KeyError, TypeError):
            continue
    return result

def prepare_cards(items):
    result, seen = [], set()
    for card in items[:40]:
        try:
            front, back = str(card["front"]).strip(), str(card["back"]).strip()
            if not front or not back or front.casefold() in seen:
                continue
            seen.add(front.casefold())
            result.append({"front": front, "back": back, "shown": False})
        except (KeyError, TypeError):
            continue
    return result

@app.route("/")
def home():
    saved = get_saved_results()
    return render_template("index.html", results=saved.get("results"), mode=saved.get("mode"), status=None)

@app.route("/generate/<kind>", methods=["POST"])
def generate(kind):
    if kind not in ("mcq", "flashcards"):
        return redirect(url_for("home"))
    file = request.files.get("notes")
    if not file or not file.filename:
        return render_template("index.html", status="Please upload your notes first.", error=True)
    try:
        upload = read_upload(file)
        generated = generate_material(upload, kind)
        if kind == "mcq":
            questions = prepare_mcqs(generated.get("questions", []))
            if not questions:
                raise ValueError("No valid MCQs could be generated. Try a more detailed file.")
            results = {"questions": questions, "cards": []}
            message = f"Generated {len(questions)} AI-powered MCQs."
        else:
            cards = prepare_cards(generated.get("flashcards", []))
            if not cards:
                raise ValueError("No useful flashcards could be generated. Try another file.")
            results = {"questions": [], "cards": cards}
            message = f"Generated {len(cards)} AI-powered flashcards."

        data_id = str(uuid.uuid4())
        RESULTS_STORE[data_id] = {"results": results, "mode": kind}
        session["data_id"] = data_id
        return render_template("index.html", results=results, mode=kind, status=message)
    except Exception as e:
        print("GENERATION ERROR:", e)
        return render_template("index.html", status=str(e), error=True)

@app.route("/answer", methods=["POST"])
def answer():
    saved = get_saved_results()
    data = saved.get("results", {})
    questions = data.get("questions", [])
    try:
        i = int(request.form.get("question_index", -1))
    except ValueError:
        i = -1
    if 0 <= i < len(questions):
        choice = request.form.get("choice", "")
        if choice in questions[i]["options"]:
            questions[i]["selected"] = choice
            questions[i]["checked"] = True
            RESULTS_STORE[session.get("data_id")]["results"] = data
    return redirect(url_for("home"))

@app.route("/reveal/<int:index>", methods=["POST"])
def reveal(index):
    saved = get_saved_results()
    data = saved.get("results", {})
    cards = data.get("cards", [])
    if 0 <= index < len(cards):
        cards[index]["shown"] = not cards[index].get("shown", False)
        RESULTS_STORE[session.get("data_id")]["results"] = data
    return redirect(url_for("home"))

@app.route("/download-pdf")
def download_pdf():
    saved = get_saved_results()
    data, mode = saved.get("results", {}), saved.get("mode")
    if not data or mode not in ("mcq", "flashcards"):
        return redirect(url_for("home"))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("QuizTitle", parent=styles["Title"], alignment=TA_CENTER,
                           textColor=colors.HexColor("#174b86"), spaceAfter=14)
    body = styles["BodyText"]
    story = [Paragraph("CTPS Quiz Generator", title), Spacer(1, 5)]

    if mode == "mcq":
        for i, q in enumerate(data.get("questions", []), 1):
            parts = [Paragraph(f"<b>Q{i}. {html.escape(q['question'])}</b>", body), Spacer(1, 5)]
            for letter, option in zip("ABCD", q["options"]):
                parts.append(Paragraph(f"{letter}. {html.escape(option)}", body))
            parts.append(Paragraph(
                f"<font color='#287653'><b>Correct answer: {html.escape(q['answer'])}</b></font>", body
            ))
            if q.get("explanation"):
                parts.append(Paragraph("<b>Explanation:</b> " + html.escape(q["explanation"]), body))
            parts.append(Spacer(1, 12))
            story.append(KeepTogether(parts))
    else:
        for i, card in enumerate(data.get("cards", []), 1):
            story.append(KeepTogether([
                Paragraph(f"<b>{i}. {html.escape(card['front'])}</b>", body),
                Paragraph(html.escape(card["back"]), body), Spacer(1, 12)
            ]))

    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="CTPS_Practice_Material.pdf",
                     mimetype="application/pdf")

@app.errorhandler(413)
def file_too_large(error):
    return render_template("index.html", status="File too large. Upload a file below 18 MB.", error=True), 413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
