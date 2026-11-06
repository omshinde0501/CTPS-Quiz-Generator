from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
import fitz
import io
import random
import re
import uuid
from collections import OrderedDict
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.units import mm

app = Flask(__name__)
app.secret_key = "ctps-quiz-generator-change-this-key"
RESULTS_STORE = {}

def get_saved_results():
    return RESULTS_STORE.get(session.get("data_id"), {})

# Read TXT files or extract selectable text from PDFs.
def read_file(file):
    name = file.filename.lower()
    data = file.read()
    if name.endswith(".txt"):
        return data.decode("utf-8", errors="ignore")
    if name.endswith(".pdf"):
        document = fitz.open(stream=data, filetype="pdf")
        return "\n".join(page.get_text("text", sort=True) for page in document)
    raise ValueError("Please choose a PDF or TXT file.")

# Identify headings, instructions and page decorations.
def is_unwanted_line(line):
    text = line.strip()
    lower = text.lower()
    if len(text) < 3:
        return True
    if re.match(r"^(unit|chapter|module|semester|subject)\s*[-:\d]", text, re.I):
        return True
    if re.match(r"^(unit|chapter|module)\s+\d+", text, re.I):
        return True
    headings = r"^(how to use these notes|how to use|instructions?|learning objectives|contents|table of contents|index|references|bibliography|acknowledgement)s?\s*[:.]?$"
    if re.match(headings, text, re.I):
        return True
    if re.match(r"^(read|use|refer to|study|follow|go through|these notes|this unit|in this unit|at the end of this unit)\b", lower):
        return True
    if re.match(r"^(page\s*)?\d+(\s*of\s*\d+)?$", text, re.I):
        return True
    if re.match(r"^(quiz generator|generated notes|study notes)$", text, re.I):
        return True
    if len(text) < 90 and re.fullmatch(r"[A-Z0-9\s&(),./:-]+", text) and re.search(r"[A-Z]", text) and not re.search(r"[.!?]", text):
        return True
    return False

# Remove unwanted lines but keep useful line boundaries.
def clean_study_text(text):
    useful = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not is_unwanted_line(line):
            useful.append(line)
    return "\n".join(useful)

# Split the cleaned notes into short statements.
def get_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts
            if 25 < len(p.strip()) < 350 and not is_unwanted_line(p.strip())]

# Extract definitions written as 'Term: meaning' or common definition phrases.
def find_definitions(sentences):
    patterns = [
        re.compile(r"^(.{2,65}?)\s+(?:is defined as|are defined as|is referred to as|are referred to as|refers to|refer to|is known as|are known as|is called|are called|is also called|are also called|is a type of|are a type of|is the process of|are the process of|is an?|are an?)\s+(.+)$", re.I),
        re.compile(r"^(.{2,65}?)\s*:\s*(.{15,})$", re.I),
        re.compile(r"^(.{2,65}?)\s+(?:means|denotes|represents|is used for|are used for|is used to|are used to)\s+(.+)$", re.I)
    ]
    found = []
    for sentence in sentences:
        line = re.sub(r"^[•\-*]\s*", "", sentence).strip()
        if is_unwanted_line(line):
            continue
        for pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            term, meaning = match.group(1).strip(), match.group(2).strip()
            if (len(term.split()) <= 8 and len(meaning) > 12 and not is_unwanted_line(term)
                    and not re.match(r"^(how to use|unit|chapter|module|instructions?)\b", term, re.I)):
                found.append({"term": re.sub(r"[,;:]+$", "", term),
                              "meaning": re.sub(r"[.!?]+$", "", meaning),
                              "source": sentence})
            break
    # Keep the first useful definition for each term.
    unique = OrderedDict()
    for item in found:
        key = item["term"].casefold()
        if key not in unique:
            unique[key] = item
    return list(unique.values())

# Put related concepts into broad groups for more relevant MCQ options.
def get_category(item):
    text = (item["term"] + " " + item["meaning"]).lower()
    groups = {
        "programming_languages": ["python", "java", "javascript", "c++", "c language", "sql", "pascal", "prolog", "haskell"],
        "programming_methods": ["procedural", "object-oriented", "functional", "logic programming", "declarative", "concurrent", "parallel"],
        "software_tools": ["compiler", "interpreter", "linker", "loader", "assembler", "debugger"],
        "program_errors": ["syntax error", "semantic error", "type error", "linking error", "runtime error", "logical error"],
        "logic_gates": ["and gate", "or gate", "not gate", "nand gate", "nor gate", "xor gate", "xnor gate"],
        "data_concepts": ["variable", "constant", "data type", "array", "string", "integer", "float", "boolean", "operator"],
        "control_structures": ["if statement", "if-else", "loop", "for loop", "while loop", "switch", "iteration", "selection"]
    }
    for category, terms in groups.items():
        if any(term in text for term in terms):
            return category
    return "other"

def word_similarity(first, second):
    a = set(re.findall(r"[a-z]{3,}", first["meaning"].lower()))
    b = set(re.findall(r"[a-z]{3,}", second["meaning"].lower()))
    stop = {"the", "and", "for", "that", "this", "with", "from", "into", "are", "which", "used"}
    a -= stop
    b -= stop
    return len(a & b) / max(1, len(a | b))

# Generate four-option questions only when three plausible related terms exist.
def make_mcqs(definitions, limit=30):
    result = []
    for item in definitions:
        if len(result) >= limit:
            break
        category = get_category(item)
        candidates = []
        for other in definitions:
            if other["term"].casefold() == item["term"].casefold():
                continue
            same_group = category != "other" and get_category(other) == category
            similar = word_similarity(item, other) >= 0.12
            if same_group or similar:
                candidates.append(other)
        candidates.sort(key=lambda x: word_similarity(item, x), reverse=True)
        distractors = []
        for candidate in candidates:
            if candidate["term"].casefold() not in {d.casefold() for d in distractors}:
                distractors.append(candidate["term"])
            if len(distractors) == 3:
                break
        if len(distractors) != 3:
            continue
        options = [item["term"]] + distractors
        random.shuffle(options)
        result.append({
            "question": "Which term matches this definition? " + item["meaning"] + " ________",
            "answer": item["term"],
            "options": options,
            "selected": None,
            "checked": False
        })
    return result

def make_flashcards(definitions):
    return [{"front": item["term"], "back": item["meaning"], "shown": False}
            for item in definitions[:50]]

@app.route("/", methods=["GET"])
def home():
    saved = get_saved_results()
    return render_template("index.html", results=saved.get("results"),
                           mode=saved.get("mode"), status=None)

@app.route("/generate/<kind>", methods=["POST"])
def generate(kind):
    if kind not in ("mcq", "flashcards"):
        return redirect(url_for("home"))
    uploaded = request.files.get("notes")
    if not uploaded or not uploaded.filename:
        return render_template("index.html", status="Please upload your notes first.", error=True)
    try:
        raw_text = read_file(uploaded)
        study_text = clean_study_text(raw_text)
        if len(study_text) < 40:
            raise ValueError("Very little text could be extracted. Scanned/image-only PDFs need OCR before uploading.")
        definitions = find_definitions(get_sentences(study_text))
        if not definitions:
            raise ValueError("No clear study definitions found. Try notes with phrases like 'refers to', 'is known as', 'means', or 'Term: meaning'.")
        if kind == "mcq":
            questions = make_mcqs(definitions, 30)
            if not questions:
                raise ValueError("Not enough related technical terms were found to create reliable four-option MCQs. Try notes with more definitions from the same topic.")
            saved_results = {"questions": questions, "cards": []}
            mode = "mcq"
            message = f"Generated {len(questions)} MCQs from {len(definitions)} valid definitions."
        else:
            cards = make_flashcards(definitions)
            saved_results = {"questions": [], "cards": cards}
            mode = "flashcards"
            message = f"Generated {len(cards)} flashcards from filtered study content."
        data_id = str(uuid.uuid4())
        RESULTS_STORE[data_id] = {"results": saved_results, "mode": mode}
        session["data_id"] = data_id
        return render_template("index.html", results=saved_results, mode=kind, status=message)
    except Exception as error:
        return render_template("index.html", status=str(error), error=True)

@app.route("/answer", methods=["POST"])
def answer():
    data = get_saved_results().get("results", {})
    questions = data.get("questions", [])
    try:
        index = int(request.form.get("question_index", -1))
    except ValueError:
        index = -1
    if 0 <= index < len(questions):
        chosen = request.form.get("choice", "")
        if chosen in questions[index]["options"]:
            questions[index]["selected"] = chosen
            questions[index]["checked"] = True
            RESULTS_STORE[session.get("data_id")]["results"] = data
    return redirect(url_for("home"))

@app.route("/reveal/<int:index>", methods=["POST"])
def reveal(index):
    data = get_saved_results().get("results", {})
    cards = data.get("cards", [])
    if 0 <= index < len(cards):
        cards[index]["shown"] = not cards[index].get("shown", False)
        RESULTS_STORE[session.get("data_id")]["results"] = data
    return redirect(url_for("home"))

@app.route("/download-pdf")
def download_pdf():
    data = get_saved_results().get("results", {})
    mode = get_saved_results().get("mode")
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
        for number, q in enumerate(data.get("questions", []), 1):
            parts = [Paragraph(f"<b>Q{number}. {q['question']}</b>", body), Spacer(1, 5)]
            for letter, option in zip("ABCD", q["options"]):
                parts.append(Paragraph(f"{letter}. {option}", body))
            parts.extend([Paragraph(f"<font color='#287653'><b>Correct answer: {q['answer']}</b></font>", body), Spacer(1, 12)])
            story.append(KeepTogether(parts))
    else:
        for number, card in enumerate(data.get("cards", []), 1):
            story.append(KeepTogether([Paragraph(f"<b>{number}. {card['front']}</b>", body),
                                       Paragraph(card["back"], body), Spacer(1, 12)]))
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="CTPS_Practice_Material.pdf",
                     mimetype="application/pdf")

if __name__ == "__main__":
    app.run(debug=True)
