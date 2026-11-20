"""Small printable PDF report generator using PyMuPDF."""
from __future__ import annotations

import json
import textwrap

import pymupdf


def evaluation_report_pdf(
    student_name: str,
    roll_no: str,
    paper_title: str,
    attempt_id: int,
    status: str,
    total_marks: int | float | None,
    evaluation_json: str,
    guidelines: str | None,
) -> bytes:
    """Create a plain, print-friendly evaluation report PDF."""
    evaluation = json.loads(evaluation_json or "{}")
    results = evaluation.get("results", [])
    maximum = sum(float(item.get("max_marks", 0)) for item in results)
    lines = [
        "AI-BASED AUTOMATED ANSWER EVALUATION SYSTEM",
        "Digital Examination and Evaluation Portal",
        "=" * 72,
        "EVALUATION REPORT",
        "",
        f"Student: {student_name}",
        f"Roll Number: {roll_no}",
        f"Question Paper: {paper_title}",
        f"Attempt ID: {attempt_id}",
        f"Evaluation Status: {status}",
        "",
        "QUESTION-WISE RESULTS",
        "-" * 72,
    ]
    for item in results:
        lines.extend([
            f"Question {item.get('question_number')} | Marks: {item.get('marks_obtained', 0)} / {item.get('max_marks', 0)}",
            f"Feedback: {item.get('feedback', '')}",
            f"Missing points: {', '.join(item.get('missing_points', [])) or 'None'}",
            "",
        ])
    lines.extend([
        "=" * 72,
        f"TOTAL MARKS: {total_marks if total_marks is not None else evaluation.get('total_marks', 0)} / {maximum:g}",
        "",
        "TEACHER EVALUATION GUIDELINES",
        guidelines or "No additional guidelines provided.",
    ])

    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=92, replace_whitespace=False) or [""])
    document = pymupdf.open()
    for start in range(0, len(wrapped), 48):
        page = document.new_page(width=595, height=842)
        page.insert_textbox(
            pymupdf.Rect(42, 42, 553, 800),
            "\n".join(wrapped[start:start + 48]),
            fontsize=10,
            fontname="helv",
            lineheight=1.3,
        )
    output = document.tobytes()
    document.close()
    return output
