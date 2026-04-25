import asyncio
import random
import time
from dataclasses import dataclass, field
from fastapi import WebSocket
from questions import get_random_question, check_answer
import database as db
from bot_player import BotBrain, ensure_bots, BOT_TIMEOUT, BOT_DELAY_MIN, BOT_DELAY_MAX, BOT_NICKNAMES

ROUNDS_TO_WIN = 7
TIMER_AFTER_ANSWER = 300  # 5 minutes after opponent answers
DISCONNECT_TIMEOUT = 180  # 3 minutes to reconnect


@dataclass
class PlayerState:
    user_id: int
    username: str
    rating: int
    ws: WebSocket | None
    subject: str = "rus"
    score: int = 0
    current_answer: str | None = None
    current_correct: bool = False
    answered_at: float | None = None
    connected: bool = True
    disconnected_at: float | None = None
    is_bot: bool = False


@dataclass
class GameSession:
    game_id: str
    player1: PlayerState
    player2: PlayerState
    subject: str = "rus"
    current_round: int = 0
    question: dict | None = None
    round_active: bool = False
    used_question_ids: set = field(default_factory=set)
    round_start_time: float = 0
    finished: bool = False
    _round_history: list = field(default_factory=list)
    _timer_task: asyncio.Task | None = None
    _disconnect_task: asyncio.Task | None = None
    bot_brain: BotBrain | None = None
    _bot_answer_task: asyncio.Task | None = None

    async def send(self, player: PlayerState, msg: dict):
        if player.is_bot:
            return
        try:
            await player.ws.send_json(msg)
        except Exception:
            pass

    async def broadcast(self, msg: dict):
        await self.send(self.player1, msg)
        await self.send(self.player2, msg)

    async def start(self):
        await self.send(self.player1, {
            "type": "matched",
            "opponent": self.player2.username,
            "opponent_rating": self.player2.rating,
        })
        await self.send(self.player2, {
            "type": "matched",
            "opponent": self.player1.username,
            "opponent_rating": self.player1.rating,
        })
        await asyncio.sleep(2)
        await self.next_round()

    async def next_round(self):
        self.current_round += 1
        self.question = get_random_question(self.subject, self.used_question_ids)
        self.used_question_ids.add(self.question["id"])
        self.round_active = True
        self.round_start_time = time.time()

        self.player1.current_answer = None
        self.player1.current_correct = False
        self.player1.answered_at = None
        self.player2.current_answer = None
        self.player2.current_correct = False
        self.player2.answered_at = None

        msg = {
            "type": "round_start",
            "round": self.current_round,
            "question_html": self.question["condition_html"],
            "question_text": self.question["condition_text"],
            "answer_type": self.question["answer_type"],
            "my_score": 0,
            "opponent_score": 0,
        }

        msg["my_score"] = self.player1.score
        msg["opponent_score"] = self.player2.score
        await self.send(self.player1, msg)

        msg["my_score"] = self.player2.score
        msg["opponent_score"] = self.player1.score
        await self.send(self.player2, msg)

        if self.bot_brain:
            self._schedule_bot_answer(human_answered=False)

    def _get_bot_player(self) -> PlayerState | None:
        if self.player1.is_bot:
            return self.player1
        if self.player2.is_bot:
            return self.player2
        return None

    def _schedule_bot_answer(self, human_answered: bool = False):
        if self._bot_answer_task and not self._bot_answer_task.done():
            self._bot_answer_task.cancel()
        self._bot_answer_task = asyncio.create_task(
            self._bot_answer_loop(human_answered)
        )

    async def _bot_answer_loop(self, human_answered: bool):
        try:
            bot = self._get_bot_player()
            if not bot or not self.bot_brain or not self.round_active:
                return
            if bot.current_answer is not None:
                return

            delay = self.bot_brain.compute_delay(self.question, human_answered)
            await asyncio.sleep(delay)

            if not self.round_active or bot.current_answer is not None:
                return

            correct, answer = self.bot_brain.decide_answer(self.question)
            await self.handle_answer(bot, answer)
        except asyncio.CancelledError:
            pass

    async def handle_answer(self, player: PlayerState, answer: str):
        if not self.round_active or player.current_answer is not None:
            return

        opponent = self.player2 if player is self.player1 else self.player1
        player.current_answer = answer
        player.answered_at = time.time()
        player.current_correct = check_answer(self.question, answer)
        time_ms = int((player.answered_at - self.round_start_time) * 1000)

        await self.send(player, {
            "type": "answer_result",
            "correct": player.current_correct,
            "correct_answer": self.question["answer"].split("|")[0],
        })

        if opponent.current_answer is not None:
            await self._end_round()
            return

        await self.send(opponent, {
            "type": "opponent_answered",
            "timer_seconds": TIMER_AFTER_ANSWER,
        })

        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._round_timer(TIMER_AFTER_ANSWER))

        if self.bot_brain and opponent.is_bot and opponent.current_answer is None:
            self._schedule_bot_answer(human_answered=True)

    async def _round_timer(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.round_active:
                await self._end_round()
        except asyncio.CancelledError:
            pass

    async def _end_round(self):
        if not self.round_active:
            return
        self.round_active = False

        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        if self.player1.current_correct:
            self.player1.score += 1
        if self.player2.current_correct:
            self.player2.score += 1

        p1_time = int((self.player1.answered_at - self.round_start_time) * 1000) if self.player1.answered_at else None
        p2_time = int((self.player2.answered_at - self.round_start_time) * 1000) if self.player2.answered_at else None

        self._round_history.append({
            "round_num": self.current_round,
            "question_id": self.question["id"],
            "p1_answer": self.player1.current_answer,
            "p2_answer": self.player2.current_answer,
            "p1_correct": self.player1.current_correct,
            "p2_correct": self.player2.current_correct,
            "p1_time": p1_time,
            "p2_time": p2_time,
        })

        await self.send(self.player1, {
            "type": "round_end",
            "round": self.current_round,
            "my_score": self.player1.score,
            "opponent_score": self.player2.score,
            "my_correct": self.player1.current_correct,
            "opponent_correct": self.player2.current_correct,
            "correct_answer": self.question["answer"].split("|")[0],
        })
        await self.send(self.player2, {
            "type": "round_end",
            "round": self.current_round,
            "my_score": self.player2.score,
            "opponent_score": self.player1.score,
            "my_correct": self.player2.current_correct,
            "opponent_correct": self.player1.current_correct,
            "correct_answer": self.question["answer"].split("|")[0],
        })

        if self.current_round >= ROUNDS_TO_WIN and self.player1.score != self.player2.score:
            await self._finish_game()
        elif self.current_round >= ROUNDS_TO_WIN and self.player1.score == self.player2.score:
            await asyncio.sleep(3)
            await self.next_round()
        else:
            await asyncio.sleep(3)
            await self.next_round()

    async def _finish_game(self):
        self.finished = True
        p1, p2 = self.player1, self.player2

        if p1.score > p2.score:
            winner, loser = p1, p2
        elif p2.score > p1.score:
            winner, loser = p2, p1
        else:
            await self._send_draw()
            return

        rating_gain = int(loser.rating * 0.1) + 10 * winner.score
        rating_loss = int(loser.rating * 0.1)

        db.update_ratings(winner.user_id, loser.user_id, rating_gain, rating_loss)

        winner_user = db.get_user(winner.user_id)
        loser_user = db.get_user(loser.user_id)

        game_id = db.save_game(
            p1.user_id, p2.user_id,
            winner.user_id, p1.score, p2.score,
            rating_gain, rating_loss, self.current_round, self.subject,
        )
        self._save_rounds(game_id)

        await self.send(p1, {
            "type": "game_over",
            "winner": "me" if winner is p1 else "opponent",
            "my_score": p1.score,
            "opponent_score": p2.score,
            "rating_change": rating_gain if winner is p1 else -rating_loss,
            "new_rating": (winner_user if winner is p1 else loser_user)["rating"],
            "total_rounds": self.current_round,
            "game_id": game_id,
        })
        await self.send(p2, {
            "type": "game_over",
            "winner": "me" if winner is p2 else "opponent",
            "my_score": p2.score,
            "opponent_score": p1.score,
            "rating_change": rating_gain if winner is p2 else -rating_loss,
            "new_rating": (winner_user if winner is p2 else loser_user)["rating"],
            "total_rounds": self.current_round,
            "game_id": game_id,
        })

    def _save_rounds(self, game_id: int):
        for rh in self._round_history:
            db.save_round(
                game_id, rh["round_num"], rh["question_id"],
                rh["p1_answer"], rh["p2_answer"],
                rh["p1_correct"], rh["p2_correct"],
                rh["p1_time"], rh["p2_time"],
            )

    async def _send_draw(self):
        self.finished = True
        db.update_draw(self.player1.user_id, self.player2.user_id)
        game_id = db.save_game(
            self.player1.user_id, self.player2.user_id, None,
            self.player1.score, self.player2.score, 0, 0,
            self.current_round, self.subject,
        )
        self._save_rounds(game_id)
        for p in [self.player1, self.player2]:
            other = self.player2 if p is self.player1 else self.player1
            await self.send(p, {
                "type": "game_over",
                "winner": "draw",
                "my_score": p.score,
                "opponent_score": other.score,
                "rating_change": 0,
                "new_rating": p.rating,
                "total_rounds": self.current_round,
                "game_id": game_id,
            })

    async def handle_disconnect(self, player: PlayerState):
        if self.finished:
            return
        player.connected = False
        player.disconnected_at = time.time()
        player.ws = None

        opponent = self.player2 if player is self.player1 else self.player1
        await self.send(opponent, {
            "type": "opponent_disconnected",
            "timeout_seconds": DISCONNECT_TIMEOUT,
        })

        if self._disconnect_task and not self._disconnect_task.done():
            self._disconnect_task.cancel()
        self._disconnect_task = asyncio.create_task(
            self._disconnect_timer(player, opponent)
        )

    async def _disconnect_timer(self, disconnected: PlayerState, opponent: PlayerState):
        try:
            await asyncio.sleep(DISCONNECT_TIMEOUT)
            if not disconnected.connected and not self.finished:
                await self._force_win(opponent, disconnected)
        except asyncio.CancelledError:
            pass

    async def _force_win(self, winner: PlayerState, loser: PlayerState):
        self.finished = True
        self.round_active = False
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        rating_gain = int(loser.rating * 0.1) + 10 * winner.score
        rating_loss = int(loser.rating * 0.1)
        db.update_ratings(winner.user_id, loser.user_id, rating_gain, rating_loss)

        winner_user = db.get_user(winner.user_id)
        game_id = db.save_game(
            self.player1.user_id, self.player2.user_id,
            winner.user_id, self.player1.score, self.player2.score,
            rating_gain, rating_loss, self.current_round, self.subject,
        )
        self._save_rounds(game_id)

        await self.send(winner, {
            "type": "game_over",
            "winner": "me",
            "my_score": winner.score,
            "opponent_score": loser.score,
            "rating_change": rating_gain,
            "new_rating": winner_user["rating"],
            "total_rounds": self.current_round,
            "reason": "opponent_timeout",
            "game_id": game_id,
        })

    async def handle_reconnect(self, player: PlayerState, ws: WebSocket):
        player.ws = ws
        player.connected = True
        player.disconnected_at = None

        if self._disconnect_task and not self._disconnect_task.done():
            self._disconnect_task.cancel()
            self._disconnect_task = None

        opponent = self.player2 if player is self.player1 else self.player1
        await self.send(opponent, {"type": "opponent_reconnected"})

        await self.send(player, {
            "type": "reconnected",
            "round": self.current_round,
            "my_score": player.score,
            "opponent_score": opponent.score,
            "opponent": opponent.username,
        })

        if self.round_active and self.question:
            msg = {
                "type": "round_start",
                "round": self.current_round,
                "question_html": self.question["condition_html"],
                "question_text": self.question["condition_text"],
                "answer_type": self.question["answer_type"],
                "my_score": player.score,
                "opponent_score": opponent.score,
            }
            await self.send(player, msg)

            if opponent.current_answer is not None and player.current_answer is None:
                elapsed = int(time.time() - opponent.answered_at)
                remaining = max(1, TIMER_AFTER_ANSWER - elapsed)
                await self.send(player, {
                    "type": "opponent_answered",
                    "timer_seconds": remaining,
                })


class MatchmakingQueue:
    def __init__(self):
        self._queues: dict[str, list[PlayerState]] = {}
        self._games: dict[int, GameSession] = {}
        self._bot_timers: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._game_counter = 0
        ensure_bots()

    async def add(self, player: PlayerState) -> GameSession | None:
        subject = player.subject
        async with self._lock:
            if subject not in self._queues:
                self._queues[subject] = []
            queue = self._queues[subject]

            queue[:] = [p for p in queue if p.user_id != player.user_id]

            if queue:
                opponent = queue.pop(0)
                self._cancel_bot_timer(opponent.user_id)
                self._game_counter += 1
                game = GameSession(
                    game_id=str(self._game_counter),
                    player1=opponent,
                    player2=player,
                    subject=subject,
                )
                self._games[opponent.user_id] = game
                self._games[player.user_id] = game
                return game
            else:
                queue.append(player)
                self._start_bot_timer(player)
                return None

    def _start_bot_timer(self, player: PlayerState):
        self._cancel_bot_timer(player.user_id)
        self._bot_timers[player.user_id] = asyncio.create_task(
            self._bot_timer_task(player)
        )

    def _cancel_bot_timer(self, user_id: int):
        task = self._bot_timers.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    async def _bot_timer_task(self, player: PlayerState):
        try:
            await asyncio.sleep(BOT_TIMEOUT)
            extra_delay = random.uniform(BOT_DELAY_MIN, BOT_DELAY_MAX)
            await asyncio.sleep(extra_delay)

            async with self._lock:
                still_in_queue = False
                subject = player.subject
                if subject in self._queues:
                    still_in_queue = any(p.user_id == player.user_id for p in self._queues[subject])
                if not still_in_queue:
                    return

                self._queues[subject] = [p for p in self._queues[subject] if p.user_id != player.user_id]

                bot_user = db.get_random_bot()
                if not bot_user:
                    self._queues[subject].append(player)
                    return

                bot_player = PlayerState(
                    user_id=bot_user["id"],
                    username=bot_user["username"],
                    rating=bot_user["rating"],
                    ws=None,
                    subject=subject,
                    is_bot=True,
                )

                brain = BotBrain(
                    bot_user_id=bot_user["id"],
                    human_user_id=player.user_id,
                )
                brain.load_human_stats()

                self._game_counter += 1
                game = GameSession(
                    game_id=str(self._game_counter),
                    player1=player,
                    player2=bot_player,
                    subject=subject,
                    bot_brain=brain,
                )
                self._games[player.user_id] = game
                self._games[bot_user["id"]] = game

            asyncio.create_task(game.start())
        except asyncio.CancelledError:
            pass

    async def remove(self, user_id: int):
        async with self._lock:
            self._cancel_bot_timer(user_id)
            for queue in self._queues.values():
                queue[:] = [p for p in queue if p.user_id != user_id]

    def get_game(self, user_id: int) -> GameSession | None:
        return self._games.get(user_id)

    def remove_game(self, user_id: int):
        if user_id in self._games:
            del self._games[user_id]


matchmaking = MatchmakingQueue()
