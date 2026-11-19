
import io
import random
import re
import uuid
from collections import OrderedDict

import fitz
from flask import Flask, render_template, request, redirect, url_for, session, send_file
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

STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "from", "into",
    "are", "which", "used", "use", "its", "their", "they", "them",
    "can", "may", "will", "would", "should", "could", "also",
    "such", "when", "where", "while", "through", "using", "often",
    "than", "then", "have", "has", "been", "being", "was", "were",
    "each", "more", "most", "some", "many", "other", "another",
    "very", "about", "because", "these", "those", "both", "only",
    "one", "two", "first", "second", "following", "example",
    "which", "what", "how", "why", "who"
}

BAD_HEADINGS = {
    "contents", "table of contents", "references", "bibliography",
    "index", "acknowledgements", "learning objectives",
    "introduction", "conclusion", "overview", "summary",
    "programming fundamentals", "digital electronics",
    "computational thinking", "problem solving",
    "advantages", "disadvantages", "applications",
    "examples", "definition", "definitions", "unit",
    "chapter", "module", "semester", "subject",
    "working", "description", "notes", "types", "steps",
    "classification", "history", "syllabus"
}


def get_saved_results():
    return RESULTS_STORE.get(session.get("data_id"), {})


def read_file(file):
    name = (file.filename or "").lower()
    data = file.read()

    if name.endswith(".txt"):
        return data.decode("utf-8-sig", errors="ignore")

    if name.endswith(".pdf"):
        try:
            with fitz.open(stream=data, filetype="pdf") as document:
                if document.is_encrypted and not document.authenticate(""):
                    raise ValueError(
                        "This PDF is password-protected. Upload an unlocked PDF."
                    )

                # Form-feed preserves page boundaries for extraction.
                return "\n\f\n".join(
                    page.get_text("text", sort=True)
                    for page in document
                )
        except fitz.FileDataError:
            raise ValueError("The PDF could not be opened. Try another file.")

    raise ValueError("Please choose a PDF or TXT file.")


def is_bad_heading(text):
    text = re.sub(r"\s+", " ", text).strip()
    lower = text.casefold().rstrip(" .:")

    if not text or len(text) < 3:
        return True

    if lower in BAD_HEADINGS:
        return True

    if re.match(
        r"^(unit|chapter|module|semester|subject)\s*[-:#\d]",
        text, re.I
    ):
        return True

    if re.match(r"^(page\s*)?\d+(\s*of\s*\d+)?$", text, re.I):
        return True

    if re.fullmatch(r"[\d\s./-]+", text):
        return True

    if re.match(
        r"^(read|refer to|study|follow|go through)\b",
        text, re.I
    ):
        return True

    return False


def clean_study_text(text):
    cleaned = []

    for line in text.replace("\x00", " ").splitlines():
        line = re.sub(r"\s+", " ", line).strip()

        if not line:
            cleaned.append("")
            continue

        if re.match(
            r"^(unit|chapter|module|semester)\s*[-:#\d]",
            line, re.I
        ):
            continue

        if re.match(r"^(page\s*)?\d+(\s*of\s*\d+)?$", line, re.I):
            continue

        if line.casefold().rstrip(" .:") in BAD_HEADINGS:
            continue

        cleaned.append(line)

    return "\n".join(cleaned)


def get_blocks(text):
    """
    Reconstruct paragraphs from PDF lines while preserving headings,
    question prompts and page boundaries.
    """
    blocks = []

    for page_number, page in enumerate(text.split("\f")):
        current = []

        def flush():
            if current:
                value = " ".join(current).strip()
                if len(value) >= 8:
                    blocks.append({
                        "text": value,
                        "page": page_number
                    })
                current.clear()

        for raw_line in page.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()

            if not line:
                flush()
                continue

            # A question-style heading is useful for identifying
            # the concept explained immediately after it.
            if re.match(
                r"^(what|define|explain)\s+(is|are|the)\b",
                line, re.I
            ) and len(line) < 120:
                flush()
                blocks.append({
                    "text": line,
                    "page": page_number
                })
                continue

            # Short unpunctuated lines are likely slide headings.
            heading = (
                len(line) <= 65
                and len(line.split()) <= 8
                and not re.search(r"[.!?]", line)
                and not re.match(
                    r"^(a|an|the)\s+\w+\s+(is|are|means)\b",
                    line, re.I
                )
            )

            if heading:
                flush()
                blocks.append({
                    "text": line,
                    "page": page_number
                })
                continue

            # Avoid joining a new sentence to a completed one.
            if current and re.search(r"[.!?]$", current[-1]):
                flush()

            current.append(line)

        flush()

    return blocks


def clean_term(term):
    term = re.sub(r"^\d+[\s.)-]+", "", term).strip()
    term = re.sub(r"^(a|an|the)\s+", "", term, flags=re.I)
    return re.sub(r"\s+", " ", term).strip(" ,;:-.")


def valid_term(term):
    term = clean_term(term)
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", term)

    if not words or len(words) > 7 or len(term) > 65:
        return False

    if term.casefold() in BAD_HEADINGS:
        return False

    if words[0].casefold() in STOP_WORDS:
        return False

    if re.search(
        r"\b(is|are|was|were|what|which|who|how|why|when|where)\b",
        term, re.I
    ):
        return False

    if re.search(r"[?=]", term):
        return False

    if re.match(
        r"^(the following|in this|according to|for example)\b",
        term, re.I
    ):
        return False

    if len(words) == 1 and len(words[0]) < 3:
        return False

    return True


def make_item(term, meaning, page, source):
    term = clean_term(term)
    meaning = re.sub(r"\s+", " ", meaning).strip()
    meaning = re.sub(r"^[,;:\-\s]+", "", meaning)
    meaning = re.sub(r"[.!?]+$", "", meaning).strip()

    if not valid_term(term):
        return None

    if len(meaning) < 20 or len(meaning.split()) < 5:
        return None

    if "?" in meaning:
        return None

    if re.match(
        r"^(what|which|who|when|where|why|how)\b",
        meaning, re.I
    ):
        return None

    if re.match(
        r"^(example|for example|note|figure|table|"
        r"source code|references)\b",
        meaning, re.I
    ):
        return None

    return {
        "term": term,
        "meaning": meaning,
        "page": page,
        "source": source
    }


def extract_sentence_definition(text, page):
    """
    Recognise definitions such as:
    A compiler is a program...
    Digital signals are...
    Programming is the process...
    Compiler: a system software component...
    """
    text = text.strip()
    if not text or "?" in text:
        return None

    patterns = [
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
        re.compile(
            r"^(.{2,65}?)\s*:\s*(?:is|are)?\s*(.{20,})$",
            re.I
        )
    ]

    for pattern in patterns:
        match = pattern.match(text)

        if not match:
            continue

        item = make_item(
            match.group(1),
            match.group(2),
            page,
            text
        )

        if item:
            return item

    # Recover a common imperfect note format:
    # "A variable a name used to store..."
    match = re.match(
        r"^(?:a|an)\s+"
        r"([A-Za-z][A-Za-z0-9+#.-]*(?:\s+"
        r"[A-Za-z][A-Za-z0-9+#.-]*){0,3})\s+"
        r"(?:a|an)\s+(.{20,})$",
        text, re.I
    )

    if match:
        return make_item(
            match.group(1), match.group(2), page, text
        )

    return None


def question_heading_term(text):
    match = re.match(
        r"^(?:what|define|explain)\s+(?:is|are|the)\s+"
        r"(.+?)\s*\??$",
        text.strip(), re.I
    )

    if not match:
        return None

    term = clean_term(match.group(1))
    return term if valid_term(term) else None


def heading_term(text):
    text = re.sub(r"^\d+[\s.)-]+", "", text).strip()
    text = clean_term(text)

    if is_bad_heading(text) or not valid_term(text):
        return None

    # A heading should be short, not a sentence or code fragment.
    if len(text.split()) > 6 or len(text) > 55:
        return None

    if re.search(r"[=<>{}\[\]();]", text):
        return None

    return text


def find_definitions(blocks):
    found = []

    # First pass: extract complete definitions.
    for block in blocks:
        item = extract_sentence_definition(
            block["text"], block["page"]
        )

        if item:
            found.append(item)

    # Second pass: pair question headings and standalone headings
    # with their explanations.
    for index, block in enumerate(blocks):
        heading = block["text"].strip()
        term = question_heading_term(heading)

        if not term:
            term = heading_term(heading)

        if not term:
            continue

        # Search the next few blocks on the same page.
        for following in blocks[index + 1:index + 4]:
            if following["page"] != block["page"]:
                break

            explanation = following["text"].strip()

            if not explanation or "?" in explanation:
                continue

            # A new heading means the previous heading had no
            # usable explanation.
            if question_heading_term(explanation):
                break

            # Prefer explanations that refer directly to the term.
            direct = extract_sentence_definition(
                explanation, following["page"]
            )

            if direct:
                if direct["term"].casefold() == term.casefold():
                    found.append(direct)
                    break

                # The following block defines another concept.
                break

            # Handle "It is a..." after a "What is X?" heading.
            pronoun = re.match(
                r"^(it|this|they)\s+(is|are)\s+(.+)$",
                explanation, re.I
            )

            if pronoun:
                explanation = pronoun.group(3)

            elif re.match(
                r"^(a|an|the)\s+" + re.escape(term) + r"\b",
                explanation, re.I
            ):
                explanation = re.sub(
                    r"^(a|an|the)\s+" + re.escape(term) + r"\s+",
                    "", explanation, count=1, flags=re.I
                )

            elif len(explanation.split()) <= 8:
                continue

            item = make_item(
                term,
                explanation,
                following["page"],
                heading + " " + explanation
            )

            if item:
                found.append(item)
                break

    # Remove duplicates while keeping the most complete explanation.
    unique = OrderedDict()

    for item in found:
        key = item["term"].casefold()

        if (
            key not in unique
            or len(item["meaning"]) > len(unique[key]["meaning"])
        ):
            unique[key] = item

    return list(unique.values())


def word_similarity(first, second):
    def words(item):
        result = set(
            re.findall(
                r"[a-z][a-z0-9+#-]{2,}",
                (item["term"] + " " + item["meaning"]).lower()
            )
        )
        return result - STOP_WORDS

    a = words(first)
    b = words(second)

    if not a or not b:
        return 0

    return len(a & b) / len(a | b)


def get_distractors(item, definitions):
    candidates = []

    for other in definitions:
        if other["term"].casefold() == item["term"].casefold():
            continue

        if not valid_term(other["term"]):
            continue

        similarity = word_similarity(item, other)

        # Prefer concepts on the same page, then related concepts
        # elsewhere in the uploaded document.
        same_page = other["page"] == item["page"]
        score = similarity + (0.15 if same_page else 0)

        candidates.append((other["term"], score))

    candidates.sort(key=lambda x: x[1], reverse=True)

    result = []
    seen = {item["term"].casefold()}

    for term, score in candidates:
        key = term.casefold()

        if key in seen:
            continue

        result.append(term)
        seen.add(key)

        if len(result) == 3:
            break

    return result


def valid_options(answer, distractors):
    if not isinstance(answer, str) or not answer.strip():
        return False

    if len(distractors) != 3:
        return False

    options = [answer] + distractors

    if any(
        not isinstance(option, str) or not option.strip()
        for option in options
    ):
        return False

    if any(not valid_term(option) for option in options):
        return False

    normalised = [option.strip().casefold() for option in options]
    return len(set(normalised)) == 4


def make_question(item, definitions):
    distractors = get_distractors(item, definitions)

    if not valid_options(item["term"], distractors):
        return None

    options = [item["term"]] + distractors
    random.shuffle(options)

    return {
        "question": (
            "Which term matches this definition? "
            + item["meaning"].rstrip(".!? ")
            + "?"
        ),
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
        if len(questions) >= limit:
            break

        key = item["term"].casefold()

        if key in used:
            continue

        question = make_question(item, definitions)

        if not question:
            continue

        questions.append(question)
        used.add(key)

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
                "Scanned or image-only PDFs need OCR."
            )

        blocks = get_blocks(study_text)
        definitions = find_definitions(blocks)

        if not definitions:
            raise ValueError(
                "No reliable concepts and explanations were found. "
                "Try a text-based PDF or TXT file with explanatory content."
            )

        if kind == "mcq":
            questions = make_mcqs(definitions, 30)

            if not questions:
                raise ValueError(
                    "The document did not contain enough distinct "
                    "concepts to create reliable four-option MCQs. "
                    "Try a more detailed document."
                )

            saved_results = {
                "questions": questions,
                "cards": []
            }

            message = (
                f"Generated {len(questions)} MCQs from "
                f"{len(definitions)} extracted concepts."
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

            data_id = session.get("data_id")

            if data_id in RESULTS_STORE:
                RESULTS_STORE[data_id]["results"] = data

    return redirect(url_for("home"))


@app.route("/reveal/<int:index>", methods=["POST"])
def reveal(index):
    data = get_saved_results().get("results", {})
    cards = data.get("cards", [])

    if 0 <= index < len(cards):
        cards[index]["shown"] = not cards[index].get(
            "shown", False
        )

        data_id = session.get("data_id")

        if data_id in RESULTS_STORE:
            RESULTS_STORE[data_id]["results"] = data

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
                    "<font color='#287653'><b>"
                    f"Correct answer: {escape(str(question['answer']))}"
                    "</b></font>",
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
                        f"<b>{number}. {escape(str(card['front']))}</b>",
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
