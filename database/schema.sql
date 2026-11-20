PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS TEACHER (
    teacher_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS DOCUMENT (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name VARCHAR(255) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    document_type TEXT NOT NULL CHECK (document_type IN ('question_paper', 'solution', 'student_answer')),
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS QUESTION_PAPER (
    paper_id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id INTEGER NOT NULL REFERENCES TEACHER(teacher_id),
    title VARCHAR(200) NOT NULL,
    question_pdf_id INTEGER NOT NULL UNIQUE REFERENCES DOCUMENT(document_id),
    solution_pdf_id INTEGER UNIQUE REFERENCES DOCUMENT(document_id),
    questions_json TEXT,
    reference_solution_json TEXT,
    evaluation_guidelines TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS STUDENT (
    student_id INTEGER PRIMARY KEY AUTOINCREMENT,
    roll_no VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS STUDENT_ATTEMPT (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES STUDENT(student_id),
    paper_id INTEGER NOT NULL REFERENCES QUESTION_PAPER(paper_id),
    submission_pdf_id INTEGER UNIQUE REFERENCES DOCUMENT(document_id),
    student_answers_json TEXT,
    evaluation_json TEXT,
    total_marks INTEGER,
    status TEXT NOT NULL DEFAULT 'assigned' CHECK (status IN ('assigned', 'submitted', 'evaluated')),
    submitted_at TIMESTAMP,
    evaluated_at TIMESTAMP
);
