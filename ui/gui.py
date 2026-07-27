"""
PyQt6 GUI for MOADMN Document Collector.

Keeps the same UX expectations as MOADMN.md:
- Collect / View Folder buttons
- Stop button during run
- Progress bar + real-time status messages
"""

from __future__ import annotations

import math
import os
import random
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QDate, QUrl, QTimer, QPoint, QRect, QEvent, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence, QMovie, QPainter, QLinearGradient, QPen, QRadialGradient, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateEdit,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from config.config import ConfigManager
from core.moadmn_scraper import MOADMNScraper


def _resolve_app_icon_path() -> Optional[Path]:
    assets_dir = Path(__file__).resolve().parent.parent / "assets"
    for name in ("icon.png", "icon.ico"):
        candidate = assets_dir / name
        if candidate.exists():
            return candidate
    return None


class IntroTitleLabel(QLabel):
    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self._text_opacity = 1.0
        self._wash_progress = -1.0
        self._shadow_emphasis = 0.0

    def get_text_opacity(self) -> float:
        return self._text_opacity

    def set_text_opacity(self, value: float) -> None:
        self._text_opacity = max(0.0, min(1.0, float(value)))
        self.update()

    def get_wash_progress(self) -> float:
        return self._wash_progress

    def set_wash_progress(self, value: float) -> None:
        self._wash_progress = float(value)
        self.update()

    def get_shadow_emphasis(self) -> float:
        return self._shadow_emphasis

    def set_shadow_emphasis(self, value: float) -> None:
        self._shadow_emphasis = max(0.0, min(1.0, float(value)))
        self.update()

    def set_shadow_profile(self, profile: str) -> None:
        self.set_shadow_emphasis(1.0 if profile == "dramatic" else 0.0)

    textOpacity = pyqtProperty(float, fget=get_text_opacity, fset=set_text_opacity)
    washProgress = pyqtProperty(float, fget=get_wash_progress, fset=set_wash_progress)
    shadowEmphasis = pyqtProperty(float, fget=get_shadow_emphasis, fset=set_shadow_emphasis)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        text = self.text()
        font = self.font()
        painter.setFont(font)
        metrics = painter.fontMetrics()
        bbox = metrics.tightBoundingRect(text)

        x = (self.width() - bbox.width()) / 2 - bbox.left()
        y = (self.height() + metrics.ascent() - metrics.descent()) / 2 - 2

        soft_layers = (
            ("#241926", 0.14, 0, 3),
            ("#FFD0EE", 0.42, 1, 1),
        )
        dramatic_layers = (
            ("#FF88D8", 0.44, 0, 5),
            ("#FFD8F1", 0.88, 1, 2),
        )
        shadow_layers = []
        for soft_layer, dramatic_layer in zip(soft_layers, dramatic_layers):
            soft_color = QColor(soft_layer[0])
            dramatic_color = QColor(dramatic_layer[0])
            t = self._shadow_emphasis
            blended_color = QColor(
                int(round(soft_color.red() + (dramatic_color.red() - soft_color.red()) * t)),
                int(round(soft_color.green() + (dramatic_color.green() - soft_color.green()) * t)),
                int(round(soft_color.blue() + (dramatic_color.blue() - soft_color.blue()) * t)),
            )
            blended_alpha = soft_layer[1] + (dramatic_layer[1] - soft_layer[1]) * t
            blended_dx = soft_layer[2] + (dramatic_layer[2] - soft_layer[2]) * t
            blended_dy = soft_layer[3] + (dramatic_layer[3] - soft_layer[3]) * t
            shadow_layers.append((blended_color, blended_alpha, blended_dx, blended_dy))

        for shadow_color, alpha, dx, dy in shadow_layers:
            shadow_color.setAlphaF(alpha * self._text_opacity)
            painter.setPen(shadow_color)
            painter.drawText(int(round(x + dx)), int(round(y + dy)), text)

        base_color = QColor("#FFF4FD")
        base_color.setAlphaF(self._text_opacity)
        painter.setPen(base_color)
        painter.drawText(int(x), int(y), text)

        if 0.0 <= self._wash_progress <= 1.0:
            band_center = self.width() * self._wash_progress
            band_width = max(128.0, self.width() * 0.24)
            gradient = QLinearGradient(band_center - band_width, 0, band_center + band_width, 0)
            transparent = QColor(0, 0, 0, 0)
            glow_cyan = QColor("#74F0FF")
            glow_cyan.setAlphaF(0.55 * self._text_opacity)
            glow_hot = QColor("#FF8FD5")
            glow_hot.setAlphaF(0.88 * self._text_opacity)
            glow_white = QColor("#FFFFFF")
            glow_white.setAlphaF(1.0 * self._text_opacity)
            glow_blue = QColor("#8BA8FF")
            glow_blue.setAlphaF(0.72 * self._text_opacity)
            gradient.setColorAt(0.00, transparent)
            gradient.setColorAt(0.16, transparent)
            gradient.setColorAt(0.32, glow_cyan)
            gradient.setColorAt(0.46, glow_hot)
            gradient.setColorAt(0.54, glow_white)
            gradient.setColorAt(0.68, glow_blue)
            gradient.setColorAt(0.84, transparent)
            gradient.setColorAt(1.00, transparent)
            wash_shadow = QColor("#9AF7FF")
            wash_shadow.setAlphaF(0.42 * self._text_opacity)
            painter.setPen(wash_shadow)
            painter.drawText(int(x), int(y) + 1, text)
            painter.setPen(QPen(gradient, 0))
            painter.drawText(int(x), int(y), text)

        painter.end()


class FairyDustOverlay(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._particles: list[dict[str, float | QColor]] = []
        self._burst_ticks_remaining = 0
        self._cooldown_ticks_remaining = random.randint(44, 92)
        self._quiet_ticks_remaining = random.randint(10, 22)
        self._sparkle_timer = QTimer(self)
        self._sparkle_timer.setInterval(42)
        self._sparkle_timer.timeout.connect(self._tick_particles)
        self._sparkle_timer.start()

    def _spawn_particle(self) -> None:
        if self.width() <= 24 or self.height() <= 24:
            return

        palette = ("#FFD3F4", "#C9F7FF", "#E6CBFF", "#FFE1F2")
        particle_color = QColor(random.choice(palette))
        depth = random.uniform(0.0, 1.0)
        size = random.uniform(1.3, 3.3) * (0.82 + depth * 0.88)
        alpha = random.uniform(0.16, 0.34) * (0.74 + depth * 0.62)
        sway = random.uniform(0.045, 0.14) * (0.70 + depth * 0.95)
        glow_scale = 1.95 + depth * 1.35
        core_scale = 0.28 + depth * 0.34
        spawn_y = random.uniform(self.height() * 0.92, self.height() * 1.04)
        self._particles.append(
            {
                "x": random.uniform(self.width() * 0.06, self.width() * 0.94),
                "y": spawn_y,
                "start_y": spawn_y,
                "vx": random.uniform(-0.16, 0.16) * (0.72 + depth * 0.55),
                "vy": random.uniform(-1.28, -0.66) * (0.82 + depth * 0.62),
                "size": size,
                "life": random.uniform(128.0, 210.0),
                "max_life": 0.0,
                "phase": random.uniform(0.0, math.tau),
                "alpha": alpha,
                "depth": depth,
                "sway": sway,
                "glow_scale": glow_scale,
                "core_scale": core_scale,
                "color": particle_color,
            }
        )
        self._particles[-1]["max_life"] = self._particles[-1]["life"]

    def _schedule_next_burst(self) -> None:
        self._cooldown_ticks_remaining = random.randint(54, 128)
        self._quiet_ticks_remaining = random.randint(12, 28)
        self._burst_ticks_remaining = 0

    def _start_burst(self) -> None:
        self._burst_ticks_remaining = random.randint(8, 16)
        self._cooldown_ticks_remaining = 0
        self._quiet_ticks_remaining = 0

    def _tick_particles(self) -> None:
        if not self.isVisible():
            return

        active_particles: list[dict[str, float | QColor]] = []
        for particle in self._particles:
            age = float(particle["max_life"]) - float(particle["life"])
            particle["x"] = float(particle["x"]) + float(particle["vx"]) + math.sin(age * 0.08 + float(particle["phase"])) * float(particle["sway"])
            particle["y"] = float(particle["y"]) + float(particle["vy"])
            particle["life"] = float(particle["life"]) - 1.0
            if (
                float(particle["life"]) > 0.0
                and -18.0 <= float(particle["x"]) <= self.width() + 18.0
                and float(particle["y"]) >= -28.0
            ):
                active_particles.append(particle)

        self._particles = active_particles

        if self._burst_ticks_remaining > 0:
            self._burst_ticks_remaining -= 1
            if len(self._particles) < 12 and random.random() < 0.48:
                self._spawn_particle()
            if len(self._particles) < 10 and random.random() < 0.20:
                self._spawn_particle()
            if self._burst_ticks_remaining <= 0:
                self._schedule_next_burst()
        else:
            if self._cooldown_ticks_remaining > 0:
                self._cooldown_ticks_remaining -= 1
            elif len(self._particles) > 1:
                # Let the previous wave fully clear so the dust feels airy, not constant.
                self._quiet_ticks_remaining = max(self._quiet_ticks_remaining, random.randint(10, 20))
            elif self._quiet_ticks_remaining > 0:
                self._quiet_ticks_remaining -= 1
            elif random.random() < 0.12:
                self._start_burst()

        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        for particle in sorted(self._particles, key=lambda p: float(p["depth"])):
            life_ratio = float(particle["life"]) / max(1.0, float(particle["max_life"]))
            depth = float(particle["depth"])
            x = float(particle["x"])
            y = float(particle["y"])
            start_y = float(particle.get("start_y", y))
            travel_span = max(24.0, start_y + 28.0)
            travel_ratio = min(1.0, max(0.0, (start_y - y) / travel_span))
            fade_out = 1.0 if travel_ratio < 0.72 else max(0.0, 1.0 - ((travel_ratio - 0.72) / 0.28))
            tail_fade = 1.0 if life_ratio > 0.14 else max(0.0, life_ratio / 0.14)
            fade = fade_out * tail_fade
            alpha = float(particle["alpha"]) * fade
            radius = float(particle["size"]) * (0.84 + (1.0 - fade) * (0.24 + depth * 0.18))
            color = QColor(particle["color"])
            glow_scale = float(particle["glow_scale"])
            core_scale = float(particle["core_scale"])

            glow = QRadialGradient(x, y, radius * glow_scale)
            outer = QColor(color)
            outer.setAlphaF(alpha * 0.0)
            mid = QColor(color)
            mid.setAlphaF(alpha * (0.16 + depth * 0.22))
            inner = QColor(color)
            inner.setAlphaF(alpha * (0.68 + depth * 0.28))
            glow.setColorAt(0.0, inner)
            glow.setColorAt(0.34 + depth * 0.10, mid)
            glow.setColorAt(1.0, outer)
            painter.setBrush(glow)
            painter.drawEllipse(
                int(round(x - radius * glow_scale)),
                int(round(y - radius * glow_scale)),
                int(round(radius * glow_scale * 2)),
                int(round(radius * glow_scale * 2)),
            )

            core = QColor("#FFF9FF")
            core.setAlphaF(alpha * (0.62 + depth * 0.30))
            painter.setBrush(core)
            painter.drawEllipse(
                int(round(x - radius * core_scale)),
                int(round(y - radius * core_scale)),
                int(round(radius * core_scale * 2)),
                int(round(radius * core_scale * 2)),
            )

        painter.end()


class MOADMNGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.scraper: Optional[MOADMNScraper] = None
        self.last_run_dir: Optional[str] = None
        self._drag_offset: Optional[QPoint] = None
        self._shimmer_phase = 0.0
        self._shimmer_timer = QTimer(self)
        self._shimmer_timer.setInterval(90)
        self._shimmer_timer.timeout.connect(self._tick_shimmer)
        self._base_window_flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
        self._topmost_window_flags = self._base_window_flags | Qt.WindowType.WindowStaysOnTopHint
        self._launch_fade_started = False
        self._launch_fade = QPropertyAnimation(self)
        self._launch_fade.setDuration(620)
        self._launch_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._intro_title_fade = QPropertyAnimation(self)
        self._intro_title_fade.setDuration(1600)
        self._intro_title_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._intro_title_wash = QPropertyAnimation(self)
        self._intro_title_wash.setDuration(2000)
        self._intro_title_wash.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._intro_animation_started = False
        self._intro_active = False
        self._intro_title_anim = QPropertyAnimation(self)
        self._intro_title_anim.setDuration(820)
        self._intro_title_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._intro_content_fade = QPropertyAnimation(self)
        self._intro_content_fade.setDuration(520)
        self._intro_content_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._title_shadow_settle = QPropertyAnimation(self)
        self._title_shadow_settle.setDuration(900)
        self._title_shadow_settle.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._chrome_intro_opacity: Optional[QGraphicsOpacityEffect] = None
        self._status_queue_counter = ""
        self.loading_gif_path = Path(__file__).resolve().parent.parent / "assets" / "loading.gif"
        self._loading_watermark_target_opacity = 0.18
        self._loading_movie_speed = 40
        self._intro_topmost = True
        self.loading_watermark: Optional[QLabel] = None
        self.loading_watermark_opacity: Optional[QGraphicsOpacityEffect] = None
        self.loading_movie: Optional[QMovie] = None
        self._loading_watermark_fade = QPropertyAnimation(self)
        self._loading_watermark_fade.setDuration(1500)
        self._loading_watermark_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._window_width = 500
        self._build()
        self.setWindowOpacity(1.0)

    @staticmethod
    def _make_shadow(blur: float, y_offset: float, color: QColor, x_offset: float = 0.0) -> QGraphicsDropShadowEffect:
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(blur)
        shadow.setOffset(x_offset, y_offset)
        shadow.setColor(color)
        return shadow

    def _create_chrome_shadow(self) -> QGraphicsDropShadowEffect:
        return self._make_shadow(36, 10, QColor(4, 8, 20, 165))

    def _build(self) -> None:
        self.setWindowTitle("MOADMN Document Collector")
        icon_path = _resolve_app_icon_path()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))
        header_font_family = "Bahnschrift SemiBold"
        header_font_path = Path(__file__).resolve().parent.parent / "assets" / "Glowfor-Regular.ttf"
        if header_font_path.exists():
            font_id = QFontDatabase.addApplicationFont(str(header_font_path))
            if font_id != -1:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    header_font_family = families[0]
        self.setWindowFlags(self._topmost_window_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(self._window_width)
        self.setMaximumWidth(self._window_width)

        root = QWidget()
        root.setObjectName("rootShell")
        self.root = root
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(0)
        self.root_layout = root_layout

        chrome = QFrame()
        chrome.setObjectName("chromeCard")
        chrome.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        chrome.setGraphicsEffect(self._create_chrome_shadow())
        self.chrome_card = chrome
        root_layout.addWidget(chrome, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.fairy_dust_overlay = FairyDustOverlay(chrome)
        self.fairy_dust_overlay.hide()

        layout = QVBoxLayout(chrome)
        layout.setContentsMargins(10, 18, 10, 10)
        layout.setSpacing(0)
        self.chrome_layout = layout

        title_font = QFont(header_font_family, 28, QFont.Weight.Normal)

        self.title_bar = QFrame()
        self.title_bar.setObjectName("titleBar")
        self.title_bar.setFixedHeight(52)
        title_bar_layout = QHBoxLayout(self.title_bar)
        title_bar_layout.setContentsMargins(0, 0, 0, 0)
        title_bar_layout.setSpacing(5)
        layout.addWidget(self.title_bar)

        left_balance = QWidget()
        left_balance.setFixedSize(52, 20)
        left_balance.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        title_bar_layout.addWidget(left_balance)

        title = IntroTitleLabel("MOADMN Collector")
        title.setObjectName("titleLabel")
        title.setFont(title_font)
        title.setContentsMargins(0, 0, 0, 0)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        title.set_shadow_profile("soft")
        self.title_label = title
        title_bar_layout.addWidget(title, 1, Qt.AlignmentFlag.AlignBottom)

        self.title_controls = QWidget()
        self.title_controls.setObjectName("titleControls")
        self.title_controls.setFixedSize(57, 24)
        title_controls_layout = QHBoxLayout(self.title_controls)
        title_controls_layout.setContentsMargins(0, 1, 0, 1)
        title_controls_layout.setSpacing(5)

        self.minimize_btn = QPushButton("-")
        self.minimize_btn.setObjectName("titleBarBtn")
        self.minimize_btn.setFixedSize(26, 22)
        self.minimize_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.minimize_btn.clicked.connect(self.showMinimized)
        title_controls_layout.addWidget(self.minimize_btn)

        self.close_btn = QPushButton("X")
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setFixedSize(26, 22)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        title_controls_layout.addWidget(self.close_btn)

        title_bar_layout.addWidget(self.title_controls, 0, Qt.AlignmentFlag.AlignTop)

        self.title_bar.installEventFilter(self)
        layout.addSpacing(6)

        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("contentStack")
        self.content_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        stack_policy = self.content_stack.sizePolicy()
        stack_policy.setRetainSizeWhenHidden(True)
        self.content_stack.setSizePolicy(stack_policy)
        self.content_opacity: Optional[QGraphicsOpacityEffect] = None
        self._intro_content_opacity: Optional[QGraphicsOpacityEffect] = None
        self._content_fade = QPropertyAnimation(self)
        self._content_fade.setDuration(180)
        self._content_fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._pending_content_index: Optional[int] = None
        layout.addWidget(self.content_stack, 0, Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)

        self.intro_title = IntroTitleLabel("MOADMN Collector", root)
        self.intro_title.setObjectName("titleLabel")
        self.intro_title.setFont(title_font)
        self.intro_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.intro_title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.intro_title.set_shadow_profile("dramatic")
        self.intro_title.hide()

        self.operation_card = QFrame()
        self.operation_card.setObjectName("controlsCard")
        self.operation_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        controls_layout = QVBoxLayout(self.operation_card)
        controls_layout.setContentsMargins(8, 6, 8, 6)
        controls_layout.setSpacing(3)

        today = QDate.currentDate()
        start_of_year = QDate(today.year(), 1, 1)

        dates_row = QHBoxLayout()
        dates_row.setContentsMargins(0, 0, 0, 0)
        dates_row.setSpacing(5)
        controls_layout.addLayout(dates_row)

        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(start_of_year)
        self.start_date.setDisplayFormat("MM/dd/yyyy")
        self.start_date.setObjectName("dateField")
        self.start_date.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        dates_row.addWidget(self._labeled("Start date", self.start_date))

        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(today)
        self.end_date.setDisplayFormat("MM/dd/yyyy")
        self.end_date.setObjectName("dateField")
        self.end_date.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        dates_row.addWidget(self._labeled("End date", self.end_date))

        self.headless_row = QWidget()
        headless_row_layout = QHBoxLayout(self.headless_row)
        headless_row_layout.setContentsMargins(0, 0, 0, 0)
        headless_row_layout.setSpacing(4)
        controls_layout.addWidget(self.headless_row)

        self.headless_checkbox = QCheckBox("Run headless (no browser window)")
        self.headless_checkbox.setObjectName("headlessToggle")
        self.headless_checkbox.setChecked(bool(self.config.get("headless", True)))
        self.headless_checkbox.stateChanged.connect(self._on_headless_changed)
        headless_row_layout.addWidget(self.headless_checkbox, 1)
        self.headless_row.hide()
        self._headless_toggle_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self._headless_toggle_shortcut.activated.connect(self._toggle_headless_visibility)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(5)
        controls_layout.addLayout(btn_row)

        self.collect_btn = QPushButton("Collect")
        self.collect_btn.setObjectName("collectBtn")
        self.collect_btn.setProperty("mode", "collect")
        self.collect_btn.setMinimumHeight(24)
        self.collect_btn.clicked.connect(self._on_collect_btn_clicked)
        btn_row.addWidget(self.collect_btn, 3)

        self.view_btn = QPushButton("View Folder")
        self.view_btn.setObjectName("viewBtn")
        self.view_btn.setMinimumHeight(24)
        self.view_btn.clicked.connect(self.open_folder)
        btn_row.addWidget(self.view_btn, 2)

        self.status_card = QFrame()
        self.status_card.setObjectName("feedbackCard")
        self.status_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        feedback_layout = QVBoxLayout(self.status_card)
        feedback_layout.setContentsMargins(9, 7, 9, 7)
        feedback_layout.setSpacing(5)

        self.progress = QProgressBar()
        self.progress.setObjectName("runProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        feedback_layout.addWidget(self.progress)

        self.status = QLabel("Ready to collect current-year documents.")
        self.status.setObjectName("statusText")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        feedback_layout.addWidget(self.status)

        run_btn_row = QHBoxLayout()
        run_btn_row.setContentsMargins(0, 6, 0, 0)
        run_btn_row.setSpacing(7)
        feedback_layout.addLayout(run_btn_row)

        self.run_stop_btn = QPushButton("Stop")
        self.run_stop_btn.setObjectName("collectBtn")
        self.run_stop_btn.setProperty("mode", "stop")
        self.run_stop_btn.setMinimumHeight(24)
        self.run_stop_btn.clicked.connect(self.stop_collection)
        run_btn_row.addWidget(self.run_stop_btn, 3)

        self.run_view_btn = QPushButton("View Folder")
        self.run_view_btn.setObjectName("viewBtn")
        self.run_view_btn.setMinimumHeight(24)
        self.run_view_btn.clicked.connect(self.open_folder)
        run_btn_row.addWidget(self.run_view_btn, 2)

        self.loading_watermark = QLabel(self.chrome_card)
        self.loading_watermark.setObjectName("statusWatermark")
        self.loading_watermark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_watermark.setScaledContents(True)
        self.loading_watermark.setStyleSheet("background: transparent;")
        self.loading_watermark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.loading_watermark.hide()
        self.loading_watermark.lower()
        self.loading_watermark_opacity = QGraphicsOpacityEffect(self.loading_watermark)
        self.loading_watermark_opacity.setOpacity(0.0)
        self.loading_watermark.setGraphicsEffect(self.loading_watermark_opacity)
        if self.loading_gif_path.exists():
            self.loading_movie = QMovie(str(self.loading_gif_path))
            self.loading_movie.setCacheMode(QMovie.CacheMode.CacheAll)
            self.loading_movie.setSpeed(self._loading_movie_speed)
            self.loading_watermark.setMovie(self.loading_movie)
        
        self.operation_page = QWidget()
        op_page_layout = QVBoxLayout(self.operation_page)
        op_page_layout.setContentsMargins(0, 0, 0, 0)
        op_page_layout.setSpacing(0)
        op_page_layout.addWidget(self.operation_card)
        op_page_layout.addStretch(1)

        self.status_page = QWidget()
        status_page_layout = QVBoxLayout(self.status_page)
        status_page_layout.setContentsMargins(0, 0, 0, 0)
        status_page_layout.setSpacing(0)
        status_page_layout.addWidget(self.status_card)
        status_page_layout.addStretch(1)

        self.content_stack.addWidget(self.operation_page)
        self.content_stack.addWidget(self.status_page)

        headless_check_icon = (Path(__file__).resolve().parent / "headless_check.svg").resolve().as_posix()
        root.setStyleSheet(
            """
            QWidget#rootShell {
                background: transparent;
                color: #F4F0FF;
                font-family: "Segoe UI";
                font-size: 11px;
            }

            QFrame#chromeCard {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(22, 19, 40, 244),
                    stop:0.48 rgba(18, 27, 45, 242),
                    stop:1 rgba(14, 18, 33, 246)
                );
                border: 1px solid rgba(146, 214, 255, 56);
                border-radius: 24px;
            }

            QFrame#titleBar {
                background: transparent;
                border: none;
            }

            QPushButton#titleBarBtn,
            QPushButton#closeBtn {
                min-height: 22px;
                max-height: 22px;
                padding: 0;
                border-radius: 8px;
                font-size: 10px;
                font-weight: 800;
                color: #F6F8FF;
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(31, 37, 60, 232),
                    stop:1 rgba(18, 23, 40, 228)
                );
                border: 1px solid rgba(143, 171, 226, 108);
            }

            QPushButton#titleBarBtn:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(67, 85, 124, 236),
                    stop:1 rgba(42, 53, 82, 232)
                );
                border: 1px solid rgba(130, 244, 255, 128);
            }

            QPushButton#closeBtn:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(247, 95, 168, 232),
                    stop:1 rgba(191, 56, 120, 228)
                );
                border: 1px solid rgba(255, 183, 225, 136);
            }

            QPushButton#titleBarBtn:pressed,
            QPushButton#closeBtn:pressed {
                background: rgba(19, 24, 38, 236);
                border: 1px solid rgba(180, 203, 255, 120);
            }

            QFrame#heroCard {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255, 170, 220, 42),
                    stop:0.48 rgba(151, 112, 255, 34),
                    stop:1 rgba(77, 232, 255, 34)
                );
                border: 1px solid rgba(255, 190, 228, 56);
                border-radius: 14px;
            }

            QLabel#titleLabel {
                color: #FFF4FD;
                letter-spacing: 0.3px;
                padding: 0px;
            }

            QFrame#controlsCard {
                background-color: rgba(15, 21, 36, 212);
                border: 1px solid rgba(121, 148, 255, 52);
                border-radius: 14px;
            }

            QFrame#feedbackCard {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255, 190, 230, 12),
                    stop:0.52 rgba(89, 148, 255, 12),
                    stop:1 rgba(91, 237, 255, 12)
                );
                border: 1px solid rgba(136, 174, 255, 48);
                border-radius: 13px;
            }

            QFrame#fieldCard {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255, 194, 236, 18),
                    stop:1 rgba(95, 238, 255, 14)
                );
                border: 1px solid rgba(149, 173, 255, 62);
                border-radius: 9px;
            }

            QLabel#fieldCaption {
                color: #F8A7E0;
                font-size: 7px;
                font-weight: 800;
                letter-spacing: 0.7px;
            }

            QLabel#statusText {
                color: #F7F4FF;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255, 183, 228, 20),
                    stop:0.5 rgba(98, 140, 255, 24),
                    stop:1 rgba(86, 233, 255, 18)
                );
                border: 1px solid rgba(148, 198, 255, 52);
                border-radius: 10px;
                padding: 5px 8px;
                min-height: 20px;
                max-height: 24px;
                font-size: 9px;
                line-height: 1.35em;
            }
            QLabel#statusText[state="running"] {
                background-color: rgba(98, 140, 255, 26);
                border: 1px solid rgba(105, 237, 255, 76);
            }
            QLabel#statusText[state="stopping"] {
                background-color: rgba(208, 71, 255, 24);
                border: 1px solid rgba(255, 131, 227, 76);
            }
            QLabel#statusText[state="success"] {
                background-color: rgba(79, 216, 173, 22);
                border: 1px solid rgba(130, 255, 212, 76);
            }
            QLabel#statusText[state="error"] {
                background-color: rgba(255, 123, 171, 22);
                border: 1px solid rgba(255, 155, 196, 76);
            }

            QPushButton {
                min-height: 24px;
                padding: 3px 10px;
                border-radius: 9px;
                border: 1px solid rgba(122, 140, 190, 76);
                background-color: rgba(26, 31, 49, 222);
                color: #F8F6FF;
                font-weight: 700;
                font-size: 10px;
            }
            QPushButton:hover { background-color: rgba(39, 46, 70, 230); }
            QPushButton:pressed { background-color: rgba(21, 25, 39, 235); }
            QPushButton:disabled {
                color: #8C92A8;
                background-color: rgba(25, 27, 38, 180);
                border: 1px solid rgba(96, 102, 122, 42);
            }

            QPushButton#collectBtn[mode="collect"] {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #74F0FF,
                    stop:0.48 #7B98FF,
                    stop:1 #FF86CB
                );
                border: 1px solid rgba(190, 246, 255, 138);
                color: #120A1F;
            }
            QPushButton#collectBtn[mode="collect"]:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #8CFFD9,
                    stop:0.45 #8EA6FF,
                    stop:1 #FFC98B
                );
                border: 1px solid rgba(214, 247, 223, 186);
            }
            QPushButton#collectBtn[mode="collect"]:pressed {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #87C2FF,
                    stop:0.52 #AA86FF,
                    stop:1 #FF7E8F
                );
                border: 1px solid rgba(182, 169, 255, 184);
                color: #FFF9FF;
            }

            QPushButton#collectBtn[mode="stop"] {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #FF73D4,
                    stop:1 #B51FC7
                );
                border: 1px solid rgba(255, 173, 235, 140);
                color: #FFF7FF;
            }
            QPushButton#collectBtn[mode="stop"]:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #FFB07C,
                    stop:0.5 #FF76D9,
                    stop:1 #B384FF
                );
                border: 1px solid rgba(255, 210, 188, 188);
            }
            QPushButton#collectBtn[mode="stop"]:pressed {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #FF6A9A,
                    stop:0.5 #C34BFF,
                    stop:1 #6B7BFF
                );
                border: 1px solid rgba(203, 122, 255, 186);
            }

            QPushButton#viewBtn {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(28, 36, 58, 238),
                    stop:1 rgba(19, 28, 47, 238)
                );
                border: 1px solid rgba(110, 244, 255, 72);
                color: #EAFBFF;
            }
            QPushButton#viewBtn:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(39, 53, 82, 242),
                    stop:1 rgba(28, 40, 68, 242)
                );
            }

            QDateEdit#dateField {
                background: transparent;
                border: none;
                padding: 0 2px 1px 2px;
                min-height: 14px;
                color: #FFF8FF;
                font-size: 10px;
                font-weight: 700;
            }
            QDateEdit#dateField:focus {
                color: #97F4FF;
            }

            QCheckBox#headlessToggle {
                spacing: 5px;
                color: #E8DEFF;
                font-size: 9px;
                font-weight: 600;
            }
            QCheckBox#headlessToggle::indicator {
                width: 12px;
                height: 12px;
                border-radius: 4px;
                border: 1px solid rgba(111, 230, 255, 92);
                background: rgba(20, 22, 35, 220);
            }
            QCheckBox#headlessToggle::indicator:checked {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #FF93DA,
                    stop:1 #6DF5FF
                );
                border: 1px solid rgba(255, 194, 236, 120);
                image: url("__HEADLESS_CHECK_ICON__");
            }

            QProgressBar {
                height: 10px;
                border-radius: 5px;
                text-align: center;
                color: #F5F1FF;
                border: 1px solid rgba(129, 158, 255, 58);
                background-color: rgba(16, 21, 34, 232);
                font-size: 8px;
                font-weight: 800;
            }
            QProgressBar::chunk {
                border-radius: 4px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7CF2FF,
                    stop:0.5 #8D9DFF,
                    stop:1 #FF96DB
                );
            }
            QProgressBar#runProgress[state="running"]::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7CF2FF,
                    stop:0.45 #92A7FF,
                    stop:0.75 #B59DFF,
                    stop:1 #FF8FD5
                );
            }
            QProgressBar#runProgress[state="stopping"]::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #FF8BD8,
                    stop:1 #C026D3
                );
            }
            QProgressBar#runProgress[state="success"]::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #6DF5FF,
                    stop:1 #6DFFB4
                );
            }
            QProgressBar#runProgress[state="error"]::chunk {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #FF8FAF,
                    stop:1 #FF68D0
                );
            }
            """.replace("__HEADLESS_CHECK_ICON__", headless_check_icon)
        )
        self._set_collect_button_mode("collect")
        self._apply_compact_layout_metrics()
        self._reset_ready_state(animate=False)
        self.content_stack.hide()
        self._set_title_bar_intro_visibility(visible=False)
        self.chrome_card.hide()

    def _set_title_bar_intro_visibility(self, visible: bool) -> None:
        self.title_label.show()
        self.title_controls.show()
        self.title_label.set_text_opacity(1.0 if visible else 0.0)
        self.minimize_btn.setVisible(visible)
        self.close_btn.setVisible(visible)
        self.title_controls.setEnabled(visible)

    def _set_intro_topmost(self, enabled: bool) -> None:
        if self._intro_topmost == enabled:
            if enabled:
                self.raise_()
                self.activateWindow()
            return

        self._intro_topmost = enabled
        if sys.platform == "win32" and self.isVisible():
            try:
                import ctypes

                self.overrideWindowFlags(self._topmost_window_flags if enabled else self._base_window_flags)
                hwnd = int(self.winId())
                hwnd_insert_after = -1 if enabled else -2  # HWND_TOPMOST / HWND_NOTOPMOST
                if enabled:
                    flags = (
                        0x0001  # SWP_NOSIZE
                        | 0x0002  # SWP_NOMOVE
                        | 0x0040  # SWP_SHOWWINDOW
                    )
                else:
                    flags = (
                        0x0001  # SWP_NOSIZE
                        | 0x0002  # SWP_NOMOVE
                        | 0x0010  # SWP_NOACTIVATE
                    )
                ctypes.windll.user32.SetWindowPos(hwnd, hwnd_insert_after, 0, 0, 0, 0, flags)
                if enabled:
                    ctypes.windll.user32.BringWindowToTop(hwnd)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        else:
            current = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            if current != enabled:
                was_visible = self.isVisible()
                current_pos = self.pos()
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
                if was_visible:
                    self.show()
                    self.move(current_pos)
        if enabled:
            self.raise_()
            self.activateWindow()

    def _update_loading_watermark_geometry(self) -> None:
        if self.loading_watermark is None:
            return

        chrome_rect = self.chrome_card.rect()
        if chrome_rect.width() <= 0 or chrome_rect.height() <= 0:
            self.loading_watermark.hide()
            return

        inset = 2
        watermark_rect = chrome_rect.adjusted(inset, inset, -inset, -inset)
        if watermark_rect.width() <= 0 or watermark_rect.height() <= 0:
            self.loading_watermark.hide()
            return

        self.loading_watermark.setGeometry(watermark_rect)
        self.loading_watermark.lower()

    def _update_fairy_dust_geometry(self) -> None:
        if not hasattr(self, "fairy_dust_overlay") or self.fairy_dust_overlay is None:
            return
        self.fairy_dust_overlay.setGeometry(self.chrome_card.rect())

    def _refresh_fairy_dust_visibility(self) -> None:
        if not hasattr(self, "fairy_dust_overlay") or self.fairy_dust_overlay is None:
            return

        active = (
            not self._intro_active
            and self.chrome_card.isVisible()
            and self.content_stack.isVisible()
            and self.content_stack.currentIndex() == 0
        )
        if active:
            self.fairy_dust_overlay.show()
            self._apply_visual_z_order()
        else:
            self.fairy_dust_overlay.hide()

    def _apply_visual_z_order(self) -> None:
        if self.loading_watermark is not None:
            self.loading_watermark.lower()
        self.title_bar.raise_()
        self.content_stack.raise_()
        if hasattr(self, "fairy_dust_overlay") and self.fairy_dust_overlay is not None and self.fairy_dust_overlay.isVisible():
            self.fairy_dust_overlay.raise_()
        if self._intro_active:
            self.intro_title.raise_()

    def _stop_loading_watermark_fade(self) -> None:
        try:
            self._loading_watermark_fade.stop()
        except RuntimeError:
            pass

    def _fade_in_loading_watermark(self) -> None:
        if self.loading_watermark_opacity is None:
            return
        self._stop_loading_watermark_fade()
        self._loading_watermark_fade.setTargetObject(self.loading_watermark_opacity)
        self._loading_watermark_fade.setPropertyName(b"opacity")
        self._loading_watermark_fade.setStartValue(self.loading_watermark_opacity.opacity())
        self._loading_watermark_fade.setEndValue(self._loading_watermark_target_opacity)
        self._loading_watermark_fade.start()

    def _refresh_loading_watermark(self) -> None:
        if self.loading_watermark is None:
            return

        self._update_loading_watermark_geometry()
        active = (
            not self._intro_active
            and self.content_stack.isVisible()
            and self.content_stack.currentIndex() == 1
            and self.status_card.isVisible()
        )
        if not active:
            self._stop_loading_watermark_fade()
            if self.loading_watermark_opacity is not None:
                self.loading_watermark_opacity.setOpacity(0.0)
            self.loading_watermark.hide()
            if self.loading_movie is not None and self.loading_movie.state() == QMovie.MovieState.Running:
                self.loading_movie.setPaused(True)
            return

        should_fade_in = not self.loading_watermark.isVisible()
        self.loading_watermark.show()
        self.loading_watermark.lower()
        self._apply_visual_z_order()
        if self.loading_movie is not None:
            self.loading_movie.setSpeed(self._loading_movie_speed)
            if self.loading_movie.state() == QMovie.MovieState.NotRunning:
                self.loading_movie.start()
            elif self.loading_movie.state() == QMovie.MovieState.Paused:
                self.loading_movie.setPaused(False)
        if self.loading_watermark_opacity is not None:
            if should_fade_in:
                self.loading_watermark_opacity.setOpacity(0.0)
                self._fade_in_loading_watermark()
            elif self._loading_watermark_fade.state() != QPropertyAnimation.State.Running:
                self.loading_watermark_opacity.setOpacity(self._loading_watermark_target_opacity)

    def _set_collect_button_mode(self, mode: str) -> None:
        self.collect_btn.setProperty("mode", mode)
        self.collect_btn.style().unpolish(self.collect_btn)
        self.collect_btn.style().polish(self.collect_btn)
        self.collect_btn.update()

    def _apply_compact_layout_metrics(self) -> None:
        self.root.ensurePolished()
        self.title_bar.layout().activate()
        self.operation_card.layout().activate()
        self.status_card.layout().activate()

        title_height = max(self.title_label.sizeHint().height() + 2, self.minimize_btn.height() + 8)
        self.title_label.setFixedHeight(title_height)
        self.title_bar.setFixedHeight(title_height + 2)

        operation_height = max(self.operation_card.sizeHint().height() + 16, 118)
        status_height = max(self.status_card.sizeHint().height() + 4, operation_height)
        self.operation_card.setFixedHeight(operation_height)
        self.status_card.setFixedHeight(status_height)

        stack_height = max(operation_height, status_height)
        self.content_stack.setFixedHeight(stack_height)

        root_margins = self.root_layout.contentsMargins()
        chrome_margins = self.chrome_layout.contentsMargins()
        chrome_width = self._window_width - root_margins.left() - root_margins.right()
        chrome_height = (
            chrome_margins.top()
            + self.title_bar.height()
            + 6
            + self.content_stack.height()
            + chrome_margins.bottom()
        )
        self.chrome_card.setFixedWidth(chrome_width)
        self.chrome_card.setFixedHeight(chrome_height)
        total_height = root_margins.top() + chrome_height + root_margins.bottom()
        self.setFixedSize(self._window_width, max(total_height, 264))
        self._update_fairy_dust_geometry()
        self._refresh_fairy_dust_visibility()
        self._update_loading_watermark_geometry()

    def _stabilize_window_geometry(self) -> None:
        self.root_layout.activate()
        self.chrome_layout.activate()
        self.content_stack.updateGeometry()
        self.chrome_card.updateGeometry()
        self._apply_compact_layout_metrics()
        self.root.updateGeometry()
        self.adjustSize()
        self._update_fairy_dust_geometry()
        self._refresh_fairy_dust_visibility()
        self._refresh_loading_watermark()

    def _reset_ready_state(self, animate: bool = True) -> None:
        self.collect_btn.setText("Collect")
        self._set_collect_button_mode("collect")
        self.collect_btn.setEnabled(True)
        self.run_stop_btn.setEnabled(True)
        self.progress.setValue(0)
        self._status_queue_counter = ""
        self.status.setText("Ready to collect current-year documents.")
        self._set_visual_state("idle")
        self._switch_content_card(0, animate=animate)

    def _clear_content_fade_handlers(self) -> None:
        for handler in (self._finish_content_fade_out, self._finish_content_fade_in):
            try:
                self._content_fade.finished.disconnect(handler)
            except TypeError:
                pass

    def _switch_content_card(self, target_index: int, animate: bool = True) -> None:
        current_opacity = 1.0
        if self.content_opacity is not None:
            try:
                current_opacity = self.content_opacity.opacity()
            except RuntimeError:
                self.content_opacity = None
                self.content_stack.setGraphicsEffect(None)
        if self.content_stack.currentIndex() == target_index and current_opacity >= 0.99:
            self._refresh_fairy_dust_visibility()
            self._refresh_loading_watermark()
            return
        if not animate:
            self._content_fade.stop()
            self._clear_content_fade_handlers()
            self._pending_content_index = None
            self.content_stack.setCurrentIndex(target_index)
            self._detach_content_opacity()
            self._refresh_fairy_dust_visibility()
            self._refresh_loading_watermark()
            return

        self._pending_content_index = target_index
        self._content_fade.stop()
        self._clear_content_fade_handlers()
        self._ensure_content_opacity()
        self._content_fade.setTargetObject(self.content_opacity)
        self._content_fade.setPropertyName(b"opacity")
        self._content_fade.setStartValue(self.content_opacity.opacity())
        self._content_fade.setEndValue(0.0)
        self._content_fade.finished.connect(self._finish_content_fade_out)
        self._content_fade.start()

    def _finish_content_fade_out(self) -> None:
        self._clear_content_fade_handlers()
        if self._pending_content_index is None:
            self._detach_content_opacity()
            self._refresh_fairy_dust_visibility()
            self._refresh_loading_watermark()
            return
        self.content_stack.setCurrentIndex(self._pending_content_index)
        self._refresh_fairy_dust_visibility()
        self._refresh_loading_watermark()
        self._ensure_content_opacity()
        self.content_opacity.setOpacity(0.0)
        self._content_fade.setTargetObject(self.content_opacity)
        self._content_fade.setPropertyName(b"opacity")
        self._content_fade.setStartValue(0.0)
        self._content_fade.setEndValue(1.0)
        self._content_fade.finished.connect(self._finish_content_fade_in)
        self._content_fade.start()

    def _finish_content_fade_in(self) -> None:
        self._clear_content_fade_handlers()
        self._detach_content_opacity()
        self._pending_content_index = None
        self._refresh_fairy_dust_visibility()
        self._refresh_loading_watermark()

    def _ensure_content_opacity(self) -> None:
        if self.content_opacity is not None:
            try:
                self.content_opacity.setOpacity(1.0)
                return
            except RuntimeError:
                self.content_opacity = None
        self.content_opacity = QGraphicsOpacityEffect(self.content_stack)
        self.content_stack.setGraphicsEffect(self.content_opacity)
        self.content_opacity.setOpacity(1.0)

    def _detach_content_opacity(self) -> None:
        effect = self.content_opacity
        self.content_opacity = None
        if effect is not None:
            try:
                effect.setOpacity(1.0)
            except RuntimeError:
                pass
        try:
            self.content_stack.setGraphicsEffect(None)
        except RuntimeError:
            pass

    def _prepare_intro_state(self) -> None:
        if self._intro_active:
            return
        self._intro_active = True
        self._intro_animation_started = False
        self._stabilize_window_geometry()
        self._set_intro_topmost(True)
        self.chrome_card.hide()
        self._update_fairy_dust_geometry()
        self._refresh_fairy_dust_visibility()
        self.content_stack.setEnabled(False)
        self.content_stack.hide()
        self._set_title_bar_intro_visibility(visible=False)
        self._chrome_intro_opacity = None
        self._refresh_loading_watermark()

        start_rect = self.root.rect().adjusted(18, 18, -18, -18)
        self.intro_title.setGeometry(start_rect)
        self.intro_title.set_text_opacity(0.0)
        self.intro_title.set_wash_progress(-1.0)
        self.intro_title.raise_()
        self.intro_title.show()

    def _start_intro_sequence(self) -> None:
        if self._intro_animation_started:
            return
        self._intro_animation_started = True
        self._start_intro_morph()

    def _begin_launch_intro(self) -> None:
        self._prepare_intro_state()
        self._intro_title_fade.stop()
        self._intro_title_fade.setTargetObject(self.intro_title)
        self._intro_title_fade.setPropertyName(b"textOpacity")
        self._intro_title_fade.setStartValue(0.0)
        self._intro_title_fade.setEndValue(1.0)
        try:
            self._intro_title_fade.finished.disconnect(self._start_title_wash)
        except TypeError:
            pass
        self._intro_title_fade.finished.connect(self._start_title_wash)
        self._intro_title_fade.start()

    def _start_title_wash(self) -> None:
        try:
            self._intro_title_fade.finished.disconnect(self._start_title_wash)
        except TypeError:
            pass
        if not self._intro_active:
            return
        self._intro_title_wash.stop()
        self._intro_title_wash.setTargetObject(self.intro_title)
        self._intro_title_wash.setPropertyName(b"washProgress")
        self._intro_title_wash.setStartValue(0.0)
        self._intro_title_wash.setEndValue(1.0)
        try:
            self._intro_title_wash.finished.disconnect(self._start_launch_fade)
        except TypeError:
            pass
        self._intro_title_wash.finished.connect(self._start_launch_fade)
        self._intro_title_wash.start()

    def _start_launch_fade(self) -> None:
        try:
            self._intro_title_wash.finished.disconnect(self._start_launch_fade)
        except TypeError:
            pass
        if not self._intro_active:
            return
        self.chrome_card.show()
        self._stabilize_window_geometry()
        self._update_fairy_dust_geometry()
        self._refresh_fairy_dust_visibility()
        self.intro_title.raise_()
        self._title_shadow_settle.stop()
        self._title_shadow_settle.setTargetObject(self.intro_title)
        self._title_shadow_settle.setPropertyName(b"shadowEmphasis")
        self._title_shadow_settle.setStartValue(self.intro_title.get_shadow_emphasis())
        self._title_shadow_settle.setEndValue(0.0)
        self._title_shadow_settle.setDuration(1500)
        self._title_shadow_settle.start()
        QTimer.singleShot(0, self._start_intro_sequence)

    def _start_intro_morph(self) -> None:
        if not self._intro_active:
            return

        end_top_left = self.title_label.mapTo(self.root, QPoint(0, 0))
        end_rect = QRect(end_top_left, self.title_label.size())

        self._intro_title_anim.stop()
        self._intro_title_anim.setTargetObject(self.intro_title)
        self._intro_title_anim.setPropertyName(b"geometry")
        self._intro_title_anim.setStartValue(self.intro_title.geometry())
        self._intro_title_anim.setEndValue(end_rect)
        try:
            self._intro_title_anim.finished.disconnect(self._start_intro_content_fade)
        except TypeError:
            pass
        self._intro_title_anim.finished.connect(self._start_intro_content_fade)

        self._intro_title_anim.start()

    def _start_intro_content_fade(self) -> None:
        try:
            self._intro_title_anim.finished.disconnect(self._start_intro_content_fade)
        except TypeError:
            pass
        if not self._intro_active:
            return

        self.content_stack.show()
        self._intro_content_opacity = QGraphicsOpacityEffect(self.content_stack)
        self._intro_content_opacity.setOpacity(0.0)
        self.content_stack.setGraphicsEffect(self._intro_content_opacity)

        self._intro_content_fade.stop()
        self._intro_content_fade.setTargetObject(self._intro_content_opacity)
        self._intro_content_fade.setPropertyName(b"opacity")
        self._intro_content_fade.setStartValue(0.0)
        self._intro_content_fade.setEndValue(1.0)
        try:
            self._intro_content_fade.finished.disconnect(self._finish_intro_sequence)
        except TypeError:
            pass
        self._intro_content_fade.finished.connect(self._finish_intro_sequence)
        self._intro_content_fade.start()

    def _finish_intro_sequence(self) -> None:
        try:
            self._intro_content_fade.finished.disconnect(self._finish_intro_sequence)
        except TypeError:
            pass
        self._intro_active = False
        current_shadow_emphasis = self.intro_title.get_shadow_emphasis()
        self.intro_title.hide()
        self.intro_title.set_wash_progress(-1.0)
        self.intro_title.set_text_opacity(1.0)
        self.title_label.set_shadow_emphasis(current_shadow_emphasis)
        self._set_title_bar_intro_visibility(visible=True)
        if self._intro_content_opacity is not None:
            self.content_stack.setGraphicsEffect(None)
            self._intro_content_opacity = None
        self.content_stack.setEnabled(True)
        self._set_intro_topmost(False)
        self._stabilize_window_geometry()
        self._update_fairy_dust_geometry()
        self._refresh_fairy_dust_visibility()
        self._refresh_loading_watermark()
        self._title_shadow_settle.stop()
        self._title_shadow_settle.setTargetObject(self.title_label)
        self._title_shadow_settle.setPropertyName(b"shadowEmphasis")
        self._title_shadow_settle.setStartValue(current_shadow_emphasis)
        self._title_shadow_settle.setEndValue(0.0)
        self._title_shadow_settle.setDuration(460)
        self._title_shadow_settle.start()

    def eventFilter(self, watched, event):
        if watched is getattr(self, "title_bar", None):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_offset = None
                return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            self._drag_offset = None
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._launch_fade_started:
            return
        self._launch_fade_started = True
        self.raise_()
        self.activateWindow()
        self._stabilize_window_geometry()
        self.setWindowOpacity(1.0)
        self._begin_launch_intro()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_loading_watermark()

    def _set_visual_state(self, state: str) -> None:
        """Apply subtle, low-contrast accent state across key controls."""
        self.status.setProperty("state", state)
        self.progress.setProperty("state", state)
        # Force stylesheet refresh for dynamic-property selectors.
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.progress.style().unpolish(self.progress)
        self.progress.style().polish(self.progress)
        self.status.update()
        self.progress.update()
        if state == "running":
            self._start_shimmer()
        elif state == "stopping":
            self._stop_shimmer()
        else:
            self._stop_shimmer()

    @staticmethod
    def _extract_status_queue_counter(text: str) -> str:
        match = re.search(r"\((\d+ of \d+)\)", text or "")
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _supports_queue_counter_prefix(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        prefixes = (
            "Finding matter ",
            "Opening matter ",
            "Opening the Documents tab ",
            "Switching ",
            "Searching documents ",
            "Checking matching document rows ",
            "Downloading an Order PDF ",
        )
        return normalized.startswith(prefixes)

    def _update_status_text(self, text: str) -> None:
        display = str(text or "").strip()
        if not display:
            self.status.setText("")
            return

        queue_counter = self._extract_status_queue_counter(display)
        if queue_counter:
            self._status_queue_counter = queue_counter
        elif self._status_queue_counter and self._supports_queue_counter_prefix(display):
            display = f"{self._status_queue_counter} | {display}"

        self.status.setText(display)

    def _start_shimmer(self) -> None:
        if self._shimmer_timer.isActive():
            return
        self._shimmer_phase = 0.0
        self._apply_progress_shimmer_style()
        self._shimmer_timer.start()

    def _stop_shimmer(self) -> None:
        if self._shimmer_timer.isActive():
            self._shimmer_timer.stop()
        # Revert to root stylesheet-driven chunk colors for non-running states.
        self.progress.setStyleSheet("")

    def _tick_shimmer(self) -> None:
        self._shimmer_phase = (self._shimmer_phase + 0.035) % 1.0
        self._apply_progress_shimmer_style()

    def _apply_progress_shimmer_style(self) -> None:
        """Slow, subtle moving highlight for a premium progress feel."""
        p = self._shimmer_phase
        band = 0.16
        left = max(0.0, p - band)
        right = min(1.0, p + band)
        self.progress.setStyleSheet(
            f"""
            QProgressBar#runProgress {{
                height: 10px;
                border-radius: 5px;
                text-align: center;
                color: #F5F1FF;
                border: 1px solid rgba(129, 158, 255, 58);
                background-color: rgba(16, 21, 34, 232);
                font-size: 8px;
                font-weight: 800;
            }}
            QProgressBar#runProgress::chunk {{
                border-radius: 4px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #78F4FF,
                    stop:{left:.3f} #91C6FF,
                    stop:{p:.3f} #FFD2F2,
                    stop:{right:.3f} #AD9BFF,
                    stop:1 #79F0E6
                );
            }}
            """
        )

    def _labeled(self, label: str, widget: QWidget) -> QWidget:
        w = QFrame()
        w.setObjectName("fieldCard")
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        w.setMaximumHeight(34)
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 4, 8, 4)
        l.setSpacing(1)
        lab = QLabel(label.upper())
        lab.setObjectName("fieldCaption")
        widget.setMinimumHeight(13)
        l.addWidget(lab)
        l.addWidget(widget)
        return w

    def _on_headless_changed(self, state: int) -> None:
        # Persist the user's preference.
        if self.config.set("headless", bool(state)):
            return

        self._show_message_box(
            QMessageBox.Icon.Warning,
            "Preference Not Saved",
            "Headless mode changed for this session, but the app could not update config.json.\n\n"
            "The setting will still work for the current run. If you want it saved permanently, "
            "close anything using config.json and try again.",
        )

    def _toggle_headless_visibility(self) -> None:
        self.headless_row.setVisible(not self.headless_row.isVisible())
        self._stabilize_window_geometry()

    def _show_message_box(self, icon: QMessageBox.Icon, title: str, message: str) -> int:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setWindowModality(Qt.WindowModality.ApplicationModal)

        try:
            self.raise_()
            self.activateWindow()
        except RuntimeError:
            pass

        box.ensurePolished()
        box.adjustSize()

        parent_geometry = self.frameGeometry()
        screen = self.screen() or QApplication.primaryScreen()
        target_center = parent_geometry.center()
        if screen is not None:
            available = screen.availableGeometry()
            if not parent_geometry.isValid() or parent_geometry.width() <= 0 or parent_geometry.height() <= 0:
                target_center = available.center()
        else:
            available = None

        box_size = box.sizeHint()
        left = int(round(target_center.x() - (box_size.width() / 2)))
        top = int(round(target_center.y() - (box_size.height() / 2)))

        if available is not None:
            left = max(available.left(), min(left, available.right() - box_size.width()))
            top = max(available.top(), min(top, available.bottom() - box_size.height()))

        box.move(left, top)
        return box.exec()

    def _on_collect_btn_clicked(self) -> None:
        """Handle collect button click - switches between Collect and Stop based on state."""
        if self.scraper and self.scraper.isRunning():
            # Currently running - stop it
            self.stop_collection()
        else:
            # Not running - start collection
            self.start_collection()

    def start_collection(self) -> None:
        start_py: date = self.start_date.date().toPyDate()
        end_py: date = self.end_date.date().toPyDate()
        if start_py > end_py:
            self._show_message_box(QMessageBox.Icon.Warning, "Invalid date range", "Start date must be before end date.")
            return

        self.collect_btn.setText("Collect")
        self._set_collect_button_mode("collect")
        self.collect_btn.setEnabled(True)
        self.run_stop_btn.setEnabled(True)
        self.progress.setValue(0)
        self._status_queue_counter = ""
        self.status.setText("Starting collection...")
        self._set_visual_state("running")
        self.last_run_dir = None
        self._switch_content_card(1, animate=True)

        headless = bool(self.config.get("headless", True))
        self.scraper = MOADMNScraper(start_date=start_py, end_date=end_py, headless=headless)
        self.scraper.progress_signal.connect(self.progress.setValue)
        self.scraper.status_signal.connect(self._update_status_text)
        self.scraper.finished_signal.connect(self.on_finished)
        self.scraper.start()

    def stop_collection(self) -> None:
        """Stop the current collection and reset UI."""
        if self.scraper and self.scraper.isRunning():
            # Cooperative stop (preferred). The thread checks `isInterruptionRequested()`.
            self.scraper.requestInterruption()
            self.run_stop_btn.setEnabled(False)
            self.status.setText("Stopping collection...")
            self._set_visual_state("stopping")

    def on_finished(self, ok: bool, message: str, run_dir: str) -> None:
        self.last_run_dir = run_dir or self.last_run_dir
        self._reset_ready_state(animate=True)

        if ok and self.last_run_dir:
            self._show_message_box(QMessageBox.Icon.Information, "MOADMN Collector", message)
            # Open run folder only after the user dismisses the success prompt.
            self.open_folder(path=self.last_run_dir)
        elif ok:
            self._show_message_box(QMessageBox.Icon.Information, "MOADMN Collector", message)
        else:
            self._show_message_box(QMessageBox.Icon.Critical, "MOADMN Collector", message)

    def open_folder(self, checked: bool = False, path: Optional[str] = None) -> None:
        # If a specific run folder exists, open it; otherwise open the base collections folder.
        target = path or self.last_run_dir
        if not target:
            target = str(Path.home() / (self.config.get("download_path") or "Downloads/MOADMN Collections"))

        p = Path(target)
        if not p.exists():
            self._show_message_box(QMessageBox.Icon.Information, "Folder not found", f"Folder does not exist yet:\n{p}")
            return

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def run_app() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon_path = _resolve_app_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    win = MOADMNGUI()
    win.show()
    sys.exit(app.exec())


