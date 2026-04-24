from pathlib import Path
import shlex
import subprocess


class PrintService:
    def __init__(self, enabled, command):
        self.enabled = enabled
        self.command = command.strip()

    def print_photo(self, photo_path):
        if not photo_path.exists():
            return False, f"Photo introuvable: {photo_path}"
        if not self.enabled:
            return True, f"PRINT stub: {photo_path.name}"
        if not self.command:
            return False, "Printing enabled but no command configured"
        try:
            cmd = shlex.split(self.command) + [str(photo_path)]
            subprocess.run(cmd, check=True)
            return True, f"Photo envoyee a l'impression: {photo_path.name}"
        except Exception as exc:
            return False, f"Erreur impression: {exc}"