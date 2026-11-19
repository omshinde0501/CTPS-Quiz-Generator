def make_question(item, definitions, style):
    distractors = get_distractors(item, definitions)

    if len(distractors) != 3:
        return None

    meaning = item["meaning"].strip().rstrip(".!?; ")
    term = item["term"]
    source = item["source"]

    if style == "definition":
        question = (
            "Which term matches this definition? "
            + meaning + "?"
        )

    elif style == "fill_blank":
        pattern = re.compile(re.escape(term), re.I)
        blanked, count = pattern.subn("________", source, count=1)

        if count == 0:
            return None

        question = "Fill in the blank: " + blanked

    elif style == "application":
        question = (
            "A student encounters the following situation: "
            + meaning
            + ". Which concept from the notes is most relevant?"
        )

    else:
        return None

    options = [term] + distractors
    random.shuffle(options)

    return {
        "question": question,
        "answer": term,
        "options": options,
        "selected": None,
        "checked": False,
        "style": style
    }


def make_mcqs(definitions, limit=30):
    if len(definitions) < 4:
        return []

    styles = [
        "definition",
        "fill_blank",
        "application"
    ]

    result = []
    used_questions = set()

    # Shuffle concepts so question order varies.
    concepts = definitions[:]
    random.shuffle(concepts)

    # Create separate question pools for each style.
    pools = {style: [] for style in styles}

    for item in concepts:
        for style in styles:
            question = make_question(item, definitions, style)

            if question:
                key = question["question"].casefold()

                if key not in used_questions:
                    pools[style].append(question)
                    used_questions.add(key)

    # Reset duplicate tracking for final selection.
    used_questions = set()

    # Select questions in rounds to maintain variety.
    position = 0

    while len(result) < limit:
        added = False

        for style in styles:
            if position < len(pools[style]):
                question = pools[style][position]
                key = question["question"].casefold()

                if key not in used_questions:
                    result.append(question)
                    used_questions.add(key)
                    added = True

                if len(result) >= limit:
                    break

        if not added:
            break

        position += 1

    random.shuffle(result)

    return result[:limit]
