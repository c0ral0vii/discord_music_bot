from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Literal

import chess
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

PIECE_NAMES = {
    chess.PAWN: "Пешка",
    chess.KNIGHT: "Конь",
    chess.BISHOP: "Слон",
    chess.ROOK: "Ладья",
    chess.QUEEN: "Ферзь",
    chess.KING: "Король",
}
PROMOTION_NAMES = {
    chess.QUEEN: "Ферзь",
    chess.ROOK: "Ладья",
    chess.BISHOP: "Слон",
    chess.KNIGHT: "Конь",
}
PIECE_GLYPHS = {
    (chess.PAWN, chess.WHITE): "♙",
    (chess.KNIGHT, chess.WHITE): "♘",
    (chess.BISHOP, chess.WHITE): "♗",
    (chess.ROOK, chess.WHITE): "♖",
    (chess.QUEEN, chess.WHITE): "♕",
    (chess.KING, chess.WHITE): "♔",
    (chess.PAWN, chess.BLACK): "♟",
    (chess.KNIGHT, chess.BLACK): "♞",
    (chess.BISHOP, chess.BLACK): "♝",
    (chess.ROOK, chess.BLACK): "♜",
    (chess.QUEEN, chess.BLACK): "♛",
    (chess.KING, chess.BLACK): "♚",
}
BOARD_SECTIONS = (
    ("Верхняя доска 8-5", (7, 6, 5, 4)),
    ("Нижняя доска 4-1", (3, 2, 1, 0)),
)
FILE_GROUPS = (
    ("A-D", (0, 1, 2, 3)),
    ("E-H", (4, 5, 6, 7)),
)
BOARD_MARGIN = 56
BOARD_TOP = 28
SQUARE_SIZE = 88
BOARD_SIZE = SQUARE_SIZE * 8
IMAGE_WIDTH = BOARD_MARGIN * 2 + BOARD_SIZE
IMAGE_HEIGHT = BOARD_TOP + BOARD_MARGIN + BOARD_SIZE
BOARD_FILENAME = "chess_board.png"
BOARD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
LIGHT_SQUARE = "#f0d9b5"
DARK_SQUARE = "#b58863"
LAST_MOVE_SQUARE = "#97c1d9"
SELECTED_SQUARE = "#f4d35e"
LEGAL_MOVE_SQUARE = "#7cc576"
CHECK_SQUARE = "#d9544d"
BACKGROUND_COLOR = "#141414"
TEXT_COLOR = "#f7f4ea"
WHITE_PIECE_COLOR = "#f5f3ea"
BLACK_PIECE_COLOR = "#161616"
WHITE_STROKE_COLOR = "#202020"
BLACK_STROKE_COLOR = "#faf5e6"

ChessMode = Literal["click", "png"]


def _side_label(color: chess.Color) -> str:
    return "Белые" if color == chess.WHITE else "Черные"


async def _send_ephemeral_message(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
        return
    await interaction.response.send_message(message, ephemeral=True)


def _disable_view_items(view: discord.ui.View) -> None:
    for item in view.children:
        item.disabled = True


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in BOARD_FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


@dataclass(slots=True)
class ChessSession:
    guild_id: int
    channel_id: int
    challenger_id: int
    opponent_id: int
    mode: ChessMode | None = None
    board: chess.Board = field(default_factory=chess.Board)
    white_player_id: int | None = None
    black_player_id: int | None = None
    selected_square: chess.Square | None = None
    last_move: chess.Move | None = None
    last_move_text: str | None = None
    message: discord.Message | None = None
    board_messages: list[discord.Message | None] = field(default_factory=lambda: [None, None])
    finished: bool = False
    result_text: str | None = None
    board_error_text: str | None = None
    pending_promotion_moves: list[chess.Move] = field(default_factory=list)
    board_file_groups: list[int] = field(default_factory=lambda: [0, 0])
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def players(self) -> set[int]:
        return {self.challenger_id, self.opponent_id}

    @property
    def current_player_id(self) -> int | None:
        if self.white_player_id is None or self.black_player_id is None:
            return None
        return self.white_player_id if self.board.turn == chess.WHITE else self.black_player_id

    def is_player(self, user_id: int) -> bool:
        return user_id in self.players

    def legal_source_squares(self) -> list[chess.Square]:
        legal_moves = list(self.board.legal_moves)
        source_squares = sorted({move.from_square for move in legal_moves})
        return source_squares

    def legal_moves_for_selected(self) -> list[chess.Move]:
        if self.selected_square is None:
            return []
        moves = [move for move in self.board.legal_moves if move.from_square == self.selected_square]
        return sorted(
            moves,
            key=lambda move: (
                move.to_square,
                move.promotion or 0,
            ),
        )

    def selected_square_name(self) -> str | None:
        if self.selected_square is None:
            return None
        return chess.square_name(self.selected_square)

    def highlighted_targets(self) -> set[chess.Square]:
        return {move.to_square for move in self.legal_moves_for_selected()}

    def promotion_choices(self) -> list[int]:
        return [move.promotion for move in self.pending_promotion_moves if move.promotion is not None]

    def describe_selected_piece(self) -> str | None:
        if self.selected_square is None:
            return None
        piece = self.board.piece_at(self.selected_square)
        if piece is None:
            return None
        return f"{PIECE_NAMES[piece.piece_type]} {chess.square_name(self.selected_square)}"

    def mode_label(self) -> str:
        if self.mode == "click":
            return "Кликабельная доска"
        if self.mode == "png":
            return "PNG + меню"
        return "не выбран"

    def content_text(self) -> str:
        if self.mode is None:
            return (
                f"Шахматы: <@{self.challenger_id}> против <@{self.opponent_id}>\n"
                "Выберите версию игры. Нажимать могут только эти два игрока."
            )

        if self.white_player_id is None or self.black_player_id is None:
            return (
                f"Шахматы: <@{self.challenger_id}> против <@{self.opponent_id}>\n"
                f"Режим: **{self.mode_label()}**\n"
                "Выберите сторону. Нажимать могут только эти два игрока.\n"
                "Кто нажмет цвет, тот его и получит."
            )

        lines = [
            f"Шахматы: Белые <@{self.white_player_id}> против Черных <@{self.black_player_id}>",
            f"Режим: **{self.mode_label()}**",
        ]
        if self.finished:
            lines.append(self.result_text or "Партия завершена.")
            return "\n".join(lines)

        current_player_id = self.current_player_id
        lines.append(f"Ход: {_side_label(self.board.turn)} <@{current_player_id}>")
        if self.board.is_check():
            lines.append("Шах.")
        if self.last_move is not None:
            lines.append(f"Последний ход: {self.last_move_text or self._move_label(self.last_move)}")
        if self.pending_promotion_moves:
            lines.append("Выбери превращение пешки кнопками ниже.")
        if self.board_error_text:
            lines.append(self.board_error_text)
        selected_piece = self.describe_selected_piece()
        if self.mode == "click":
            if selected_piece is None:
                lines.append("Нажми на клетку с фигурой, потом на клетку назначения.")
            else:
                lines.append(
                    f"Выбрана фигура: {selected_piece}. Доступные клетки подсвечены зелёным на кнопках."
                )
            lines.append("Ниже две кликабельные доски: верхняя и нижняя половина.")
        else:
            if selected_piece is None:
                lines.append("Выбери фигуру в меню ниже, затем выбери ход.")
            else:
                lines.append(
                    f"Выбрана фигура: {selected_piece}. Возможные ходы подсвечены зелёным на PNG-доске."
                )
        return "\n".join(lines)

    def _move_label(self, move: chess.Move) -> str:
        start = chess.square_name(move.from_square)
        end = chess.square_name(move.to_square)
        piece = self.board.piece_at(move.from_square)
        if piece is None:
            return f"{start}->{end}"

        capture = self.board.piece_at(move.to_square) is not None or self.board.is_en_passant(move)
        label = f"{PIECE_NAMES[piece.piece_type]} {start} {'x' if capture else '->'} {end}"
        if move.promotion:
            label += f" = {PROMOTION_NAMES[move.promotion]}"
        if self.board.is_castling(move):
            label += " (рокировка)"
        return label

    def start(self, color: chess.Color, chooser_id: int) -> None:
        other_player_id = self.opponent_id if chooser_id == self.challenger_id else self.challenger_id
        if color == chess.WHITE:
            self.white_player_id = chooser_id
            self.black_player_id = other_player_id
        else:
            self.white_player_id = other_player_id
            self.black_player_id = chooser_id
        self.board.reset()
        self.selected_square = None
        self.last_move = None
        self.last_move_text = None
        self.pending_promotion_moves.clear()
        self.board_file_groups = [0, 0]
        self.finished = False
        self.result_text = None
        self.board_error_text = None

    def choose_mode(self, mode: ChessMode) -> None:
        self.mode = mode
        self.selected_square = None
        self.pending_promotion_moves.clear()
        self.board_error_text = None

    def start_random(self) -> None:
        chooser = random.choice([self.challenger_id, self.opponent_id])
        color = random.choice([chess.WHITE, chess.BLACK])
        self.start(color, chooser)

    def board_file(self) -> discord.File:
        image = self._render_board_image()
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=BOARD_FILENAME)

    def _render_board_image(self) -> Image.Image:
        image = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), BACKGROUND_COLOR)
        draw = ImageDraw.Draw(image)
        piece_font = _load_font(56)
        coord_font = _load_font(22)
        title_font = _load_font(26)

        draw.text((BOARD_MARGIN, 4), "Chess Play", font=title_font, fill=TEXT_COLOR)

        highlighted_targets = self.highlighted_targets()
        checked_king = self.board.king(self.board.turn) if self.board.is_check() else None

        for rank_index in range(8):
            rank = 7 - rank_index
            for file_index in range(8):
                square = chess.square(file_index, rank)
                x0 = BOARD_MARGIN + file_index * SQUARE_SIZE
                y0 = BOARD_TOP + rank_index * SQUARE_SIZE
                x1 = x0 + SQUARE_SIZE
                y1 = y0 + SQUARE_SIZE

                base_color = LIGHT_SQUARE if (file_index + rank_index) % 2 == 0 else DARK_SQUARE
                square_color = base_color
                if self.last_move and square in {self.last_move.from_square, self.last_move.to_square}:
                    square_color = LAST_MOVE_SQUARE
                if square == self.selected_square:
                    square_color = SELECTED_SQUARE
                elif square in highlighted_targets:
                    square_color = LEGAL_MOVE_SQUARE
                if checked_king == square:
                    square_color = CHECK_SQUARE

                draw.rectangle((x0, y0, x1, y1), fill=square_color)

                piece = self.board.piece_at(square)
                if piece is None:
                    continue

                glyph = PIECE_GLYPHS[(piece.piece_type, piece.color)]
                fill = WHITE_PIECE_COLOR if piece.color == chess.WHITE else BLACK_PIECE_COLOR
                stroke = WHITE_STROKE_COLOR if piece.color == chess.WHITE else BLACK_STROKE_COLOR
                bbox = draw.textbbox((0, 0), glyph, font=piece_font, stroke_width=2)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
                text_x = x0 + (SQUARE_SIZE - text_width) / 2 - bbox[0]
                text_y = y0 + (SQUARE_SIZE - text_height) / 2 - bbox[1] - 4
                draw.text(
                    (text_x, text_y),
                    glyph,
                    font=piece_font,
                    fill=fill,
                    stroke_width=2,
                    stroke_fill=stroke,
                )

        for idx, file_name in enumerate("abcdefgh"):
            x = BOARD_MARGIN + idx * SQUARE_SIZE + SQUARE_SIZE / 2 - 6
            draw.text((x, IMAGE_HEIGHT - BOARD_MARGIN + 6), file_name, font=coord_font, fill=TEXT_COLOR)
        for idx, rank_name in enumerate("87654321"):
            y = BOARD_TOP + idx * SQUARE_SIZE + SQUARE_SIZE / 2 - 12
            draw.text((18, y), rank_name, font=coord_font, fill=TEXT_COLOR)

        return image

    async def refresh_message(self) -> None:
        if self.message is None:
            return

        if self.finished:
            view = ChessFinishedView(self)
        elif self.mode is None:
            view = ChessModeView(self)
        elif self.white_player_id is None or self.black_player_id is None:
            view = ChessSetupView(self)
        elif self.mode == "png":
            view = ChessPngGameView(self)
        else:
            view = ChessControlsView(self)

        if self.finished:
            _disable_view_items(view)

        attachments: list[discord.File] = []
        if self.mode == "png" and self.white_player_id is not None and self.black_player_id is not None:
            attachments = [self.board_file()]

        with suppress(discord.HTTPException):
            await self.message.edit(
                content=self.content_text(),
                view=view,
                attachments=attachments,
            )
        if self.mode == "click" and self.white_player_id is not None and self.black_player_id is not None:
            await self.refresh_board_messages()
        else:
            await self.cleanup_board_messages()

    async def refresh_board_messages(self) -> None:
        if self.message is None or self.white_player_id is None or self.black_player_id is None:
            return

        channel = self.message.channel
        board_error_text: str | None = None
        for board_index, section in enumerate(BOARD_SECTIONS):
            existing = self.board_messages[board_index]
            view = ChessBoardView(self, board_index)
            file_group_name = FILE_GROUPS[self.board_file_groups[board_index]][0]
            content = f"{section[0]} · {file_group_name}"
            if existing is None:
                try:
                    self.board_messages[board_index] = await channel.send(content, view=view)
                except discord.HTTPException as exc:
                    board_error_text = (
                        "Не удалось отправить кликабельную доску в Discord. "
                        f"HTTP {exc.status}."
                    )
                continue
            try:
                await existing.edit(content=content, view=view)
            except discord.HTTPException as exc:
                board_error_text = (
                    "Не удалось обновить кликабельную доску в Discord. "
                    f"HTTP {exc.status}."
                )

        if self.board_error_text != board_error_text:
            self.board_error_text = board_error_text
            with suppress(discord.HTTPException):
                await self.message.edit(
                    content=self.content_text(),
                    view=ChessFinishedView(self) if self.finished else ChessControlsView(self),
                )

    async def cleanup_board_messages(self) -> None:
        for index, board_message in enumerate(self.board_messages):
            if board_message is None:
                continue
            with suppress(discord.HTTPException):
                await board_message.delete()
            self.board_messages[index] = None

    async def apply_move(self, move: chess.Move) -> None:
        self.last_move_text = self._move_label(move)
        self.board.push(move)
        self.last_move = move
        self.selected_square = None
        self.pending_promotion_moves.clear()
        self._update_outcome()

    def resign(self, loser_id: int) -> None:
        winner_id = self.opponent_id if loser_id == self.challenger_id else self.challenger_id
        self.finished = True
        self.result_text = f"<@{loser_id}> сдался. Победил <@{winner_id}>."
        self.pending_promotion_moves.clear()

    async def handle_square_click(self, square: chess.Square, user_id: int) -> str | None:
        if user_id != self.current_player_id:
            return "Сейчас ход другого игрока."

        piece = self.board.piece_at(square)
        legal_source_squares = set(self.legal_source_squares())

        if self.selected_square is None:
            if piece is None or piece.color != self.board.turn or square not in legal_source_squares:
                return "Нужно выбрать свою фигуру."
            self.selected_square = square
            self.pending_promotion_moves.clear()
            return None

        if square == self.selected_square:
            self.selected_square = None
            self.pending_promotion_moves.clear()
            return None

        target_moves = [
            move
            for move in self.board.legal_moves
            if move.from_square == self.selected_square and move.to_square == square
        ]
        if target_moves:
            if len(target_moves) == 1:
                await self.apply_move(target_moves[0])
                return None
            self.pending_promotion_moves = target_moves
            return None

        if piece is not None and piece.color == self.board.turn and square in legal_source_squares:
            self.selected_square = square
            self.pending_promotion_moves.clear()
            return None

        self.pending_promotion_moves.clear()
        return "Сюда эта фигура пойти не может."

    def _update_outcome(self) -> None:
        outcome = self.board.outcome(claim_draw=True)
        if outcome is None:
            return

        self.finished = True
        if outcome.winner is None:
            termination_text = {
                chess.Termination.STALEMATE: "Пат.",
                chess.Termination.INSUFFICIENT_MATERIAL: "Ничья: недостаточно материала.",
                chess.Termination.FIFTY_MOVES: "Ничья по правилу 50 ходов.",
                chess.Termination.THREEFOLD_REPETITION: "Ничья по троекратному повторению.",
            }.get(outcome.termination, "Ничья.")
            self.result_text = termination_text
            return

        winner_id = self.white_player_id if outcome.winner == chess.WHITE else self.black_player_id
        if outcome.termination == chess.Termination.CHECKMATE:
            self.result_text = f"Мат. Победил <@{winner_id}>."
        else:
            self.result_text = f"Партия завершена. Победил <@{winner_id}>."

    async def finish_due_to_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.result_text = "Партия завершена по таймауту бездействия."
        await self.refresh_message()
        _release_session(self)


active_chess_games: dict[int, ChessSession] = {}


def _release_session(session: ChessSession) -> None:
    active_session = active_chess_games.get(session.channel_id)
    if active_session is session:
        active_chess_games.pop(session.channel_id, None)


def _find_session_for_user(user_id: int) -> ChessSession | None:
    for session in active_chess_games.values():
        if not session.finished and session.is_player(user_id):
            return session
    return None


class ChessModeView(discord.ui.View):
    def __init__(self, session: ChessSession) -> None:
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.session.players:
            await _send_ephemeral_message(interaction, "Эта партия не для тебя.")
            return False
        if self.session.finished:
            await _send_ephemeral_message(interaction, "Эта партия уже завершена.")
            return False
        return True

    async def on_timeout(self) -> None:
        await self.session.finish_due_to_timeout()

    @discord.ui.button(label="Кликабельная доска", style=discord.ButtonStyle.primary, row=0)
    async def choose_click_mode(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.choose_mode("click")
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="PNG + меню", style=discord.ButtonStyle.success, row=0)
    async def choose_png_mode(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.choose_mode("png")
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.danger, row=0)
    async def cancel_game(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.finished = True
            self.session.result_text = "Создание партии отменено."
            await interaction.response.defer()
            await self.session.refresh_message()
            _release_session(self.session)


class ChessSetupView(discord.ui.View):
    def __init__(self, session: ChessSession) -> None:
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.session.players:
            await _send_ephemeral_message(interaction, "Эта партия не для тебя.")
            return False
        if self.session.finished:
            await _send_ephemeral_message(interaction, "Эта партия уже завершена.")
            return False
        return True

    async def on_timeout(self) -> None:
        await self.session.finish_due_to_timeout()

    @discord.ui.button(label="Играть белыми", style=discord.ButtonStyle.success, row=0)
    async def choose_white(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.start(chess.WHITE, interaction.user.id)
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="Играть черными", style=discord.ButtonStyle.primary, row=0)
    async def choose_black(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.start(chess.BLACK, interaction.user.id)
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="Случайно", style=discord.ButtonStyle.secondary, row=0)
    async def choose_random(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.start_random()
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.danger, row=0)
    async def cancel_game(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.finished = True
            self.session.result_text = "Создание партии отменено."
            await interaction.response.defer()
            await self.session.refresh_message()
            _release_session(self.session)


class ChessPieceSelect(discord.ui.Select["ChessPngGameView"]):
    def __init__(self, session: ChessSession) -> None:
        options: list[discord.SelectOption] = []
        for square in session.legal_source_squares():
            piece = session.board.piece_at(square)
            if piece is None:
                continue
            moves_count = len([move for move in session.board.legal_moves if move.from_square == square])
            options.append(
                discord.SelectOption(
                    label=f"{chess.square_name(square)} · {PIECE_NAMES[piece.piece_type]}"[:100],
                    value=chess.square_name(square),
                    description=f"Доступно ходов: {moves_count}"[:100],
                )
            )

        super().__init__(
            placeholder=(
                f"Фигура: {chess.square_name(session.selected_square)}"
                if session.selected_square is not None
                else "Выбери фигуру"
            ),
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        async with self.session.lock:
            if interaction.user.id != self.session.current_player_id:
                await interaction.response.send_message("Сейчас ход другого игрока.", ephemeral=True)
                return
            self.session.selected_square = chess.parse_square(self.values[0])
            await interaction.response.defer()
            await self.session.refresh_message()


class ChessMoveSelect(discord.ui.Select["ChessPngGameView"]):
    def __init__(self, session: ChessSession, moves: list[chess.Move], row: int, index: int) -> None:
        options = [
            discord.SelectOption(
                label=session._move_label(move)[:100],
                value=move.uci(),
            )
            for move in moves
        ]
        super().__init__(
            placeholder=f"Выбери ход ({index})",
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        async with self.session.lock:
            if interaction.user.id != self.session.current_player_id:
                await interaction.response.send_message("Сейчас ход другого игрока.", ephemeral=True)
                return
            move = chess.Move.from_uci(self.values[0])
            if move not in self.session.board.legal_moves:
                await interaction.response.send_message("Этот ход больше недоступен.", ephemeral=True)
                return
            await interaction.response.defer()
            await self.session.apply_move(move)
            await self.session.refresh_message()
            if self.session.finished:
                _release_session(self.session)


class ChessPngGameView(discord.ui.View):
    def __init__(self, session: ChessSession) -> None:
        super().__init__(timeout=1800)
        self.session = session
        self.add_item(ChessPieceSelect(session))
        selected_moves = session.legal_moves_for_selected()
        for index, offset in enumerate(range(0, len(selected_moves), 25), start=1):
            chunk = selected_moves[offset : offset + 25]
            if not chunk or index > 2:
                break
            self.add_item(ChessMoveSelect(session, chunk, row=index, index=index))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in {self.session.white_player_id, self.session.black_player_id}:
            await _send_ephemeral_message(
                interaction,
                "В этой партии могут ходить только два выбранных игрока.",
            )
            return False
        if self.session.finished:
            await _send_ephemeral_message(interaction, "Эта партия уже завершена.")
            return False
        return True

    async def on_timeout(self) -> None:
        await self.session.finish_due_to_timeout()

    @discord.ui.button(label="Сбросить выбор", style=discord.ButtonStyle.secondary, row=4)
    async def clear_selection(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            if interaction.user.id != self.session.current_player_id:
                await interaction.response.send_message("Сейчас ход другого игрока.", ephemeral=True)
                return
            self.session.selected_square = None
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="Сдаться", style=discord.ButtonStyle.danger, row=4)
    async def resign(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.resign(interaction.user.id)
            await interaction.response.defer()
            await self.session.refresh_message()
            if self.session.finished:
                _release_session(self.session)


def _square_label(session: ChessSession, square: chess.Square) -> str:
    piece = session.board.piece_at(square)
    prefix = PIECE_GLYPHS[(piece.piece_type, piece.color)] if piece else "·"
    return f"{prefix}{chess.square_name(square)}"


def _square_style(session: ChessSession, square: chess.Square) -> discord.ButtonStyle:
    if session.selected_square == square:
        return discord.ButtonStyle.primary
    if square in session.highlighted_targets():
        return discord.ButtonStyle.success
    checked_king = session.board.king(session.board.turn) if session.board.is_check() else None
    if checked_king == square:
        return discord.ButtonStyle.danger
    return discord.ButtonStyle.secondary


class ChessPromotionButton(discord.ui.Button["ChessControlsView"]):
    def __init__(self, session: ChessSession, promotion: int, row: int) -> None:
        super().__init__(
            label=f"Пешка -> {PROMOTION_NAMES[promotion]}",
            style=discord.ButtonStyle.success,
            row=row,
        )
        self.session = session
        self.promotion = promotion

    async def callback(self, interaction: discord.Interaction) -> None:
        async with self.session.lock:
            if interaction.user.id != self.session.current_player_id:
                await interaction.response.send_message("Сейчас ход другого игрока.", ephemeral=True)
                return

            move = next(
                (candidate for candidate in self.session.pending_promotion_moves if candidate.promotion == self.promotion),
                None,
            )
            if move is None:
                await interaction.response.send_message("Этот вариант превращения уже недоступен.", ephemeral=True)
                return

            await interaction.response.defer()
            await self.session.apply_move(move)
            await self.session.refresh_message()
            if self.session.finished:
                _release_session(self.session)


class ChessControlsView(discord.ui.View):
    def __init__(self, session: ChessSession) -> None:
        super().__init__(timeout=1800)
        self.session = session
        for promotion in session.promotion_choices():
            self.add_item(ChessPromotionButton(session, promotion, row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in {self.session.white_player_id, self.session.black_player_id}:
            await _send_ephemeral_message(
                interaction,
                "В этой партии могут ходить только два выбранных игрока.",
            )
            return False
        if self.session.finished:
            await _send_ephemeral_message(interaction, "Эта партия уже завершена.")
            return False
        return True

    async def on_timeout(self) -> None:
        await self.session.finish_due_to_timeout()

    @discord.ui.button(label="Сбросить выбор", style=discord.ButtonStyle.secondary, row=4)
    async def clear_selection(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            if interaction.user.id != self.session.current_player_id:
                await interaction.response.send_message("Сейчас ход другого игрока.", ephemeral=True)
                return
            self.session.selected_square = None
            self.session.pending_promotion_moves.clear()
            await interaction.response.defer()
            await self.session.refresh_message()

    @discord.ui.button(label="Сдаться", style=discord.ButtonStyle.danger, row=4)
    async def resign(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        async with self.session.lock:
            self.session.resign(interaction.user.id)
            await interaction.response.defer()
            await self.session.refresh_message()
            _release_session(self.session)


class ChessSquareButton(discord.ui.Button["ChessBoardView"]):
    def __init__(self, session: ChessSession, square: chess.Square, row: int) -> None:
        super().__init__(
            label=_square_label(session, square),
            style=_square_style(session, square),
            disabled=session.finished,
            custom_id=f"chess:{session.channel_id}:{chess.square_name(square)}",
            row=row,
        )
        self.session = session
        self.square = square

    async def callback(self, interaction: discord.Interaction) -> None:
        async with self.session.lock:
            if interaction.user.id not in {self.session.white_player_id, self.session.black_player_id}:
                await interaction.response.send_message(
                    "В этой партии могут ходить только два выбранных игрока.",
                    ephemeral=True,
                )
                return
            if self.session.finished:
                await interaction.response.send_message("Эта партия уже завершена.", ephemeral=True)
                return

            message = await self.session.handle_square_click(self.square, interaction.user.id)
            await interaction.response.defer()
            await self.session.refresh_message()
            if self.session.finished:
                _release_session(self.session)
            if message:
                await interaction.followup.send(message, ephemeral=True)


class ChessFilesToggleButton(discord.ui.Button["ChessBoardView"]):
    def __init__(self, session: ChessSession, board_index: int, file_group_index: int) -> None:
        label = FILE_GROUPS[file_group_index][0]
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if session.board_file_groups[board_index] == file_group_index
                else discord.ButtonStyle.secondary
            ),
            disabled=session.finished or session.board_file_groups[board_index] == file_group_index,
            custom_id=f"chess:files:{session.channel_id}:{board_index}:{file_group_index}",
            row=4,
        )
        self.session = session
        self.board_index = board_index
        self.file_group_index = file_group_index

    async def callback(self, interaction: discord.Interaction) -> None:
        async with self.session.lock:
            if interaction.user.id not in {self.session.white_player_id, self.session.black_player_id}:
                await interaction.response.send_message(
                    "В этой партии могут ходить только два выбранных игрока.",
                    ephemeral=True,
                )
                return
            self.session.board_file_groups[self.board_index] = self.file_group_index
            await interaction.response.defer()
            await self.session.refresh_message()


class ChessBoardView(discord.ui.View):
    def __init__(self, session: ChessSession, board_index: int) -> None:
        super().__init__(timeout=1800)
        self.session = session
        self.board_index = board_index
        _title, ranks = BOARD_SECTIONS[board_index]
        _file_group_name, file_indexes = FILE_GROUPS[session.board_file_groups[board_index]]
        for row_index, rank in enumerate(ranks):
            for file_index in file_indexes:
                self.add_item(
                    ChessSquareButton(
                        session,
                        chess.square(file_index, rank),
                        row=row_index,
                    )
                )
        self.add_item(ChessFilesToggleButton(session, board_index, 0))
        self.add_item(ChessFilesToggleButton(session, board_index, 1))

    async def on_timeout(self) -> None:
        await self.session.finish_due_to_timeout()


class ChessFinishedView(discord.ui.View):
    def __init__(self, session: ChessSession) -> None:
        super().__init__(timeout=1)
        self.session = session


async def start_chess_game(
    ctx: commands.Context[commands.Bot],
    opponent: discord.Member | None,
) -> None:
    if ctx.guild is None:
        await ctx.send("Эта команда работает только на сервере.")
        return

    challenger = ctx.author
    if not isinstance(challenger, discord.Member):
        await ctx.send("Не удалось определить игрока.")
        return

    if opponent is None:
        await ctx.send("Использование: `!chess_play @пользователь`")
        return

    if opponent.bot:
        await ctx.send("С ботами играть нельзя.")
        return

    if opponent.id == challenger.id:
        await ctx.send("Нельзя играть с самим собой.")
        return

    if ctx.channel.id in active_chess_games and not active_chess_games[ctx.channel.id].finished:
        await ctx.send("В этом канале уже идет шахматная партия.")
        return

    existing = _find_session_for_user(challenger.id) or _find_session_for_user(opponent.id)
    if existing is not None:
        await ctx.send("Один из игроков уже участвует в другой шахматной партии.")
        return

    session = ChessSession(
        guild_id=ctx.guild.id,
        channel_id=ctx.channel.id,
        challenger_id=challenger.id,
        opponent_id=opponent.id,
    )
    active_chess_games[ctx.channel.id] = session
    view = ChessModeView(session)
    message = await ctx.send(session.content_text(), view=view)
    session.message = message
