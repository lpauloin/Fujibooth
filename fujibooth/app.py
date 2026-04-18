from __future__ import annotations

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .config import load_settings
from .ui.main_window import MainWindow


def main() -> int:
    print('[APP] demarrage application')
    settings = load_settings()

    app = QApplication.instance() or QApplication(sys.argv)

    print('[APP] creation MainWindow')
    window = MainWindow(settings)
    app.aboutToQuit.connect(window.shutdown)

    print('[APP] start_services()')
    window.start_services()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sigint_timer = QTimer()
    sigint_timer.setInterval(200)
    sigint_timer.timeout.connect(lambda: None)
    sigint_timer.start()

    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
