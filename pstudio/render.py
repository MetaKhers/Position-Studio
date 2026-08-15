"""Candlestick chart renderer.

Draws the chart the brief asks for: candles only, no indicators or leftover
objects, 80-120 bars in view, and dotted entry/stop/target lines that span the
life of the trade rather than running edge to edge.

Why render rather than screenshot MT5: a screenshot inherits whatever template,
indicator and object clutter the terminal happens to have, needs a compiled EA
dropped into the terminal by hand, and cannot be reproduced later. Drawing from
the terminal's own OHLC data is deterministic and identical on every machine,
and satisfies "clear every other object but candles" by construction - there is
nothing on the chart that was not put there deliberately.

Quality comes from supersampling: everything is drawn at 2x and reduced with
Lanczos, which gives clean dotted lines and readable small text without
per-pixel antialiasing work.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import model, paths

_FONT_FILE = "Vazirmatn-VariableFont_wght.ttf"
_FALLBACKS = ("segoeui.ttf", "arial.ttf", "calibri.ttf")

# Bar length per timeframe. Duplicated from mt5conn deliberately: the renderer
# draws saved bar data and must not require a live terminal connection.
_TF_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M5": 300, "M10": 600, "M15": 900,
    "M30": 1800, "H1": 3600, "H2": 7200, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200, "D1": 86400, "W1": 604800, "MN1": 2592000,
}

_TF_LABELS = {
    "M1": "1 minute", "M5": "5 minutes", "M15": "15 minutes",
    "M30": "30 minutes", "H1": "1 hour", "H4": "4 hours", "D1": "Daily",
}


@dataclass
class Theme:
    """Chart palette. Two are shipped; both are colour-blind safe on the
    win/loss pair, which is why teal is used for bullish rather than green."""

    name: str = "midnight"
    bg: tuple = (11, 15, 25)
    panel: tuple = (17, 23, 37)
    grid: tuple = (30, 39, 58)
    grid_soft: tuple = (23, 30, 46)
    axis_text: tuple = (122, 138, 168)
    title: tuple = (236, 242, 252)
    subtitle: tuple = (146, 162, 191)
    bull: tuple = (38, 198, 168)
    bear: tuple = (242, 84, 108)
    bull_soft: tuple = (38, 198, 168, 40)
    bear_soft: tuple = (242, 84, 108, 40)
    entry: tuple = (108, 168, 255)
    stop: tuple = (242, 84, 108)
    target: tuple = (38, 198, 168)
    marker_in: tuple = (108, 168, 255)
    marker_out: tuple = (255, 196, 84)
    risk_fill: tuple = (242, 84, 108, 26)
    reward_fill: tuple = (38, 198, 168, 24)
    span_fill: tuple = (108, 168, 255, 16)
    watermark: tuple = (54, 66, 92)
    now_line: tuple = (255, 196, 84)
    badge_bg: tuple = (24, 32, 50)
    excursion: tuple = (168, 132, 255)

    @classmethod
    def light(cls) -> "Theme":
        return cls(
            name="daylight",
            bg=(248, 250, 253),
            panel=(255, 255, 255),
            grid=(222, 229, 240),
            grid_soft=(236, 241, 248),
            axis_text=(104, 118, 143),
            title=(18, 26, 42),
            subtitle=(88, 102, 128),
            bull=(0, 150, 122),
            bear=(214, 45, 74),
            bull_soft=(0, 150, 122, 40),
            bear_soft=(214, 45, 74, 40),
            entry=(38, 108, 224),
            stop=(214, 45, 74),
            target=(0, 150, 122),
            marker_in=(38, 108, 224),
            marker_out=(196, 132, 0),
            risk_fill=(214, 45, 74, 22),
            reward_fill=(0, 150, 122, 20),
            span_fill=(38, 108, 224, 14),
            watermark=(206, 216, 230),
            now_line=(196, 132, 0),
            badge_bg=(238, 243, 250),
            excursion=(126, 78, 220),
        )


THEMES = {"midnight": Theme(), "daylight": Theme.light()}


@dataclass
class TradeOverlay:
    """What to draw on top of the candles."""

    side: str = "buy"
    entry_price: float | None = None
    exit_price: float | None = None
    entry_time: float | None = None
    exit_time: float | None = None
    sl_initial: float | None = None
    tp_initial: float | None = None
    sl_final: float | None = None
    tp_final: float | None = None
    mae_price: float | None = None
    mfe_price: float | None = None
    digits: int = 2
    labels: dict = field(default_factory=dict)


class Fonts:
    """Lazily loaded font ladder, with a weight axis when the file supports it."""

    def __init__(self, scale: int = 1):
        self.scale = scale
        self._cache: dict[tuple[int, int], ImageFont.FreeTypeFont] = {}
        self._path = paths.font_path(_FONT_FILE)
        if self._path is None:
            for name in _FALLBACKS:
                self._path = paths.font_path(name)
                if self._path:
                    break

    def get(self, size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
        key = (size * self.scale, weight)
        if key in self._cache:
            return self._cache[key]
        try:
            font = ImageFont.truetype(str(self._path), size * self.scale)
            try:
                font.set_variation_by_axes([float(weight)])
            except Exception:
                pass  # static font - weight comes from the file itself
        except Exception:
            font = ImageFont.load_default()
        self._cache[key] = font
        return font


def _fmt_price(value: float | None, digits: int) -> str:
    if value is None:
        return "-"
    return f"{value:,.{digits}f}"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+,.2f}"


def _nice_step(span: float, target_lines: int) -> float:
    """A round grid interval close to span/target_lines."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_lines)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= magnitude * multiple:
            return magnitude * multiple
    return magnitude * 10


def _dashed_line(draw: ImageDraw.ImageDraw, start: tuple, end: tuple, colour,
                 width: int = 1, dash: int = 10, gap: int = 7) -> None:
    """Dashed segment drawing - PIL has no dash support of its own."""
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    step_x = (x2 - x1) / length
    step_y = (y2 - y1) / length
    position = 0.0
    while position < length:
        end_pos = min(position + dash, length)
        draw.line(
            [
                (x1 + step_x * position, y1 + step_y * position),
                (x1 + step_x * end_pos, y1 + step_y * end_pos),
            ],
            fill=colour,
            width=width,
        )
        position = end_pos + gap


def _dotted_line(draw: ImageDraw.ImageDraw, start: tuple, end: tuple, colour,
                 radius: int = 2, spacing: int = 9) -> None:
    """Round-dot line. Reads as finer than a dash at chart scale, which is what
    the brief asked for on the entry/stop/target levels."""
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    step_x = (x2 - x1) / length
    step_y = (y2 - y1) / length
    position = 0.0
    while position <= length:
        cx = x1 + step_x * position
        cy = y1 + step_y * position
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius], fill=colour
        )
        position += spacing


def _text(draw: ImageDraw.ImageDraw, xy: tuple, message: str, font, fill,
          anchor: str = "la") -> None:
    draw.text(xy, message, font=font, fill=fill, anchor=anchor)


def _text_width(draw: ImageDraw.ImageDraw, message: str, font) -> int:
    box = draw.textbbox((0, 0), message, font=font)
    return box[2] - box[0]


def _pill(draw: ImageDraw.ImageDraw, xy: tuple, message: str, font, fill,
          background, padding: int = 8, radius: int = 6) -> tuple:
    """A rounded label chip. Returns the box it occupied."""
    box = draw.textbbox((0, 0), message, font=font)
    width = box[2] - box[0] + padding * 2
    height = box[3] - box[1] + padding
    x, y = xy
    draw.rounded_rectangle(
        [x, y, x + width, y + height], radius=radius, fill=background
    )
    draw.text(
        (x + padding, y + height / 2), message, font=font, fill=fill, anchor="lm"
    )
    return (x, y, x + width, y + height)


class ChartCanvas:
    """Renders one candlestick chart with an optional trade overlay."""

    def __init__(self, width: int = 1920, height: int = 1080,
                 theme: Theme | str = "midnight", supersample: int = 2):
        self.out_width = int(width)
        self.out_height = int(height)
        self.scale = max(1, int(supersample))
        self.theme = THEMES.get(theme, THEMES["midnight"]) if isinstance(theme, str) else theme
        self.fonts = Fonts(self.scale)
        self.width = self.out_width * self.scale
        self.height = self.out_height * self.scale
        self.image = Image.new("RGB", (self.width, self.height), self.theme.bg)
        self.draw = ImageDraw.Draw(self.image, "RGBA")
        # Plot area insets, in output pixels before scaling.
        self.pad_left = 26
        self.pad_right = 104
        self.pad_top = 104
        self.pad_bottom = 62
        self._digits = 2

    # -- geometry ----------------------------------------------------------
    @property
    def plot_left(self) -> float:
        return self.pad_left * self.scale

    @property
    def plot_right(self) -> float:
        return self.width - self.pad_right * self.scale

    @property
    def plot_top(self) -> float:
        return self.pad_top * self.scale

    @property
    def plot_bottom(self) -> float:
        return self.height - self.pad_bottom * self.scale

    def _setup_scales(self, bars: list[dict], extra_prices: list[float],
                      digits: int = 2) -> None:
        self._digits = int(digits)
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        levels = [p for p in extra_prices if p]
        top = max(highs + levels) if levels else max(highs)
        bottom = min(lows + levels) if levels else min(lows)
        span = top - bottom
        if span <= 0:
            span = max(abs(top) * 0.001, 1e-6)
        # Headroom so candles and level labels never touch the frame edge.
        self.price_max = top + span * 0.09
        self.price_min = bottom - span * 0.09
        self.bar_count = len(bars)
        self.slot = (self.plot_right - self.plot_left) / max(1, self.bar_count)
        # Candle body is a fraction of the slot; the rest is the gap.
        self.body_width = max(self.scale, self.slot * 0.62)

    def x_of(self, index: float) -> float:
        return self.plot_left + (index + 0.5) * self.slot

    def y_of(self, price: float) -> float:
        span = self.price_max - self.price_min
        if span <= 0:
            return (self.plot_top + self.plot_bottom) / 2
        ratio = (price - self.price_min) / span
        return self.plot_bottom - ratio * (self.plot_bottom - self.plot_top)

    # -- layers ------------------------------------------------------------
    def draw_frame(self) -> None:
        self.draw.rectangle(
            [self.plot_left, self.plot_top, self.plot_right, self.plot_bottom],
            fill=self.theme.panel,
        )

    def draw_grid(self, bars: list[dict], timeframe: str) -> None:
        theme = self.theme
        font = self.fonts.get(11, 400)
        step = _nice_step(self.price_max - self.price_min, 9)
        level = math.ceil(self.price_min / step) * step
        digits = self._digits
        while level <= self.price_max:
            y = self.y_of(level)
            self.draw.line(
                [(self.plot_left, y), (self.plot_right, y)],
                fill=theme.grid_soft,
                width=max(1, self.scale // 2),
            )
            _text(
                self.draw,
                (self.plot_right + 10 * self.scale, y),
                _fmt_price(level, digits),
                font,
                theme.axis_text,
                anchor="lm",
            )
            level += step

        # Time gridlines on round boundaries. The interval is chosen from the
        # window's own span rather than the timeframe, so a 100-bar M1 chart
        # gets a label every 15 minutes instead of a single lonely hour mark.
        if not bars:
            return
        span = bars[-1]["time"] - bars[0]["time"]
        span += _TF_SECONDS.get(timeframe.upper(), 60)
        ladder = (
            (300, "%H:%M"), (900, "%H:%M"), (1800, "%H:%M"), (3600, "%H:%M"),
            (10800, "%H:%M"), (21600, "%d %b %H:%M"), (43200, "%d %b %H:%M"),
            (86400, "%d %b"), (172800, "%d %b"), (604800, "%d %b"),
            (2592000, "%b %Y"),
        )
        unit, fmt = ladder[-1]
        for candidate, candidate_fmt in ladder:
            if span / candidate <= 9:
                unit, fmt = candidate, candidate_fmt
                break

        last_label = None
        for index, bar in enumerate(bars):
            moment = model.broker_dt(bar["time"])
            if moment is None:
                continue
            marker = int(bar["time"] // unit)
            if marker == last_label:
                continue
            last_label = marker
            if index == 0:
                continue
            x = self.x_of(index)
            self.draw.line(
                [(x, self.plot_top), (x, self.plot_bottom)],
                fill=theme.grid_soft,
                width=max(1, self.scale // 2),
            )
            _text(
                self.draw,
                (x, self.plot_bottom + 12 * self.scale),
                moment.strftime(fmt),
                font,
                theme.axis_text,
                anchor="mt",
            )

    def draw_candles(self, bars: list[dict]) -> None:
        theme = self.theme
        wick_width = max(self.scale, int(self.body_width * 0.16))
        for index, bar in enumerate(bars):
            x = self.x_of(index)
            bullish = bar["close"] >= bar["open"]
            colour = theme.bull if bullish else theme.bear
            top = self.y_of(bar["high"])
            bottom = self.y_of(bar["low"])
            self.draw.line([(x, top), (x, bottom)], fill=colour, width=wick_width)

            open_y = self.y_of(bar["open"])
            close_y = self.y_of(bar["close"])
            body_top = min(open_y, close_y)
            body_bottom = max(open_y, close_y)
            # A doji still needs to be visible.
            if body_bottom - body_top < self.scale:
                body_bottom = body_top + self.scale
            half = self.body_width / 2
            self.draw.rectangle(
                [x - half, body_top, x + half, body_bottom], fill=colour
            )

    def _bar_index_for_time(self, bars: list[dict], moment: float | None,
                            clamp: bool = True) -> float | None:
        """Fractional bar index for a wall-clock moment.

        Fractional so an entry halfway through a 4H bar is drawn halfway across
        it instead of snapping to the open - at high timeframes that snap is a
        visible lie about when the trade happened.
        """
        if moment is None or not bars:
            return None
        step = bars[1]["time"] - bars[0]["time"] if len(bars) > 1 else 60
        if step <= 0:
            step = 60
        for index, bar in enumerate(bars):
            if bar["time"] <= moment < bar["time"] + step:
                return index + (moment - bar["time"]) / step
        if moment < bars[0]["time"]:
            return 0.0 if clamp else None
        if moment >= bars[-1]["time"] + step:
            return float(len(bars) - 1) + 1.0 if clamp else None
        return None

    def draw_trade(self, bars: list[dict], trade: TradeOverlay,
                   show_excursions: bool = True) -> None:
        """Entry/SL/TP levels drawn dotted, spanning entry to exit only."""
        theme = self.theme
        digits = trade.digits
        entry_index = self._bar_index_for_time(bars, trade.entry_time)
        exit_index = self._bar_index_for_time(bars, trade.exit_time)
        if entry_index is None:
            return

        x_entry = self.x_of(entry_index)
        # An open trade, or one closing beyond this window, runs to the edge.
        x_exit = self.x_of(exit_index) if exit_index is not None else self.plot_right
        x_exit = min(max(x_exit, x_entry), self.plot_right)
        # Guarantee a visible span even on a trade that opened and closed
        # inside a single bar.
        if x_exit - x_entry < 6 * self.scale:
            x_exit = min(x_entry + 6 * self.scale, self.plot_right)

        y_entry = self.y_of(trade.entry_price) if trade.entry_price else None

        # Shade the risk and reward bands across the life of the trade so the
        # geometry of the setup is readable at a glance. Initial levels define
        # the plan; a level that only ever existed as a later amendment still
        # gets shaded, since otherwise the band silently disappears.
        band_sl = trade.sl_initial or trade.sl_final
        band_tp = trade.tp_initial or trade.tp_final
        if y_entry is not None and band_sl:
            y_sl = self.y_of(band_sl)
            self.draw.rectangle(
                [x_entry, min(y_entry, y_sl), x_exit, max(y_entry, y_sl)],
                fill=theme.risk_fill,
            )
        if y_entry is not None and band_tp:
            y_tp = self.y_of(band_tp)
            self.draw.rectangle(
                [x_entry, min(y_entry, y_tp), x_exit, max(y_entry, y_tp)],
                fill=theme.reward_fill,
            )

        font = self.fonts.get(11, 600)
        dot_radius = max(1, int(1.6 * self.scale))
        spacing = int(7 * self.scale)
        tick = 10 ** -digits

        levels = []
        if trade.entry_price:
            levels.append(("ENTRY", trade.entry_price, theme.entry))
        if trade.sl_initial:
            levels.append(("SL", trade.sl_initial, theme.stop))
        if trade.tp_initial:
            levels.append(("TP", trade.tp_initial, theme.target))
        # Only show a moved level when it actually moved, otherwise it is just a
        # second line on top of the first. A target that was absent at entry and
        # present at exit still has to be drawn - a trade that closed at take
        # profit with no visible target line reads as a data error.
        # "moved" spelled out rather than an arrow: Vazirmatn carries no U+2192,
        # so "SL→" drew as "SL" plus a blank ten-pixel advance - two lines both
        # labelled "SL", one with a mysterious gap. A word always has glyphs.
        if trade.sl_final and abs(trade.sl_final - (trade.sl_initial or 0)) > tick:
            label = "SL moved" if trade.sl_initial else "SL (set later)"
            levels.append((label, trade.sl_final, theme.marker_out))
        if trade.tp_final and abs(trade.tp_final - (trade.tp_initial or 0)) > tick:
            label = "TP moved" if trade.tp_initial else "TP (set later)"
            levels.append((label, trade.tp_final, theme.marker_out))

        pending: list[tuple[float, str, tuple]] = []
        for label, price, colour in levels:
            y = self.y_of(price)
            _dotted_line(
                self.draw, (x_entry, y), (x_exit, y), colour,
                radius=dot_radius, spacing=spacing,
            )
            pending.append((y, f"{label} {_fmt_price(price, digits)}", colour))

        if show_excursions:
            for label, price in (("MFE", trade.mfe_price), ("MAE", trade.mae_price)):
                if not price:
                    continue
                y = self.y_of(price)
                _dashed_line(
                    self.draw, (x_entry, y), (x_exit, y), theme.excursion,
                    width=max(1, self.scale), dash=4 * self.scale, gap=5 * self.scale,
                )
                pending.append(
                    (y, f"{label} {_fmt_price(price, digits)}", theme.excursion)
                )

        self._place_labels(pending, x_entry, x_exit, font)

        # Entry and exit markers.
        if y_entry is not None:
            self._marker(x_entry, y_entry, trade.side == "buy", theme.marker_in)
        if exit_index is not None and trade.exit_price:
            y_exit = self.y_of(trade.exit_price)
            self._marker(x_exit, y_exit, trade.side != "buy", theme.marker_out)
            # Connect entry to exit so the trade reads as one gesture.
            _dashed_line(
                self.draw, (x_entry, y_entry), (x_exit, y_exit), theme.subtitle,
                width=max(1, self.scale), dash=3 * self.scale, gap=4 * self.scale,
            )

    def _place_labels(self, requests: list[tuple], x_entry: float,
                      x_exit: float, font) -> None:
        """Draw level labels, nudged apart so none is hidden under another.

        On a high timeframe a short trade's entry, stop and target land within a
        few pixels of each other and the labels overlap into an unreadable
        smear. Each label is pushed to its own row and a leader line ties it
        back to the price it belongs to, so nothing is lost and nothing lies
        about where the level sits.
        """
        if not requests:
            return
        theme = self.theme
        row = 19 * self.scale
        ordered = sorted(requests, key=lambda item: item[0])
        placed = [float(y) for y, _, _ in ordered]

        # One downward pass opens the minimum gaps, then a clamped upward pass
        # pulls the stack back inside the plot when it has run off the bottom.
        for index in range(1, len(placed)):
            if placed[index] - placed[index - 1] < row:
                placed[index] = placed[index - 1] + row
        overflow = placed[-1] - (self.plot_bottom - row / 2)
        if overflow > 0:
            for index in range(len(placed)):
                placed[index] -= overflow
            for index in range(len(placed) - 2, -1, -1):
                if placed[index + 1] - placed[index] < row:
                    placed[index] = placed[index + 1] - row
        top_limit = self.plot_top + row / 2
        if placed[0] < top_limit:
            shift = top_limit - placed[0]
            for index in range(len(placed)):
                placed[index] += shift

        widest = max(_text_width(self.draw, text, font) for _, text, _ in ordered)
        x_label = x_exit + 9 * self.scale
        flipped = False
        if x_label + widest + 6 * self.scale > self.plot_right:
            x_label = x_entry - widest - 9 * self.scale
            flipped = True
        x_label = max(x_label, self.plot_left + 4 * self.scale)

        anchor_x = x_entry if flipped else x_exit
        for (true_y, text, colour), y in zip(ordered, placed):
            width = _text_width(self.draw, text, font)
            if abs(y - true_y) > 1.5 * self.scale:
                edge = x_label + width + 4 * self.scale if flipped else x_label - 4 * self.scale
                self.draw.line(
                    [(anchor_x, true_y), (edge, y)], fill=colour,
                    width=max(1, self.scale // 2),
                )
            self.draw.rectangle(
                [x_label - 4 * self.scale, y - 9 * self.scale,
                 x_label + width + 4 * self.scale, y + 9 * self.scale],
                fill=theme.badge_bg,
            )
            _text(self.draw, (x_label, y), text, font, colour, anchor="lm")

    def _marker(self, x: float, y: float, pointing_up: bool, colour) -> None:
        size = max(5 * self.scale, self.slot * 0.5)
        offset = size * 1.15
        if pointing_up:
            apex = (x, y - offset * 0.35)
            points = [apex, (x - size * 0.62, y - offset), (x + size * 0.62, y - offset)]
        else:
            apex = (x, y + offset * 0.35)
            points = [apex, (x - size * 0.62, y + offset), (x + size * 0.62, y + offset)]
        self.draw.polygon(points, fill=colour)

    def draw_moment(self, bars: list[dict], moment: float | None,
                    label: str) -> None:
        """Vertical marker for the instant the shot represents.

        A capture is "the chart at the moment of opening" - without this line
        the viewer has to infer which candle that was, and on M15 or H4 the
        entry can sit mid-bar where the guess is wrong.
        """
        index = self._bar_index_for_time(bars, moment, clamp=False)
        if index is None:
            return
        x = self.x_of(index)
        _dashed_line(
            self.draw, (x, self.plot_top), (x, self.plot_bottom),
            self.theme.now_line, width=max(1, self.scale),
            dash=5 * self.scale, gap=6 * self.scale,
        )
        font = self.fonts.get(10, 600)
        _pill(
            self.draw,
            (x + 6 * self.scale, self.plot_top + 6 * self.scale),
            label, font, self.theme.bg, self.theme.now_line,
            padding=6 * self.scale, radius=5 * self.scale,
        )

    def draw_watermark(self, text: str) -> None:
        font = self.fonts.get(34, 700)
        _text(
            self.draw,
            ((self.plot_left + self.plot_right) / 2,
             (self.plot_top + self.plot_bottom) / 2),
            text, font, self.theme.watermark, anchor="mm",
        )

    def draw_header(self, meta: dict) -> None:
        """Title block: what instrument, what timeframe, what happened."""
        theme = self.theme
        left = self.plot_left
        top = 22 * self.scale

        title_font = self.fonts.get(21, 700)
        sub_font = self.fonts.get(12, 400)
        chip_font = self.fonts.get(11, 600)

        symbol = str(meta.get("symbol", ""))
        _text(self.draw, (left, top), symbol, title_font, theme.title, anchor="ls")

        cursor = left + _text_width(self.draw, symbol, title_font) + 12 * self.scale
        timeframe = str(meta.get("timeframe", "")).upper()
        chip = _pill(
            self.draw, (cursor, top - 15 * self.scale),
            timeframe + "  ·  " + _TF_LABELS.get(timeframe, timeframe),
            chip_font, theme.subtitle, theme.badge_bg,
            padding=7 * self.scale, radius=5 * self.scale,
        )
        cursor = chip[2] + 8 * self.scale

        side = str(meta.get("side", "")).lower()
        if side:
            colour = theme.bull if side == "buy" else theme.bear
            volume = meta.get("volume")
            text = side.upper() + (f"  {volume:g}" if volume else "")
            chip = _pill(
                self.draw, (cursor, top - 15 * self.scale),
                text, chip_font, theme.bg, colour,
                padding=7 * self.scale, radius=5 * self.scale,
            )
            cursor = chip[2] + 8 * self.scale

        phase = meta.get("phase")
        if phase:
            _pill(
                self.draw, (cursor, top - 15 * self.scale),
                str(phase).upper(), chip_font, theme.bg, theme.marker_out,
                padding=7 * self.scale, radius=5 * self.scale,
            )

        subtitle = meta.get("subtitle")
        if subtitle:
            _text(
                self.draw, (left, top + 21 * self.scale),
                str(subtitle), sub_font, theme.subtitle, anchor="ls",
            )

        # Right-hand result block. Colour carries the outcome so the eye gets
        # it before it reads the number.
        net = meta.get("net_profit")
        if net is not None:
            value_font = self.fonts.get(22, 700)
            label_font = self.fonts.get(10, 500)
            colour = theme.bull if net > 0 else theme.bear if net < 0 else theme.subtitle
            right = self.plot_right
            text = (_fmt_money(net) + " " + str(meta.get("currency", ""))).strip()
            _text(self.draw, (right, top), text, value_font, colour, anchor="rs")
            bits = []
            if meta.get("r_multiple") is not None:
                bits.append(f"{meta['r_multiple']:+.2f}R")
            if meta.get("duration_label"):
                bits.append(str(meta["duration_label"]))
            if meta.get("exit_reason"):
                bits.append(str(meta["exit_reason"]))
            if bits:
                _text(
                    self.draw, (right, top + 21 * self.scale),
                    "   ·   ".join(bits), label_font, theme.subtitle,
                    anchor="rs",
                )

    def draw_footer(self, meta: dict) -> None:
        """Excursion facts and provenance along the bottom edge."""
        theme = self.theme
        font = self.fonts.get(10, 500)
        y = self.height - 22 * self.scale

        bits = []
        if meta.get("mfe_money") is not None:
            bits.append("MFE " + _fmt_money(meta["mfe_money"]))
        if meta.get("mae_money") is not None:
            bits.append("MAE " + _fmt_money(meta["mae_money"]))
        if meta.get("heat_pct") is not None:
            bits.append(f"Heat {meta['heat_pct']:.0f}%")
        if meta.get("planned_rr") is not None:
            bits.append(f"Planned {meta['planned_rr']:.2f}R")
        if bits:
            _text(self.draw, (self.plot_left, y), "   ·   ".join(bits),
                  font, theme.subtitle, anchor="lm")

        stamp = meta.get("stamp")
        if stamp:
            _text(self.draw, (self.plot_right, y), str(stamp), font,
                  theme.axis_text, anchor="rm")

    # -- output ------------------------------------------------------------
    def save(self, destination, quality: int = 92) -> Path:
        """Downsample the supersampled canvas and write it out."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = self.image
        if self.scale > 1:
            image = image.resize((self.out_width, self.out_height), Image.LANCZOS)
        if destination.suffix.lower() in (".jpg", ".jpeg"):
            image.save(destination, quality=quality, subsampling=0, optimize=True)
        else:
            image.save(destination, optimize=True)
        return destination


def render_chart(bars: list[dict], meta: dict,
                 trade: TradeOverlay | None = None,
                 destination=None,
                 theme: Theme | str = "midnight",
                 width: int = 1920, height: int = 1080,
                 supersample: int = 2,
                 moment: float | None = None,
                 moment_label: str = "",
                 watermark: str | None = None):
    """Draw one chart. Returns the written path, or the canvas if none given."""
    if not bars:
        raise ValueError("no bars to render")

    canvas = ChartCanvas(width, height, theme, supersample)
    extra: list[float] = []
    if trade:
        # Every level that gets drawn has to be in the y-scale, or it is drawn
        # off the plot. tp_final belongs here as much as sl_final: a target
        # pulled in past the initial range would otherwise land outside the
        # frame with only its label visible.
        extra = [
            p for p in (
                trade.entry_price, trade.exit_price, trade.sl_initial,
                trade.tp_initial, trade.sl_final, trade.tp_final,
                trade.mae_price, trade.mfe_price,
            ) if p
        ]
    canvas._setup_scales(bars, extra, digits=int(meta.get("digits", 2)))
    canvas.draw_frame()
    if watermark:
        canvas.draw_watermark(watermark)
    canvas.draw_grid(bars, str(meta.get("timeframe", "M1")))
    canvas.draw_candles(bars)
    if trade:
        canvas.draw_trade(
            bars, trade, show_excursions=bool(meta.get("show_excursions", True))
        )
    if moment is not None:
        canvas.draw_moment(bars, moment, moment_label)
    canvas.draw_header(meta)
    canvas.draw_footer(meta)

    if destination is None:
        return canvas
    return canvas.save(destination)
