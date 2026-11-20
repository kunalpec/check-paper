"""Gemini OCR and answer grading using the supplied exam OCR reference."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError


class Answer(BaseModel):
    question_number: int
    answer_text: str


class AnswersResult(BaseModel):
    answers: list[Answer]


class EvaluationRow(BaseModel):
    question_number: int
    marks_obtained: float
    max_marks: float
    feedback: str
    missing_points: list[str] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    results: list[EvaluationRow]
    total_marks: float


QUESTION_PAPER_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["document", "questions"],
    "properties": {
        "document": {
            "type": "OBJECT",
            "required": ["title", "subject", "instructions", "total_marks"],
            "properties": {
                "title": {"type": "STRING"},
                "subject": {"type": "STRING"},
                "instructions": {"type": "ARRAY", "items": {"type": "STRING"}},
                "total_marks": {"type": "NUMBER"},
            },
        },
        "questions": {
            "type": "ARRAY",
            "minItems": 1,
            "items": {
                "type": "OBJECT",
                "required": [
                    "question_id", "question_text", "max_marks", "options",
                    "subquestions", "tables", "visual_elements", "equations",
                    "handwritten_annotations",
                ],
                "properties": {
                    "question_id": {"type": "STRING"},
                    "question_text": {"type": "STRING"},
                    "max_marks": {"type": "NUMBER"},
                    "options": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "subquestions": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "required": ["question_id", "question_text", "max_marks"],
                            "properties": {
                                "question_id": {"type": "STRING"},
                                "question_text": {"type": "STRING"},
                                "max_marks": {"type": "NUMBER"},
                            },
                        },
                    },
                    "tables": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "required": ["title", "headers", "rows"],
                            "properties": {
                                "title": {"type": "STRING"},
                                "headers": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "rows": {
                                    "type": "ARRAY",
                                    "items": {"type": "ARRAY", "items": {"type": "STRING"}},
                                },
                            },
                        },
                    },
                    "visual_elements": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "required": ["type", "description", "labels", "relationships"],
                            "properties": {
                                "type": {"type": "STRING"},
                                "description": {"type": "STRING"},
                                "labels": {"type": "ARRAY", "items": {"type": "STRING"}},
                                "relationships": {"type": "ARRAY", "items": {"type": "STRING"}},
                            },
                        },
                    },
                    "equations": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "handwritten_annotations": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        },
    },
}


OCR_PROMPT = """
You are an advanced document OCR and exam-paper structure extraction system.
Examine every supplied image page and extract all visible examination paper information.

OCR RULES
- Do not summarize, omit, invent, or silently correct text.
- Preserve reading order, section headings, instructions, question/subquestion numbers,
  choices, marks, captions, labels, mathematical notation, and symbols.
- Transcribe readable content faithfully. Mark unreadable spans as [UNCLEAR].
- Do not guess uncertain values.
- Keep handwritten annotations separate from printed question text.

QUESTIONS AND VISUAL CONTENT
- Extract every question and subquestion, its printed identifier, exact text and marks.
- Preserve all multiple-choice options.
- Keep tables as headers and rows, not as prose.
- For figures, diagrams and graphs, describe only visible structure, labels, values,
  captions, components, arrows, connections, axes, legends and relationships.
- Preserve equations and formulas, using LaTeX where helpful.
- Attach visual elements to the question they appear with.

Return only JSON matching the supplied schema. Do not return an empty questions array
when a question is visible. Do not invent missing information; use [UNCLEAR] where needed.
""".strip()


_GEMINI_CLIENT: genai.Client | None = None
_GEMINI_CLIENT_KEY: str | None = None


def _client() -> genai.Client:
    """Get a persistent Gemini client configured from the environment."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured. Add it to your .env file.")
    global _GEMINI_CLIENT, _GEMINI_CLIENT_KEY
    if _GEMINI_CLIENT is None or _GEMINI_CLIENT_KEY != key:
        _GEMINI_CLIENT = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        _GEMINI_CLIENT_KEY = key
    return _GEMINI_CLIENT


def _generate_raw(images: list[Any], prompt: str, schema: Any) -> dict[str, Any]:
    """Request schema-constrained JSON from Gemini, following the supplied reference."""
    global _GEMINI_CLIENT

    def request(client: genai.Client) -> Any:
        return client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            # The reference sends the instruction first, followed by its image.
            contents=[prompt, *images],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )

    try:
        response = request(_client())
    except Exception as exc:
        if "client has been closed" not in str(exc).lower():
            raise
        _GEMINI_CLIENT = None
        response = request(_client())
    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    try:
        value = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Gemini JSON response must be an object.")
    return value


def _generate_model(images: list[Any], prompt: str, model: type[BaseModel]) -> dict[str, Any]:
    """Generate JSON and validate it against a Pydantic response model."""
    value = _generate_raw(images, prompt, model)
    try:
        return model.model_validate(value).model_dump()
    except ValidationError as exc:
        raise ValueError(f"Gemini output did not match the required JSON structure: {exc}") from exc


def _question_number(identifier: str) -> int | None:
    """Parse a printed top-level number without fabricating one."""
    match = re.match(r"^\s*(?:question\s*)?q?\s*(\d+)", identifier, re.IGNORECASE)
    return int(match.group(1)) if match else None


def extract_questions(images: list[Any]) -> dict[str, Any]:
    """Extract a detailed paper structure and add fields used by the app workflow."""
    if not images:
        raise ValueError("No PDF pages were rendered for question extraction.")
    extracted = _generate_raw(images, OCR_PROMPT, QUESTION_PAPER_SCHEMA)
    raw_questions = extracted.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("Gemini did not find any questions. Check the PDF page images and retry.")

    questions: list[dict[str, Any]] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            raise ValueError("Gemini returned a question in an invalid format.")
        question_id = str(item.get("question_id", ""))
        criteria = [
            f"{part.get('question_id', '')}: {part.get('question_text', '')}".strip(": ")
            for part in item.get("subquestions", [])
            if part.get("question_text")
        ]
        if item.get("options"):
            criteria.append("Account for all listed answer options")
        questions.append({
            **item,
            "question_number": _question_number(question_id),
            "marks": item.get("max_marks", 0),
            "checking_criteria": criteria,
        })

    return {
        "document": extracted.get("document", {}),
        "questions": questions,
    }


def extract_answers(images: list[Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Extract student answers, aligning them to paper numbering where possible."""
    prompt = (
        "Transcribe all handwritten student answers accurately in reading order. "
        "Use the supplied question list to map answer question_number values. Do not guess unreadable writing; "
        "mark it [UNCLEAR]. Preserve printed numbering and mention uncertain mappings in answer_text. "
        f"Question list: {json.dumps(questions, ensure_ascii=False)}"
    )
    return _generate_model(images, prompt, AnswersResult)


def extract_reference_answers(images: list[Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Extract official answers using the existing paper question numbers."""
    prompt = (
        "Extract official reference answers and match them to existing question_number values only. "
        "Do not invent or renumber questions. Note uncertain mappings in answer_text. "
        f"Question list: {json.dumps(questions, ensure_ascii=False)}"
    )
    return _generate_model(images, prompt, AnswersResult)


def evaluate(questions: dict[str, Any], references: dict[str, Any], answers: dict[str, Any], guidelines: str) -> dict[str, Any]:
    """Grade student answers against the questions, solution and teacher guidance."""
    prompt = (
        "Evaluate every answer using question text, maximum marks, checking criteria, reference answer, student answer, "
        "and teacher guidelines. Award partial credit, do not require exact wording, and do not exceed maximum marks. "
        "For missing answers, award zero and explain. Return a result for every question and an accurate total. "
        f"Teacher guidelines: {guidelines}\nQuestions: {json.dumps(questions, ensure_ascii=False)}"
        f"\nOfficial answers: {json.dumps(references, ensure_ascii=False)}"
        f"\nStudent answers: {json.dumps(answers, ensure_ascii=False)}"
    )
    return _generate_model([], prompt, EvaluationResult)
