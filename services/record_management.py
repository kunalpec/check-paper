"""Update and delete helpers for the existing five-table schema."""
from __future__ import annotations

from pathlib import Path

from database.db import ROOT, connect


def _remove_files(paths: list[str]) -> None:
    """Remove saved uploads after their database references are deleted."""
    for stored_path in paths:
        path = Path(stored_path)
        if not path.is_absolute():
            path = ROOT / path
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A stale file can be cleaned up manually; database deletion is complete.
            continue


def _delete_unreferenced_documents(db, document_ids: set[int], paths: list[str]) -> None:
    """Delete document rows only after all three possible FK roles are clear."""
    for document_id in document_ids:
        document = db.execute("SELECT file_path FROM DOCUMENT WHERE document_id=?", (document_id,)).fetchone()
        referenced = db.execute(
            "SELECT 1 FROM QUESTION_PAPER WHERE question_pdf_id=? OR solution_pdf_id=? "
            "UNION SELECT 1 FROM STUDENT_ATTEMPT WHERE submission_pdf_id=? LIMIT 1",
            (document_id, document_id, document_id),
        ).fetchone()
        if not referenced:
            db.execute("DELETE FROM DOCUMENT WHERE document_id=?", (document_id,))
            if document:
                paths.append(document["file_path"])


def update_question_paper(
    teacher_id: int,
    paper_id: int,
    title: str,
    guidelines: str,
    questions_json: str,
    reference_json: str | None,
) -> None:
    """Update editable question-paper fields for the signed-in teacher."""
    with connect() as db:
        cursor = db.execute(
            "UPDATE QUESTION_PAPER SET title=?,evaluation_guidelines=?,questions_json=?,reference_solution_json=? "
            "WHERE paper_id=? AND teacher_id=?",
            (title.strip(), guidelines, questions_json, reference_json, paper_id, teacher_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Question paper was not found for this teacher.")


def update_teacher_profile(teacher_id: int, name: str, email: str, password_hash: str | None = None) -> None:
    """Update teacher identity and optionally reset the password hash."""
    with connect() as db:
        if password_hash:
            cursor = db.execute(
                "UPDATE TEACHER SET name=?,email=?,password_hash=? WHERE teacher_id=?",
                (name.strip(), email.strip().lower(), password_hash, teacher_id),
            )
        else:
            cursor = db.execute(
                "UPDATE TEACHER SET name=?,email=? WHERE teacher_id=?",
                (name.strip(), email.strip().lower(), teacher_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Teacher account was not found.")


def delete_teacher_account(teacher_id: int) -> list[str]:
    """Delete a teacher and all owned papers, attempts, and unshared files."""
    paths: list[str] = []
    with connect() as db:
        paper_rows = db.execute(
            "SELECT paper_id,question_pdf_id,solution_pdf_id FROM QUESTION_PAPER WHERE teacher_id=?",
            (teacher_id,),
        ).fetchall()
        paper_ids = [row["paper_id"] for row in paper_rows]
        document_ids = {
            doc_id
            for row in paper_rows
            for doc_id in (row["question_pdf_id"], row["solution_pdf_id"])
            if doc_id
        }
        student_ids: set[int] = set()
        if paper_ids:
            placeholders = ",".join("?" for _ in paper_ids)
            attempts = db.execute(
                f"SELECT student_id,submission_pdf_id FROM STUDENT_ATTEMPT WHERE paper_id IN ({placeholders})",
                tuple(paper_ids),
            ).fetchall()
            student_ids.update(row["student_id"] for row in attempts)
            document_ids.update(row["submission_pdf_id"] for row in attempts if row["submission_pdf_id"])
            db.execute(f"DELETE FROM STUDENT_ATTEMPT WHERE paper_id IN ({placeholders})", tuple(paper_ids))
            db.execute("DELETE FROM QUESTION_PAPER WHERE teacher_id=?", (teacher_id,))
        _delete_unreferenced_documents(db, document_ids, paths)
        for student_id in student_ids:
            has_attempts = db.execute(
                "SELECT 1 FROM STUDENT_ATTEMPT WHERE student_id=? LIMIT 1", (student_id,)
            ).fetchone()
            if not has_attempts:
                db.execute("DELETE FROM STUDENT WHERE student_id=?", (student_id,))
        cursor = db.execute("DELETE FROM TEACHER WHERE teacher_id=?", (teacher_id,))
        if cursor.rowcount != 1:
            raise ValueError("Teacher account was not found.")
    _remove_files(paths)
    return paths


def delete_question_paper(teacher_id: int, paper_id: int) -> None:
    """Delete a paper, its attempts and unshared PDF metadata/files."""
    paths: list[str] = []
    with connect() as db:
        paper = db.execute(
            "SELECT question_pdf_id,solution_pdf_id FROM QUESTION_PAPER WHERE paper_id=? AND teacher_id=?",
            (paper_id, teacher_id),
        ).fetchone()
        if not paper:
            raise ValueError("Question paper was not found for this teacher.")
        document_ids = {value for value in (paper["question_pdf_id"], paper["solution_pdf_id"]) if value}
        attempts = db.execute(
            "SELECT submission_pdf_id FROM STUDENT_ATTEMPT WHERE paper_id=?", (paper_id,)
        ).fetchall()
        document_ids.update(row["submission_pdf_id"] for row in attempts if row["submission_pdf_id"])
        db.execute("DELETE FROM STUDENT_ATTEMPT WHERE paper_id=?", (paper_id,))
        db.execute("DELETE FROM QUESTION_PAPER WHERE paper_id=? AND teacher_id=?", (paper_id, teacher_id))
        _delete_unreferenced_documents(db, document_ids, paths)
    _remove_files(paths)


def update_student(student_id: int, roll_no: str, name: str, email: str | None) -> None:
    """Update student profile fields."""
    with connect() as db:
        cursor = db.execute(
            "UPDATE STUDENT SET roll_no=?,name=?,email=? WHERE student_id=?",
            (roll_no.strip(), name.strip(), email.strip().lower() if email and email.strip() else None, student_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Student record was not found.")


def delete_student(student_id: int, teacher_id: int) -> None:
    """Delete a student and this teacher's attempts if no other teacher uses them."""
    paths: list[str] = []
    with connect() as db:
        student = db.execute("SELECT student_id FROM STUDENT WHERE student_id=?", (student_id,)).fetchone()
        if not student:
            raise ValueError("Student record was not found.")
        other_teacher_attempt = db.execute(
            "SELECT 1 FROM STUDENT_ATTEMPT a JOIN QUESTION_PAPER q ON q.paper_id=a.paper_id "
            "WHERE a.student_id=? AND q.teacher_id<>? LIMIT 1",
            (student_id, teacher_id),
        ).fetchone()
        if other_teacher_attempt:
            raise ValueError("This student has attempts linked to another teacher's paper and cannot be deleted here.")
        attempts = db.execute(
            "SELECT submission_pdf_id FROM STUDENT_ATTEMPT WHERE student_id=?", (student_id,)
        ).fetchall()
        document_ids = {row["submission_pdf_id"] for row in attempts if row["submission_pdf_id"]}
        db.execute("DELETE FROM STUDENT_ATTEMPT WHERE student_id=?", (student_id,))
        db.execute("DELETE FROM STUDENT WHERE student_id=?", (student_id,))
        _delete_unreferenced_documents(db, document_ids, paths)
    _remove_files(paths)


def delete_attempt(attempt_id: int, teacher_id: int) -> None:
    """Delete one attempt and its unshared uploaded answer sheet."""
    paths: list[str] = []
    with connect() as db:
        attempt = db.execute(
            "SELECT a.submission_pdf_id FROM STUDENT_ATTEMPT a "
            "JOIN QUESTION_PAPER q ON q.paper_id=a.paper_id "
            "WHERE a.attempt_id=? AND q.teacher_id=?",
            (attempt_id, teacher_id),
        ).fetchone()
        if not attempt:
            raise ValueError("Student attempt was not found for this teacher.")
        document_ids = {attempt["submission_pdf_id"]} if attempt["submission_pdf_id"] else set()
        db.execute("DELETE FROM STUDENT_ATTEMPT WHERE attempt_id=?", (attempt_id,))
        _delete_unreferenced_documents(db, document_ids, paths)
    _remove_files(paths)
