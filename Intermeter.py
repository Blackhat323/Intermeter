import sys
import time
import threading
import psutil
import socket
import subprocess
from pathlib import Path
from collections import deque
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSizePolicy, QMenu, QPushButton
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont, QPainter, QColor, QPen, QAction, QIcon

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = "#080B14"
CARD    = "#0F1520"
BORDER  = "#1A2235"
ACCENT  = "#00E5FF"
GREEN   = "#00E676"
ORANGE  = "#FF9100"
RED     = "#FF3D57"
MUTED   = "#4A5568"
TEXT    = "#E2E8F0"
SUBTEXT = "#718096"

def speed_color(kbps: float) -> str:
    if kbps >= 5000:  # 5 Mbps
        return GREEN
    if kbps >= 500:   # 0.5 Mbps
        return ORANGE
    return RED

def format_speed(kbps: float) -> tuple[str, str]:
    """Dynamically formats network speed from Kbps to Mbps/Gbps as needed."""
    if kbps >= 1000000:
        return f"{kbps / 1000000:.2f}", "Gbps"
    elif kbps >= 1000:
        return f"{kbps / 1000:.1f}", "Mbps"
    else:
        return f"{int(kbps)}", "Kbps"

def flush_dns() -> bool:
    """Executes system DNS flush command, hiding command prompt window on Windows."""
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            
        subprocess.run(
            ["ipconfig", "/flushdns"],
            capture_output=True,
            text=True,
            check=True,
            startupinfo=startupinfo
        )
        return True
    except Exception:
        return False

# ── Network speed using psutil (sampled every 200ms) ─────────────────────────
_last_counters = None
_last_time     = None

def get_net_speed_instant() -> tuple[float, float]:
    """Returns (dl_kbps, ul_kbps) based on delta since last call."""
    global _last_counters, _last_time
    now = time.perf_counter()
    c   = psutil.net_io_counters()
    if _last_counters is None:
        _last_counters = c
        _last_time     = now
        return 0.0, 0.0
    dt = now - _last_time
    if dt <= 0:
        return 0.0, 0.0
    dl = (c.bytes_recv - _last_counters.bytes_recv) * 8 / 1_000 / dt
    ul = (c.bytes_sent - _last_counters.bytes_sent) * 8 / 1_000 / dt
    _last_counters = c
    _last_time     = now
    return max(dl, 0.0), max(ul, 0.0)

# ── Graph ─────────────────────────────────────────────────────────────────────
class Graph(QWidget):
    MAX_POINTS = 200  # More points = smoother/denser history

    def __init__(self, color=GREEN, parent=None):
        super().__init__(parent)
        self.data: deque[float | None] = deque(maxlen=self.MAX_POINTS)
        self.line_color = color
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def add_point(self, val: float | None):
        self.data.append(val)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad  = 8
        painter.fillRect(0, 0, w, h, QColor(CARD))
        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawRoundedRect(0, 0, w - 1, h - 1, 8, 8)

        if len(self.data) < 2:
            painter.setPen(QColor(MUTED))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, "Waiting for speed data...")
            painter.end()
            return

        valid = [v for v in self.data if v is not None]
        if not valid:
            painter.end()
            return

        max_val = max(max(valid) * 1.2, 1)
        points  = list(self.data)
        step    = (w - pad * 2) / max(len(points) - 1, 1)

        painter.setPen(QPen(QColor(BORDER), 1))
        for i in range(1, 4):
            y = pad + (h - pad * 2) * i // 3
            painter.drawLine(pad, y, w - pad, y)

        prev = None
        for i, val in enumerate(points):
            if val is None:
                prev = None
                continue
            x = int(pad + i * step)
            y = int(h - pad - (val / max_val) * (h - pad * 2))
            c = QColor(self.line_color)
            painter.setPen(QPen(c, 1.5))
            if prev:
                painter.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)
        painter.end()

# ── Signals ───────────────────────────────────────────────────────────────────
class Signals(QObject):
    speed_updated = pyqtSignal(float, float)

# ── Main Window ───────────────────────────────────────────────────────────────
class SpeedMonitor(QWidget):
    def __init__(self):
        super().__init__()
        self.running = False
        self.drag_position = None
        self.locked = False
        self.is_widget_mode = True  # Starts in Widget Mode by default

        # Speed smoothing variables (Exponential Moving Average)
        self.dl_smooth = 0.0
        self.ul_smooth = 0.0
        self.ema_alpha = 0.20

        # Configure initial widget window properties
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self.signals = Signals()
        self.signals.speed_updated.connect(self._on_speed)
        
        self._build_ui()
        self._switch_mode(True)  # Set layouts for Widget Mode initially
        self._snap_to_bottom_right()
        
        # Auto-start monitoring and stabilization on launch
        self._start()

    def _build_ui(self):
        self.setWindowTitle("Intermeter")
        self.setFixedSize(320, 320)  # Reference size

        # Outer layout to manage main container margins
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # Translucent glassmorphic widget background frame
        self.container = QFrame()
        self.container.setObjectName("MainContainer")
        self.container.setStyleSheet(f"""
            QFrame#MainContainer {{
                background-color: rgba(8, 11, 20, 0.92);
                border: 1px solid {BORDER};
                border-radius: 14px;
            }}
        """)
        outer_layout.addWidget(self.container)

        self.root_layout = QVBoxLayout(self.container)
        self.root_layout.setContentsMargins(12, 12, 12, 12)
        self.root_layout.setSpacing(10)

        # Title and status header
        title_layout = QHBoxLayout()
        title = QLabel("Intermeter")
        title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {TEXT}; border: none; background: transparent;")
        
        self.status_header = QLabel("● Active & Stabilizing")
        self.status_header.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.status_header.setStyleSheet(f"color: {GREEN}; border: none; background: transparent;")
        
        title_layout.addWidget(title)
        title_layout.addStretch()
        title_layout.addWidget(self.status_header)
        self.root_layout.addLayout(title_layout)

        # Side-by-side Download and Upload cards
        dl_ul_row = QHBoxLayout()
        dl_ul_row.setSpacing(8)
        self.dl_big = self._big_card("DOWNLOAD", "—", "Kbps", GREEN)
        self.ul_big = self._big_card("UPLOAD", "—", "Kbps", ORANGE)
        dl_ul_row.addWidget(self.dl_big["frame"])
        dl_ul_row.addWidget(self.ul_big["frame"])
        self.root_layout.addLayout(dl_ul_row)

        # Graph Container (handles hiding elements + spacings cleanly)
        self.graph_container = QWidget()
        self.graph_container.setStyleSheet("background: transparent; border: none;")
        graph_layout = QVBoxLayout(self.graph_container)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        graph_layout.setSpacing(6)
        
        self.graph_label = self._lbl("SPEED HISTORY (updates every 200ms)")
        self.speed_graph = Graph(color=GREEN)
        self.speed_graph.setMinimumHeight(80)
        
        graph_layout.addWidget(self.graph_label)
        graph_layout.addWidget(self.speed_graph)
        self.root_layout.addWidget(self.graph_container)

        # Buttons Row: OPEN, CLOSE, MODE (STANDARD/WIDGET), EXIT
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        
        self.open_btn = QPushButton("OPEN")
        self.open_btn.setFixedHeight(30)
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.setStyleSheet(self._btn_style(GREEN))
        self.open_btn.clicked.connect(self._start)
        
        self.close_btn = QPushButton("CLOSE")
        self.close_btn.setFixedHeight(30)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet(self._btn_style(RED))
        self.close_btn.clicked.connect(self._stop)
        
        self.mode_btn = QPushButton("STANDARD")  # Toggle mode button
        self.mode_btn.setFixedHeight(30)
        self.mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_btn.setStyleSheet(self._btn_style(ACCENT))
        self.mode_btn.clicked.connect(self._toggle_mode)
        
        self.exit_btn = QPushButton("EXIT")
        self.exit_btn.setFixedHeight(30)
        self.exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exit_btn.setStyleSheet(self._btn_style(ORANGE))
        self.exit_btn.clicked.connect(self.close)
        
        btn_layout.addWidget(self.open_btn)
        btn_layout.addWidget(self.close_btn)
        btn_layout.addWidget(self.mode_btn)
        btn_layout.addWidget(self.exit_btn)
        self.root_layout.addLayout(btn_layout)

        # Status indicator / helper tooltip
        self.status_label = QLabel("● Active & Stabilizing · Right-click for options")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(f"color: {SUBTEXT}; font-size: 9px; font-weight: 500;")
        self.root_layout.addWidget(self.status_label)

    def _big_card(self, label, value, unit, color):
        frame = QFrame()
        frame.setMinimumHeight(80)  # Ensures card is never squished below 80px
        frame.setStyleSheet(f"""
            QFrame {{
                background: {CARD};
                border-radius: 8px;
                border: 1px solid {BORDER};
            }}
            QFrame:hover {{
                border-color: {color};
            }}
        """)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(0)
        
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {MUTED}; font-size: 8px; font-weight: 600; letter-spacing: 0.5px; border: none; background: transparent;")
        
        val = QLabel(value)
        val.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        val.setStyleSheet(f"color: {color}; border: none; background: transparent;")
        
        u = QLabel(unit)
        u.setStyleSheet(f"color: {MUTED}; font-size: 9px; border: none; background: transparent;")
        
        layout.addWidget(lbl)
        layout.addWidget(val)
        layout.addWidget(u)
        return {"frame": frame, "val": val, "unit": u}

    def _lbl(self, text):
        l = QLabel(text)
        l.setStyleSheet(f"color: {MUTED}; font-size: 8px; font-weight: 600; letter-spacing: 0.5px; background: transparent;")
        return l

    def _btn_style(self, color):
        return f"""
            QPushButton {{
                background: transparent;
                color: {color};
                border: 1.5px solid {color};
                border-radius: 6px;
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 0.5px;
            }}
            QPushButton:hover {{
                background: {color}18;
            }}
            QPushButton:pressed {{
                background: {color}30;
            }}
        """

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {CARD};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background-color: {BORDER};
                color: {ACCENT};
            }}
            QMenu::separator {{
                height: 1px;
                background: {BORDER};
                margin: 4px 0px;
            }}
        """)
        
        # 1. Monitoring Toggle Action
        toggle_text = "Close Monitoring" if self.running else "Open Monitoring"
        toggle_action = menu.addAction(toggle_text)
        toggle_action.triggered.connect(self._toggle)
        
        # 2. Flush System DNS Action
        dns_action = menu.addAction("Flush DNS Cache")
        dns_action.triggered.connect(self._flush_dns_cache)
        
        menu.addSeparator()
        
        # 3. Window Mode Toggle Action
        mode_text = "Switch to Standard Mode" if self.is_widget_mode else "Switch to Widget Mode"
        mode_action = menu.addAction(mode_text)
        mode_action.triggered.connect(self._toggle_mode)
        
        menu.addSeparator()
        
        # 4. Lock Position Action
        lock_action = menu.addAction("Lock Widget Position")
        lock_action.setCheckable(True)
        lock_action.setChecked(self.locked)
        lock_action.triggered.connect(self._toggle_lock)
        
        # 5. Opacity Selection Submenu
        opacity_menu = menu.addMenu("Widget Opacity")
        opacity_menu.setStyleSheet(menu.styleSheet())
        opacities = [("100% Opacity", 1.0), ("85% Opacity", 0.85), ("70% Opacity", 0.70), ("55% Opacity", 0.55)]
        current_opacity = self.windowOpacity()
        for label, val in opacities:
            act = opacity_menu.addAction(label)
            act.setCheckable(True)
            if abs(current_opacity - val) < 0.05:
                act.setChecked(True)
            act.triggered.connect(lambda checked, v=val: self.setWindowOpacity(v))
            
        menu.addSeparator()
        
        # 6. Exit Widget Action
        exit_action = menu.addAction("Exit Application")
        exit_action.triggered.connect(self.close)
        
        menu.exec(self.mapToGlobal(pos))

    def _toggle(self):
        if self.running: self._stop()
        else: self._start()

    def _start(self):
        if self.running:
            return
        self.running = True
        self.speed_graph.data.clear()
        self._set_status("● Active & Stabilizing · Right-click for options", ACCENT)
        self.status_header.setText("● Active & Stabilizing")
        self.status_header.setStyleSheet(f"color: {GREEN}; border: none; background: transparent;")
        
        # Start both speed sampling and connection keep-alive heartbeat threads
        threading.Thread(target=self._speed_loop, daemon=True).start()
        threading.Thread(target=self._stabilize_loop, daemon=True).start()

    def _stop(self):
        if not self.running:
            return
        self.running = False
        self._set_status("● Paused · Right-click for options", MUTED)
        self.status_header.setText("● Paused")
        self.status_header.setStyleSheet(f"color: {MUTED}; border: none; background: transparent;")
        
        # Clear UI stats display on stop
        self.dl_big["val"].setText("—")
        self.ul_big["val"].setText("—")

    def _flush_dns_cache(self):
        """Triggers system DNS cache flushing and updates status label with quick feedback."""
        success = flush_dns()
        if success:
            self._set_status("● DNS Cache Flushed Successfully!", ACCENT)
            QTimer.singleShot(2500, self._reset_status_message)
        else:
            self._set_status("● DNS Cache Flush Failed!", RED)
            QTimer.singleShot(2500, self._reset_status_message)

    def _reset_status_message(self):
        """Resets the status message according to active monitoring state."""
        if self.running:
            self._set_status("● Active & Stabilizing · Right-click for options", ACCENT)
        else:
            self._set_status("● Paused · Right-click for options", MUTED)

    def _toggle_mode(self):
        self._switch_mode(not self.is_widget_mode)

    def _switch_mode(self, to_widget: bool):
        self.is_widget_mode = to_widget
        self.hide()
        
        if to_widget:
            # Hide graph container (including child graph + labels + margins)
            self.graph_container.setVisible(False)
            
            # Widget Mode: Frameless window stays on top
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | 
                Qt.WindowType.WindowStaysOnTopHint | 
                Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            
            # Semi-transparent styling
            self.container.setStyleSheet(f"""
                QFrame#MainContainer {{
                    background-color: rgba(8, 11, 20, 0.92);
                    border: 1px solid {BORDER};
                    border-radius: 14px;
                }}
            """)
            self.setFixedSize(320, 195)
            self._snap_to_bottom_right()
            self.mode_btn.setText("STANDARD")
            self._reset_status_message()
        else:
            # Show graph container
            self.graph_container.setVisible(True)
            
            # Standard Mode: Normal window with borders & OS titlebar
            self.setWindowFlags(Qt.WindowType.Window)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            
            # Solid opaque styling
            self.container.setStyleSheet(f"""
                QFrame#MainContainer {{
                    background-color: {BG};
                    border: 1px solid {BORDER};
                    border-radius: 14px;
                }}
            """)
            self.setFixedSize(380, 380)
            
            # Center standard mode window
            screen_geom = QApplication.primaryScreen().availableGeometry()
            x = screen_geom.x() + (screen_geom.width() - self.width()) // 2
            y = screen_geom.y() + (screen_geom.height() - self.height()) // 2
            self.setGeometry(x, y, self.width(), self.height())
            
            self.mode_btn.setText("WIDGET")
            self._reset_status_message()
            
        self.show()

    def _snap_to_bottom_right(self):
        """Snaps the widget to the bottom right corner of the screen, just above the taskbar."""
        screen_geom = QApplication.primaryScreen().availableGeometry()
        x = screen_geom.x() + screen_geom.width() - self.width() - 16
        y = screen_geom.y() + screen_geom.height() - self.height() - 16
        self.setGeometry(x, y, self.width(), self.height())

    def _speed_loop(self):
        """Network speed sampled every 200ms — 5x per second."""
        while self.running:
            dl, ul = get_net_speed_instant()
            self.signals.speed_updated.emit(dl, ul)
            time.sleep(0.2)

    def _stabilize_loop(self):
        """Sends lightweight connection heartbeats every 150ms to prevent connection adaptor idle mode."""
        target_host = "8.8.8.8"
        port = 53
        while self.running:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect((target_host, port))
                s.close()
            except Exception:
                pass
            time.sleep(0.15)

    def _on_speed(self, dl: float, ul: float):
        if not self.running:
            return
            
        # Apply Exponential Moving Average (EMA) smoothing
        if self.dl_smooth == 0.0:
            self.dl_smooth = dl
        else:
            self.dl_smooth = self.ema_alpha * dl + (1 - self.ema_alpha) * self.dl_smooth

        if self.ul_smooth == 0.0:
            self.ul_smooth = ul
        else:
            self.ul_smooth = self.ema_alpha * ul + (1 - self.ema_alpha) * self.ul_smooth

        self.speed_graph.add_point(self.dl_smooth)
        
        # Dynamic units formatting (Kbps / Mbps / Gbps)
        dl_val, dl_unit = format_speed(self.dl_smooth)
        ul_val, ul_unit = format_speed(self.ul_smooth)
        
        self.dl_big["val"].setText(dl_val)
        self.dl_big["unit"].setText(dl_unit)
        self.dl_big["val"].setStyleSheet(f"color: {speed_color(self.dl_smooth)}; border: none; background: transparent;")
        
        self.ul_big["val"].setText(ul_val)
        self.ul_big["unit"].setText(ul_unit)
        self.ul_big["val"].setStyleSheet(f"color: {speed_color(self.ul_smooth)}; border: none; background: transparent;")

    def _toggle_lock(self, checked):
        self.locked = checked

    def _set_status(self, text, color):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 9px; font-weight: 500;")

    # ── Mouse event overrides for window dragging ─────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.locked:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and not self.locked and self.drag_position is not None:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = None
            event.accept()

    def closeEvent(self, event):
        self.running = False
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    icon_path = Path(__file__).with_name("app.ico")
    app_icon = QIcon(str(icon_path)) if icon_path.exists() else None
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    window = SpeedMonitor()
    if app_icon is not None and not app_icon.isNull():
        window.setWindowIcon(app_icon)

    window.show()
    sys.exit(app.exec())
