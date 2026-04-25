import sqlite3
import hashlib
import secrets
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "battle.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            rating INTEGER NOT NULL DEFAULT 1000,
            games_played INTEGER NOT NULL DEFAULT 0,
            games_won INTEGER NOT NULL DEFAULT 0,
            is_bot INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player1_id INTEGER NOT NULL,
            player2_id INTEGER NOT NULL,
            winner_id INTEGER,
            player1_score INTEGER NOT NULL DEFAULT 0,
            player2_score INTEGER NOT NULL DEFAULT 0,
            rating_change_winner INTEGER NOT NULL DEFAULT 0,
            rating_change_loser INTEGER NOT NULL DEFAULT 0,
            total_rounds INTEGER NOT NULL DEFAULT 0,
            subject TEXT NOT NULL DEFAULT 'rus',
            created_at TEXT NOT NULL,
            FOREIGN KEY (player1_id) REFERENCES users(id),
            FOREIGN KEY (player2_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            round_num INTEGER NOT NULL,
            question_id TEXT NOT NULL,
            player1_answer TEXT,
            player2_answer TEXT,
            player1_correct INTEGER NOT NULL DEFAULT 0,
            player2_correct INTEGER NOT NULL DEFAULT 0,
            player1_time_ms INTEGER,
            player2_time_ms INTEGER,
            FOREIGN KEY (game_id) REFERENCES games(id)
        );
    """)
    conn.commit()

    # migrations
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(games)").fetchall()]
    if "subject" not in cols:
        conn.execute("ALTER TABLE games ADD COLUMN subject TEXT NOT NULL DEFAULT 'rus'")
        conn.commit()

    user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_bot" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    conn.close()


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode()).hexdigest()


def create_user(username: str, password: str) -> dict | None:
    conn = get_db()
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, rating, created_at) VALUES (?, ?, ?, 1000, ?)",
            (username, pw_hash, salt, datetime.now().isoformat()),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        return dict(user)
    except sqlite3.IntegrityError:
        conn.close()
        return None


def authenticate(username: str, password: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        return None
    user = dict(row)
    if _hash_password(password, user["salt"]) != user["password_hash"]:
        return None
    return user


def get_user(user_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_leaderboard(limit: int = 10) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, rating, games_played, games_won FROM users ORDER BY rating DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_game(player1_id: int, player2_id: int, winner_id: int | None,
              p1_score: int, p2_score: int, rating_w: int, rating_l: int,
              total_rounds: int, subject: str = "rus") -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO games (player1_id, player2_id, winner_id, player1_score, player2_score,
           rating_change_winner, rating_change_loser, total_rounds, subject, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (player1_id, player2_id, winner_id, p1_score, p2_score, rating_w, rating_l,
         total_rounds, subject, datetime.now().isoformat()),
    )
    game_id = cur.lastrowid
    conn.commit()
    conn.close()
    return game_id


def save_round(game_id: int, round_num: int, question_id: str,
               p1_answer: str | None, p2_answer: str | None,
               p1_correct: bool, p2_correct: bool,
               p1_time: int | None, p2_time: int | None):
    conn = get_db()
    conn.execute(
        """INSERT INTO rounds (game_id, round_num, question_id, player1_answer, player2_answer,
           player1_correct, player2_correct, player1_time_ms, player2_time_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (game_id, round_num, question_id, p1_answer, p2_answer,
         int(p1_correct), int(p2_correct), p1_time, p2_time),
    )
    conn.commit()
    conn.close()


def update_ratings(winner_id: int, loser_id: int, winner_gain: int, loser_loss: int):
    conn = get_db()
    conn.execute("UPDATE users SET rating = MAX(0, rating + ?), games_played = games_played + 1, games_won = games_won + 1 WHERE id = ?", (winner_gain, winner_id))
    conn.execute("UPDATE users SET rating = MAX(0, rating - ?), games_played = games_played + 1 WHERE id = ?", (loser_loss, loser_id))
    conn.commit()
    conn.close()


def get_user_games(user_id: int, limit: int = 10) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT g.id, g.player1_id, g.player2_id, g.winner_id,
                  g.player1_score, g.player2_score, g.total_rounds,
                  g.rating_change_winner, g.rating_change_loser,
                  g.subject, g.created_at,
                  u1.username as player1_name, u2.username as player2_name
           FROM games g
           JOIN users u1 ON g.player1_id = u1.id
           JOIN users u2 ON g.player2_id = u2.id
           WHERE g.player1_id = ? OR g.player2_id = ?
           ORDER BY g.id DESC LIMIT ?""",
        (user_id, user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_game_rounds(game_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT r.*, g.player1_id, g.player2_id
           FROM rounds r JOIN games g ON r.game_id = g.id
           WHERE r.game_id = ? ORDER BY r.round_num""",
        (game_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_round_stats(user_id: int) -> list[dict]:
    """All rounds played by this user, with per-round perspective."""
    conn = get_db()
    rows = conn.execute(
        """SELECT r.question_id, g.subject,
                  CASE WHEN g.player1_id = ? THEN r.player1_correct ELSE r.player2_correct END as correct,
                  CASE WHEN g.player1_id = ? THEN r.player1_time_ms ELSE r.player2_time_ms END as time_ms,
                  CASE WHEN g.player1_id = ? THEN r.player1_answer ELSE r.player2_answer END as answer
           FROM rounds r
           JOIN games g ON r.game_id = g.id
           WHERE g.player1_id = ? OR g.player2_id = ?""",
        (user_id, user_id, user_id, user_id, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_game_results(user_id: int) -> list[dict]:
    """All games for win-streak calculation, ordered chronologically."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, winner_id, subject, created_at
           FROM games
           WHERE player1_id = ? OR player2_id = ?
           ORDER BY id ASC""",
        (user_id, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_bot(username: str, rating: int) -> dict:
    conn = get_db()
    salt = secrets.token_hex(4)
    pw_hash = _hash_password(secrets.token_hex(16), salt)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, rating, is_bot, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (username, pw_hash, salt, rating, datetime.now().isoformat()),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        return dict(user) if user else {}
    except sqlite3.IntegrityError:
        conn.close()
        conn2 = get_db()
        row = conn2.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn2.close()
        return dict(row) if row else {}


def get_random_bot() -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE is_bot = 1 ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_topic_avg_times(user_id: int) -> dict:
    """Returns {topic_id: avg_time_ms} for a user across all subjects."""
    conn = get_db()
    rows = conn.execute(
        """SELECT r.question_id, g.subject,
                  CASE WHEN g.player1_id = ? THEN r.player1_time_ms ELSE r.player2_time_ms END as time_ms,
                  CASE WHEN g.player1_id = ? THEN r.player1_correct ELSE r.player2_correct END as correct
           FROM rounds r JOIN games g ON r.game_id = g.id
           WHERE (g.player1_id = ? OR g.player2_id = ?) AND time_ms IS NOT NULL""",
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    conn.close()

    topic_times: dict[int, list[int]] = {}
    from questions import get_question_by_id
    for r in rows:
        q = get_question_by_id(str(r["question_id"]))
        if not q:
            continue
        tid = q["topic_id"]
        t = r["time_ms"]
        if t and t > 0:
            topic_times.setdefault(tid, []).append(t)

    return {tid: sum(ts) // len(ts) for tid, ts in topic_times.items()}


def get_user_solve_rate(user_id: int) -> float:
    """Returns overall solve rate for a user (0.0..1.0)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT
               CASE WHEN g.player1_id = ? THEN r.player1_correct ELSE r.player2_correct END as correct,
               CASE WHEN g.player1_id = ? THEN r.player1_answer ELSE r.player2_answer END as answer
           FROM rounds r JOIN games g ON r.game_id = g.id
           WHERE g.player1_id = ? OR g.player2_id = ?""",
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    conn.close()
    answered = [r for r in rows if r["answer"]]
    if not answered:
        return 0.5
    return sum(1 for r in answered if r["correct"]) / len(answered)


def update_draw(player1_id: int, player2_id: int):
    conn = get_db()
    conn.execute("UPDATE users SET games_played = games_played + 1 WHERE id IN (?, ?)", (player1_id, player2_id))
    conn.commit()
    conn.close()


init_db()
