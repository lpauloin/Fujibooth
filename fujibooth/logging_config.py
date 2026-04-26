import logging
import logging.config

PREFIX = "fujibooth."


class ShortNameFormatter(logging.Formatter):
    def format(self, record):
        record.short_name = record.name.removeprefix(PREFIX)
        return super().format(record)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            "()": "fujibooth.logging_config.ShortNameFormatter",
            "fmt": "%(asctime)s.%(msecs)03d %(levelname)-8s %(short_name)-40s %(message)s",
            "datefmt": "%H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "stream": "ext://sys.stdout",
        }
    },
    "loggers": {
        # ── Application root ───────────────────────────────────────────
        "fujibooth": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        # ── UI layer ───────────────────────────────────────────────────
        "fujibooth.ui.main_window": {
            "level": "INFO",
        },
        "fujibooth.ui.exposure_bar": {
            # set_focused_control / set_editing fire on every nav key press
            "level": "WARNING",
        },
        # ── Camera backend ─────────────────────────────────────────────
        "fujibooth.backends.fujifilm_sdk_backend": {
            "level": "WARNING",
        },
        # ── SDK bridge ─────────────────────────────────────────────────
        "fujibooth.sdk_bridge.wrapper": {
            "level": "WARNING",
        },
        "fujibooth.sdk_bridge.xsdk_ctypes": {
            # Pure C-API binding noise — suppress by default
            "level": "WARNING",
        },
        # ── Services ───────────────────────────────────────────────────
        "fujibooth.services.photo_repository": {
            "level": "WARNING",
        },
        "fujibooth.services.remote_control": {
            "level": "WARNING",
        },
        "fujibooth.services.usb_monitor": {
            "level": "WARNING",
        },
        "fujibooth.services.printer": {
            "level": "WARNING",
        },
        "fujibooth.services.bluetooth_monitor": {
            "level": "WARNING",
        },
        "fujibooth.services.instax_minilink": {
            "level": "WARNING",
        },
    },
    "root": {
        "level": "WARNING",
    },
}


def setup():
    logging.config.dictConfig(LOGGING_CONFIG)
