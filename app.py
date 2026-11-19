
from flask import Flask, render_template, request, redirect, url_for, session, send_file
import fitz, io, random, re, uuid
from collections import OrderedDict, Counter
from html import escape
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

def read_file(file):
    name = (file.filename or "").lower()
    data = file.read()
    if name.endswith(".txt"):
        return data.decode("utf-8", errors="ignore")
    if name.endswith(".pdf"):
        try:
            with fitz.open(stream=data, filetype="pdf") as document:
                if document.is_encrypted and not document.authenticate(""):
                    raise ValueError("This PDF is password-protected. Please upload an unlocked PDF.")
                return "\n".join(page.get_text("text", sort=True) for page in document)
        except fitz.FileDataError:
            raise ValueError("The uploaded PDF could not be read. Please try another PDF.")
    raise ValueError("Please choose a PDF or TXT file.")

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

def clean_study_text(text):
    useful = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not is_unwanted_line(line):
            useful.append(line)
    return "\n".join(useful)

def get_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts
            if 25 < len(p.strip()) < 400 and not is_unwanted_line(p.strip())]

def find_definitions(sentences):
    patterns = [
        re.compile(r"^(.{2,65}?)\s+(?:is defined as|are defined as|is referred to as|are referred to as|refers to|refer to|is known as|are known as|is called|are called|is also called|are also called|is a type of|are a type of|is the process of|are the process of|is an?|are an?)\s+(.+)$", re.I),
        re.compile(r"^(.{2,65}?)\s*:\s*(.{15,})$", re.I),
        re.compile(r"^(.{2,65}?)\s+(?:means|denotes|represents|is used for|are used for|is used to|are used to|is responsible for|are responsible for)\s+(.+)$", re.I),
        re.compile(r"^(.{2,55}?)\s+(consists of|is composed of|are composed of|contains|include|includes|comprises|has|have|supports|allows|enables|performs|converts|stores|produces|generates|controls|connects|requires|depends on)\s+(.+)$", re.I)
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
            term = re.sub(r"^(the|a|an)\s+", "", match.group(1).strip(" ,;:-"), flags=re.I)
            meaning = (match.group(2) + " " + match.group(3)).strip() if len(match.groups()) == 3 else match.group(2).strip()
            if (1 <= len(term.split()) <= 8 and 15 < len(meaning) < 300
                and not is_unwanted_line(term) and len(term) < 66
                and not re.match(r"^(how to use|unit|chapter|module|instructions?|it|this|they|these|we|you|the system|system|the method|method|the process|process|the following|following)\b", term, re.I)):
                found.append({"term": term.rstrip(",;:"), "meaning": re.sub(r"[.!?]+$", "", meaning), "source": sentence})
            break
    unique = OrderedDict()
    for item in found:
        unique.setdefault(item["term"].casefold(), item)
    return list(unique.values())

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
    stop = {"the", "and", "for", "that", "this", "with", "from", "into", "are", "which", "used", "can", "its", "their", "when", "where", "often", "also"}
    a = set(re.findall(r"[a-z]{3,}", (first["term"] + " " + first["meaning"]).lower())) - stop
    b = set(re.findall(r"[a-z]{3,}", (second["term"] + " " + second["meaning"]).lower())) - stop
    return len(a & b) / max(1, len(a | b))

def get_distractors(item, definitions):
    candidates = []
    category = get_category(item)
    for other in definitions:
        if other["term"].casefold() == item["term"].casefold():
            continue
        same_group = category != "other" and get_category(other) == category
        similarity = word_similarity(item, other)
        if same_group or similarity >= 0.08:
            candidates.append((other["term"], similarity + (0.25 if same_group else 0)))
    candidates.sort(key=lambda x: x[1], reverse=True)
    for other in definitions:
        if other["term"].casefold() != item["term"].casefold():
            candidates.append((other["term"], word_similarity(item, other)))
    distractors = []
    for term, _ in candidates:
        if term.casefold() != item["term"].casefold() and term.casefold() not in {x.casefold() for x in distractors}:
            distractors.append(term)
        if len(distractors) == 3:
            break
    return distractors

def make_question(item, definitions, style):
    distractors = get_distractors(item, definitions)
    if len(distractors) != 3:
        return None
    term = item["term"]
    meaning = item["meaning"].strip().rstrip(".!?; ")
    source = item["source"]

    if style == "definition":
        question = "Which term matches this definition? " + meaning + "?"
    elif style == "fill_blank":
        blanked, count = re.subn(re.escape(term), "________", source, count=1, flags=re.I)
        if not count:
            return None
        question = "Fill in the blank: " + blanked
    elif style == "function":
        if not re.search(r"\b(used|purpose|function|responsible|allows|enables|performs|converts|stores|controls|generates|produces)\b", meaning, re.I):
            return None
        question = "Which concept performs this function? " + meaning + "?"
    elif style == "application":
        question = "A task requires this capability: " + meaning + ". Which concept is most relevant?"
    else:
        return None

    options = [term] + distractors
    random.shuffle(options)
    return {"question": question, "answer": term, "options": options,
            "selected": None, "checked": False, "style": style}

def make_mcqs(definitions, limit=30):
    if len(definitions) < 4:
        return []
    styles = ["definition", "fill_blank", "function", "application"]
    concepts = definitions[:]
    random.shuffle(concepts)
    pools = {style: [] for style in styles}
    for item in concepts:
        for style in styles:
            question = make_question(item, definitions, style)
            if question:
                pools[style].append(question)
    result, used, position = [], set(), 0
    while len(result) < limit:
        added = False
        for style in styles:
            if position < len(pools[style]):
                question = pools[style][position]
                key = question["question"].casefold()
                if key not in used:
                    result.append(question)
                    used.add(key)
                    added = True
                if len(result) >= limit:
                    break
        if not added:
            break
        position += 1
    random.shuffle(result)
    return result[:limit]

def make_flashcards(definitions):
    return [{"front": x["term"], "back": x["meaning"], "shown": False} for x in definitions[:50]]

@app.route("/", methods=["GET"])
def home():
    saved = get_saved_results()
    return render_template("index.html", results=saved.get("results"), mode=saved.get("mode"), status=None)

@app.route("/generate/<kind>", methods=["POST"])
def generate(kind):
    if kind not in ("mcq", "flashcards"):
        return redirect(url_for("home"))
    uploaded = request.files.get("notes")
    if not uploaded or not uploaded.filename:
        return render_template("index.html", status="Please upload your notes first.", error=True)
    try:
        study_text = clean_study_text(read_file(uploaded))
        if len(study_text) < 40:
            raise ValueError("Very little text could be extracted. Scanned/image-only PDFs need OCR before uploading.")
        definitions = find_definitions(get_sentences(study_text))
        if not definitions:
            raise ValueError("No clear definitions or concept explanations were found. Try notes containing 'X is...', 'X refers to...', 'X means...', or 'X: meaning'.")
        if kind == "mcq":
            questions = make_mcqs(definitions, 30)
            if not questions:
                raise ValueError("At least four distinct concepts with usable explanations are needed for four-option MCQs.")
            saved_results = {"questions": questions, "cards": []}
            style_counts = Counter(q["style"] for q in questions)
            message = f"Generated {len(questions)} mixed-style MCQs from {len(definitions)} concepts ({len(style_counts)} question styles)."
        else:
            cards = make_flashcards(definitions)
            saved_results = {"questions": [], "cards": cards}
            message = f"Generated {len(cards)} flashcards from filtered study content."
        data_id = str(uuid.uuid4())
        RESULTS_STORE[data_id] = {"results": saved_results, "mode": kind}
        session["data_id"] = data_id
        return render_template("index.html", results=saved_results, mode=kind, status=message)
    except ValueError as error:
        return render_template("index.html", status=str(error), error=True)
    except Exception:
        app.logger.exception("Quiz generation failed")
        return render_template("index.html", status="Something went wrong while processing this file. Please try another PDF or TXT file.", error=True)

@app.route("/answer", methods=["POST"])
def answer():
    data = get_saved_results().get("results", {})
    questions = data.get("questions", [])
    try:
        index = int(request.form.get("question_index", -1))
    except (TypeError, ValueError):
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
    saved = get_saved_results()
    data = saved.get("results", {})
    mode = saved.get("mode")
    if not data or mode not in ("mcq", "flashcards"):
        return redirect(url_for("home"))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("QuizTitle", parent=styles["Title"], alignment=TA_CENTER, textColor=colors.HexColor("#174b86"), spaceAfter=14)
    body = styles["BodyText"]
    story = [Paragraph("CTPS Quiz Generator", title), Spacer(1, 5)]
    if mode == "mcq":
        for number, question in enumerate(data.get("questions", []), 1):
            parts = [Paragraph(f"<b>Q{number}. {escape(question['question'])}</b>", body), Spacer(1, 5)]
            for letter, option in zip("ABCD", question["options"]):
                parts.append(Paragraph(f"{letter}. {escape(str(option))}", body))
            parts.extend([Paragraph(f"<font color='#287653'><b>Correct answer: {escape(str(question['answer']))}</b></font>", body), Spacer(1, 12)])
            story.append(KeepTogether(parts))
    else:
        for number, card in enumerate(data.get("cards", []), 1):
            story.append(KeepTogether([
                Paragraph(f"<b>{number}. {escape(str(card['front']))}</b>", body),
                Paragraph(escape(str(card["back"])), body), Spacer(1, 12)
            ]))
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="CTPS_Practice_Material.pdf", mimetype="application/pdf")

if __name__ == "__main__":
    app.run(debug=True)
