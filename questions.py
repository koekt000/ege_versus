import sqlite3
import os
import re
import random

QUESTIONS_DB = os.path.join(os.path.dirname(__file__), "sdamgia_bank.db")

SUBJECTS = {
    "rus":  {"label": "Русский язык",  "max_topic": 22, "subject_db": "rus"},
    "math": {"label": "Математика",    "max_topic": 18, "subject_db": "math"},
    "phys": {"label": "Физика",        "max_topic": 26, "subject_db": "phys"},
    "inf":  {"label": "Информатика",   "max_topic": 27, "subject_db": "inf"},
}

_cache: dict[str, list[dict]] = {}
_by_id: dict[str, dict] = {}


def _detect_answer_type(answer: str) -> str:
    if not answer:
        return "text"
    primary = answer.split("|")[0]
    return "digits" if re.match(r"^\d+$", primary) else "text"


def _clean_html(html: str) -> str:
    html = re.sub(r'<a\s+href="javascript:void\(0\)"[^>]*>.*?</a>', '', html, flags=re.DOTALL)
    html = re.sub(r'<img[^>]*(exclamation|chain|printer)\.png[^>]*/?\s*>', '', html)
    html = re.sub(r'<div[^>]*class="[^"]*probButtons[^"]*"[^>]*>.*?</div>', '', html, flags=re.DOTALL)
    return html.strip()


def _extract_task_html(condition_html: str, solution_html: str) -> str | None:
    if solution_html and 'id="body' in solution_html:
        return solution_html
    if condition_html and 'id="body' in condition_html:
        return condition_html
    if solution_html and 'javascript:void' not in solution_html[:300]:
        return solution_html
    return None


def _extract_task_text(condition_text: str, solution_text: str, task_html: str | None,
                       condition_html: str) -> str:
    if task_html is None:
        return condition_text or ""
    if task_html is condition_html or (condition_html and 'id="body' in condition_html):
        return condition_text or ""
    return solution_text or ""


def load_questions():
    if _cache:
        return

    conn = sqlite3.connect(QUESTIONS_DB)
    conn.row_factory = sqlite3.Row

    for key, cfg in SUBJECTS.items():
        _cache[key] = []
        rows = conn.execute(
            "SELECT problem_id, topic_id, condition_html, condition_text, "
            "       solution_html, solution_text, answer "
            "FROM problems WHERE subject = ? "
            "AND CAST(topic_id AS INTEGER) <= ? "
            "AND answer IS NOT NULL AND answer != '' ",
            (cfg["subject_db"], cfg["max_topic"]),
        ).fetchall()

        for r in rows:
            cond_html = r["condition_html"] or ""
            sol_html = r["solution_html"] or ""
            task_html = _extract_task_html(cond_html, sol_html)
            if not task_html:
                continue

            q_id = str(r["problem_id"])
            cleaned_task = _clean_html(task_html)
            sol_for_review = _clean_html(sol_html) if sol_html else ""
            entry = {
                "id": q_id,
                "subject": key,
                "topic_id": int(r["topic_id"]),
                "condition_html": cleaned_task,
                "condition_text": _extract_task_text(
                    r["condition_text"] or "", r["solution_text"] or "",
                    task_html, cond_html,
                ),
                "answer": r["answer"],
                "answer_type": _detect_answer_type(r["answer"]),
                "solution_html": sol_for_review,
            }
            _cache[key].append(entry)
            _by_id[q_id] = entry

    conn.close()

    for key in _cache:
        print(f"  [{key}] loaded {len(_cache[key])} questions")


def get_random_question(subject: str = "rus", exclude_ids: set[str] | None = None) -> dict:
    load_questions()
    pool = _cache.get(subject, _cache.get("rus", []))
    if exclude_ids:
        filtered = [q for q in pool if q["id"] not in exclude_ids]
        if filtered:
            pool = filtered
    return random.choice(pool)


def get_question_by_id(q_id: str) -> dict | None:
    load_questions()
    return _by_id.get(q_id)


def get_subject_list() -> list[dict]:
    load_questions()
    return [
        {"key": k, "label": v["label"], "count": len(_cache.get(k, []))}
        for k, v in SUBJECTS.items()
    ]


def check_answer(question: dict, user_answer: str) -> bool:
    correct = question["answer"].lower().strip()
    user = user_answer.lower().strip()
    if not user:
        return False

    if "|" in correct:
        return any(v.strip() == user for v in correct.split("|"))

    if question["answer_type"] == "digits":
        return sorted(correct) == sorted(user)

    return correct == user


load_questions()
