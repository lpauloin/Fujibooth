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
            "fmt": "%(levelname)-8s  %(short_name)-40s  %(message)s",
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
            "level": "INFO",
        },
        # ── SDK bridge ─────────────────────────────────────────────────
        "fujibooth.sdk_bridge.wrapper": {
            "level": "INFO",
        },
        "fujibooth.sdk_bridge.xsdk_ctypes": {
            # Pure C-API binding noise — suppress by default
            "level": "WARNING",
        },
        # ── Services ───────────────────────────────────────────────────
        "fujibooth.services.photo_repository": {
            "level": "INFO",
        },
        "fujibooth.services.remote_control": {
            "level": "INFO",
        },
        "fujibooth.services.usb_monitor": {
            "level": "INFO",
        },
        "fujibooth.services.printer": {
            "level": "INFO",
        },
        "fujibooth.services.bluetooth_monitor": {
            "level": "INFO",
        },
        "fujibooth.services.instax_minilink": {
            "level": "INFO",
        },
    },
    "root": {
        "level": "WARNING",
    },
}


def setup():
    logging.config.dictConfig(LOGGING_CONFIG)
