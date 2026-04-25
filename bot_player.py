"""
Bot player logic: simulates a real opponent in EGE Battle.
- Solve probability matches the human player's stats (min 50%)
- Answer time based on human's avg time per topic type
- Bot accounts persist in the database with real ratings
"""

import asyncio
import random
import time
from dataclasses import dataclass, field

import database as db
from questions import check_answer

BOT_NICKNAMES = [
    # english misc
    "xNova", "dr1zzle", "fl0wstate", "quietstorm", "blqck_ice",
    "kryptonix", "sh4dowrun", "nullptrr", "redshift42", "c0mpass",
    "hyp3rion", "starlynx", "n1ghtowl", "zenithX", "pxlghost",
    "arcticfox99", "v0rtex", "subcero", "ironfrost", "skyward_",
    "glitchwave", "d4rkn0va", "echopoint", "blazerx", "mr_nobody",
    "synthwave", "qw3rtz", "t0xicfree", "alphagrind", "deadpxl",
    # russian misc
    "картоха", "кефирчик", "сосиска_в_тесте", "ноль_паник",
    "печенька", "шкаф228", "кот_учёный", "тыж_программист",
    "борщ_ок", "зачёт100", "мозг_кипит", "топ1селоо",
    "хакер_егэ", "задачкорешатель", "фантик",
    # mixed / number-style
    "user8173", "qwerty_pro", "xx_legend_xx", "1337solve",
    "not_a_bot", "just4fun", "tryharder", "ege_warrior",
    "ctrl_c_v", "big_brain42",
]

BOT_TIMEOUT = 100  # seconds of search before bot joins (1:40)
BOT_DELAY_MIN = 3
BOT_DELAY_MAX = 20


def _ensure_bots_exist():
    """Create bot accounts in DB if they don't exist yet. Remove stale bots."""
    conn = db.get_db()
    nick_set = set(BOT_NICKNAMES)
    existing = conn.execute("SELECT id, username FROM users WHERE is_bot = 1").fetchall()
    for row in existing:
        if row["username"] not in nick_set:
            conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()

    for name in BOT_NICKNAMES:
        rating = random.randint(700, 1500)
        db.create_bot(name, rating)


_bots_initialized = False


def ensure_bots():
    global _bots_initialized
    if not _bots_initialized:
        _ensure_bots_exist()
        _bots_initialized = True


@dataclass
class BotBrain:
    """Manages bot decision-making during a game."""
    bot_user_id: int
    human_user_id: int
    solve_rate: float = 0.5
    human_topic_times: dict = field(default_factory=dict)
    _task: asyncio.Task | None = None

    def load_human_stats(self):
        rate = db.get_user_solve_rate(self.human_user_id)
        self.solve_rate = max(0.5, rate)
        self.human_topic_times = db.get_user_topic_avg_times(self.human_user_id)

    def decide_answer(self, question: dict) -> tuple[bool, str]:
        """Returns (will_be_correct, answer_text)."""
        correct = random.random() < self.solve_rate
        if correct:
            answer = question["answer"].split("|")[0]
        else:
            real_answer = question["answer"].split("|")[0]
            if question["answer_type"] == "digits":
                digits = list("1234567890")
                random.shuffle(digits)
                answer = "".join(digits[:len(real_answer)])
                if answer == real_answer:
                    answer = "".join(digits[:len(real_answer) + 1]) if len(real_answer) < 5 else real_answer[:-1] + "0"
            else:
                answer = real_answer + "x"
        return correct, answer

    def compute_delay(self, question: dict, human_answered: bool) -> float:
        """Compute how long the bot should wait before answering (in seconds)."""
        topic_id = question.get("topic_id", 0)
        avg_ms = self.human_topic_times.get(topic_id)

        if human_answered:
            return random.uniform(5, 60)

        if avg_ms and avg_ms >= 60_000:
            base_sec = avg_ms / 1000
            return random.uniform(base_sec, base_sec + 240)
        else:
            return random.uniform(120, 300)
