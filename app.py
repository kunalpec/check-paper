"""Streamlit application for AI-assisted answer-sheet evaluation."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

from database.db import ROOT, connect, decode_json, encode_json, initialize
from services import gemini_service, pdf_service
from services.report_service import evaluation_report_pdf
from services import record_management
from utils.auth import hash_password, verify_password

load_dotenv(ROOT / ".env")
initialize()
st.set_page_config(page_title="Answer Evaluation", layout="wide")


def display_questions(questions: dict[str, Any]) -> None:
    """Show extracted questions without truncating long text or nested OCR data."""
    for index, item in enumerate(questions.get("questions", []), start=1):
        identifier = item.get("question_id") or item.get("question_number") or f"Unmapped {index}"
        marks = item.get("max_marks", item.get("marks", 0))
        with st.expander(f"Question {identifier} · {marks} marks"):
            st.write(item.get("question_text", "[Question text not extracted]"))
            if item.get("options"):
                st.markdown("**Options**")
                for option in item["options"]:
                    st.write(f"- {option}")
            if item.get("subquestions"):
                st.markdown("**Subquestions**")
                for subquestion in item["subquestions"]:
                    st.write(
                        f"{subquestion.get('question_id', '')} · "
                        f"{subquestion.get('max_marks', 0)} marks · "
                        f"{subquestion.get('question_text', '')}"
                    )
            for label, field in [
                ("Tables", "tables"),
                ("Figures and diagrams", "visual_elements"),
                ("Equations", "equations"),
                ("Handwritten annotations", "handwritten_annotations"),
                ("Checking criteria", "checking_criteria"),
            ]:
                if item.get(field):
                    st.markdown(f"**{label}**")
                    st.json(item[field])
    with st.expander("View complete extracted JSON"):
        st.json(questions, expanded=1)


def display_answers(answers: dict[str, Any], questions: dict[str, Any], answer_label: str) -> None:
    """Show each answer in a short dropdown with question and answer separated."""
    question_texts = {
        item.get("question_number"): item.get("question_text", "")
        for item in questions.get("questions", [])
    }
    for index, answer in enumerate(answers.get("answers", []), start=1):
        number = answer.get("question_number")
        identifier = f"Q{number}" if number is not None else f"Unmatched answer {index}"
        with st.expander(f"{identifier} · {answer_label}"):
            question_text = question_texts.get(number)
            if question_text:
                st.markdown("**Question**")
                st.write(question_text)
            st.markdown(f"**{answer_label}**")
            st.write(answer.get("answer_text", "[No answer text extracted]"))


def paper_has_questions(paper: dict[str, Any]) -> bool:
    """Return whether a paper has at least one extracted question."""
    data = decode_json(paper.get("questions_json"), {})
    return bool(data.get("questions"))


def normalize_evaluation(evaluation: dict[str, Any], questions: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """Check question coverage and marks, then calculate the approved total."""
    question_map = {
        int(item["question_number"]): float(item.get("marks", item.get("max_marks", 0)))
        for item in questions.get("questions", [])
        if isinstance(item.get("question_number"), int)
    }
    normalized = []
    for row in evaluation.get("results", []):
        question_number = int(row["question_number"])
        if question_number not in question_map:
            raise ValueError(f"Q{question_number} is not part of this question paper.")
        maximum = question_map[question_number]
        marks = float(row["marks_obtained"])
        if marks < 0 or marks > maximum:
            raise ValueError(f"Q{question_number} marks must be between 0 and {maximum:g}.")
        normalized.append({**row, "question_number": question_number, "max_marks": maximum, "marks_obtained": marks})
    if sorted(row["question_number"] for row in normalized) != sorted(question_map):
        raise ValueError("Evaluation must include exactly one result for each question.")
    total = sum(row["marks_obtained"] for row in normalized)
    evaluation["results"] = normalized
    evaluation["total_marks"] = total
    return evaluation, total


def save_upload(upload: Any, category: str) -> tuple[int, str]:
    """Persist an uploaded PDF and create its document metadata row."""
    if upload is None:
        raise ValueError("Choose a PDF first.")
    if not upload.name.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are supported.")
    data = upload.getvalue()
    pdf_service.pdf_to_images(data)  # validate before retaining or creating records
    configured = Path(os.getenv("UPLOAD_DIR", "storage"))
    base = configured if configured.is_absolute() else ROOT / configured
    folder = base / category
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(upload.name).name)
    destination = folder / f"{uuid.uuid4().hex}_{safe_name}"
    destination.write_bytes(data)
    relative_path = str(destination.relative_to(ROOT)) if destination.is_relative_to(ROOT) else str(destination)
    try:
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO DOCUMENT(file_name,file_path,document_type) VALUES(?,?,?)",
                (upload.name, relative_path, category),
            )
            return int(cursor.lastrowid), relative_path
    except Exception:
        # Keep the upload available for recovery if metadata creation fails.
        raise


def document_images(path: str) -> list[Any]:
    """Load a stored PDF and render its pages for Gemini."""
    full_path = Path(path)
    if not full_path.is_absolute():
        full_path = ROOT / full_path
    if not full_path.exists():
        raise FileNotFoundError(f"Stored PDF not found: {full_path}")
    return pdf_service.pdf_to_images(full_path.read_bytes())


def json_editor(label: str, value: Any, key: str) -> Any | None:
    """Render an editable JSON box and return its validated Python value."""
    raw = st.text_area(label, json.dumps(value, ensure_ascii=False, indent=2), height=260, key=key)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("JSON root must be an object.")
        return parsed
    except (json.JSONDecodeError, ValueError) as exc:
        st.error(f"Invalid JSON: {exc}")
        return None


def login_screen() -> None:
    """Handle teacher registration and sign-in."""
    st.title("AI Answer Evaluation")
    st.caption("Teacher workspace · question papers, submissions and review")
    login, register = st.tabs(["Sign in", "Create teacher account"])
    with login:
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in")
        if submitted:
            with connect() as db:
                teacher = db.execute("SELECT * FROM TEACHER WHERE email = ?", (email.strip().lower(),)).fetchone()
            if teacher and verify_password(password, teacher["password_hash"]):
                st.session_state.teacher_id = teacher["teacher_id"]
                st.session_state.teacher_name = teacher["name"]
                st.rerun()
            st.error("Email or password is incorrect.")
    with register:
        with st.form("register"):
            name = st.text_input("Name")
            email = st.text_input("Email address")
            password = st.text_input("Password", type="password")
            confirm = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create account")
        if submitted:
            if not name.strip() or "@" not in email or len(password) < 10 or password != confirm:
                st.error("Enter your name and a valid email; use a 10+ character password and confirm it.")
            else:
                try:
                    with connect() as db:
                        cursor = db.execute("INSERT INTO TEACHER(name,email,password_hash) VALUES(?,?,?)", (name.strip(), email.strip().lower(), hash_password(password)))
                        st.session_state.teacher_id = cursor.lastrowid
                        st.session_state.teacher_name = name.strip()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not create account: {exc}")


if "teacher_id" not in st.session_state:
    login_screen()
    st.stop()

teacher_id = int(st.session_state.teacher_id)
with st.sidebar:
    st.title("Answer Evaluation")
    st.caption(f"Teacher: {st.session_state.teacher_name}")
    with st.expander("Teacher account"):
        with connect() as db:
            account = db.execute("SELECT name,email FROM TEACHER WHERE teacher_id=?", (teacher_id,)).fetchone()
        with st.form("edit_teacher_profile"):
            account_name = st.text_input("Name", account["name"])
            account_email = st.text_input("Email", account["email"])
            new_password = st.text_input("New password (optional)", type="password")
            save_profile = st.form_submit_button("Update account")
        if save_profile:
            if not account_name.strip() or "@" not in account_email or (new_password and len(new_password) < 10):
                st.error("Enter a valid name and email. A new password must have at least 10 characters.")
            else:
                try:
                    password_hash = hash_password(new_password) if new_password else None
                    record_management.update_teacher_profile(teacher_id, account_name, account_email, password_hash)
                    st.session_state.teacher_name = account_name.strip()
                    st.success("Account updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not update account: {exc}")
        confirm_account_delete = st.text_input("Type DELETE to remove this account and its data", key="confirm_teacher_delete")
        if st.button("Delete teacher account", key="delete_teacher_account"):
            if confirm_account_delete != "DELETE":
                st.error("Type DELETE exactly to confirm account and data removal.")
            else:
                try:
                    record_management.delete_teacher_account(teacher_id)
                    st.session_state.pop("teacher_id", None)
                    st.session_state.pop("teacher_name", None)
                    st.success("Teacher account and its records were deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not delete account: {exc}")
    page = st.radio(
        "Page",
        ["Dashboard", "Papers", "Upload Paper", "Solutions", "Students", "Submissions", "Reports"],
    )
    if st.button("Logout"):
        del st.session_state.teacher_id
        st.rerun()

with connect() as db:
    papers = [dict(row) for row in db.execute("SELECT * FROM QUESTION_PAPER WHERE teacher_id=? ORDER BY created_at DESC", (teacher_id,)).fetchall()]

if page == "Dashboard":
    st.title("Teacher Dashboard")
    st.caption("Examination Management and Automated Evaluation System")
    with connect() as db:
        student_count = db.execute("SELECT COUNT(*) FROM STUDENT").fetchone()[0]
        totals = db.execute("SELECT COUNT(*) total, SUM(status='evaluated') evaluated FROM STUDENT_ATTEMPT WHERE paper_id IN (SELECT paper_id FROM QUESTION_PAPER WHERE teacher_id=?)", (teacher_id,)).fetchone()
        recent_attempts = [dict(row) for row in db.execute(
            "SELECT a.attempt_id,s.name AS student_name,s.roll_no,q.title AS paper_title,a.submitted_at,a.status "
            "FROM STUDENT_ATTEMPT a JOIN STUDENT s ON s.student_id=a.student_id "
            "JOIN QUESTION_PAPER q ON q.paper_id=a.paper_id "
            "WHERE q.teacher_id=? ORDER BY a.attempt_id DESC LIMIT 10", (teacher_id,)
        ).fetchall()]
    completed = totals["evaluated"] or 0
    pending = (totals["total"] or 0) - completed
    cols = st.columns(5)
    for col, label, value in zip(cols, ["Total Question Papers", "Total Students", "Total Submissions", "Pending Evaluations", "Completed Evaluations"], [len(papers), student_count, totals["total"], pending, completed]):
        col.metric(label, value or 0)
    st.subheader("Recent Question Papers")
    st.dataframe([
        {"Paper ID": p["paper_id"], "Title": p["title"], "Questions": "Ready" if paper_has_questions(p) else "Pending", "Solution Status": "Uploaded" if p["reference_solution_json"] else "Pending", "Action": "Open in Question Papers"}
        for p in papers[:10]
    ], width="stretch", hide_index=True)
    st.subheader("Recent Student Submissions")
    st.dataframe([
        {"Attempt ID": row["attempt_id"], "Student": f"{row['student_name']} ({row['roll_no']})", "Question Paper": row["paper_title"], "Submission Date": row["submitted_at"] or "—", "Status": row["status"]}
        for row in recent_attempts
    ], width="stretch", hide_index=True)

elif page == "Upload Paper":
    st.title("Upload question paper")
    with st.form("new_paper"):
        title = st.text_input("Paper title")
        guidelines = st.text_area("Evaluation guidelines", "Award partial marks for correct concepts. Do not require exact wording. Follow the maximum marks for each question.")
        upload = st.file_uploader("Question paper PDF", type=["pdf"], key="question_pdf")
        submit = st.form_submit_button("Save and extract questions")
    if submit:
        if not title.strip() or upload is None:
            st.error("Enter a title and choose a PDF.")
        else:
            try:
                images = pdf_service.pdf_to_images(upload.getvalue())
                document_id, _ = save_upload(upload, "question_paper")
                with connect() as db:
                    cursor = db.execute("INSERT INTO QUESTION_PAPER(teacher_id,title,question_pdf_id,evaluation_guidelines) VALUES(?,?,?,?)", (teacher_id, title.strip(), document_id, guidelines.strip()))
                    paper_id = cursor.lastrowid
                with st.spinner("Extracting numbered questions with Gemini…"):
                    extracted = gemini_service.extract_questions(images)
                st.session_state[f"questions_{paper_id}"] = extracted
                st.session_state["current_paper"] = paper_id
                with connect() as db:
                    db.execute("UPDATE QUESTION_PAPER SET questions_json=? WHERE paper_id=? AND teacher_id=?", (encode_json(extracted), paper_id, teacher_id))
                st.success("Paper saved and questions extracted. Review the questions on the Papers page.")
                display_questions(extracted)
            except Exception as exc:
                st.error(f"Upload or extraction failed: {exc}. The uploaded PDF and paper record are retained where already saved.")

elif page == "Papers":
    left_space, content, right_space = st.columns([1, 5, 1])
    with content:
        st.markdown("<h2 style='text-align:center'>Question papers</h2>", unsafe_allow_html=True)
        if not papers:
            st.caption("No question papers have been uploaded yet.")
        else:
            st.markdown("<h4 style='text-align:center'>Available papers</h4>", unsafe_allow_html=True)
            for item in papers:
                button_left, button_center, button_right = st.columns([1, 3, 1])
                with button_center:
                    if st.button(item["title"], key=f"paper_item_{item['paper_id']}", width="stretch"):
                        st.session_state["view_paper_id"] = item["paper_id"]
                        st.session_state[f"paper_page_{item['paper_id']}"] = 1
                        st.rerun()

            selected_id = st.session_state.get("view_paper_id")
            paper = next((item for item in papers if item["paper_id"] == selected_id), None)
            if paper is None:
                message_left, message_center, message_right = st.columns([1, 4, 1])
                message_center.markdown(
                    "<div style='text-align:center'>Select a paper to preview its PDF.</div>",
                    unsafe_allow_html=True,
                )
            else:
                title_column, close_column = st.columns([8, 2])
                with title_column:
                    title_left, title_center, title_right = st.columns([1, 4, 1])
                    title_center.markdown(f"**{paper['title']}**")
                with close_column:
                    if st.button("X Close", key=f"close_paper_{paper['paper_id']}", width="content"):
                        st.session_state.pop("view_paper_id", None)
                        st.rerun()

                with connect() as db:
                    doc = db.execute("SELECT file_path,file_name FROM DOCUMENT WHERE document_id=?", (paper["question_pdf_id"],)).fetchone()
                path = ROOT / doc["file_path"] if not Path(doc["file_path"]).is_absolute() else Path(doc["file_path"])
                if path.exists():
                    st.caption(doc["file_name"])
                    try:
                        pdf_bytes = path.read_bytes()
                        pages = pdf_service.page_count(pdf_bytes)
                        page_number = st.number_input("PDF page", min_value=1, max_value=pages, step=1, key=f"paper_page_{paper['paper_id']}")
                        page_image = pdf_service.render_page(pdf_bytes, int(page_number))
                        st.image(page_image, caption=f"Page {page_number} of {pages}", width="stretch")
                    except Exception as exc:
                        st.error(f"Could not display this PDF: {exc}")
                    st.download_button("Download PDF", path.read_bytes(), file_name=doc["file_name"], mime="application/pdf")
                else:
                    st.error("The saved question paper PDF could not be found in local storage.")

                st.divider()
                st.subheader("Extracted questions")
                extracted_questions = decode_json(paper["questions_json"], {"questions": []})
                unmapped_questions = [
                    item for item in extracted_questions.get("questions", [])
                    if not isinstance(item.get("question_number"), int)
                ]
                if unmapped_questions:
                    st.warning("Some printed question IDs could not be read as numbers. Correct their question_number values in the JSON before adding a solution or grading answers.")
                if extracted_questions.get("questions"):
                    display_questions(extracted_questions)
                else:
                    st.info("Questions have not been extracted yet. Check the Gemini API configuration and retry.")
                with st.form(f"paper_{paper['paper_id']}"):
                    paper_title = st.text_input("Paper title", paper["title"])
                    guidelines = st.text_area("Evaluation guidelines", paper["evaluation_guidelines"] or "")
                    questions = json_editor("Questions JSON · review and correct numbering, marks and criteria", decode_json(paper["questions_json"], {"questions": []}), f"qjson_{paper['paper_id']}")
                    references = None
                    if paper["reference_solution_json"]:
                        references = json_editor("Reference solution JSON", decode_json(paper["reference_solution_json"], {"answers": []}), f"reference_{paper['paper_id']}")
                    save = st.form_submit_button("Save paper details")
                if save and questions is not None and (references is not None or not paper["reference_solution_json"]):
                    try:
                        record_management.update_question_paper(
                            teacher_id,
                            paper["paper_id"],
                            paper_title,
                            guidelines,
                            encode_json(questions),
                            encode_json(references) if references is not None else None,
                        )
                        st.success("Paper details updated.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not save: {exc}")
                if not paper_has_questions(paper) and st.button("Retry question extraction", key=f"retry_questions_{paper['paper_id']}"):
                    try:
                        with st.spinner("Extracting questions with Gemini…"):
                            extracted = gemini_service.extract_questions(document_images(doc["file_path"]))
                        with connect() as db:
                            db.execute("UPDATE QUESTION_PAPER SET questions_json=? WHERE paper_id=?", (encode_json(extracted), paper["paper_id"]))
                        st.success("Questions extracted. Review and save them above.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Question extraction failed: {exc}")
                if paper["reference_solution_json"]:
                    st.subheader("Official reference answers")
                    display_answers(
                        decode_json(paper["reference_solution_json"]),
                        decode_json(paper["questions_json"], {"questions": []}),
                        "Reference answer",
                    )
                with connect() as db:
                    paper_attempt_count = db.execute("SELECT COUNT(*) FROM STUDENT_ATTEMPT WHERE paper_id=?", (paper["paper_id"],)).fetchone()[0]
                st.caption(f"Deleting this paper also deletes {paper_attempt_count} student attempt(s) and their unshared uploaded PDFs.")
                confirm_paper_delete = st.checkbox("I understand this permanently deletes this paper and linked records", key=f"confirm_delete_paper_{paper['paper_id']}")
                if st.button("Delete question paper", key=f"delete_paper_{paper['paper_id']}"):
                    if not confirm_paper_delete:
                        st.error("Confirm deletion before continuing.")
                    else:
                        try:
                            record_management.delete_question_paper(teacher_id, paper["paper_id"])
                            st.session_state.pop("view_paper_id", None)
                            st.success("Question paper and linked records deleted.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not delete question paper: {exc}")

elif page == "Solutions":
    st.title("Upload official solution")
    if not papers:
        st.info("Upload a question paper first.")
    else:
        paper = st.selectbox("Question paper", papers, format_func=lambda p: f"{p['title']} (#{p['paper_id']})")
        upload = st.file_uploader("Official solution PDF", type=["pdf"], key="solution_pdf")
        if st.button("Save and extract reference answers"):
            if upload is None:
                st.error("Choose a solution PDF.")
            elif not paper["questions_json"]:
                st.error("Extract and review this paper's questions before adding its solution.")
            else:
                try:
                    images = pdf_service.pdf_to_images(upload.getvalue())
                    document_id, _ = save_upload(upload, "solution")
                    with connect() as db:
                        db.execute("UPDATE QUESTION_PAPER SET solution_pdf_id=? WHERE paper_id=? AND teacher_id=?", (document_id, paper["paper_id"], teacher_id))
                    with st.spinner("Extracting reference answers against the existing question numbers…"):
                        result = gemini_service.extract_reference_answers(images, decode_json(paper["questions_json"]))
                    st.session_state[f"solution_draft_{paper['paper_id']}"] = result
                    st.success("Reference answers extracted. Confirm the question mappings below before saving.")
                except Exception as exc:
                    st.error(f"Solution extraction failed: {exc}. The existing paper remains unchanged except for any saved solution PDF reference.")
        draft = st.session_state.get(f"solution_draft_{paper['paper_id']}")
        if draft:
            st.subheader("Confirm reference answer mapping")
            questions_data = decode_json(paper["questions_json"], {"questions": []})
            question_lookup = {item["question_number"]: item.get("question_text", "") for item in questions_data.get("questions", [])}
            display_answers(draft, questions_data, "Reference answer")
            with st.expander("Edit reference answer JSON"):
                edited_solution = json_editor("Correct answer text or question numbers", draft, f"solution_json_{paper['paper_id']}")
            confirmed = st.checkbox("I reviewed and confirm these question-number mappings", key=f"confirm_solution_{paper['paper_id']}")
            if st.button("Confirm and save reference solution", key=f"save_solution_{paper['paper_id']}"):
                if not confirmed:
                    st.error("Please confirm the question-number mappings first.")
                elif edited_solution is not None:
                    valid_numbers = set(question_lookup)
                    unmatched = [a.get("question_number") for a in edited_solution.get("answers", []) if a.get("question_number") not in valid_numbers]
                    if unmatched:
                        st.error(f"These question numbers do not exist in the selected paper: {unmatched}. Correct them before saving.")
                    else:
                        with connect() as db:
                            db.execute("UPDATE QUESTION_PAPER SET reference_solution_json=? WHERE paper_id=? AND teacher_id=?", (encode_json(edited_solution), paper["paper_id"], teacher_id))
                        st.session_state.pop(f"solution_draft_{paper['paper_id']}", None)
                        st.session_state.pop(f"confirm_solution_{paper['paper_id']}", None)
                        st.success("Reference solution saved to the existing question paper.")
                        st.rerun()

elif page == "Students":
    st.title("Students")
    with st.form("student_create"):
        name = st.text_input("Student name")
        roll = st.text_input("Roll number")
        email = st.text_input("Email (optional)")
        paper = st.selectbox("Assign question paper", papers, format_func=lambda p: p["title"]) if papers else None
        submit = st.form_submit_button("Create student and assign paper")
    if submit:
        if not name.strip() or not roll.strip() or paper is None:
            st.error("Name, roll number and an existing question paper are required.")
        else:
            try:
                with connect() as db:
                    student = db.execute("SELECT student_id FROM STUDENT WHERE roll_no=?", (roll.strip(),)).fetchone()
                    student_id = student["student_id"] if student else db.execute("INSERT INTO STUDENT(roll_no,name,email) VALUES(?,?,?)", (roll.strip(), name.strip(), email.strip() or None)).lastrowid
                    db.execute("UPDATE STUDENT SET name=?, email=? WHERE student_id=?", (name.strip(), email.strip() or None, student_id))
                    db.execute("INSERT INTO STUDENT_ATTEMPT(student_id,paper_id,status) VALUES(?,?,'assigned')", (student_id, paper["paper_id"]))
                st.success("Student saved and assigned. Repeat assignments create separate attempts.")
            except Exception as exc:
                st.error(f"Could not save student or assignment: {exc}")
    with connect() as db:
        rows = [dict(row) for row in db.execute("SELECT s.student_id,s.roll_no,s.name,s.email,COUNT(a.attempt_id) attempts FROM STUDENT s LEFT JOIN STUDENT_ATTEMPT a ON a.student_id=s.student_id GROUP BY s.student_id ORDER BY s.roll_no").fetchall()]
    st.dataframe([{key: value for key, value in row.items() if key != "student_id"} for row in rows], width="stretch", hide_index=True)
    if rows:
        student = st.selectbox("Manage student", rows, format_func=lambda row: f"{row['roll_no']} · {row['name']}")
        with st.form(f"edit_student_{student['student_id']}"):
            updated_roll = st.text_input("Roll number", student["roll_no"])
            updated_name = st.text_input("Student name", student["name"])
            updated_email = st.text_input("Email (optional)", student["email"] or "")
            update_student = st.form_submit_button("Update student")
        if update_student:
            if not updated_roll.strip() or not updated_name.strip():
                st.error("Roll number and student name are required.")
            else:
                try:
                    record_management.update_student(student["student_id"], updated_roll, updated_name, updated_email)
                    st.success("Student updated.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not update student: {exc}")
        st.caption(f"Deleting this student also deletes {student['attempts']} attempt(s) and their unshared answer-sheet PDFs.")
        confirm_student_delete = st.checkbox("I understand this permanently deletes this student and linked records", key=f"confirm_student_delete_{student['student_id']}")
        if st.button("Delete student", key=f"delete_student_{student['student_id']}"):
            if not confirm_student_delete:
                st.error("Confirm deletion before continuing.")
            else:
                try:
                    record_management.delete_student(student["student_id"], teacher_id)
                    st.success("Student and linked attempts deleted.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not delete student: {exc}")

elif page == "Submissions":
    st.title("Submissions and grading")
    if not papers:
        st.info("Upload a question paper first.")
    else:
        paper = st.selectbox("Question paper", papers, format_func=lambda p: p["title"])
        with connect() as db:
            attempts = [dict(row) for row in db.execute("SELECT a.*,s.name,s.roll_no FROM STUDENT_ATTEMPT a JOIN STUDENT s ON a.student_id=s.student_id WHERE a.paper_id=? ORDER BY a.attempt_id DESC", (paper["paper_id"],)).fetchall()]
        if not attempts:
            st.info("No students are assigned to this paper yet. Add them in Students.")
        else:
            attempt = st.selectbox("Student attempt", attempts, format_func=lambda a: f"{a['roll_no']} · {a['name']} · {a['status']} (attempt #{a['attempt_id']})")
            st.write(f"Status: **{attempt['status']}**")
            upload = st.file_uploader("Student answer-sheet PDF", type=["pdf"], key=f"answer_{attempt['attempt_id']}")
            if st.button("Upload and extract student answers"):
                if upload is None:
                    st.error("Choose a PDF.")
                elif not paper["questions_json"]:
                    st.error("This paper has no reviewed question JSON.")
                else:
                    try:
                        images = pdf_service.pdf_to_images(upload.getvalue())
                        document_id, _ = save_upload(upload, "student_answer")
                        with connect() as db:
                            db.execute("UPDATE STUDENT_ATTEMPT SET submission_pdf_id=?,status='submitted',submitted_at=CURRENT_TIMESTAMP,evaluation_json=NULL,total_marks=NULL,evaluated_at=NULL WHERE attempt_id=?", (document_id, attempt["attempt_id"]))
                        with st.spinner("Reading handwritten answers with Gemini…"):
                            answers = gemini_service.extract_answers(images, decode_json(paper["questions_json"]))
                        with connect() as db:
                            db.execute("UPDATE STUDENT_ATTEMPT SET student_answers_json=? WHERE attempt_id=?", (encode_json(answers), attempt["attempt_id"]))
                        st.success("Answers extracted. Review them below before grading.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Submission extraction failed: {exc}. The uploaded file is retained; retry extraction after resolving the issue.")
            answers = decode_json(attempt["student_answers_json"], None)
            if answers:
                st.subheader("Extracted answers · review before evaluation")
                display_answers(answers, decode_json(paper["questions_json"], {"questions": []}), "Student answer")
                with st.expander("Edit extracted answers JSON"):
                    edited_answers = json_editor("Correct answer text or question numbers", answers, f"answers_{attempt['attempt_id']}")
                    if st.button("Save corrected answers", key=f"saveans_{attempt['attempt_id']}") and edited_answers is not None:
                        with connect() as db:
                            db.execute("UPDATE STUDENT_ATTEMPT SET student_answers_json=? WHERE attempt_id=?", (encode_json(edited_answers), attempt["attempt_id"]))
                        st.success("Corrected answers saved.")
                        st.rerun()
                if not paper["reference_solution_json"]:
                    st.warning("Add an official solution before evaluation.")
                else:
                    st.caption("Answers are saved. Use the batch evaluation button on Reports to grade all submitted students.")
            result = st.session_state.get(f"evaldraft_{attempt['attempt_id']}") or decode_json(attempt["evaluation_json"], None)
            if result:
                st.subheader("Evaluation · teacher review required")
                editable = json_editor("Results JSON", result, f"result_{attempt['attempt_id']}")
                if editable and isinstance(editable.get("results"), list):
                    total_max = sum(float(row.get("max_marks", 0)) for row in editable["results"])
                    st.metric("Total marks", f"{editable.get('total_marks', 0)} / {total_max:g}")
                    st.subheader("Marks by question")
                    st.bar_chart(
                        [
                            {"Question": f"Q{row['question_number']}", "Maximum Marks": row["max_marks"], "Obtained Marks": row["marks_obtained"]}
                            for row in editable["results"]
                        ],
                        x="Question",
                        y=["Maximum Marks", "Obtained Marks"],
                    )
                    st.dataframe([{"Question": f"Q{r['question_number']}", "Maximum": r["max_marks"], "Obtained": r["marks_obtained"], "Feedback": r["feedback"], "Missing points": "; ".join(r.get("missing_points", []))} for r in editable["results"]], width="stretch", hide_index=True)
                if st.button("Save teacher corrections", key=f"approve_{attempt['attempt_id']}") and editable is not None:
                    try:
                        editable, total = normalize_evaluation(editable, decode_json(paper["questions_json"]))
                        with connect() as db:
                            db.execute("UPDATE STUDENT_ATTEMPT SET evaluation_json=?,total_marks=?,status='evaluated',evaluated_at=CURRENT_TIMESTAMP WHERE attempt_id=?", (encode_json(editable), round(total), attempt["attempt_id"]))
                        st.session_state.pop(f"evaldraft_{attempt['attempt_id']}", None)
                        st.success("Teacher corrections saved.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Evaluation could not be saved: {exc}")
            if attempt["submission_pdf_id"]:
                with connect() as db:
                    doc = db.execute("SELECT file_path,file_name FROM DOCUMENT WHERE document_id=?", (attempt["submission_pdf_id"],)).fetchone()
                path = ROOT / doc["file_path"] if not Path(doc["file_path"]).is_absolute() else Path(doc["file_path"])
                if path.exists():
                    st.download_button("Download original answer sheet", path.read_bytes(), file_name=doc["file_name"], mime="application/pdf")
            confirm_attempt_delete = st.checkbox(
                "I understand this permanently deletes the selected attempt and its evaluation",
                key=f"confirm_attempt_delete_{attempt['attempt_id']}",
            )
            if st.button("Delete student attempt", key=f"delete_attempt_{attempt['attempt_id']}"):
                if not confirm_attempt_delete:
                    st.error("Confirm deletion before continuing.")
                else:
                    try:
                        record_management.delete_attempt(attempt["attempt_id"], teacher_id)
                        st.session_state.pop(f"evaldraft_{attempt['attempt_id']}", None)
                        st.success("Student attempt and its unshared answer PDF deleted.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not delete attempt: {exc}")

elif page == "Reports":
    st.title("Evaluation Reports")
    st.caption("Review collected submissions and evaluate them as a batch")
    with connect() as db:
        pending_attempts = [dict(row) for row in db.execute(
            "SELECT a.*,s.name AS student_name,s.roll_no,q.title AS paper_title,q.questions_json,"
            "q.reference_solution_json,q.evaluation_guidelines "
            "FROM STUDENT_ATTEMPT a JOIN STUDENT s ON s.student_id=a.student_id "
            "JOIN QUESTION_PAPER q ON q.paper_id=a.paper_id "
            "WHERE q.teacher_id=? AND a.status='submitted' AND a.student_answers_json IS NOT NULL "
            "ORDER BY a.attempt_id", (teacher_id,),
        ).fetchall()]

    if pending_attempts:
        st.write(f"{len(pending_attempts)} submitted student attempt(s) are waiting for evaluation.")
        if st.button("Evaluate all pending students", type="primary"):
            progress = st.progress(0, text="Starting batch evaluation…")
            completed: list[str] = []
            failed: list[str] = []
            with st.spinner("Evaluating submissions with Gemini…"):
                for index, attempt in enumerate(pending_attempts, start=1):
                    progress.progress(
                        (index - 1) / len(pending_attempts),
                        text=f"Evaluating student {index} of {len(pending_attempts)}: {attempt['student_name']}…",
                    )
                    try:
                        result = gemini_service.evaluate(
                            decode_json(attempt["questions_json"], {}),
                            decode_json(attempt["reference_solution_json"], {}),
                            decode_json(attempt["student_answers_json"], {}),
                            attempt["evaluation_guidelines"] or "",
                        )
                        result, total = normalize_evaluation(result, decode_json(attempt["questions_json"], {}))
                        with connect() as db:
                            db.execute(
                                "UPDATE STUDENT_ATTEMPT SET evaluation_json=?,total_marks=?,status='evaluated',evaluated_at=CURRENT_TIMESTAMP WHERE attempt_id=?",
                                (encode_json(result), round(total), attempt["attempt_id"]),
                            )
                        completed.append(f"{attempt['student_name']} ({attempt['roll_no']})")
                    except Exception as exc:
                        failed.append(f"{attempt['student_name']} ({attempt['roll_no']}): {exc}")
                    if index < len(pending_attempts):
                        time.sleep(5)
                    progress.progress(
                        index / len(pending_attempts),
                        text=f"Finished {index} of {len(pending_attempts)} student attempt(s).",
                    )
            st.session_state["batch_eval_summary"] = {"completed": completed, "failed": failed}
            st.rerun()
    else:
        st.info("No submitted answer sheets are waiting for evaluation.")

    batch_summary = st.session_state.pop("batch_eval_summary", None)
    if batch_summary:
        if batch_summary["completed"]:
            st.success(f"Saved evaluations for {len(batch_summary['completed'])} student attempt(s).")
        if batch_summary["failed"]:
            st.error("Some attempts could not be evaluated:")
            for failure in batch_summary["failed"]:
                st.write(f"- {failure}")

    with connect() as db:
        reports = [dict(row) for row in db.execute(
            "SELECT a.*,s.name AS student_name,s.roll_no,q.title AS paper_title,q.evaluation_guidelines "
            "FROM STUDENT_ATTEMPT a JOIN STUDENT s ON s.student_id=a.student_id "
            "JOIN QUESTION_PAPER q ON q.paper_id=a.paper_id "
            "WHERE q.teacher_id=? AND a.status='evaluated' ORDER BY a.evaluated_at DESC",
            (teacher_id,),
        ).fetchall()]
    if reports:
        st.subheader("Student results")
        summary_rows = [
            {"Attempt ID": item["attempt_id"], "Student": item["student_name"], "Roll Number": item["roll_no"], "Question Paper": item["paper_title"], "Total Marks": item["total_marks"], "Status": item["status"], "Evaluated": item["evaluated_at"]}
            for item in reports
        ]
        st.dataframe(summary_rows, width="stretch", hide_index=True)
        st.subheader("Marks by student")
        st.bar_chart(
            [
                {"Student": f"{item['student_name']} ({item['roll_no']})", "Total Marks": item["total_marks"]}
                for item in reports
            ],
            x="Student",
            y="Total Marks",
        )
        report = st.selectbox(
            "Open report",
            reports,
            format_func=lambda item: f"Attempt #{item['attempt_id']} · {item['student_name']} · {item['paper_title']}",
        )
        evaluation = decode_json(report["evaluation_json"], {"results": [], "total_marks": report["total_marks"]})
        st.subheader("Student and examination details")
        st.write(f"**Student:** {report['student_name']}  ·  **Roll number:** {report['roll_no']}")
        st.write(f"**Question paper:** {report['paper_title']}  ·  **Attempt:** {report['attempt_id']}  ·  **Status:** {report['status']}")
        st.subheader("Question-wise marks and feedback")
        report_results = evaluation.get("results", [])
        st.bar_chart(
            [
                {"Question": f"Q{item['question_number']}", "Maximum Marks": item["max_marks"], "Obtained Marks": item["marks_obtained"]}
                for item in report_results
            ],
            x="Question",
            y=["Maximum Marks", "Obtained Marks"],
        )
        st.dataframe([
            {"Question": f"Q{item['question_number']}", "Maximum Marks": item["max_marks"], "Obtained Marks": item["marks_obtained"], "Feedback": item["feedback"], "Missing Key Concepts": "; ".join(item.get("missing_points", []))}
            for item in report_results
        ], width="stretch", hide_index=True)
        maximum = sum(float(item.get("max_marks", 0)) for item in report_results)
        st.metric("Total Marks Obtained", f"{report['total_marks']} / {maximum:g}")
        st.subheader("Teacher Evaluation Guidelines")
        st.write(report["evaluation_guidelines"] or "No additional guidelines provided.")
        report_bytes = evaluation_report_pdf(
            report["student_name"], report["roll_no"], report["paper_title"],
            report["attempt_id"], report["status"], report["total_marks"],
            report["evaluation_json"], report["evaluation_guidelines"],
        )
        st.download_button(
            "Download / Print Evaluation Report (PDF)",
            report_bytes,
            file_name=f"evaluation_report_attempt_{report['attempt_id']}.pdf",
            mime="application/pdf",
        )
    elif not pending_attempts:
        st.caption("Completed evaluations will appear here with a results table and graph.")
