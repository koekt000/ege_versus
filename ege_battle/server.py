import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import database as db
from game_manager import matchmaking, PlayerState, GameSession
from questions import get_subject_list, get_question_by_id, SUBJECTS

app = FastAPI(title="EGE Battle")


class AuthRequest(BaseModel):
    username: str
    password: str


@app.post("/api/register")
async def register(req: AuthRequest):
    if len(req.username) < 2 or len(req.password) < 3:
        raise HTTPException(400, "Имя >= 2, пароль >= 3 символов")
    user = db.create_user(req.username, req.password)
    if not user:
        raise HTTPException(409, "Пользователь уже существует")
    return {"id": user["id"], "username": user["username"], "rating": user["rating"]}


@app.post("/api/login")
async def login(req: AuthRequest):
    user = db.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(401, "Неверное имя или пароль")
    return {"id": user["id"], "username": user["username"], "rating": user["rating"],
            "games_played": user["games_played"], "games_won": user["games_won"]}


@app.get("/api/leaderboard")
async def leaderboard():
    return db.get_leaderboard(10)


@app.get("/api/subjects")
async def subjects():
    return get_subject_list()


@app.get("/api/me/{user_id}")
async def get_me(user_id: int):
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(404)
    return {"id": user["id"], "username": user["username"], "rating": user["rating"],
            "games_played": user["games_played"], "games_won": user["games_won"]}


@app.get("/api/history/{user_id}")
async def history(user_id: int):
    games = db.get_user_games(user_id, 10)
    result = []
    for g in games:
        is_p1 = g["player1_id"] == user_id
        opponent_name = g["player2_name"] if is_p1 else g["player1_name"]
        my_score = g["player1_score"] if is_p1 else g["player2_score"]
        opp_score = g["player2_score"] if is_p1 else g["player1_score"]
        if g["winner_id"] == user_id:
            outcome = "win"
        elif g["winner_id"] is None:
            outcome = "draw"
        else:
            outcome = "loss"
        subj_label = SUBJECTS.get(g.get("subject", "rus"), {}).get("label", g.get("subject", ""))
        result.append({
            "game_id": g["id"],
            "opponent": opponent_name,
            "my_score": my_score,
            "opponent_score": opp_score,
            "outcome": outcome,
            "total_rounds": g["total_rounds"],
            "subject": subj_label,
            "created_at": g["created_at"],
        })
    return result


@app.get("/api/stats/{user_id}")
async def stats(user_id: int):
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(404)

    round_rows = db.get_user_round_stats(user_id)
    game_rows = db.get_user_game_results(user_id)

    # --- per-subject, per-topic aggregation ---
    topic_agg: dict[str, dict[int, dict]] = {}  # subject -> topic_id -> {total, correct, times}
    subject_agg: dict[str, dict] = {}
    total_correct = 0
    total_answered = 0
    all_times: list[int] = []
    recent_times: list[int] = []

    for r in round_rows:
        subj = r["subject"] or "rus"
        q = get_question_by_id(str(r["question_id"]))
        topic_id = q["topic_id"] if q else 0

        if subj not in topic_agg:
            topic_agg[subj] = {}
        if topic_id not in topic_agg[subj]:
            topic_agg[subj][topic_id] = {"total": 0, "correct": 0, "times": []}

        bucket = topic_agg[subj][topic_id]
        has_answer = r["answer"] is not None and r["answer"] != ""
        if has_answer:
            bucket["total"] += 1
            total_answered += 1
            if r["correct"]:
                bucket["correct"] += 1
                total_correct += 1
            if r["time_ms"] and r["time_ms"] > 0:
                bucket["times"].append(r["time_ms"])
                all_times.append(r["time_ms"])

        if subj not in subject_agg:
            subject_agg[subj] = {"total": 0, "correct": 0, "games": 0, "wins": 0}
        if has_answer:
            subject_agg[subj]["total"] += 1
            if r["correct"]:
                subject_agg[subj]["correct"] += 1

    # subject-level game counts
    for g in game_rows:
        subj = g.get("subject", "rus")
        if subj not in subject_agg:
            subject_agg[subj] = {"total": 0, "correct": 0, "games": 0, "wins": 0}
        subject_agg[subj]["games"] += 1
        if g["winner_id"] == user_id:
            subject_agg[subj]["wins"] += 1

    # last 20 rounds for "recent" avg time
    for r in round_rows[-20:]:
        if r["time_ms"] and r["time_ms"] > 0 and r["answer"]:
            recent_times.append(r["time_ms"])

    # --- win streaks ---
    current_streak = 0
    best_streak = 0
    streak = 0
    for g in game_rows:
        if g["winner_id"] == user_id:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0
    current_streak = streak

    # --- build topic tables ---
    topics_by_subject = {}
    weak_topics = []
    for subj, topics in topic_agg.items():
        subj_label = SUBJECTS.get(subj, {}).get("label", subj)
        topic_list = []
        for tid, d in sorted(topics.items()):
            pct = round(d["correct"] / d["total"] * 100) if d["total"] else 0
            avg_time = round(sum(d["times"]) / len(d["times"])) if d["times"] else None
            entry = {
                "topic_id": tid,
                "label": f"Задание {tid}",
                "total": d["total"],
                "correct": d["correct"],
                "percent": pct,
                "avg_time_ms": avg_time,
            }
            topic_list.append(entry)
            if d["total"] >= 2:
                weak_topics.append({**entry, "subject": subj_label})
        topics_by_subject[subj] = {
            "label": subj_label,
            "topics": topic_list,
        }

    weak_topics.sort(key=lambda x: x["percent"])
    strong_topics = sorted([t for t in weak_topics], key=lambda x: -x["percent"])

    # --- subjects overview ---
    subjects_overview = []
    for subj, d in subject_agg.items():
        subj_label = SUBJECTS.get(subj, {}).get("label", subj)
        subjects_overview.append({
            "key": subj,
            "label": subj_label,
            "games": d["games"],
            "wins": d["wins"],
            "win_pct": round(d["wins"] / d["games"] * 100) if d["games"] else 0,
            "questions_total": d["total"],
            "questions_correct": d["correct"],
            "solve_pct": round(d["correct"] / d["total"] * 100) if d["total"] else 0,
        })

    return {
        "rating": user["rating"],
        "games_played": user["games_played"],
        "games_won": user["games_won"],
        "win_pct": round(user["games_won"] / user["games_played"] * 100) if user["games_played"] else 0,
        "total_answered": total_answered,
        "total_correct": total_correct,
        "solve_pct": round(total_correct / total_answered * 100) if total_answered else 0,
        "avg_time_ms": round(sum(all_times) / len(all_times)) if all_times else None,
        "recent_avg_time_ms": round(sum(recent_times) / len(recent_times)) if recent_times else None,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "subjects": subjects_overview,
        "topics_by_subject": topics_by_subject,
        "weak_topics": weak_topics[:5],
        "strong_topics": strong_topics[:5],
    }


@app.get("/api/game/{game_id}/review")
async def game_review(game_id: int, user_id: int):
    rounds = db.get_game_rounds(game_id)
    if not rounds:
        raise HTTPException(404)
    is_p1 = rounds[0]["player1_id"] == user_id
    result = []
    for r in rounds:
        q = get_question_by_id(str(r["question_id"]))
        my_answer = r["player1_answer"] if is_p1 else r["player2_answer"]
        opp_answer = r["player2_answer"] if is_p1 else r["player1_answer"]
        my_correct = bool(r["player1_correct"] if is_p1 else r["player2_correct"])
        opp_correct = bool(r["player2_correct"] if is_p1 else r["player1_correct"])
        result.append({
            "round": r["round_num"],
            "question_html": q["condition_html"] if q else "",
            "question_text": q["condition_text"] if q else "",
            "correct_answer": q["answer"].split("|")[0] if q else "",
            "solution_html": q.get("solution_html", "") if q else "",
            "my_answer": my_answer or "",
            "opponent_answer": opp_answer or "",
            "my_correct": my_correct,
            "opponent_correct": opp_correct,
        })
    return result


@app.websocket("/ws/game")
async def game_ws(ws: WebSocket):
    await ws.accept()
    player: PlayerState | None = None
    game: GameSession | None = None
    reconnected = False

    try:
        auth = await ws.receive_json()
        if auth.get("type") != "auth":
            await ws.close(4001, "Expected auth message")
            return
        user = db.get_user(auth.get("user_id", 0))
        if not user:
            await ws.close(4001, "Invalid user")
            return

        existing_game = matchmaking.get_game(user["id"])
        if existing_game and not existing_game.finished:
            if existing_game.player1.user_id == user["id"]:
                player = existing_game.player1
            else:
                player = existing_game.player2
            await existing_game.handle_reconnect(player, ws)
            game = existing_game
            reconnected = True
        else:
            player = PlayerState(
                user_id=user["id"],
                username=user["username"],
                rating=user["rating"],
                ws=ws,
            )

        await ws.send_json({"type": "authenticated", "username": user["username"], "rating": user["rating"]})

        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")

            if msg_type == "find_game":
                player.subject = msg.get("subject", "rus")
                game = await matchmaking.add(player)
                if game:
                    asyncio.create_task(game.start())
                else:
                    await ws.send_json({"type": "searching"})

            elif msg_type == "cancel_search":
                await matchmaking.remove(player.user_id)
                await ws.send_json({"type": "search_cancelled"})

            elif msg_type == "submit_answer":
                game = matchmaking.get_game(player.user_id)
                if game:
                    await game.handle_answer(player, msg.get("answer", ""))

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if player:
            await matchmaking.remove(player.user_id)
            game = matchmaking.get_game(player.user_id)
            if game and not game.finished:
                await game.handle_disconnect(player)
                # Don't remove game from map — allow reconnection


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
