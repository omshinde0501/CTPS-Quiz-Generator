
from flask import Flask, render_template, request, redirect, url_for, session, send_file
import fitz
import io
import random
import re
import uuid
from collections import OrderedDict
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

INVALID_TERMS = {
    "what", "which", "who", "when", "where", "why", "how",
    "it", "they", "we", "you", "this", "that", "these", "those",
    "the", "a", "an", "is", "are", "was", "were",
    "system", "method", "process", "example", "following",
    "concept", "thing", "way", "type", "kind", "term", "use",
    "introduction", "conclusion", "contents", "references",
    "applications", "advantages", "disadvantages", "working",
    "overview", "objectives", "description", "notes", "chapter",
    "unit", "module", "definition", "types", "steps"
}

STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "from", "into",
    "are", "which", "used", "can", "its", "their", "when",
    "where", "often", "also", "has", "have", "was", "were",
    "been", "being", "such", "than", "then", "they", "them",
    "these", "those", "will", "would", "should", "could",
    "while", "through", "using", "use", "used", "one"
}


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
                    raise ValueError(
                        "This PDF is password-protected. Please upload an unlocked PDF."
                    )
                return "\n".join(
                    page.get_text("text", sort=True)
                    for page in document
                )
        except fitz.FileDataError:
            raise ValueError(
                "The uploaded PDF could not be read. Please try another PDF."
            )

    raise ValueError("Please choose a PDF or TXT file.")


def is_unwanted_line(line):
    text = line.strip()
    lower = text.lower()

    if len(text) < 3:
        return True

    if re.match(
        r"^(unit|chapter|module|semester|subject)\s*[-:\d]",
        text, re.I
    ):
        return True

    if re.match(r"^(page\s*)?\d+(\s*of\s*\d+)?$", text, re.I):
        return True

    unwanted = (
        r"^(how to use|instructions?|learning objectives|contents|"
        r"table of contents|index|references|bibliography|"
        r"acknowledgements?)\s*[:.]?$"
    )

    if re.match(unwanted, text, re.I):
        return True

    if re.match(
        r"^(read|refer to|study|follow|go through|"
        r"these notes|this unit|in this unit)\b",
        lower
    ):
        return True

    if re.match(
        r"^(quiz generator|generated notes|study notes)$",
        lower
    ):
        return True

    return False


def clean_study_text(text):
    lines = []

    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()

        if line and not is_unwanted_line(line):
            lines.append(line)

    return "\n".join(lines)


def get_sentences(text):
    lines = text.splitlines()
    paragraphs = []
    current = []

    def flush():
        if current:
            paragraphs.append(" ".join(current).strip())
            current.clear()

    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()

        if not line:
            flush()
            continue

        # Preserve short headings separately.
        is_heading = (
            len(line) <= 65
            and len(line.split()) <= 8
            and not re.search(r"[.!?]", line)
            and not re.match(
                r"^(a|an|the)\s+\w+\s+(is|are|means)\b",
                line, re.I
            )
        )

        if is_heading:
            flush()
            paragraphs.append(line)
        else:
            if current and re.search(r"[.!?]$", current[-1]):
                flush()
            current.append(line)

    flush()

    sentences = []

    for paragraph in paragraphs:
        parts = re.split(
            r"(?<=[.!?])\s+(?=[A-Z0-9])",
            paragraph
        )

        for part in parts:
            part = part.strip()

            if 12 < len(part) < 700:
                sentences.append(part)

    return sentences


def valid_term(term):
    term = term.strip(" ,;:-.")
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", term)

    if not words or len(words) > 7 or len(term) > 65:
        return False

    if term.casefold() in INVALID_TERMS:
        return False

    if words[0].casefold() in INVALID_TERMS:
        return False

    if re.search(
        r"\b(is|are|was|were|what|which|how|why|when|where)\b",
        term, re.I
    ):
        return False

    if re.match(
        r"^(the following|in this|according to|for example)\b",
        term, re.I
    ):
        return False

    return True


def extract_definition(sentence):
    line = re.sub(r"^[•\-*]\s*", "", sentence).strip()
    line = re.sub(r"^\d+[\s.)-]+", "", line).strip()

    if is_unwanted_line(line) or "?" in line:
        return None

    patterns = [
        # A compiler is a program...
        re.compile(
            r"^(?:a|an|the)\s+(.{2,65}?)\s+"
            r"(?:is defined as|are defined as|is referred to as|"
            r"refers to|is known as|are known as|is called|"
            r"are called|is a type of|are a type of|"
            r"is the process of|is|are|means|denotes|represents|"
            r"consists of|contains|includes|comprises|"
            r"allows|enables|performs|converts|stores|produces|"
            r"generates|controls|connects|requires|depends on)\s+"
            r"(.+)$",
            re.I
        ),

        # Compiler: a system software program...
        re.compile(
            r"^(.{2,65}?)\s*:\s*(?:is|are)?\s*(.{15,})$",
            re.I
        ),

        # Compiler is a system software program...
        re.compile(
            r"^(.{2,65}?)\s+"
            r"(?:is defined as|are defined as|refers to|"
            r"is known as|is called|means|denotes|represents|"
            r"consists of|contains|includes|comprises|"
            r"supports|allows|enables|performs|converts|"
            r"stores|produces|generates|controls|connects|"
            r"requires|depends on)\s+(.+)$",
            re.I
        ),

        # Handles imperfect notes such as "A variable a name..."
        re.compile(
            r"^(?:a|an)\s+([A-Za-z][A-Za-z0-9+#.-]*(?:\s+"
            r"[A-Za-z][A-Za-z0-9+#.-]*){0,4})\s+"
            r"(?:a|an)\s+(.{15,})$",
            re.I
        )
    ]

    for pattern in patterns:
        match = pattern.match(line)

        if not match:
            continue

        term = match.group(1).strip(" ,;:-.")
        meaning = match.group(2).strip()
        meaning = re.sub(r"[.!?]+$", "", meaning).strip()

        if not valid_term(term):
            continue

        if len(meaning) < 18 or len(meaning.split()) < 4:
            continue

        if re.match(
            r"^(what|which|who|when|where|why|how)\b",
            meaning, re.I
        ):
            continue

        if re.fullmatch(
            r"[\d\s.,;:()=+\-*/]+",
            meaning
        ):
            continue

        return {
            "term": term,
            "meaning": meaning,
            "source": sentence
        }

    return None


def find_definitions(sentences):
    found = []

    # First, extract definitions written in normal sentence format.
    for sentence in sentences:
        item = extract_definition(sentence)

        if item:
            found.append(item)

    # Next, handle standalone headings followed by explanations.
    for index in range(len(sentences) - 1):
        heading = sentences[index].strip()
        explanation = sentences[index + 1].strip()

        heading = re.sub(r"^\d+[\s.)-]+", "", heading).strip()

        if not valid_term(heading):
            continue

        if len(heading.split()) > 6:
            continue

        if extract_definition(heading):
            continue

        if len(explanation.split()) < 5:
            continue

        if len(explanation) < 20:
            continue

        if is_unwanted_line(explanation):
            continue

        # Avoid pairing two unrelated complete definitions.
        if extract_definition(explanation):
            continue

        if re.match(
            r"^(example|for example|note|figure|table|source code)\b",
            explanation, re.I
        ):
            continue

        found.append({
            "term": heading,
            "meaning": re.sub(r"[.!?]+$", "", explanation),
            "source": heading + " " + explanation
        })

    # Remove duplicate concepts and retain the fuller explanation.
    unique = OrderedDict()

    for item in found:
        key = re.sub(
            r"\s+", " ", item["term"]
        ).casefold()

        if (
            key not in unique
            or len(item["meaning"]) > len(unique[key]["meaning"])
        ):
            unique[key] = item

    return list(unique.values())


def get_category(item):
    text = (item["term"] + " " + item["meaning"]).lower()

    groups = {
        "programming_languages": [
            "python", "java", "javascript", "c++",
            "c language", "sql", "pascal", "prolog", "haskell"
        ],
        "programming_methods": [
            "procedural", "object-oriented",
            "functional programming", "logic programming",
            "declarative", "concurrent", "parallel"
        ],
        "software_tools": [
            "compiler", "interpreter", "linker",
            "loader", "assembler", "debugger"
        ],
        "program_errors": [
            "syntax error", "semantic error", "type error",
            "linking error", "runtime error", "logical error"
        ],
        "logic_gates": [
            "and gate", "or gate", "not gate", "nand gate",
            "nor gate", "xor gate", "xnor gate"
        ],
        "data_concepts": [
            "variable", "constant", "data type", "array",
            "string", "integer", "float", "boolean", "operator",
            "list", "tuple", "dictionary", "set"
        ],
        "control_structures": [
            "if statement", "if-else", "loop",
            "for loop", "while loop", "switch",
            "iteration", "selection"
        ]
    }

    for category, terms in groups.items():
        if any(term in text for term in terms):
            return category

    return "other"


def word_similarity(first, second):
    a = set(
        re.findall(
            r"[a-z]{3,}",
            (first["term"] + " " + first["meaning"]).lower()
        )
    ) - STOP_WORDS

    b = set(
        re.findall(
            r"[a-z]{3,}",
            (second["term"] + " " + second["meaning"]).lower()
        )
    ) - STOP_WORDS

    if not a or not b:
        return 0

    return len(a & b) / len(a | b)


def get_distractors(item, definitions):
    candidates = []
    category = get_category(item)

    for other in definitions:
        if other["term"].casefold() == item["term"].casefold():
            continue

        if not valid_term(other["term"]):
            continue

        other_category = get_category(other)
        similarity = word_similarity(item, other)

        # Prefer concepts from the same technical category.
        score = similarity

        if category != "other" and other_category == category:
            score += 2

        # Give a small preference to concepts with similar wording.
        if similarity > 0:
            score += similarity

        candidates.append((other["term"], score))

    candidates.sort(key=lambda x: x[1], reverse=True)

    distractors = []
    seen = {item["term"].casefold()}

    for term, score in candidates:
        key = term.casefold()

        if key not in seen:
            distractors.append(term)
            seen.add(key)

        if len(distractors) == 3:
            break

    return distractors


def make_question(item, definitions):
    distractors = get_distractors(item, definitions)

    if len(distractors) != 3:
        return None

    question = (
        "Which term matches this definition? "
        + item["meaning"].rstrip(".!?; ")
        + "?"
    )

    options = [item["term"]] + distractors
    random.shuffle(options)

    return {
        "question": question,
        "answer": item["term"],
        "options": options,
        "selected": None,
        "checked": False
    }


def make_mcqs(definitions, limit=30):
    questions = []
    used = set()

    concepts = definitions[:]
    random.shuffle(concepts)

    for item in concepts:
        question = make_question(item, definitions)

        if not question:
            continue

        key = question["question"].casefold()

        if key in used:
            continue

        # Confirm all four choices are different.
        if len(set(x.casefold() for x in question["options"])) != 4:
            continue

        questions.append(question)
        used.add(key)

        if len(questions) >= limit:
            break

    return questions


def make_flashcards(definitions):
    return [
        {
            "front": item["term"],
            "back": item["meaning"],
            "shown": False
        }
        for item in definitions[:50]
    ]


@app.route("/", methods=["GET"])
def home():
    saved = get_saved_results()

    return render_template(
        "index.html",
        results=saved.get("results"),
        mode=saved.get("mode"),
        status=None
    )


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
        study_text = clean_study_text(read_file(uploaded))

        if len(study_text) < 40:
            raise ValueError(
                "Very little text could be extracted. "
                "Scanned/image-only PDFs need OCR before uploading."
            )

        sentences = get_sentences(study_text)
        definitions = find_definitions(sentences)

        if not definitions:
            raise ValueError(
                "No clear concepts or explanations were found. "
                "Please try a text-based PDF or TXT file."
            )

        if kind == "mcq":
            questions = make_mcqs(definitions, 30)

            if not questions:
                raise ValueError(
                    "The document did not contain enough distinct "
                    "concepts to create reliable four-option MCQs."
                )

            saved_results = {
                "questions": questions,
                "cards": []
            }

            message = (
                f"Generated {len(questions)} definition-based MCQs "
                f"from {len(definitions)} extracted concepts."
            )

        else:
            cards = make_flashcards(definitions)

            saved_results = {
                "questions": [],
                "cards": cards
            }

            message = (
                f"Generated {len(cards)} flashcards "
                f"from extracted concepts."
            )

        data_id = str(uuid.uuid4())

        RESULTS_STORE[data_id] = {
            "results": saved_results,
            "mode": kind
        }

        session["data_id"] = data_id

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
                "Please try another PDF or TXT file."
            ),
            error=True
        )


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
        cards[index]["shown"] = not cards[index].get(
            "shown", False
        )

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

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    styles = getSampleStyleSheet()

    title = ParagraphStyle(
        "QuizTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#174b86"),
        spaceAfter=14
    )

    body = styles["BodyText"]

    story = [
        Paragraph("CTPS Quiz Generator", title),
        Spacer(1, 5)
    ]

    if mode == "mcq":
        for number, question in enumerate(
            data.get("questions", []), 1
        ):
            parts = [
                Paragraph(
                    f"<b>Q{number}. "
                    f"{escape(question['question'])}</b>",
                    body
                ),
                Spacer(1, 5)
            ]

            for letter, option in zip(
                "ABCD", question["options"]
            ):
                parts.append(
                    Paragraph(
                        f"{letter}. {escape(str(option))}",
                        body
                    )
                )

            parts.extend([
                Paragraph(
                    f"<font color='#287653'><b>"
                    f"Correct answer: "
                    f"{escape(str(question['answer']))}"
                    f"</b></font>",
                    body
                ),
                Spacer(1, 12)
            ])

            story.append(KeepTogether(parts))

    else:
        for number, card in enumerate(
            data.get("cards", []), 1
        ):
            story.append(
                KeepTogether([
                    Paragraph(
                        f"<b>{number}. "
                        f"{escape(str(card['front']))}</b>",
                        body
                    ),
                    Paragraph(
                        escape(str(card["back"])),
                        body
                    ),
                    Spacer(1, 12)
                ])
            )

    doc.build(story)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="CTPS_Practice_Material.pdf",
        mimetype="application/pdf"
    )


if __name__ == "__main__":
    app.run(debug=True)
