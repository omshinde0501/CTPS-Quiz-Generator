CTPS QUIZ GENERATOR - PYTHON WEBSITE

FILES
- app.py: Python server, PDF/TXT reading, definition filtering, MCQ and flashcard generation, answer checking and PDF export.
- templates/index.html: website structure.
- static/style.css: website design.
- requirements.txt: required Python libraries.

RUN
1. Install Python.
2. Open a terminal in this project folder.
3. Install libraries:  pip install -r requirements.txt
4. Start the website:  python app.py
5. Open this address in your browser:  http://127.0.0.1:5000
6. Upload a text-based PDF or TXT file and generate MCQs or flashcards.

NOTES
- Keep the templates and static folders in the same folder as app.py.
- The app uses Python and Flask; it does not need a separate JavaScript file.
- The generator is rule-based and does not use an AI API.
- Scanned/image-only PDFs need OCR before uploading.
- Four-option MCQs are created only when three related distractor terms can be found. The program skips weak questions rather than filling options with unrelated terms.
- Use Download as PDF to save the generated practice material.
- For deployment beyond your own computer, change the Flask secret key and use a production server.
