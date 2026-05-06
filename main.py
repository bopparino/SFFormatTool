"""Salesforce -> ADP Payroll Converter — GUI entry point.

Friendly, no-jargon GUI for non-technical users. Never surfaces a
traceback — all errors translated to plain English. Full tracebacks are
written to %APPDATA%/PayrollConverter/error.log on Windows (or the
equivalent app data folder on macOS for development).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

# tkinterdnd2 is optional at import-time; we'll fall back gracefully if
# it's missing so dev environments without it can still launch the app.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except Exception:  # pragma: no cover - optional dep
    DND_FILES = None
    TkinterDnD = None
    _DND_AVAILABLE = False

import converter
from converter import ConversionError, ConversionResult


APP_NAME = "PayrollConverter"
DEVELOPER_CONTACT = "your IT support"
WINDOW_W = 800
WINDOW_H = 620


# -----------------------------------------------------------------------------
# Persistent config (last save folder, etc.)
# -----------------------------------------------------------------------------

def _config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    p = base / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _error_log_path() -> Path:
    return _config_dir() / "error.log"


def load_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def log_error(exc: BaseException) -> None:
    try:
        with open(_error_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat()} ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class _AppBase:
    """Shared init that creates the right root class depending on whether
    tkinterdnd2 is available.
    """


def _make_root():
    if _DND_AVAILABLE:
        class Root(ctk.CTk, TkinterDnD.DnDWrapper):
            def __init__(self):
                super().__init__()
                self.TkdndVersion = TkinterDnD._require(self)
        return Root()
    return ctk.CTk()


class PayrollConverterApp:
    def __init__(self):
        self.root = _make_root()
        self.root.title("Salesforce → ADP Payroll Converter")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.minsize(WINDOW_W, WINDOW_H)

        self.config = load_config()
        self.input_path: str | None = None
        self.last_result: ConversionResult | None = None

        self._build_ui()

    # ----- UI construction -----

    def _build_ui(self):
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=24, pady=20)

        title = ctk.CTkLabel(
            outer,
            text="Salesforce → ADP Payroll Converter",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title.pack(pady=(0, 16))

        # Drop zone
        self.drop_frame = ctk.CTkFrame(
            outer,
            height=140,
            corner_radius=12,
            fg_color="#1f2630",
            border_width=2,
            border_color="#3a4250",
        )
        self.drop_frame.pack(fill="x", pady=(0, 16))
        self.drop_frame.pack_propagate(False)

        self.drop_label = ctk.CTkLabel(
            self.drop_frame,
            text="Drop Salesforce xlsx here\n(or click to browse)",
            font=ctk.CTkFont(size=18),
            text_color="#c8cfd9",
        )
        self.drop_label.pack(expand=True)

        # Click to browse
        for w in (self.drop_frame, self.drop_label):
            w.bind("<Button-1>", lambda _e: self._browse_file())

        # Drag-and-drop registration
        if _DND_AVAILABLE:
            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)

        # Convert button
        self.convert_btn = ctk.CTkButton(
            outer,
            text="Convert",
            height=48,
            font=ctk.CTkFont(size=18, weight="bold"),
            command=self._on_convert,
            state="disabled",
        )
        self.convert_btn.pack(fill="x", pady=(0, 16))

        # Status pane
        status_label = ctk.CTkLabel(
            outer,
            text="Status",
            font=ctk.CTkFont(size=12),
            text_color="#7d8694",
            anchor="w",
        )
        status_label.pack(fill="x", pady=(0, 4))

        self.status = ctk.CTkTextbox(
            outer,
            height=240,
            font=ctk.CTkFont(family="Menlo", size=12),
            fg_color="#0d1117",
            text_color="#d0d7de",
            wrap="word",
        )
        self.status.pack(fill="both", expand=True)
        self.status.configure(state="disabled")

    # ----- Helpers -----

    def _set_drop_loaded(self, filename: str):
        self.drop_frame.configure(
            fg_color="#1d2d1d",
            border_color="#3d6b3d",
        )
        self.drop_label.configure(
            text=f"Loaded: {filename}\n(click to choose a different file)",
        )

    def _set_drop_idle(self):
        self.drop_frame.configure(
            fg_color="#1f2630",
            border_color="#3a4250",
        )
        self.drop_label.configure(
            text="Drop Salesforce xlsx here\n(or click to browse)",
        )

    def _append_status(self, text: str):
        self.status.configure(state="normal")
        self.status.insert("end", text + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")
        self.root.update_idletasks()

    def _append_separator(self):
        self.status.configure(state="normal")
        if self.status.get("1.0", "end").strip():
            self.status.insert("end", "─" * 60 + "\n")
        self.status.configure(state="disabled")

    # ----- Event handlers -----

    def _on_drop(self, event):
        # tkdnd encodes paths; spaces wrapped in {}. The .splitlist helper
        # handles this correctly.
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        if not paths:
            return
        self._set_input(paths[0])

    def _browse_file(self):
        initial = self.config.get("last_input_dir") or str(Path.home() / "Desktop")
        path = filedialog.askopenfilename(
            title="Choose Salesforce labor remittance file",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
            initialdir=initial,
        )
        if path:
            self._set_input(path)

    def _set_input(self, path: str):
        if not path.lower().endswith(".xlsx"):
            messagebox.showwarning(
                "Wrong file type",
                "That doesn't look like a Salesforce labor report. "
                "Please drop the .xlsx file from Salesforce.",
            )
            return
        self.input_path = path
        self.config["last_input_dir"] = str(Path(path).parent)
        save_config(self.config)
        self._set_drop_loaded(Path(path).name)
        self.convert_btn.configure(state="normal")

    def _on_convert(self):
        if not self.input_path:
            return

        self._append_separator()
        self._append_status(f"Loaded: {Path(self.input_path).name}")

        try:
            result = converter.convert_workbook(
                self.input_path, log=self._append_status
            )
        except ConversionError as exc:
            messagebox.showerror("Couldn't convert", str(exc))
            self._append_status(f"Error: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log_error(exc)
            short = type(exc).__name__
            msg = (
                f"Something went wrong: {short}. "
                f"If this keeps happening, contact {DEVELOPER_CONTACT}."
            )
            messagebox.showerror("Unexpected error", msg)
            self._append_status(f"Error: {msg}")
            return

        self.last_result = result
        self.root.after(150, self._save_dialog)

    def _save_dialog(self):
        if not self.last_result:
            return

        initial_dir = self.config.get("last_save_dir") or str(Path.home() / "Desktop")

        # Force the filename to PRJISEPI.csv. The save dialog will offer it
        # as default; we re-coerce after the fact in case they typed something.
        chosen = filedialog.asksaveasfilename(
            title="Save ADP file as",
            defaultextension=".csv",
            initialfile=converter.OUTPUT_FILENAME,
            initialdir=initial_dir,
            filetypes=[("CSV files", "*.csv")],
        )
        if not chosen:
            self._append_status("Save cancelled. Click Convert to try again.")
            return

        # Coerce filename to PRJISEPI.csv regardless of what was typed.
        target_dir = os.path.dirname(chosen) or initial_dir
        target = os.path.join(target_dir, converter.OUTPUT_FILENAME)
        if os.path.basename(chosen) != converter.OUTPUT_FILENAME:
            messagebox.showinfo(
                "Filename adjusted",
                f"ADP requires this file to be named {converter.OUTPUT_FILENAME}. "
                f"Saving as {converter.OUTPUT_FILENAME} in the folder you chose.",
            )

        try:
            converter.write_output_csv(target, self.last_result.output_rows)
        except PermissionError as exc:
            log_error(exc)
            messagebox.showerror(
                "Couldn't save",
                "Couldn't save to that folder. Try saving somewhere else, "
                "like your Desktop.",
            )
            self._append_status("Error: Couldn't save to that folder.")
            return
        except OSError as exc:
            log_error(exc)
            messagebox.showerror(
                "Couldn't save",
                "Couldn't save to that folder. Try saving somewhere else, "
                "like your Desktop.",
            )
            self._append_status("Error: Couldn't save to that folder.")
            return
        except Exception as exc:  # noqa: BLE001
            log_error(exc)
            messagebox.showerror(
                "Unexpected error",
                f"Something went wrong: {type(exc).__name__}. "
                f"If this keeps happening, contact {DEVELOPER_CONTACT}.",
            )
            return

        self.config["last_save_dir"] = target_dir
        save_config(self.config)
        self._append_status(f"Saved to: {target}")
        messagebox.showinfo("Conversion complete!", f"Saved to:\n{target}")

    def run(self):
        self.root.mainloop()


def main():
    try:
        app = PayrollConverterApp()
        app.run()
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        log_error(exc)
        try:
            messagebox.showerror(
                "Unexpected error",
                f"Something went wrong starting the app: {type(exc).__name__}. "
                f"If this keeps happening, contact {DEVELOPER_CONTACT}.",
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
