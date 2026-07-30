"""
Altium CSV Librarian
====================
A dark-themed CSV editor built with tkinter.

Features
--------
- Open a folder → all CSV files load automatically
- Sidebar file list with unsaved-changes indicator
- Editable grid; first column (GUID) is read-only
- First row (headers) is static and not editable
- Add Row  → new row with auto-generated GUID, data copied from row above
- Arrow-key navigation between cells
- Ctrl+D   → copy value from the cell above
- Ctrl+↓   → insert a new row below current, cloning data with a fresh GUID
- Autosave after 30 s of edit inactivity (countdown shown in status bar)
- Backups: before every save the old file is copied to .backups/
  keeping only the 5 most recent backups per file
- backdrop.PNG in the folder is used as the header background image

Requirements: Python 3.8+, Pillow  (pip install Pillow)
tkinter is included with standard Python on Windows and macOS.
On Linux: sudo apt install python3-tk
"""

import csv
import os
import re
import shutil
import sys
import uuid as _uuid_mod
from datetime import datetime
from pathlib import Path
from tkinter import (
    BOTH, BOTTOM, END, FLAT, HORIZONTAL, LEFT, NONE, RIGHT,
    TOP, W, X, Y, YES, BooleanVar, Frame, Label, Listbox,
    Menu, PanedWindow, PhotoImage, Scrollbar, StringVar,
    Text, Tk, messagebox, filedialog,
)
import tkinter as tk
import tkinter.ttk as ttk

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ── Colour palette (mirrors the HTML design tokens) ──────────────────────────
C = {
    "bg":          "#0f1117",
    "surface":     "#181b24",
    "panel":       "#1e2130",
    "border":      "#2a2f42",
    "accent":      "#4f8ef7",
    "danger":      "#e05c5c",
    "success":     "#3ecf8e",
    "warning":     "#f9a825",
    "text":        "#e2e6f0",
    "text_muted":  "#7a82a0",
    "text_dim":    "#3d4460",
    "guid_col":    "#1e2236",
    "header_bg":   "#151720",
    "row_alt":     "#181c2b",
    "gold":        "#CFB53B",
    "white":       "#ffffff",
}

MAX_BACKUPS   = 5
BACKUP_FOLDER = ".backups"
AUTOSAVE_SECS = 30
GUID_COL_W    = 260
MIN_COL_W     = 90


# ── Helpers ───────────────────────────────────────────────────────────────────

def new_guid() -> str:
    return str(_uuid_mod.uuid4())


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return headers, rows


def write_csv(path: Path, headers: list[str], rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=headers, extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)


def make_backup(path: Path):
    """Copy path → .backups/stem.YYYYMMDD-HHmmss.ext, keep last MAX_BACKUPS."""
    if not path.exists():
        return
    backup_dir = path.parent / BACKUP_FOLDER
    backup_dir.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = path.stem
    ext  = path.suffix
    dest = backup_dir / f"{stem}.{ts}{ext}"
    shutil.copy2(path, dest)
    # Prune
    pattern = re.compile(rf"^{re.escape(stem)}\.\d{{8}}-\d{{6}}{re.escape(ext)}$")
    backups = sorted(p for p in backup_dir.iterdir() if pattern.match(p.name))
    for old in backups[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)


# ── Custom widget: editable grid ──────────────────────────────────────────────

class CsvGrid(ttk.Frame):
    """
    A scrollable, editable grid backed by a list-of-dicts data model.
    Row 0 = headers (static).  Column 0 = GUID (read-only cells).
    """

    def __init__(self, master, on_dirty, **kw):
        super().__init__(master, **kw)
        self.on_dirty = on_dirty          # called whenever data changes
        self.headers: list[str] = []
        self.rows: list[dict]   = []
        self._cells: list[list[tk.Entry | tk.Label]] = []  # [row][col]
        self._dirty = False
        self._col_widths: list[int] = []

        self.configure(style="Grid.TFrame")

        # ── Fixed header frame (never scrolls vertically) ─────────────────────
        self._header_frame = tk.Frame(self, bg=C["header_bg"])
        self._header_frame.pack(side=TOP, fill=X)

        # ── Scrollbars + canvas for data rows ─────────────────────────────────
        self._vsb = ttk.Scrollbar(self, orient="vertical")
        self._hsb = ttk.Scrollbar(self, orient="horizontal")

        self._canvas = tk.Canvas(
            self, bg=C["bg"], highlightthickness=0,
            yscrollcommand=self._vsb.set,
        )
        self._vsb.config(command=self._canvas.yview)
        self._hsb.config(command=self._xscroll_both)

        self._vsb.pack(side=RIGHT, fill=Y)
        self._hsb.pack(side=BOTTOM, fill=X)
        self._canvas.pack(side=LEFT, fill=BOTH, expand=True)

        self._inner = tk.Frame(self._canvas, bg=C["bg"])
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw"
        )
        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Mousewheel
        self._canvas.bind_all("<MouseWheel>",   self._on_mousewheel)
        self._canvas.bind_all("<Button-4>",     self._on_mousewheel)
        self._canvas.bind_all("<Button-5>",     self._on_mousewheel)

        # Ctrl+A — autosize all columns
        self.bind_all("<Control-a>", lambda e: self.autosize_columns())
        self.bind_all("<Control-A>", lambda e: self.autosize_columns())

    def _xscroll_both(self, *args):
        """Drive horizontal scroll on both data canvas and header canvas in sync."""
        self._canvas.xview(*args)
        try:
            self._header_canvas.xview(*args)
        except Exception:
            pass
        # Update the scrollbar thumb to reflect the data canvas position
        self._hsb.set(*self._canvas.xview())

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, headers: list[str], rows: list[dict]):
        self.headers = headers
        self.rows    = [dict(r) for r in rows]
        self._dirty  = False
        self._rebuild()

    def get_data(self) -> tuple[list[str], list[dict]]:
        return self.headers, [dict(r) for r in self.rows]

    def add_row(self, clone_last: bool = False):
        """
        Always appends a new row at the bottom.
        If clone_last=True, copies data from the current last row (new GUID).
        """
        new_row: dict = {}
        for i, h in enumerate(self.headers):
            if i == 0:
                new_row[h] = new_guid()
            elif clone_last and self.rows:
                new_row[h] = self.rows[-1].get(h, "")
            else:
                new_row[h] = ""

        ri = len(self.rows)
        self.rows.append(new_row)
        self._cells.append(self._build_row_widgets(ri, new_row))

        self._mark_dirty()
        self._update_scrollregion()
        self.after(20, self._scroll_to_bottom)
        self.after(40, lambda: self._focus_cell(ri, 1))

    # ── Internal build ────────────────────────────────────────────────────────

    def _rebuild(self):
        # Clear data rows
        for w in self._inner.winfo_children():
            w.destroy()
        # Clear header frame
        for w in self._header_frame.winfo_children():
            w.destroy()
        self._cells = []

        if not self.headers:
            return

        self._col_widths = [GUID_COL_W] + [
            max(MIN_COL_W, len(h) * 8 + 16) for h in self.headers[1:]
        ]

        # ── Fixed header: row-number cell + column labels ─────────────────────
        # We use an inner canvas inside _header_frame so horizontal scroll
        # can shift it in sync with the data canvas.
        self._header_canvas = tk.Canvas(
            self._header_frame, bg=C["header_bg"],
            highlightthickness=0, height=28,
            xscrollcommand=self._hsb.set,
        )
        self._header_canvas.pack(fill=X, expand=True)

        self._header_inner = tk.Frame(self._header_canvas, bg=C["header_bg"])
        self._header_canvas.create_window((0, 0), window=self._header_inner, anchor="nw")

        # Row-# corner
        tk.Label(
            self._header_inner, text="#",
            bg=C["header_bg"], fg=C["text_dim"],
            font=("Consolas", 9), width=4, pady=4,
        ).grid(row=0, column=0, sticky="nsew", padx=(0, 1), pady=(0, 1))

        for ci, h in enumerate(self.headers):
            fg = C["text_muted"]
            tk.Label(
                self._header_inner, text=h.upper(),
                bg=C["header_bg"], fg=fg,
                font=("Consolas", 9, "bold"), anchor=W,
                padx=8, pady=4, relief=FLAT,
                width=self._col_widths[ci] // 8,
            ).grid(row=0, column=ci + 1, sticky="nsew", padx=(0, 1), pady=(0, 1))

        self._header_inner.update_idletasks()
        self._header_canvas.configure(
            scrollregion=self._header_canvas.bbox("all")
        )
        # Separator line under header
        tk.Frame(self._header_frame, bg=C["border"], height=1).pack(fill=X)

        # ── Data rows in scrollable _inner ────────────────────────────────────
        for ri, row in enumerate(self.rows):
            self._cells.append(self._build_row_widgets(ri, row))

        self._update_scrollregion()

    def _build_row_widgets(self, ri: int, row: dict) -> list:
        """Create and grid all widgets for a single data row. Returns the cell list."""
        bg_row = C["surface"] if ri % 2 == 0 else C["row_alt"]
        row_cells = []

        # Row number label
        tk.Label(
            self._inner, text=str(ri + 1),
            bg=C["header_bg"], fg=C["text_dim"],
            font=("Consolas", 9), width=4, pady=0,
        ).grid(row=ri, column=0, sticky="nsew", padx=(0, 1), pady=(0, 1))

        for ci, h in enumerate(self.headers):
            val     = row.get(h, "")
            is_guid = (ci == 0)
            bg = C["guid_col"] if is_guid else bg_row
            fg = C["text_muted"] if is_guid else C["text"]

            var   = tk.StringVar(value=val)
            entry = tk.Entry(
                self._inner,
                textvariable=var,
                bg=bg, fg=fg,
                insertbackground=C["accent"],
                relief=FLAT,
                font=("Consolas", 10 if not is_guid else 9),
                width=self._col_widths[ci] // 8,
                disabledbackground=bg,
                disabledforeground=C["text_muted"],
                readonlybackground=C["guid_col"],
            )
            if is_guid:
                entry.config(state="readonly")

            entry.grid(row=ri, column=ci + 1, sticky="nsew",
                       padx=(0, 1), pady=(0, 1))
            entry._row = ri
            entry._col = ci
            entry._var = var

            if not is_guid:
                var.trace_add(
                    "write",
                    lambda *_, r=ri, c=ci, v=var, hh=h: self._on_edit(r, hh, v),
                )
                entry.bind("<FocusIn>",  lambda e, w=entry: self._on_focus_in(w))
                entry.bind("<FocusOut>", lambda e, w=entry: self._on_focus_out(w))
                entry.bind("<KeyPress>", lambda e, r=ri, c=ci: self._on_keypress(e, r, c))

            row_cells.append(entry)
        return row_cells

    def _update_scrollregion(self):
        self._inner.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._hsb.set(*self._canvas.xview())

    def _scroll_to_bottom(self):
        self._canvas.yview_moveto(1.0)

    def autosize_columns(self):
        """
        Resize every column to fit its widest content (header or any cell value).
        Triggered by Ctrl+A.
        """
        if not self.headers:
            return

        CHAR_W   = 8    # approximate pixels per character in Consolas 10
        PAD      = 16   # extra padding per cell
        MIN_W    = MIN_COL_W
        MAX_W    = 600

        # Measure widest content per column (in characters)
        new_widths = []
        for ci, h in enumerate(self.headers):
            # Start with the header label width
            max_chars = len(h)
            for row in self.rows:
                val_len = len(str(row.get(h, "") or ""))
                if val_len > max_chars:
                    max_chars = val_len
            pixel_w = max(MIN_W, min(MAX_W, max_chars * CHAR_W + PAD))
            new_widths.append(pixel_w)

        self._col_widths = new_widths
        self._apply_col_widths()

    def _apply_col_widths(self):
        """Push updated column widths to all existing widgets without rebuilding."""
        # Header labels
        try:
            header_widgets = self._header_inner.grid_slaves()
            # grid_slaves returns in reverse order; sort by column
            header_widgets.sort(key=lambda w: int(w.grid_info().get("column", 0)))
            for w in header_widgets:
                col = int(w.grid_info().get("column", 0))
                if col == 0:
                    continue  # row-number corner
                ci = col - 1
                if ci < len(self._col_widths):
                    w.config(width=self._col_widths[ci] // 8)
        except Exception:
            pass

        # Data cells
        for ri, row_cells in enumerate(self._cells):
            for ci, widget in enumerate(row_cells):
                if ci < len(self._col_widths):
                    try:
                        widget.config(width=self._col_widths[ci] // 8)
                    except Exception:
                        pass

        self._update_scrollregion()
        try:
            self._header_inner.update_idletasks()
            self._header_canvas.configure(
                scrollregion=self._header_canvas.bbox("all")
            )
        except Exception:
            pass

    def _focus_cell(self, row_i: int, col_i: int):
        """Focus cell at data-row row_i, column col_i (0=GUID, skip it)."""
        col_i = max(1, col_i)
        # _cells[ri][ci] — no header row offset any more
        if row_i < len(self._cells) and col_i < len(self._cells[row_i]):
            w = self._cells[row_i][col_i]
            if isinstance(w, tk.Entry) and str(w.cget("state")) != "readonly":
                w.focus_set()
                w.icursor(END)
                self._scroll_to_cell(w)

    def _scroll_to_cell(self, widget):
        self._inner.update_idletasks()
        x = widget.winfo_x()
        y = widget.winfo_y()
        w = widget.winfo_width()
        h = widget.winfo_height()
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        total_w = self._inner.winfo_width()
        total_h = self._inner.winfo_height()
        if total_h > 0:
            self._canvas.yview_moveto(max(0, (y - ch // 2) / total_h))
        if total_w > 0:
            self._canvas.xview_moveto(max(0, (x - cw // 2) / total_w))

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_edit(self, row_i: int, header: str, var: tk.StringVar):
        self.rows[row_i][header] = var.get()
        self._mark_dirty()

    def _on_focus_in(self, widget):
        if str(widget.cget("state")) == "readonly":
            return
        widget.config(bg=C["panel"], relief="solid",
                      highlightbackground=C["accent"], highlightthickness=1)

    def _on_focus_out(self, widget):
        ri = widget._row
        bg = C["surface"] if ri % 2 == 0 else C["row_alt"]
        widget.config(bg=bg, relief=FLAT, highlightthickness=0)

    def _on_inner_configure(self, event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._hsb.set(*self._canvas.xview())
        try:
            self._header_canvas.configure(
                scrollregion=self._header_canvas.bbox("all")
            )
        except Exception:
            pass

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        # Keep header scroll position in sync on resize
        try:
            self._header_canvas.xview_moveto(self._canvas.xview()[0])
        except Exception:
            pass

    def _on_keypress(self, event, row_i: int, col_i: int):
        key  = event.keysym
        ctrl = (event.state & 0x4) != 0

        editable = [i for i in range(len(self.headers)) if i != 0]
        n_rows   = len(self.rows)
        n_cols   = len(editable)

        def go(r, c):
            self.after(0, lambda: self._focus_cell(r, c))

        # ── Ctrl+Down — append cloned row at bottom ──────────────────────────
        if ctrl and key == "Down":
            self.add_row(clone_last=True)
            return "break"

        # ── Ctrl+Up — jump to first row ──────────────────────────────────────
        if ctrl and key == "Up":
            go(0, col_i)
            return "break"

        # ── Ctrl+D — copy from cell above ────────────────────────────────────
        if ctrl and key == "d":
            if row_i > 0:
                h   = self.headers[col_i]
                val = self.rows[row_i - 1].get(h, "")
                self.rows[row_i][h] = val
                # Update the entry widget directly
                w = self._cells[row_i][col_i]
                w._var.set(val)
                self._mark_dirty()
            return "break"

        # ── Arrow navigation ─────────────────────────────────────────────────
        pos = editable.index(col_i) if col_i in editable else 0

        if key == "Up":
            if row_i > 0: go(row_i - 1, col_i)
            return "break"
        if key == "Down":
            if row_i < n_rows - 1: go(row_i + 1, col_i)
            return "break"
        if key == "Left":
            if pos > 0:
                go(row_i, editable[pos - 1])
            elif row_i > 0:
                go(row_i - 1, editable[-1])
            return "break"
        if key == "Right":
            if pos < n_cols - 1:
                go(row_i, editable[pos + 1])
            elif row_i < n_rows - 1:
                go(row_i + 1, editable[0])
            return "break"
        if key == "Return":
            if row_i < n_rows - 1: go(row_i + 1, col_i)
            return "break"
        if key == "Tab":
            if pos < n_cols - 1:
                go(row_i, editable[pos + 1])
            elif row_i < n_rows - 1:
                go(row_i + 1, editable[0])
            return "break"

    def _mark_dirty(self):
        self._dirty = True
        self.on_dirty()

    def _on_mousewheel(self, event):
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


# ── Main application ──────────────────────────────────────────────────────────

class AltiumCSVLibrarian(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Altium CSV Librarian")
        self.geometry("1280x780")
        self.minsize(800, 500)
        self.configure(bg=C["bg"])

        # State
        self._folder: Path | None    = None
        self._files: dict            = {}   # name → {headers, rows, dirty, path}
        self._active: str | None     = None
        self._autosave_job           = None
        self._autosave_tick_job      = None
        self._autosave_deadline: float = 0

        self._build_styles()
        self._build_ui()
        self._update_status("No folder open", "muted")

        # Auto-load CSVs from the directory the script lives in
        self.after(0, self._load_script_folder)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",
            background=C["bg"], foreground=C["text"],
            troughcolor=C["panel"], bordercolor=C["border"],
            relief=FLAT, font=("Segoe UI", 10),
        )
        s.configure("Grid.TFrame", background=C["bg"])
        s.configure("Sidebar.TFrame", background=C["surface"])
        s.configure("Toolbar.TFrame", background=C["surface"])
        s.configure("TScrollbar",
            background=C["panel"], troughcolor=C["bg"],
            bordercolor=C["border"], arrowcolor=C["text_muted"],
        )
        s.configure("Accent.TButton",
            background=C["accent"], foreground=C["white"],
            font=("Segoe UI", 10, "bold"), relief=FLAT, padding=(10, 6),
        )
        s.map("Accent.TButton",
            background=[("active", "#3a72d8"), ("pressed", "#3a72d8")],
        )
        s.configure("Ghost.TButton",
            background=C["surface"], foreground=C["text_muted"],
            font=("Segoe UI", 9), relief=FLAT, padding=(8, 5),
            bordercolor=C["border"],
        )
        s.map("Ghost.TButton",
            background=[("active", C["panel"])],
            foreground=[("active", C["text"])],
        )
        s.configure("Success.TButton",
            background="#1a3a2a", foreground=C["success"],
            font=("Segoe UI", 9), relief=FLAT, padding=(8, 5),
        )
        s.map("Success.TButton",
            background=[("active", "#1f4a35")],
        )
        s.configure("Danger.TButton",
            background=C["surface"], foreground=C["danger"],
            font=("Segoe UI", 9), relief=FLAT, padding=(8, 5),
        )
        s.map("Danger.TButton",
            background=[("active", "#2a1a1a")],
        )

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Brand banner ─────────────────────────────────────────────────────
        self._banner = tk.Frame(self, bg="#000000", height=64)
        self._banner.pack(side=TOP, fill=X)
        self._banner.pack_propagate(False)
        self._banner_img_ref = None  # keep PhotoImage alive
        self._banner_lbl = tk.Label(
            self._banner,
            text="Altium CSV Librarian",
            font=("Arial", 18, "bold"),
            fg=C["gold"], bg="#000000",
            padx=22,
        )
        self._banner_lbl.pack(side=LEFT, fill=Y)

        # ── Status / toolbar bar ─────────────────────────────────────────────
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", height=40)
        toolbar.pack(side=TOP, fill=X)
        toolbar.pack_propagate(False)

        self._folder_lbl = tk.Label(
            toolbar, text="No folder open",
            bg=C["panel"], fg=C["text_muted"],
            font=("Consolas", 9), padx=10, pady=4,
            relief=FLAT,
        )
        self._folder_lbl.pack(side=LEFT, padx=(12, 0), pady=6)

        self._status_lbl = tk.Label(
            toolbar, text="No file open",
            bg=C["panel"], fg=C["text_muted"],
            font=("Consolas", 9), padx=10, pady=4,
            relief=FLAT, width=22, anchor="center",
        )
        self._status_lbl.pack(side=RIGHT, padx=(0, 12), pady=6)

        # ── Workspace (sidebar + main) ────────────────────────────────────────
        workspace = tk.Frame(self, bg=C["bg"])
        workspace.pack(side=TOP, fill=BOTH, expand=True)

        # Sidebar
        sidebar = tk.Frame(workspace, bg=C["surface"], width=220)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        tk.Label(
            sidebar, text="CSV FILES",
            bg=C["surface"], fg=C["text_muted"],
            font=("Segoe UI", 8, "bold"), pady=10, padx=16, anchor=W,
        ).pack(fill=X)
        tk.Frame(sidebar, bg=C["border"], height=1).pack(fill=X)

        btn_area = tk.Frame(sidebar, bg=C["surface"], pady=8, padx=10)
        btn_area.pack(fill=X)

        ttk.Button(
            btn_area, text="📂  Open Folder",
            style="Accent.TButton", command=self._open_folder,
        ).pack(fill=X, pady=(0, 4))

        tk.Frame(sidebar, bg=C["border"], height=1).pack(fill=X)

        # File list
        self._file_listbox = tk.Listbox(
            sidebar,
            bg=C["surface"], fg=C["text_muted"],
            selectbackground=C["panel"], selectforeground=C["accent"],
            font=("Consolas", 10), relief=FLAT, borderwidth=0,
            activestyle="none", highlightthickness=0,
        )
        self._file_listbox.pack(fill=BOTH, expand=True, pady=4)
        self._file_listbox.bind("<<ListboxSelect>>", self._on_file_select)

        # Separator
        tk.Frame(workspace, bg=C["border"], width=1).pack(side=LEFT, fill=Y)

        # Main area
        main = tk.Frame(workspace, bg=C["bg"])
        main.pack(side=LEFT, fill=BOTH, expand=True)

        # Toolbar
        self._grid_toolbar = tk.Frame(main, bg=C["surface"], height=44)
        self._grid_toolbar.pack(side=TOP, fill=X)
        self._grid_toolbar.pack_propagate(False)
        tk.Frame(main, bg=C["border"], height=1).pack(side=TOP, fill=X)

        self._file_title_lbl = tk.Label(
            self._grid_toolbar, text="",
            bg=C["surface"], fg=C["text"],
            font=("Consolas", 11, "bold"), padx=12,
        )
        self._file_title_lbl.pack(side=LEFT, fill=Y)

        self._row_count_lbl = tk.Label(
            self._grid_toolbar, text="",
            bg=C["panel"], fg=C["text_dim"],
            font=("Consolas", 9), padx=8, pady=2, relief=FLAT,
        )
        self._row_count_lbl.pack(side=LEFT, padx=4, pady=10)

        # Right-side toolbar buttons
        ttk.Button(
            self._grid_toolbar, text="✕  Remove",
            style="Danger.TButton", command=self._remove_file,
        ).pack(side=RIGHT, padx=(0, 10), pady=8)

        ttk.Button(
            self._grid_toolbar, text="💾  Save",
            style="Success.TButton", command=self._save_active,
        ).pack(side=RIGHT, padx=(0, 4), pady=8)

        ttk.Button(
            self._grid_toolbar, text="＋  Add Row",
            style="Ghost.TButton", command=self._add_row,
        ).pack(side=RIGHT, padx=(0, 4), pady=8)

        # Empty state
        self._empty_frame = tk.Frame(main, bg=C["bg"])
        self._empty_frame.pack(fill=BOTH, expand=True)
        tk.Label(
            self._empty_frame,
            text="🗂",
            font=("Segoe UI", 36),
            bg=C["bg"], fg=C["text_dim"],
        ).pack(pady=(80, 8))
        tk.Label(
            self._empty_frame,
            text="No file selected",
            font=("Segoe UI", 14),
            bg=C["bg"], fg=C["text_muted"],
        ).pack()
        tk.Label(
            self._empty_frame,
            text="Open a folder using the panel on the left,\nthen click a file to start editing.",
            font=("Segoe UI", 10),
            bg=C["bg"], fg=C["text_dim"],
            justify="center",
        ).pack(pady=8)

        # Grid
        self._grid = CsvGrid(main, on_dirty=self._on_grid_dirty)
        # not packed yet — shown when file opened

    # ── Backdrop image ────────────────────────────────────────────────────────

    def _try_load_backdrop(self, folder: Path):
        path = folder / "backdrop.PNG"
        if not path.exists():
            path = folder / "backdrop.png"
        if not path.exists():
            return
        if not PIL_AVAILABLE:
            return
        try:
            img  = Image.open(path)
            img  = img.resize((self.winfo_width() or 1280, 64), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._banner_img_ref = photo
            self._banner.config(image=photo)     # type: ignore[arg-type]
            # tkinter Frame doesn't support image natively — use a Canvas trick
            self._paint_banner(photo)
        except Exception:
            pass

    def _paint_banner(self, photo):
        """Replace banner Label background with the image using a Canvas."""
        for w in self._banner.winfo_children():
            w.destroy()
        canvas = tk.Canvas(self._banner, highlightthickness=0, bd=0)
        canvas.pack(fill=BOTH, expand=True)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        # Dark gradient overlay (simulate via semi-transparent rectangle using stipple)
        canvas.create_rectangle(0, 0, 700, 64, fill="#000000", stipple="gray50", outline="")
        canvas.create_text(
            22, 32,
            text="Altium CSV Librarian",
            font=("Arial", 18, "bold"),
            fill=C["gold"],
            anchor=W,
        )

    @staticmethod
    def _inject_guids(headers: list[str], rows: list[dict]) -> bool:
        """
        Inspect every row's first-column value. If blank, fill it with a new GUID.
        Returns True if any GUIDs were injected, False if all rows were already populated.
        """
        if not headers:
            return False
        guid_field = headers[0]
        injected = False
        for row in rows:
            if not str(row.get(guid_field, "")).strip():
                row[guid_field] = new_guid()
                injected = True
        return injected

    # ── Folder open ───────────────────────────────────────────────────────────

    def _load_script_folder(self):
        """Called once on startup — load CSVs from the script's own directory."""
        script_dir = Path(sys.argv[0]).resolve().parent
        self._load_folder(script_dir, prompted=False)

    def _open_folder(self):
        """Open-Folder button — let the user pick any directory."""
        folder = filedialog.askdirectory(title="Select CSV folder")
        if not folder:
            return
        self._load_folder(Path(folder), prompted=True)

    def _load_folder(self, folder: Path, prompted: bool = False):
        self._folder = folder
        self._folder_lbl.config(text=str(folder), fg=C["text_muted"])
        self._files  = {}
        self._active = None
        self._try_load_backdrop(folder)

        csv_files = sorted(folder.glob("*.csv"))
        patched: list[str] = []   # names of files that had GUIDs injected
        for p in csv_files:
            try:
                headers, rows = read_csv(p)
                injected = self._inject_guids(headers, rows)
                self._files[p.name] = {
                    "headers": headers, "rows": rows, "dirty": False, "path": p
                }
                if injected:
                    # Save immediately — make a backup of the original first
                    make_backup(p)
                    write_csv(p, headers, rows)
                    patched.append(p.name)
            except Exception as exc:
                print(f"Could not load {p.name}: {exc}")

        self._refresh_file_list()
        if self._files:
            first = next(iter(self._files))
            self._open_file(first)
            if patched:
                summary = ", ".join(patched)
                self._update_status(
                    f"GUIDs added & saved: {len(patched)} file(s)", "saved"
                )
                print(f"GUIDs injected and saved: {summary}")
            else:
                self._update_status(f"Loaded {len(self._files)} file(s)", "saved")
        else:
            self._show_empty()
            if prompted:
                self._update_status("No CSV files found", "muted")
            else:
                self._update_status("No folder open", "muted")

    # ── File management ───────────────────────────────────────────────────────

    def _refresh_file_list(self):
        self._file_listbox.delete(0, END)
        for name, info in self._files.items():
            marker = " ●" if info["dirty"] else ""
            self._file_listbox.insert(END, f"  {name}{marker}")
        # Re-highlight active
        names = list(self._files.keys())
        if self._active and self._active in names:
            idx = names.index(self._active)
            self._file_listbox.selection_clear(0, END)
            self._file_listbox.selection_set(idx)

    def _on_file_select(self, event):
        sel = self._file_listbox.curselection()
        if not sel:
            return
        name = list(self._files.keys())[sel[0]]
        self._open_file(name)

    def _open_file(self, name: str):
        if name not in self._files:
            return

        f = self._files[name]
        path: Path = f["path"]

        # Re-read from disk every time the file is selected
        try:
            headers, rows = read_csv(path)
        except Exception as exc:
            messagebox.showerror("Read error", f"Could not read {name}:\n{exc}")
            return

        # GUID check — inject into any blank first-column cells
        injected = self._inject_guids(headers, rows)
        if injected:
            try:
                make_backup(path)
                write_csv(path, headers, rows)
                self._update_status(f"GUIDs added & saved: {name}", "saved")
            except Exception as exc:
                messagebox.showerror("Save error", f"Could not save {name}:\n{exc}")

        # Update state with freshly read (and possibly patched) data
        f["headers"] = headers
        f["rows"]    = rows
        f["dirty"]   = False

        self._active = name
        self._grid.load(headers, rows)
        self._show_grid()
        self._file_title_lbl.config(text=name)
        self._update_row_count()
        self._refresh_file_list()
        self._update_status_from_dirty(False)

    def _show_grid(self):
        self._empty_frame.pack_forget()
        self._grid.pack(fill=BOTH, expand=True)

    def _show_empty(self):
        self._grid.pack_forget()
        self._empty_frame.pack(fill=BOTH, expand=True)
        self._file_title_lbl.config(text="")
        self._row_count_lbl.config(text="")

    def _update_row_count(self):
        if self._active:
            n = len(self._files[self._active]["rows"])
            self._row_count_lbl.config(text=f"{n} row{'s' if n != 1 else ''}")

    def _remove_file(self):
        if not self._active:
            return
        f = self._files[self._active]
        if f["dirty"]:
            if not messagebox.askyesno(
                "Unsaved changes",
                f'"{self._active}" has unsaved changes. Remove anyway?'
            ):
                return
        del self._files[self._active]
        names = list(self._files.keys())
        self._active = names[-1] if names else None
        self._refresh_file_list()
        if self._active:
            self._open_file(self._active)
        else:
            self._show_empty()
            self._update_status("No file open", "muted")

    # ── Row operations ────────────────────────────────────────────────────────

    def _add_row(self):
        """Add a blank row at the end (toolbar button)."""
        if not self._active:
            return
        self._grid.add_row(clone_last=False)

    # ── Dirty / autosave ──────────────────────────────────────────────────────

    def _on_grid_dirty(self):
        if not self._active:
            return
        # Sync grid data back into state
        headers, rows = self._grid.get_data()
        self._files[self._active]["headers"] = headers
        self._files[self._active]["rows"]    = rows
        self._files[self._active]["dirty"]   = True
        self._update_row_count()
        self._refresh_file_list()
        self._update_status_from_dirty(True)
        self._schedule_autosave()

    def _update_status(self, msg: str, kind: str = "muted"):
        colours = {
            "muted":  C["text_muted"],
            "saved":  C["success"],
            "unsaved": C["warning"],
            "danger": C["danger"],
        }
        self._status_lbl.config(text=msg, fg=colours.get(kind, C["text_muted"]))

    def _update_status_from_dirty(self, dirty: bool):
        if dirty:
            self._update_status("Unsaved changes", "unsaved")
        else:
            self._update_status("All saved", "saved")

    def _schedule_autosave(self):
        self._cancel_autosave()
        self._autosave_deadline = _time() + AUTOSAVE_SECS
        self._tick_autosave()

    def _tick_autosave(self):
        remaining = int(self._autosave_deadline - _time())
        if remaining > 0:
            # Update status if a dirty file is open
            if self._active and self._files.get(self._active, {}).get("dirty"):
                self._update_status(f"Auto-save in {remaining}s", "unsaved")
            self._autosave_tick_job = self.after(1000, self._tick_autosave)
        else:
            self._do_autosave()

    def _cancel_autosave(self):
        if self._autosave_job:
            self.after_cancel(self._autosave_job)
            self._autosave_job = None
        if self._autosave_tick_job:
            self.after_cancel(self._autosave_tick_job)
            self._autosave_tick_job = None

    def _do_autosave(self):
        dirty = [n for n, f in self._files.items() if f["dirty"]]
        for name in dirty:
            self._save_file(name, silent=True)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_active(self):
        if self._active:
            self._save_file(self._active, silent=False)

    def _save_file(self, name: str, silent: bool = False):
        f = self._files.get(name)
        if not f:
            return
        path: Path = f["path"]
        try:
            make_backup(path)
            write_csv(path, f["headers"], f["rows"])
            f["dirty"] = False
            self._cancel_autosave()
            self._refresh_file_list()
            if self._active == name:
                self._update_status_from_dirty(False)
            if not silent:
                self._update_status(f"Saved {name}", "saved")
            else:
                self._update_status(f"Auto-saved {name}", "saved")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))


# ── Entry point ───────────────────────────────────────────────────────────────

def _time() -> float:
    import time
    return time.monotonic()


def main():
    app = AltiumCSVLibrarian()
    app.mainloop()


if __name__ == "__main__":
    main()
