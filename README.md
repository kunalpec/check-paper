# AI-Based Automated Answer Evaluation

A local Streamlit application for teachers to upload question papers, official solutions and student answer sheets; extract structured content with Gemini; and review grading produced by Gemini.

## Architecture and database

The application uses Python, Streamlit, SQLite (`sqlite3`), PyMuPDF, Google's `google-genai` SDK. Gemini handles visual OCR for PDFs converted to page images; Gemini evaluates the extracted questions, reference solutions, student answers and teacher guidelines. The database initializes automatically at startup and contains exactly five tables:

| Table | Purpose and relationships |
|---|---|
| `TEACHER` | Teacher accounts, unique email, PBKDF2 password hash. One teacher owns many question papers. |
| `QUESTION_PAPER` | Paper title, teacher FK, unique question and optional solution document FKs, extracted questions/reference JSON and evaluation guidelines. |
| `DOCUMENT` | File name, local path and one of `question_paper`, `solution`, `student_answer`. It has no OCR text/status columns. |
| `STUDENT` | Unique roll number, name and optional unique email. |
| `STUDENT_ATTEMPT` | Student and paper FKs, optional unique submission document FK, answers/evaluation JSON, marks and lifecycle timestamps/status. A student can have multiple attempts for the same paper. |

The solution workflow updates the selected existing `QUESTION_PAPER`; it never creates another paper or renumbers questions. Questions, reference answers, student answers and grading are matched by `question_number` within that paper/attempt. Primary keys come only from SQLite.

### OCR approach

The supplied OCR reference uses `google-genai`, PIL images, `generate_content`, JSON MIME output, a detailed `response_schema`, and `json.loads()`. PDFs are validated and rendered page-by-page to PIL images using PyMuPDF, then passed to Gemini with the reference's detailed exam OCR instructions. The OCR schema preserves document metadata, questions, options, subquestions, tables, visuals, equations, and annotations. Extracted `question_id` values are mapped to the app's `question_number` field only when a printed number can be parsed; uncertain IDs are left unmapped for teacher correction. Answer extraction and grading use their own validated Pydantic schemas. The API key remains configured as `GEMINI_API_KEY` in `.env`.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and use Python 3.11 or newer.
2. Sync the project dependencies (uv creates `.venv` automatically):

   ```powershell
   uv sync
   ```

3. Copy `.env.example` to `.env`. Set `GEMINI_API_KEY`; `GEMINI_MODEL` selects the Gemini model for OCR and evaluation. Never put the API key in source files.
4. Start the app:

   ```powershell
   uv run streamlit run app.py
   ```

The SQLite database and upload directories are created automatically under `storage/` by default. PDFs remain stored locally if Gemini fails; missing extraction can be retried from the paper page, and extracted answers can be edited before evaluation. The Gemini model is configurable; the default follows the supplied working image OCR code (`gemini-2.5-flash`).

## Teacher workflow

1. Create a teacher account and sign in.
2. Upload a question paper and review/correct the extracted `questions` JSON and criteria.
3. Upload the official solution later from **Upload solution**. It is attached to the existing paper and extracted against its question numbers.
4. Add students and assign them to a paper. Repeating an assignment creates a separate attempt.
5. In **Submissions**, upload each student's answer sheet and review/correct extracted answers.
6. In **Reports**, click **Evaluate all pending students**. Gemini evaluates one full paper per student, waits five seconds between students, and saves each completed result to SQLite. Review the results table and graph; teacher corrections remain available from **Submissions**.

The interface uses Streamlit's standard styling and a simple teacher sidebar. **Papers** shows paper-name buttons beside an in-app PDF preview; **Reports** lists approved results and provides a print-friendly PDF download.

Teachers can update paper details, student profiles, their account, and evaluation data. Delete actions require confirmation and remove dependent attempts and unshared uploaded files while keeping the five-table schema unchanged.

## JSON example

`sample_data.json` includes question, reference-answer, student-answer and evaluation examples. It is sample data only; the application does not claim an OCR or grading result until Gemini is configured and called.

## Operational notes

- PDFs are limited to 40 pages per upload to bound local rendering memory.
- Gemini/API and JSON validation errors are shown in the UI; uploads are preserved for retry.
- Teacher review is required for extracted questions, student answers and AI marks.
- This prototype has teacher account sign-in; students are managed by teachers and do not have separate login access.
