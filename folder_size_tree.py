"""
Folder Size Explorer
---------------------
A single-file tkinter app (standard library only) to find which folders and
files are eating your disk space.

Layout (sidebar app, inspired by tools like TreeSize):
    - Disk View     : pick a folder, see an expandable size tree + a
                      "treemap" strip of the biggest items at the current
                      level + a status bar with scan time / item count /
                      total size.
    - File Types    : groups every file by extension; click a type to
                      drill into its files. Files can be multi-selected and
                      deleted (see below).
    - Cleanup       : the cleanup tools, as sub-tabs (CleanupPanel):
        - Old Files     : recursively scans every file under the folder
                          chosen in Disk View and lists the oldest ones
                          first. Same select-and-delete support.
        - Temp Files    : finds known junk/temp files (.tmp, .bak, .log,
                          Thumbs.db, Office lock files, etc.) at least a
                          day old. Same select-and-delete support.
        - Empty Folders : finds folders whose entire subtree holds zero
                          files. Deleted directly (no review folder) since
                          there's nothing to lose, with a live re-check
                          right before each delete.
        - Leftovers     : (Windows) finds top-level folders in AppData,
                          ProgramData and Program Files that no installed
                          program accounts for — what uninstalled programs
                          left behind. Selected folders move whole into the
                          review folder; admin-only ones can be moved after
                          "Restart as Administrator". Folders that aren't
                          leftovers can be hidden from future scans.
        - Duplicates    : finds true duplicate files (size -> partial hash
                          -> full/sampled hash funnel). Files over 1GB are
                          only sample-checked and flagged "Likely" rather
                          than confirmed. Same select-and-delete support,
                          plus a bulk "keep oldest" convenience action for
                          confirmed groups.
    - Review Folder : lists everything currently sitting in the review
                      folders, with Restore and Delete Permanently actions.
    - Settings      : shows the current scanned folder and review folder
                      locations, and manages the Leftovers exceptions.

Deleting files: nothing is ever silently, permanently deleted by the app.
Selected files are moved into a review folder on their own drive (the one
in your home directory for the home drive, "<drive>:\\FolderSizeExplorer_Review"
for any other), so a move is an instant rename instead of copying the whole
file across drives. Each file is renamed to encode its original full path
(so files from different folders never collide) with a short hash suffix
for extra safety. A small sidecar ".meta" file is written next to each one
recording its original location, size, and when it was moved — this is
what makes Restore possible later, even across app restarts. Moves,
restores and permanent deletes run on a worker thread behind a progress
window, so the app stays responsive. Review folders are excluded from all scans so they never get
re-analyzed as regular data.

Run:
    python folder_size_tree.py
"""

import os
import re
import sys
import stat
import time
import json
import errno
import ctypes
import shutil
import hashlib
import functools
import datetime
import unicodedata
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import winreg  # Windows only: the Leftovers tab reads the installed-programs list from it
except ImportError:
    winreg = None


# Name of the folder deleted files get moved into: in the user's home
# directory for the home drive, and at the root of any other drive (see
# review_folder_for). Excluded from all scans by name, wherever it is, so it
# never gets re-analyzed as regular data.
REVIEW_FOLDER_NAME = "FolderSizeExplorer_Review"


def get_review_folder_path():
    """Absolute path to the main review folder, in the home directory."""
    return os.path.join(os.path.expanduser("~"), REVIEW_FOLDER_NAME)


# Kept inside the main review folder: the review folders the app has made on
# other drives, so the Review Folder tab can list them again after a restart.
REVIEW_FOLDERS_FILE = ".review_folders.json"


@functools.lru_cache(maxsize=4096)
def _volume_root(folder):
    """The mount point (the drive root on Windows) of the volume holding `folder`."""
    folder = os.path.abspath(folder)
    while not os.path.ismount(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return folder


def review_folder_for(path):
    """
    The review folder a file is moved into: the one on its own drive, so the
    move is a rename (instant at any size) instead of a full copy across
    drives. Files on the home drive use the main folder in the home directory.
    """
    volume = _volume_root(os.path.dirname(os.path.abspath(path)))
    home_review = get_review_folder_path()
    home_volume = _volume_root(os.path.dirname(home_review))
    if os.path.normcase(volume) == os.path.normcase(home_volume):
        return home_review
    return os.path.join(volume, REVIEW_FOLDER_NAME)


def _read_app_list(filename):
    """A JSON list of strings the app keeps in the main review folder ([] if missing or unreadable)."""
    try:
        with open(os.path.join(get_review_folder_path(), filename), "r", encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    return [p for p in items if isinstance(p, str)] if isinstance(items, list) else []


def _write_app_list(filename, items):
    """Save a list of strings for _read_app_list(); raises OSError if it can't."""
    os.makedirs(get_review_folder_path(), exist_ok=True)
    with open(os.path.join(get_review_folder_path(), filename), "w", encoding="utf-8") as f:
        json.dump(list(items), f, indent=1)


def _remembered_review_folders():
    return _read_app_list(REVIEW_FOLDERS_FILE)


def _remember_review_folder(folder):
    folders = _remembered_review_folders()
    if any(os.path.normcase(p) == os.path.normcase(folder) for p in folders):
        return
    try:
        _write_app_list(REVIEW_FOLDERS_FILE, folders + [folder])
    except OSError:
        pass  # files still moved fine; that folder just won't be listed until this works


def get_review_folders():
    """
    Every review folder to list: the main one first, then those on other
    drives. A drive that isn't plugged in right now is skipped, not forgotten.
    """
    folders = [get_review_folder_path()]
    for p in _remembered_review_folders():
        if os.path.isdir(p) and all(os.path.normcase(p) != os.path.normcase(q) for q in folders):
            folders.append(p)
    return folders


def _prepare_review_folder(folder):
    """
    Create `folder` and return the folder to actually use. If another drive's
    root can't be written to, the main folder is used instead: the move then
    becomes a copy across drives (slow), but it still works.
    """
    home_review = get_review_folder_path()
    if folder != home_review:
        try:
            os.makedirs(folder, exist_ok=True)
            _remember_review_folder(folder)
            return folder
        except OSError:
            pass
    os.makedirs(home_review, exist_ok=True)
    return home_review


def _is_cross_device(error):
    return error.errno == errno.EXDEV or getattr(error, "winerror", None) == 17  # ERROR_NOT_SAME_DEVICE


def _move_file(src, dst):
    """
    Move one file or folder. On the same drive this is a rename. Only a file
    is ever copied across drives (then the original deleted), and if the
    original can't be deleted (it's open in another program) the copy is
    removed again, so a failed move never leaves a stray duplicate behind.
    A folder is never copied across drives — a copy that fails half-way
    through deleting the original would leave both halves broken — so that
    move is refused instead. Something in use on the same drive fails
    straight away instead of being copied first (which is what shutil.move
    would do).
    """
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if not _is_cross_device(e):
            raise
    if os.path.isdir(src):
        raise OSError(errno.EXDEV, "Folders are only moved within their own drive, and this drive has no review folder")
    shutil.copy2(src, dst)
    try:
        os.remove(src)
    except OSError:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def human_size(num_bytes) -> str:
    """Format a byte count as a human-readable string."""
    if num_bytes is None:
        return ""
    n = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(n) < 1024.0:
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} EB"


def open_in_file_manager(path):
    """Open the given path's containing folder in Explorer / Finder / etc."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as e:
        messagebox.showerror("Couldn't open folder", str(e))


def _unique_path(path):
    """If `path` already exists, append (1), (2), ... before the extension."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def flatten_path_for_review(path):
    """
    Turn an absolute path into a single safe filename that encodes its
    original location, so files from different folders never collide in
    a review folder and you can always tell where one came
    from, e.g.:
        C:\\Users\\John\\Downloads\\notes.txt -> C_Users_John_Downloads_notes_a1b2c3d4.txt

    A short hash of the full original path is appended so that even an
    original folder name containing underscores can never be confused with
    a path separator — the sidecar .meta file is still the actual source of
    truth for restoring, this is just belt-and-suspenders uniqueness.
    """
    abs_path = os.path.abspath(path)
    drive, rest = os.path.splitdrive(abs_path)
    drive = drive.rstrip(":\\/")
    parts = [p for p in re.split(r"[\\/]+", rest) if p]
    if drive:
        parts.insert(0, drive)
    combined = "_".join(parts) if parts else os.path.basename(abs_path)
    combined = re.sub(r'[<>:"|?*]', "_", combined)

    digest = hashlib.sha1(abs_path.encode("utf-8", errors="ignore")).hexdigest()[:8]
    base, ext = os.path.splitext(combined)
    return f"{base}_{digest}{ext}"


def _meta_path_for(target_file_path):
    return target_file_path + ".meta"


def _write_sidecar(target_file_path, original_path, size, is_folder=False):
    meta = {
        "original_path": os.path.abspath(original_path),
        "size": size,
        "deleted_at": time.time(),
    }
    if is_folder:
        meta["is_folder"] = True  # older sidecars have no key: they're files
    try:
        with open(_meta_path_for(target_file_path), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception:
        pass  # file still moved fine; it'll just show as "can't be restored"


def _read_sidecar(target_file_path):
    """Returns the sidecar dict, or None if missing/corrupted."""
    meta_path = _meta_path_for(target_file_path)
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def delete_files(paths, progress=None, cancel=None):
    """
    Move each file or folder in `paths` into the review folder on its own
    drive (see review_folder_for), giving it a flattened+hashed name so
    nothing collides, and write a sidecar .meta file next to it recording
    where it came from (for Restore later). A folder moves whole, as one
    item. Nothing is permanently deleted here.

    Safe to run on a worker thread (it never touches tkinter): `progress`
    is called as progress(index, total, path) before each file, and setting
    the `cancel` event stops the batch between files. Both are optional.

    Returns (moved, failed, review_roots):
        moved        - list of (original_path, target_path) successfully moved
        failed       - list of (path, error_message) tuples
        review_roots - the review folders the moved files went into
    """
    moved = []
    failed = []
    ready = {}  # review folder wanted -> folder actually used (created once per batch)

    for i, path in enumerate(paths):
        if cancel is not None and cancel.is_set():
            break
        if progress is not None:
            progress(i, len(paths), path)
        try:
            wanted = review_folder_for(path)
            if wanted not in ready:
                ready[wanted] = _prepare_review_folder(wanted)
            is_folder = os.path.isdir(path)
            if is_folder:
                size = _dir_stats(path)[0]
            else:
                size = os.path.getsize(path) if os.path.exists(path) else None
            target = os.path.join(ready[wanted], flatten_path_for_review(path))
            target = _unique_path(target)
            _move_file(path, target)
            _write_sidecar(target, path, size, is_folder)
            moved.append((path, target))
        except Exception as e:
            failed.append((path, str(e)))

    review_roots = list(dict.fromkeys(os.path.dirname(target) for _path, target in moved))
    return moved, failed, review_roots


def _center_on_master(win, height=None):
    """
    Place a (withdrawn) window over the middle of its master, a little above
    centre, and keep its bottom edge on screen. `height` caps the window's
    height; by default it gets the height it asks for.
    """
    master = win.master
    win.update_idletasks()
    w = win.winfo_reqwidth()
    h = height or win.winfo_reqheight()
    x = master.winfo_rootx() + (master.winfo_width() - w) // 2
    y = master.winfo_rooty() + (master.winfo_height() - h) // 3
    y = min(y, win.winfo_screenheight() - h - 60)  # clear of the taskbar
    size = f"{w}x{height}" if height else ""
    win.geometry(f"{size}+{max(x, 0)}+{max(y, 0)}")


def run_with_progress(verb, work, paths):
    """
    Run work(paths, progress=..., cancel=...) on a worker thread while a
    small progress window (with Cancel) keeps the app responsive, and return
    what it returns. The wait runs the Tk event loop, so callers stay simple
    and synchronous. The window only appears if the batch takes a moment,
    so quick moves don't flash it; once shown it takes the input grab so
    nothing else can be started meanwhile.
    """
    updates = queue.Queue()
    cancel = threading.Event()
    outcome = {}

    def worker():
        try:
            outcome["result"] = work(paths, progress=lambda *u: updates.put(u), cancel=cancel)
        except Exception as e:
            outcome["error"] = e

    win = tk.Toplevel()
    win.withdraw()
    win.title(f"{verb} files")
    win.resizable(False, False)
    win.transient(win.master)

    frame = ttk.Frame(win, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    label_var = tk.StringVar(value=f"{verb}...")
    ttk.Label(frame, textvariable=label_var, width=64).pack(anchor=tk.W)
    bar = ttk.Progressbar(frame, length=420, maximum=max(len(paths), 1))
    bar.pack(fill=tk.X, pady=8)

    def stop():
        cancel.set()
        label_var.set("Stopping after the current file...")
        cancel_btn.config(state="disabled")

    cancel_btn = ttk.Button(frame, text="Cancel", command=stop)
    cancel_btn.pack(anchor=tk.E)
    win.protocol("WM_DELETE_WINDOW", stop)

    thread = threading.Thread(target=worker, daemon=True)
    started = time.time()

    def poll():
        latest = None
        while not updates.empty():
            latest = updates.get_nowait()
        if latest and not cancel.is_set():
            index, total, path = latest
            label_var.set(f"{verb} {index + 1:,} of {total:,}: {os.path.basename(path)}")
            bar["value"] = index
        if not thread.is_alive():
            win.destroy()
            return
        if win.state() == "withdrawn" and time.time() - started > 0.3:
            _center_on_master(win)
            win.deiconify()
            try:
                win.grab_set()
            except tk.TclError:
                pass  # another window holds the grab; the progress still shows
        win.after(50, poll)

    thread.start()
    win.after(50, poll)
    win.wait_window()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _plural(count, word):
    """'1 file', '2,048 files'."""
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _sum_sizes(sizes):
    """Total of the known sizes, or None if none is known."""
    known = [s for s in sizes if s is not None]
    return sum(known) if known else None


def group_items(entries, by_type=True, noun="file"):
    """
    Group items for an ItemsDialog. `entries` are (path, size) pairs, or
    (path, size, note) where the note (an error, say) is shown next to the
    item; sizes may be None. With by_type, files are grouped by type, then
    by folder: ".png — 25 files · 80 MB" > "C:\\Photos — 18 files" >
    "photo1.png". Otherwise items are grouped by the folder they're in
    (folders being moved, restore destinations). Biggest groups first.

    Returns nodes: (label, detail, size, children), children being nodes.
    """
    rows = [(e[0], e[1], e[2] if len(e) > 2 else "") for e in entries]

    def group(rows, key, make_children):
        groups = {}
        for row in rows:
            groups.setdefault(key(row[0]), []).append(row)
        nodes = []
        for label, members in groups.items():
            size = _sum_sizes(r[1] for r in members)
            nodes.append((
                (-(size or 0), -len(members), label.lower()),  # biggest first, then most items
                (label, _plural(len(members), noun), size, make_children(members)),
            ))
        return [node for _order, node in sorted(nodes, key=lambda n: n[0])]

    def items(rows):
        rows = sorted(rows, key=lambda r: (-(r[1] or 0), os.path.basename(r[0]).lower()))
        return [(os.path.basename(path) or path, note, size, []) for path, size, note in rows]

    def by_folder(rows):
        return group(rows, os.path.dirname, items)

    if not by_type:
        return by_folder(rows)
    return group(rows, lambda p: os.path.splitext(p)[1].lower() or "(no extension)", by_folder)


def result_groups(sections, noun="file"):
    """
    Nodes for a results dialog: one top-level group per (heading, entries)
    section that has entries ("Moved", "Failed", ...), each listing its
    items by folder. Entries are as for group_items(); a failure's error
    goes in as the note.
    """
    return [
        (heading, _plural(len(entries), noun), _sum_sizes(e[1] for e in entries),
         group_items(entries, by_type=False, noun=noun))
        for heading, entries in sections if entries
    ]


class ItemsDialog(tk.Toplevel):
    """
    A modal dialog for any message that lists items: a one-line summary and
    an optional note on top, the items as a collapsible tree (see
    group_items) in the middle, and the buttons at the bottom. The window is
    at most MAX_SCREEN_SHARE of the screen tall and only the tree shrinks,
    so the buttons are always on screen. Groups start collapsed (a lone
    group starts open) and a group's rows are only added when it's opened,
    so thousands of items open instantly.

    Enter presses the focused button (the first one to start with); Escape
    and the window's X count as the last one (No / OK). Use ask_items() and
    show_items() rather than this class directly.
    """

    MAX_SCREEN_SHARE = 0.7
    PLACEHOLDER = "__placeholder__"

    def __init__(self, title, summary, nodes, note="", buttons=("Yes", "No")):
        super().__init__()
        self.withdraw()
        self.title(title)
        self.transient(self.master)
        self.minsize(420, 240)
        self.choice = len(buttons) - 1
        self._pending = {}  # group row -> its child nodes, not inserted until it's opened

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        # Packed first: a capped window takes its space from what's packed last (the tree), never from here.
        button_bar = ttk.Frame(outer)
        button_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        self.buttons = [ttk.Button(button_bar, text=text, command=lambda i=i: self._close(i))
                        for i, text in enumerate(buttons)]
        for button in reversed(self.buttons):
            button.pack(side=tk.RIGHT, padx=(6, 0))

        ttk.Label(outer, text=summary, font=("Segoe UI", 10, "bold"), wraplength=620, justify=tk.LEFT).pack(
            side=tk.TOP, anchor=tk.W
        )
        if note:
            ttk.Label(outer, text=note, foreground="#555", wraplength=620, justify=tk.LEFT).pack(
                side=tk.TOP, anchor=tk.W, pady=(6, 0)
            )

        frame = ttk.Frame(outer)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(
            frame, columns=("detail", "size"), show="tree", selectmode="none",
            height=min(max(len(nodes), 4), 16),
        )
        self.tree.column("#0", width=380, minwidth=200)
        self.tree.column("detail", width=170, minwidth=80)
        self.tree.column("size", width=90, minwidth=70, anchor=tk.E, stretch=False)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self._add_nodes("", nodes)
        if len(nodes) == 1 and nodes[0][3]:
            only = self.tree.get_children()[0]
            self._fill(only)
            self.tree.item(only, open=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close(len(buttons) - 1))
        self.bind("<Escape>", lambda e: self._close(len(buttons) - 1))
        self.bind("<Return>", self._on_return)

    def run(self):
        """Show the dialog, wait for it to close, and return the index of the button pressed."""
        self.update_idletasks()
        cap = int(self.winfo_screenheight() * self.MAX_SCREEN_SHARE)
        _center_on_master(self, height=min(self.winfo_reqheight(), cap))
        self.deiconify()
        self.buttons[0].focus_set()
        try:
            self.grab_set()
        except tk.TclError:
            pass  # another window holds the grab; the dialog still works
        self.wait_window()
        return self.choice

    def _add_nodes(self, parent, nodes):
        for label, detail, size, children in nodes:
            item = self.tree.insert(parent, "end", text=label, values=(detail, human_size(size)))
            if children:
                self._pending[item] = children
                self.tree.insert(item, "end", text=self.PLACEHOLDER)  # gives the row its open arrow

    def _fill(self, item):
        children = self._pending.pop(item, None)
        if children is not None:
            self.tree.delete(*self.tree.get_children(item))
            self._add_nodes(item, children)

    def _on_click(self, event):
        """A click anywhere on a group's row opens or closes it, not just on its arrow."""
        item = self.tree.identify_row(event.y)
        if not item or not self.tree.get_children(item):
            return None
        opening = not self.tree.item(item, "open")
        if opening:
            self._fill(item)
        self.tree.item(item, open=opening)
        return "break"

    def _on_open(self, event=None):
        # Opened from the keyboard or by double-click; <<TreeviewOpen>> doesn't say which row.
        for item in [i for i in self._pending if self.tree.item(i, "open")]:
            self._fill(item)

    def _on_return(self, event):
        focused = self.focus_get()
        (focused if focused in self.buttons else self.buttons[0]).invoke()

    def _close(self, choice):
        self.choice = choice
        self.destroy()


def ask_items(title, summary, nodes, note=""):
    """Yes/No ItemsDialog: True for Yes; False for No, Escape or closing the window."""
    return ItemsDialog(title, summary, nodes, note, ("Yes", "No")).run() == 0


def show_items(title, summary, nodes, note=""):
    """ItemsDialog with just an OK button, for showing what an action did."""
    ItemsDialog(title, summary, nodes, note, ("OK",)).run()


def _size_of(path, sizes):
    """The size the tab already knows for `path`, or a file's size from disk; None for an unknown folder."""
    size = sizes.get(path)
    if size is None and not os.path.isdir(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            pass
    return size


def confirm_and_delete(paths, sizes, note=""):
    """
    Ask to move the given paths into the review folder (an ItemsDialog,
    files grouped by type and folder, folders by location), move them via
    delete_files() (on a worker thread, behind a progress window), then
    show what happened. `sizes` maps paths to the sizes the tab already
    knows; `note` adds a tab's own warning to the dialog. Returns (moved,
    failed) — where `moved` is a list of (original_path, target_path) — or
    None if the user cancelled.
    """
    all_folders = all(os.path.isdir(p) for p in paths)
    noun = "folder" if all_folders else "file"
    size_by_path = {p: _size_of(p, sizes) for p in paths}
    entries = list(size_by_path.items())
    folders = "\n".join(dict.fromkeys(review_folder_for(p) for p in paths))

    details = (
        (f"{note}\n\n" if note else "")
        + f"Location (each one stays on its own drive):\n{folders}\n\n"
        "Nothing is permanently deleted — you can restore these later, or "
        "permanently remove them yourself from the Review Folder tab."
    )
    total = _sum_sizes(size_by_path.values()) or 0
    if not ask_items(
        "Move to review folder",
        f"Move {_plural(len(paths), noun)} ({human_size(total)}) to the review folder?",
        group_items(entries, by_type=not all_folders, noun=noun),
        note=details,
    ):
        return None

    moved, failed, review_roots = run_with_progress("Moving", delete_files, paths)

    done = {orig for orig, _target in moved} | {p for p, _err in failed}
    show_items(
        "Done",
        f"Moved {len(moved):,} of {_plural(len(paths), noun)} to the review folder.",
        result_groups([
            ("Moved", [(orig, size_by_path.get(orig)) for orig, _target in moved]),
            ("Failed", [(p, size_by_path.get(p), err) for p, err in failed]),
            ("Cancelled — left where they are", [(p, size_by_path[p]) for p in paths if p not in done]),
        ], noun),
        note=(
            ("Review folders:\n" if len(review_roots) > 1 else "Review folder:\n")
            + "\n".join(review_roots) + "\n\n" if review_roots else ""
        ) + "Rescan Disk View / Old Files / Temp Files / File Types to see fully updated results.",
    )
    return moved, failed


def restore_files(review_paths, progress=None, cancel=None):
    """
    Restore each review-folder file back to its original location, read
    from its sidecar .meta. Recreates missing folders as needed and
    auto-renames on collision. Files with no valid sidecar are skipped
    (reported separately) rather than guessed at. `progress` and `cancel`
    work as in delete_files(), so this can run behind run_with_progress().

    Returns (restored, failed, skipped):
        restored - list of (review_path, restored_to_path)
        failed   - list of (review_path, error_message)
        skipped  - list of review_path with no usable sidecar info
    """
    restored = []
    failed = []
    skipped = []

    for i, path in enumerate(review_paths):
        if cancel is not None and cancel.is_set():
            break
        if progress is not None:
            progress(i, len(review_paths), path)
        meta = _read_sidecar(path)
        if not meta or not meta.get("original_path"):
            skipped.append(path)
            continue
        original_path = meta["original_path"]
        try:
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            target = _unique_path(original_path)
            _move_file(path, target)
            meta_path = _meta_path_for(path)
            if os.path.isfile(meta_path):
                os.remove(meta_path)
            restored.append((path, target))
        except Exception as e:
            failed.append((path, str(e)))

    return restored, failed, skipped


def _remove_tree(path):
    """shutil.rmtree that also removes read-only files (common in program folders)."""
    def clear_read_only(func, failed_path, _error):
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_read_only)
    else:
        shutil.rmtree(path, onerror=clear_read_only)


def delete_permanently(review_paths, progress=None, cancel=None):
    """
    Permanently delete each review-folder file or folder and its sidecar
    .meta. `progress` and `cancel` work as in delete_files(), so this can
    run behind run_with_progress().

    Returns (deleted, failed):
        deleted - list of review_path removed
        failed  - list of (review_path, error_message)
    """
    deleted = []
    failed = []

    for i, path in enumerate(review_paths):
        if cancel is not None and cancel.is_set():
            break
        if progress is not None:
            progress(i, len(review_paths), path)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                _remove_tree(path)
            else:
                os.remove(path)
            meta_path = _meta_path_for(path)
            if os.path.isfile(meta_path):
                os.remove(meta_path)
            deleted.append(path)
        except Exception as e:
            failed.append((path, str(e)))

    return deleted, failed


def _dir_stats(path):
    """Recursively compute (total_size, file_count, folder_count) for a dir."""
    total_size = 0
    file_count = 0
    folder_count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name == REVIEW_FOLDER_NAME:
                                continue
                            folder_count += 1
                            stack.append(entry.path)
                        else:
                            file_count += 1
                            try:
                                total_size += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            continue
    return total_size, file_count, folder_count


def scan_folder_sizes(path):
    """
    Scan the immediate children of `path`. Folder sizes are computed
    recursively (full subtree size). Returns a list of dicts sorted by
    size, descending.
    """
    entries = []
    try:
        with os.scandir(path) as it:
            children = list(it)
    except (PermissionError, FileNotFoundError, OSError):
        return entries

    for entry in children:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name == REVIEW_FOLDER_NAME:
                    continue
                size, file_count, folder_count = _dir_stats(entry.path)
                entries.append({
                    "name": entry.name,
                    "full_path": entry.path,
                    "is_dir": True,
                    "size": size,
                    "file_count": file_count,
                    "folder_count": folder_count,
                })
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                entries.append({
                    "name": entry.name,
                    "full_path": entry.path,
                    "is_dir": False,
                    "size": size,
                    "file_count": 0,
                    "folder_count": 0,
                })
        except (PermissionError, FileNotFoundError, OSError):
            continue

    entries.sort(key=lambda e: e["size"], reverse=True)
    return entries


def fmt_mtime(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%d-%m-%Y")


# --- Temp Files detection ---------------------------------------------------

TEMP_EXTENSIONS = {"tmp", "temp", "bak", "old", "log", "swp", "crdownload", "part"}
TEMP_EXACT_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
TEMP_MIN_AGE_SECONDS = 24 * 60 * 60  # 1 day — avoid flagging files still being written

# Old Files only lists files not modified for at least this long (label, days); picked in the tab.
OLD_FILE_AGES = [("15 days", 15), ("30 days", 30), ("6 months", 182), ("1 year", 365)]
OLD_FILE_DEFAULT_AGE = "30 days"


def is_temp_file(name):
    """True if `name` matches a known junk/temp-file pattern (by name alone,
    not age — age is checked separately by the caller)."""
    lower = name.lower()
    if lower in TEMP_EXACT_NAMES:
        return True
    if lower.startswith("~$"):  # Office lock files, e.g. ~$notes.docx
        return True
    if "." in lower:
        ext = lower.rsplit(".", 1)[-1]
        if ext in TEMP_EXTENSIONS:
            return True
    return False


# --- Empty Folder detection --------------------------------------------------

def find_empty_folders(root, stop_event=None):
    """
    Recursively find every folder under `root` whose entire subtree contains
    zero files (it may still contain nested empty folders). Bottom-up walk:
    a folder counts as empty only if it has no files directly AND every
    child folder is also empty. The review folder is excluded entirely.

    Returns a list of folder paths (deepest folders naturally appear
    alongside/after the folders they're nested under, since os.walk with
    topdown=False visits children before parents).
    """
    empty = []
    contains_data = {}  # path -> True if it or anything under it has a file

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if stop_event and stop_event.is_set():
            break
        if REVIEW_FOLDER_NAME in dirpath.split(os.sep):
            contains_data[dirpath] = True  # never touch/report inside it
            continue

        has_own_files = len(filenames) > 0
        any_child_has_data = any(
            contains_data.get(os.path.join(dirpath, d), True) for d in dirnames
        )
        is_data_free = not has_own_files and not any_child_has_data
        contains_data[dirpath] = not is_data_free

        if is_data_free and os.path.normpath(dirpath) != os.path.normpath(root):
            empty.append(dirpath)

    return empty


def filter_top_level_paths(paths):
    """
    Given a list of folder paths, drop any path that is nested inside
    another path already in the list — deleting the ancestor will already
    remove it. Returns only the outermost paths, so batch-deleting a parent
    and its already-empty child doesn't attempt to delete the child twice.
    """
    normalized = sorted({os.path.normpath(p) for p in paths}, key=len)
    kept = []
    for p in normalized:
        if not any(p == k or p.startswith(k + os.sep) for k in kept):
            kept.append(p)
    return kept


# --- Leftover detection (Windows) --------------------------------------------
#
# Finds folders that uninstalled programs left behind: a top-level folder in
# AppData, ProgramData or Program Files that no installed program accounts
# for. Matching is deliberately generous — a folder that loosely matches any
# installed program's name, publisher, install folder or exe folder counts
# as owned and isn't listed — because listing a folder that's still in use
# is far worse than missing a real leftover. Recently used folders and ones
# that look like game saves are flagged, and everything found goes through
# the review folder, so a mistake can be restored.

LEFTOVER_RECENT_DAYS = 90  # used within this many days -> probably still in use

# Folders the user marked "not a leftover" (paths as written), kept in the
# main review folder so they stay hidden in later scans. Managed from the
# Leftovers tab and from Settings.
LEFTOVER_EXCLUSIONS_FILE = ".leftover_exclusions.json"

# Windows, system, shared and driver folders: never leftovers of one program.
LEFTOVER_SKIP_NAMES = {
    "microsoft", "packages", "temp", "tmp", "common files", "internet explorer", "msbuild",
    "reference assemblies", "uninstall information", "windowsapps", "modifiablewindowsapps",
    "package cache", "packagemanagement", "softwaredistribution", "start menu", "templates",
    "desktop", "documents", "favorites", "application data", "history", "crashdumps",
    "d3dscache", "virtualstore", "isolatedstorage", "connecteddevicesplatform", "comms",
    "programs", "publishers", "placeholdertilelogofolder", "peerdistrepub", "ssh",
    "usoshared", "usoprivate", "intel", "amd", "ati", "realtek", "dolby", "fontcache",
    "squirreltemp", "dotnet", "assembly", "installer", "cache",
    # Shared by many games or apps: removing one breaks programs still installed.
    "battleye", "eaanticheat", "crashreportclient", "unrealengine", "unreal engine",
    "gameanalytics", "mod.io", "sentry", "toastnotificationmanagercompat",
}
LEFTOVER_SKIP_PREFIXES = ("windows", "microsoft", "regid.", "nvidia", "easyanticheat", "{")

# Words too common to identify a program on their own ("JAM Software" must
# match on "jam software", not on "software").
LEFTOVER_STOP_WORDS = {
    "software", "inc", "corp", "corporation", "company", "games", "game", "studio", "studios",
    "ltd", "llc", "gmbh", "the", "and", "for", "app", "apps", "data", "files", "program",
    "programs", "technologies", "technology", "limited", "group", "entertainment",
    "interactive", "digital", "systems", "media", "labs", "update", "updater", "setup",
    "installer", "service", "services", "launcher", "client", "desktop", "tools", "edition",
    "version", "free", "x64", "x86",
}

SAVE_DIR_NAMES = {"save", "saves", "saved", "savegame", "savegames", "savedata", "saved games"}
SAVE_EXTENSIONS = {".sav", ".save"}

_UNINSTALL_KEYS = (
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
)
_STORE_PACKAGES_KEY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
)


def _path_key(path):
    return os.path.normcase(os.path.normpath(path))


def load_leftover_exclusions():
    """The folders the user hid from the Leftovers tab, as {_path_key(path): path}."""
    return {_path_key(p): p for p in _read_app_list(LEFTOVER_EXCLUSIONS_FILE)}


def save_leftover_exclusions(paths):
    """Save the hidden folders, as written; raises OSError if it can't."""
    _write_app_list(LEFTOVER_EXCLUSIONS_FILE, sorted(paths, key=str.casefold))


def is_admin():
    """True when the app runs elevated (Windows); False elsewhere or if unknown."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def leftover_locations():
    """
    The places leftovers are looked for, as (label, path, needs_admin)
    tuples. needs_admin marks places where only an administrator can move
    folders. Missing places and duplicates are left out.
    """
    env = os.environ.get
    local = env("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    candidates = [
        ("AppData\\Roaming", env("APPDATA"), False),
        ("AppData\\Local", local, False),
        ("AppData\\LocalLow", os.path.join(os.path.dirname(local), "LocalLow"), False),
        ("AppData\\Local\\Programs", os.path.join(local, "Programs"), False),
        ("ProgramData", env("ProgramData"), True),
        ("Program Files", env("ProgramW6432") or env("ProgramFiles"), True),
        ("Program Files (x86)", env("ProgramFiles(x86)"), True),
    ]
    seen = set()
    locations = []
    for label, path, needs_admin in candidates:
        if not path or not os.path.isdir(path):
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            locations.append((label, path, needs_admin))
    return locations


def _plain_text(text):
    """Lower-case ASCII form of a name: ™/®/© and accents dropped."""
    text = re.sub("[\u2122\u00ae\u00a9]", " ", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.casefold()


def _squash(text):
    return re.sub(r"[^0-9a-z]+", "", _plain_text(text))


def _words(text):
    return [w for w in re.split(r"[^0-9a-z]+", _plain_text(text)) if w]


def _is_within(child, parent):
    """True if normalized path `child` is `parent` or inside it."""
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _exe_folder(command):
    """
    The folder of the program a registry command line or icon path points
    to (UninstallString, DisplayIcon), or None. Quotes, arguments and icon
    indexes like ",0" are stripped; Windows' own msiexec / rundll32 are
    ignored, since they say nothing about where the program lives.
    """
    command = (command or "").strip()
    if not command:
        return None
    if command.startswith('"'):
        exe = command[1:].split('"', 1)[0]
    else:
        cut = command.lower().find(".exe")
        exe = command[: cut + 4] if cut != -1 else command.split(",", 1)[0]
    exe = os.path.expandvars(exe.strip())
    if not os.path.isabs(exe):
        return None
    if os.path.basename(exe).lower() in ("msiexec.exe", "rundll32.exe", "cmd.exe", "powershell.exe"):
        return None
    return os.path.dirname(exe)


def _registry_value(key, name):
    try:
        value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ""
    return value if isinstance(value, str) else ""


def _registry_subkeys(key):
    try:
        count = winreg.QueryInfoKey(key)[0]
    except OSError:
        return []
    names = []
    for i in range(count):
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            break
    return names


def installed_programs(locations=None):
    """
    What the installed programs tell us about the folders they own, from the
    registry's Uninstall lists (machine-wide, 32-bit, current user) plus the
    names of installed Store apps. `locations` defaults to
    leftover_locations(). Returns a dict:
        names - display names and publishers (raw strings, for reports)
        paths - normalized install and exe folders
        squashed - squashed names (letters/digits only), 4+ characters,
                   including the folder names programs install into
        words - significant words (4+ letters, not stop words) of all names
    """
    names = []
    paths = set()
    if winreg is not None:
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        for hive_name, subkey in _UNINSTALL_KEYS:
            hive = getattr(winreg, hive_name)
            try:
                root = winreg.OpenKey(hive, subkey, 0, access)
            except OSError:
                continue
            with root:
                for child in _registry_subkeys(root):
                    try:
                        key = winreg.OpenKey(root, child, 0, access)
                    except OSError:
                        continue
                    with key:
                        display = _registry_value(key, "DisplayName")
                        if not display:
                            continue
                        names.append(display)
                        publisher = _registry_value(key, "Publisher")
                        if publisher:
                            names.append(publisher)
                        folders = [
                            os.path.expandvars(_registry_value(key, "InstallLocation").strip().strip('"')),
                            _exe_folder(_registry_value(key, "UninstallString")),
                            _exe_folder(_registry_value(key, "DisplayIcon")),
                        ]
                        for folder in folders:
                            if folder and os.path.isabs(folder):
                                paths.add(os.path.normcase(os.path.normpath(folder)))
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STORE_PACKAGES_KEY) as store:
                for package in _registry_subkeys(store):
                    names.append(package.split("_", 1)[0])  # "Publisher.App_1.2.3_x64__id" -> "Publisher.App"
        except OSError:
            pass

    if locations is None:
        locations = leftover_locations()
    roots = [os.path.normcase(os.path.normpath(p)) for _label, p, _admin in locations]
    # An install folder that is a location itself, or above one (an odd
    # InstallLocation like "C:\Program Files"), would make everything owned.
    paths = {p for p in paths if not any(_is_within(r, p) for r in roots)}

    squashed = {s for s in (_squash(n) for n in names) if len(s) >= 4}
    # The folder names programs install into are names too: Logitech G HUB
    # lives in "Program Files\LGHUB", so "ProgramData\LGHUB" is its as well,
    # and a game installed in "D:\Games\Sekiro" owns "AppData\Roaming\Sekiro".
    for p in paths:
        root = next((r for r in roots if _is_within(p, r)), None)
        parts = os.path.relpath(p, root).split(os.sep)[:2] if root else [os.path.basename(p)]
        for part in parts:
            s = _squash(part)
            if len(s) >= 4 and not s.isdigit() and not part.startswith("{"):
                squashed.add(s)

    words = {w for n in names for w in _words(n) if len(w) >= 4 and w not in LEFTOVER_STOP_WORDS}
    return {"names": names, "paths": paths, "squashed": squashed, "words": words}


def _skip_leftover_name(name):
    lower = name.casefold()
    return (
        lower in LEFTOVER_SKIP_NAMES
        or lower.startswith(LEFTOVER_SKIP_PREFIXES)
        or lower == REVIEW_FOLDER_NAME.casefold()
    )


def is_owned(path, name, programs):
    """
    True if any installed program plausibly owns the folder at `path` (named
    `name`), so it must not be listed as a leftover. Deliberately generous:
        - the folder is, or contains, an install folder or exe folder
        - its squashed name is inside a program's or publisher's squashed
          name, or the other way round ("StardewValley" ~ "Stardew Valley")
        - its first significant word is a word of a program or publisher
          ("riot-client-ux" ~ "Riot Vanguard")
        - its name is too short (under 4 letters) to match reliably
    """
    folder = os.path.normcase(os.path.normpath(path))
    if any(_is_within(p, folder) for p in programs["paths"]):
        return True
    squashed = _squash(name)
    if len(squashed) < 4:
        return True
    if any(squashed in s or s in squashed for s in programs["squashed"]):
        return True
    significant = [w for w in _words(name) if len(w) >= 4 and w not in LEFTOVER_STOP_WORDS]
    return bool(significant) and significant[0] in programs["words"]


def _is_reparse_point(st):
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _folder_usage(path, stop_event=None):
    """
    (total_size, file_count, newest_time, has_saves) for a folder tree, like
    _dir_stats() but also the newest file/folder time (when anything in it
    was last touched) and whether it looks like it holds game saves. Links
    and junctions aren't followed, and `stop_event` cuts the walk short.
    """
    total = 0
    files = 0
    newest = _safe_mtime(path) or 0
    has_saves = False
    stack = [path]
    while stack:
        if stop_event is not None and stop_event.is_set():
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, st.st_mtime)
                    lower = entry.name.casefold()
                    if entry.is_dir(follow_symlinks=False):
                        has_saves = has_saves or lower in SAVE_DIR_NAMES
                        if not _is_reparse_point(st):
                            stack.append(entry.path)
                    else:
                        files += 1
                        total += st.st_size
                        has_saves = has_saves or os.path.splitext(lower)[1] in SAVE_EXTENSIONS
        except OSError:
            continue
    return total, files, newest, has_saves


def find_leftovers(stop_event=None, progress=None):
    """
    Look through the immediate subfolders of every leftover location and
    return the ones no installed program owns, biggest first, as dicts:
    path, name, location, size, files, last_used, recent, saves, needs_admin.
    `progress(path)` is called before each candidate is measured; setting
    `stop_event` returns what was found so far. Windows only (else []).
    """
    if winreg is None:
        return []
    locations = leftover_locations()
    programs = installed_programs(locations)
    elevated = is_admin()
    recent_after = time.time() - LEFTOVER_RECENT_DAYS * 24 * 60 * 60

    found = []
    for label, root, needs_admin in locations:
        try:
            with os.scandir(root) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.is_symlink() or _is_reparse_point(entry.stat(follow_symlinks=False)):
                    continue
            except OSError:
                continue
            if _skip_leftover_name(entry.name) or is_owned(entry.path, entry.name, programs):
                continue
            if progress is not None:
                progress(entry.path)
            size, files, newest, has_saves = _folder_usage(entry.path, stop_event)
            found.append({
                "path": entry.path,
                "name": entry.name,
                "location": label,
                "size": size,
                "files": files,
                "last_used": newest,
                "recent": newest >= recent_after,
                "saves": has_saves,
                "needs_admin": needs_admin and not elevated,
            })
    found.sort(key=lambda r: r["size"], reverse=True)
    return found


# --- Duplicate detection -----------------------------------------------------
#
# Three-stage funnel, cheapest checks first, so the expensive full read only
# ever happens for files that are already extremely likely to be true
# duplicates:
#   1. Group by exact size (free — metadata only). Singleton sizes drop out.
#   2. Partial hash of the first 8KB. Files that differ here can't match;
#      singletons drop out again.
#   3. For files up to 1GB: full-content hash to confirm true duplicates.
#      For files over 1GB: hashing the whole thing is expensive, so instead
#      sample small chunks from the start, middle, and end. A match there is
#      reported as "likely" rather than confirmed, and the UI requires an
#      explicit acknowledgment before deleting anything from such a group.

PARTIAL_HASH_BYTES = 8192
SAMPLE_CHUNK_BYTES = 8192
LARGE_FILE_THRESHOLD = 1024 ** 3  # 1 GB
FULL_HASH_CHUNK_SIZE = 1024 * 1024  # read 1MB at a time


def _read_chunk(path, offset, size):
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)


def partial_hash(path):
    """Hash of just the first PARTIAL_HASH_BYTES — cheap first-pass filter."""
    try:
        return hashlib.sha1(_read_chunk(path, 0, PARTIAL_HASH_BYTES)).hexdigest()
    except OSError:
        return None


def sampled_hash(path, size):
    """
    Hash of small chunks from the start, middle, and end of a large file.
    This is a fast proxy, NOT full verification — a match here means
    "likely duplicate," not "confirmed identical."
    """
    try:
        h = hashlib.sha1()
        h.update(_read_chunk(path, 0, SAMPLE_CHUNK_BYTES))
        mid_offset = max(0, size // 2 - SAMPLE_CHUNK_BYTES // 2)
        h.update(_read_chunk(path, mid_offset, SAMPLE_CHUNK_BYTES))
        end_offset = max(0, size - SAMPLE_CHUNK_BYTES)
        h.update(_read_chunk(path, end_offset, SAMPLE_CHUNK_BYTES))
        return h.hexdigest()
    except OSError:
        return None


def full_hash(path, stop_event=None):
    """Full-content hash, read in chunks so it can be interrupted mid-file."""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            while True:
                if stop_event and stop_event.is_set():
                    return None
                chunk = f.read(FULL_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def find_duplicate_groups(root, stop_event=None, progress_callback=None):
    """
    Recursively scans `root` for duplicate files. Excludes 0-byte files
    (trivially "identical," not a useful finding) and the review folder.

    Returns (confirmed_groups, likely_groups):
        confirmed_groups - list of lists of (path, size, mtime); byte-for-byte
                            verified identical (files up to 1GB)
        likely_groups    - list of lists of (path, size, mtime); matched only
                            on sampled chunks (files over 1GB) — NOT fully
                            verified, needs manual confirmation before delete
    """
    def stopped():
        return stop_event is not None and stop_event.is_set()

    # Stage 1: group by exact size.
    size_map = {}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if stopped():
            return [], []
        dirnames[:] = [d for d in dirnames if d != REVIEW_FOLDER_NAME]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size == 0:
                continue
            size_map.setdefault(st.st_size, []).append((full, st.st_mtime))
            count += 1
            if progress_callback and count % 500 == 0:
                progress_callback(f"Scanning... ({count:,} files)")

    size_candidates = [g for g in size_map.values() if len(g) > 1]
    if progress_callback:
        total_candidates = sum(len(g) for g in size_candidates)
        progress_callback(f"Checking {total_candidates:,} same-size file(s)...")

    # Stage 2: partial hash within each same-size group.
    partial_map = {}
    for group in size_candidates:
        if stopped():
            return [], []
        for path, mtime in group:
            ph = partial_hash(path)
            if ph is None:
                continue
            partial_map.setdefault(ph, []).append((path, mtime))
    stage2_candidates = [g for g in partial_map.values() if len(g) > 1]

    # Stage 3: full hash (<=1GB) or sampled hash (>1GB) to finish grouping.
    confirmed_groups = []
    likely_groups = []
    for idx, group in enumerate(stage2_candidates):
        if stopped():
            return confirmed_groups, likely_groups
        if progress_callback and idx % 5 == 0:
            progress_callback(f"Verifying possible duplicates... ({idx}/{len(stage2_candidates)} group(s))")

        try:
            size = os.path.getsize(group[0][0])
        except OSError:
            continue

        if size > LARGE_FILE_THRESHOLD:
            sample_map = {}
            for path, mtime in group:
                sh = sampled_hash(path, size)
                if sh is None:
                    continue
                sample_map.setdefault(sh, []).append((path, size, mtime))
            for sub in sample_map.values():
                if len(sub) > 1:
                    likely_groups.append(sub)
        else:
            full_map = {}
            for path, mtime in group:
                fh = full_hash(path, stop_event=stop_event)
                if fh is None:
                    continue
                full_map.setdefault(fh, []).append((path, size, mtime))
            for sub in full_map.values():
                if len(sub) > 1:
                    confirmed_groups.append(sub)

    return confirmed_groups, likely_groups


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------

class StatusBar(ttk.Frame):
    """Persistent bottom bar: message on the left, item count / size on the right."""

    def __init__(self, parent):
        super().__init__(parent, padding=(12, 6))
        self.msg_var = tk.StringVar(value="Ready")
        self.items_var = tk.StringVar(value="")
        self.size_var = tk.StringVar(value="")

        ttk.Label(self, textvariable=self.msg_var, foreground="#444").pack(side=tk.LEFT)
        ttk.Label(self, textvariable=self.size_var, foreground="#444").pack(side=tk.RIGHT, padx=(20, 0))
        ttk.Label(self, textvariable=self.items_var, foreground="#444").pack(side=tk.RIGHT, padx=(20, 0))

    def set(self, msg=None, items=None, size=None):
        if msg is not None:
            self.msg_var.set(msg)
        if items is not None:
            self.items_var.set(f"Items: {items:,}")
        if size is not None:
            self.size_var.set(f"Size: {size}")


class TickTreeview(ttk.Treeview):
    """
    A multi-select list in "tick mode": every row shows ☐ / ☑, a plain click
    ticks or unticks one row (no Ctrl needed), Shift+click still selects a
    range, and Ctrl+A ticks everything. The ticks are the selection drawn as
    text, so panels keep using selection() as before.

    The behaviour is bound to a "TickTreeview" bind tag rather than to the
    widget itself: panels bind <<TreeviewSelect>> on the widget without
    add="+", which would replace it. The tag sits just before Tk's own
    "Treeview" class tag, so a click handled here can stop Tk's default
    (select only this row).
    """

    UNTICKED = "☐ "
    TICKED = "☑ "

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._ticked = set()  # rows drawn as ticked, so only changed rows are redrawn
        tags = list(self.bindtags())
        tags.insert(tags.index("Treeview"), "TickTreeview")
        self.bindtags(tuple(tags))
        if not self.bind_class("TickTreeview"):  # once per Tk interpreter
            self.bind_class("TickTreeview", "<Button-1>", lambda e: e.widget._on_click(e))
            self.bind_class("TickTreeview", "<Control-a>", lambda e: e.widget._on_select_all())
            self.bind_class("TickTreeview", "<Control-A>", lambda e: e.widget._on_select_all())
            self.bind_class("TickTreeview", "<<TreeviewSelect>>", lambda e: e.widget._sync_ticks())

    def insert(self, parent, index, iid=None, **kw):
        text = kw.get("text", "")
        kw["text"] = self.UNTICKED + (text[2:] if text.startswith("  ") else text)
        return super().insert(parent, index, iid, **kw)

    def select_all(self):
        self.selection_set(self.get_children())

    def select_none(self):
        self.selection_set(())

    def add_select_buttons(self, bar, before=None):
        """Pack Select All and Select None at the left of `bar`, ahead of `before` if given."""
        select_all = ttk.Button(bar, text="Select All", command=self.select_all)
        select_none = ttk.Button(bar, text="Select None", command=self.select_none)
        if before is not None:
            select_all.pack(side=tk.LEFT, before=before)
            select_none.pack(side=tk.LEFT, padx=(6, 12), before=before)
        else:
            select_all.pack(side=tk.LEFT, padx=(12, 0))
            select_none.pack(side=tk.LEFT, padx=(6, 0))

    def _on_click(self, event):
        if event.state & 0x5:
            return None  # Shift or Ctrl held: Tk's own range / toggle selection
        if self.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None  # headings sort, separators resize, empty space does nothing
        item = self.identify_row(event.y)
        if not item:
            return None
        self.focus_set()
        self.focus(item)  # Shift+click ranges from the focus row
        self.selection_toggle(item)
        return "break"

    def _on_select_all(self):
        self.select_all()
        return "break"

    def _sync_ticks(self):
        now = set(self.selection())
        for item in now - self._ticked:
            self._mark(item, self.TICKED)
        for item in self._ticked - now:
            if self.exists(item):  # skip rows deleted since
                self._mark(item, self.UNTICKED)
        self._ticked = now

    def _mark(self, item, mark):
        self.item(item, text=mark + self.item(item, "text")[len(mark):])


class TreemapStrip(ttk.Frame):
    """A row of proportionally-sized colored blocks for the top N items."""

    COLORS = ["#2f6fed", "#2fb673", "#f2b134", "#ef5b5b", "#8a5cf6", "#17a2b8"]

    def __init__(self, parent, height=90):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, height=height, bg="#f4f5f7", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self._entries = []

    def set_entries(self, entries):
        """entries: list of dicts with 'name' and 'size', already limited/sorted."""
        self._entries = entries
        self.redraw()

    def redraw(self):
        self.canvas.delete("all")
        if not self._entries:
            w = self.canvas.winfo_width() or 400
            h = self.canvas.winfo_height() or 90
            self.canvas.create_text(
                w / 2, h / 2, text="Select a scanned folder to see its breakdown",
                fill="#999", font=("Segoe UI", 9)
            )
            return

        w = self.canvas.winfo_width() or 800
        h = self.canvas.winfo_height() or 90
        total = sum(e["size"] for e in self._entries) or 1
        x = 0.0
        n = len(self._entries)
        for i, e in enumerate(self._entries):
            frac = e["size"] / total
            block_w = (w - x) if i == n - 1 else frac * w  # last block absorbs rounding
            block_w = max(2, block_w)
            color = self.COLORS[i % len(self.COLORS)]
            self.canvas.create_rectangle(x, 0, x + block_w, h, fill=color, outline="#f4f5f7", width=2)
            if block_w > 55:
                label = f"{e['name']}\n{human_size(e['size'])}"
                self.canvas.create_text(
                    x + block_w / 2, h / 2, text=label, fill="white",
                    font=("Segoe UI", 9, "bold"), justify=tk.CENTER
                )
            x += block_w


class PlaceholderPanel(ttk.Frame):
    """A "coming soon" tab with no logic behind it yet."""

    def __init__(self, parent, title, description):
        super().__init__(parent)
        wrap = ttk.Frame(self)
        wrap.place(relx=0.5, rely=0.42, anchor="center")
        ttk.Label(wrap, text=title, font=("Segoe UI", 15, "bold")).pack(pady=(0, 8))
        ttk.Label(wrap, text=description, foreground="#777", wraplength=360, justify=tk.CENTER).pack()
        ttk.Label(wrap, text="(Coming soon)", foreground="#aaa", font=("Segoe UI", 9, "italic")).pack(pady=(10, 0))


# ---------------------------------------------------------------------------
# Disk View panel
# ---------------------------------------------------------------------------

class DiskViewPanel(ttk.Frame):
    PLACEHOLDER = "__loading__"

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.root_path = None
        self.node_data = {}
        self.work_queue = queue.Queue()
        self._scanning = 0
        self._last_summary = None  # (msg, items, size) shown once idle
        self._build_ui()
        self.after(100, self._poll_queue)

    # ---- UI ----

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(toolbar, text="Folder:").pack(side=tk.LEFT)
        self.path_var = tk.StringVar(value="No folder selected")
        path_entry = ttk.Entry(toolbar, textvariable=self.path_var, state="readonly")
        path_entry.pack(side=tk.LEFT, padx=(6, 8), fill=tk.X, expand=True)

        ttk.Button(toolbar, text="Browse...", command=self.choose_folder).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Rescan", command=self.rescan_root).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Open Folder", command=self.open_selected).pack(side=tk.LEFT, padx=(6, 0))

        # --- bottom-of-panel widgets packed first so the tree gets the middle ---
        self.treemap = TreemapStrip(self)
        self.treemap.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 12))
        ttk.Label(self, text="Folder breakdown (selected folder's biggest items)",
                  font=("Segoe UI", 9, "bold")).pack(side=tk.BOTTOM, anchor=tk.W, padx=12)

        tree_frame = ttk.Frame(self)
        tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        columns = ("size", "files", "folders", "percent", "modified")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("files", text="Files")
        self.tree.heading("folders", text="Folders")
        self.tree.heading("percent", text="% of Parent")
        self.tree.heading("modified", text="Last Modified")

        self.tree.column("#0", width=380, minwidth=200)
        self.tree.column("size", width=100, minwidth=80, anchor=tk.E)
        self.tree.column("files", width=70, minwidth=60, anchor=tk.E)
        self.tree.column("folders", width=70, minwidth=60, anchor=tk.E)
        self.tree.column("percent", width=90, minwidth=80, anchor=tk.E)
        self.tree.column("modified", width=120, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewOpen>>", self.on_expand)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Button-3>", self.on_right_click)

        self.tree.tag_configure("big", background="#eaf1ff")
        self.tree.tag_configure("evenrow", background="#fafbfc")

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open Folder", command=self.open_selected)
        self.context_menu.add_command(label="Copy Path", command=self.copy_selected_path)
        self.context_menu.add_command(label="Rescan This Folder", command=self.rescan_selected)

    # ---- Scanning ----

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Select a folder to scan")
        if folder:
            self.load_root(folder)

    def load_root(self, folder):
        self.root_path = folder
        self.app.set_root_path(folder)
        self.path_var.set(folder)
        self.tree.delete(*self.tree.get_children())
        self.node_data.clear()
        self._last_summary = None
        self.treemap.set_entries([])

        root_id = self.tree.insert(
            "", "end", text=f"  {os.path.basename(folder) or folder}",
            values=("Scanning...", "", "", "", ""), open=False
        )
        self.node_data[root_id] = {
            "full_path": folder, "is_dir": True, "size": None,
            "name": os.path.basename(folder) or folder,
        }
        self._start_scan(root_id, folder, is_root=True)

    def rescan_root(self):
        if self.root_path:
            self.load_root(self.root_path)
        else:
            messagebox.showinfo("No folder", "Select a folder first.")

    def rescan_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        data = self.node_data.get(item)
        if not data or not data.get("is_dir"):
            return
        for child in self.tree.get_children(item):
            self.tree.delete(child)
        self.tree.insert(item, "end", text="Scanning...", values=("", "", "", "", ""))
        is_root = self.tree.parent(item) == ""
        self._start_scan(item, data["full_path"], is_root=is_root)

    def _start_scan(self, item_id, path, is_root=False):
        self._scanning += 1
        if is_root:
            self._scan_start_time = time.time()
        self._update_status()
        t = threading.Thread(target=self._scan_worker, args=(item_id, path, is_root), daemon=True)
        t.start()

    def _scan_worker(self, item_id, path, is_root):
        entries = scan_folder_sizes(path)
        self.work_queue.put(("result", item_id, entries, is_root))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "result":
                    _, item_id, entries, is_root = msg
                    self._apply_scan_result(item_id, entries, is_root)
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _update_status(self):
        if self._scanning > 0:
            self.app.status.set(msg=f"Scanning... ({self._scanning} active)")
        elif self._last_summary:
            msg, items, size = self._last_summary
            self.app.status.set(msg=msg, items=items, size=size)
        else:
            self.app.status.set(msg="Ready")

    def _apply_scan_result(self, item_id, entries, is_root):
        self._scanning = max(0, self._scanning - 1)

        for child in self.tree.get_children(item_id):
            self.tree.delete(child)

        total_size = sum(e["size"] for e in entries) if entries else 0

        if is_root:
            elapsed = time.time() - self._scan_start_time
            total_items = 0
            for e in entries:
                if e["is_dir"]:
                    total_items += 1 + e["file_count"] + e["folder_count"]
                else:
                    total_items += 1
            self._last_summary = (
                f"Scan completed in {elapsed:.1f} seconds", total_items, human_size(total_size)
            )
            mtime_str = fmt_mtime(_safe_mtime(self.node_data[item_id]["full_path"]))
            self.tree.item(item_id, values=(
                human_size(total_size),
                sum(1 for e in entries if not e["is_dir"]),
                sum(1 for e in entries if e["is_dir"]),
                "100.0 %", mtime_str,
            ))
            self.node_data[item_id]["size"] = total_size
            self.tree.item(item_id, open=True)
            self.tree.selection_set(item_id)

        self._update_status()

        parent_total = total_size if total_size > 0 else 1
        for idx, e in enumerate(entries):
            pct = (e["size"] / parent_total * 100) if parent_total else 0
            mtime_str = fmt_mtime(_safe_mtime(e["full_path"]))
            icon = "  " if not e["is_dir"] else "  "
            tag = "big" if pct >= 25 else ("evenrow" if idx % 2 == 0 else "")
            child_id = self.tree.insert(
                item_id, "end",
                text=f"{icon}{e['name']}",
                values=(
                    human_size(e["size"]),
                    e["file_count"] if e["is_dir"] else "",
                    e["folder_count"] if e["is_dir"] else "",
                    f"{pct:.1f} %",
                    mtime_str,
                ),
                tags=(tag,) if tag else (),
            )
            self.node_data[child_id] = e
            if e["is_dir"]:
                self.tree.insert(child_id, "end", text=self.PLACEHOLDER)

        self.update_treemap_for_item(item_id)

    def update_treemap_for_item(self, item_id):
        children = self.tree.get_children(item_id)
        entries = []
        for c in children:
            d = self.node_data.get(c)
            if d and d.get("size") is not None and d.get("name") != self.PLACEHOLDER:
                entries.append({"name": d["name"], "size": d["size"]})
        entries.sort(key=lambda e: e["size"], reverse=True)
        self.treemap.set_entries(entries[:4])

    # ---- Interaction ----

    def on_expand(self, event):
        item = self.tree.focus()
        children = self.tree.get_children(item)
        if len(children) == 1 and self.tree.item(children[0], "text") == self.PLACEHOLDER:
            self.tree.delete(children[0])
            data = self.node_data.get(item)
            if data and data.get("is_dir"):
                self._start_scan(item, data["full_path"], is_root=False)

    def on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        data = self.node_data.get(item)
        if data and data.get("is_dir"):
            self.update_treemap_for_item(item)

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def open_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a row in the tree first.")
            return
        data = self.node_data.get(sel[0])
        if data:
            open_in_file_manager(data["full_path"])

    def copy_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return
        data = self.node_data.get(sel[0])
        if data:
            self.clipboard_clear()
            self.clipboard_append(data["full_path"])


def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Old Files panel (recursive)
# ---------------------------------------------------------------------------

class OldFilesPanel(ttk.Frame):
    MAX_SHOWN = 500  # cap the list so the UI stays responsive on huge trees

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.work_queue = queue.Queue()
        self.stop_event = threading.Event()
        self._paths = {}
        self._sizes = {}
        self._build_ui()
        self.after(150, self._poll_queue)

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.info_var = tk.StringVar(value="No folder selected yet — choose one in Disk View.")
        ttk.Label(toolbar, textvariable=self.info_var).pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(toolbar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT)
        self.scan_btn = ttk.Button(toolbar, text="Scan for Old Files", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.age_var = tk.StringVar(value=OLD_FILE_DEFAULT_AGE)
        ttk.Combobox(
            toolbar, textvariable=self.age_var, values=[label for label, _days in OLD_FILE_AGES],
            state="readonly", width=9,
        ).pack(side=tk.RIGHT, padx=(0, 12))
        ttk.Label(toolbar, text="Older than:").pack(side=tk.RIGHT, padx=(0, 4))

        note = ttk.Label(
            self, foreground="#888",
            text="Recursively scans every file under the selected folder and lists the ones not modified in the "
                 f"chosen time, oldest first (scan again after changing it). Showing the {self.MAX_SHOWN} oldest."
        )
        note.pack(side=tk.TOP, anchor=tk.W, padx=12)

        select_bar = ttk.Frame(self, padding=(12, 6))
        select_bar.pack(side=tk.TOP, fill=tk.X)
        self.selection_var = tk.StringVar(value="No files selected")
        ttk.Label(select_bar, textvariable=self.selection_var, foreground="#555").pack(side=tk.LEFT)
        self.move_btn = ttk.Button(
            select_bar, text="Move Selected to Review Folder", command=self.move_selected, state="disabled"
        )
        self.move_btn.pack(side=tk.RIGHT)

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        columns = ("path", "size", "modified")
        self.tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.tree.heading("#0", text="Name")
        self.tree.heading("path", text="Folder")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Last Modified")
        self.tree.column("#0", width=260, minwidth=150)
        self.tree.column("path", width=380, minwidth=150)
        self.tree.column("size", width=100, minwidth=80, anchor=tk.E)
        self.tree.column("modified", width=130, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("evenrow", background="#fafbfc")
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_selection_changed)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open Folder", command=self.open_selected)
        self.context_menu.add_command(label="Copy Path", command=self.copy_selected_path)

    def refresh_folder_label(self):
        root = self.app.get_root_path()
        if root:
            self.info_var.set(f"Folder: {root}")
            self.scan_btn.config(state="normal")
        else:
            self.info_var.set("No folder selected yet — choose one in Disk View.")
            self.scan_btn.config(state="disabled")

    def start_scan(self):
        root = self.app.get_root_path()
        if not root:
            return
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()
        self._sizes.clear()
        self.on_selection_changed()
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.app.status.set(msg="Scanning for old files...", items=None, size=None)
        self._age_label = self.age_var.get()
        days = dict(OLD_FILE_AGES).get(self._age_label, 30)
        cutoff = time.time() - days * 24 * 60 * 60
        t = threading.Thread(target=self._scan_worker, args=(root, cutoff), daemon=True)
        t.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self, root, cutoff):
        """Every file under `root` modified before `cutoff` (a timestamp); counts all files scanned."""
        found = []
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            if self.stop_event.is_set():
                break
            dirnames[:] = [d for d in dirnames if d != REVIEW_FOLDER_NAME]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                count += 1
                if count % 500 == 0:
                    self.work_queue.put(("progress", count))
                if st.st_mtime <= cutoff:
                    found.append((full, st.st_size, st.st_mtime))
        found.sort(key=lambda x: x[2])  # oldest first
        limited = found[: self.MAX_SHOWN]
        self.work_queue.put(("done", limited, count, self.stop_event.is_set(), len(found)))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "progress":
                    self.app.status.set(msg=f"Scanning for old files... ({msg[1]:,} scanned)")
                elif msg[0] == "done":
                    _, limited, total, stopped, old_count = msg
                    self._populate(limited, total, stopped, old_count)
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _populate(self, limited, total, stopped, old_count=None):
        """`old_count`: how many scanned files were old enough (unknown when not given)."""
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        for idx, (full, size, mtime) in enumerate(limited):
            name = os.path.basename(full)
            folder = os.path.dirname(full)
            tag = "evenrow" if idx % 2 == 0 else ""
            item = self.tree.insert(
                "", "end", text=f"  {name}",
                values=(folder, human_size(size), fmt_mtime(mtime)),
                tags=(tag,) if tag else (),
            )
            self._paths[item] = full
            self._sizes[item] = size
        note = " (stopped early)" if stopped else ""
        if old_count is None:
            msg = f"Old files scan complete{note} — showing {len(limited)} oldest of {total:,} files scanned"
        else:
            age = getattr(self, "_age_label", OLD_FILE_DEFAULT_AGE)
            if old_count:
                msg = (f"Old files scan complete{note} — showing {len(limited):,} oldest of {old_count:,} "
                       f"files older than {age} ({total:,} scanned)")
            else:
                msg = f"Old files scan complete{note} — no files older than {age} ({total:,} scanned)"
        self.app.status.set(msg=msg, items=total, size=None)

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def on_selection_changed(self, event=None):
        sel = self.tree.selection()
        count = len(sel)
        total = sum(self._sizes.get(i, 0) for i in sel)
        if count:
            self.selection_var.set(f"Selected: {count} file(s) · {human_size(total)}")
            self.move_btn.config(state="normal")
        else:
            self.selection_var.set("No files selected")
            self.move_btn.config(state="disabled")

    def open_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0])
        if path:
            open_in_file_manager(path)

    def copy_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0])
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def move_selected(self):
        sel = list(self.tree.selection())
        paths = [self._paths[i] for i in sel if i in self._paths]
        if not paths:
            return

        result = confirm_and_delete(paths, {self._paths[i]: self._sizes.get(i) for i in sel if i in self._paths})
        if result is None:
            return
        moved, failed = result
        removed_set = {orig for orig, _target in moved}

        for item in sel:
            if self._paths.get(item) in removed_set:
                self.tree.delete(item)
                self._paths.pop(item, None)
                self._sizes.pop(item, None)
        self.on_selection_changed()


# ---------------------------------------------------------------------------
# Temp Files panel
# ---------------------------------------------------------------------------

class TempFilesPanel(ttk.Frame):
    """
    Recursively finds known junk/temp files (.tmp, .bak, .log, Thumbs.db,
    Office lock files, etc.) under the selected folder that are at least a
    day old, so nothing currently being written gets flagged. Same
    select-and-move-to-review support as Old Files / File Types.
    """

    MAX_SHOWN = 500

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.work_queue = queue.Queue()
        self.stop_event = threading.Event()
        self._paths = {}
        self._sizes = {}
        self._build_ui()
        self.after(150, self._poll_queue)

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.info_var = tk.StringVar(value="No folder selected yet — choose one in Disk View.")
        ttk.Label(toolbar, textvariable=self.info_var).pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(toolbar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT)
        self.scan_btn = ttk.Button(toolbar, text="Scan for Temp Files", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side=tk.RIGHT, padx=(0, 6))

        note = ttk.Label(
            self, foreground="#888",
            text="Finds .tmp/.bak/.log/.old files, Thumbs.db, desktop.ini, Office lock files, etc. — "
                 f"only ones at least 1 day old. Showing the {self.MAX_SHOWN} biggest."
        )
        note.pack(side=tk.TOP, anchor=tk.W, padx=12)

        select_bar = ttk.Frame(self, padding=(12, 6))
        select_bar.pack(side=tk.TOP, fill=tk.X)
        self.selection_var = tk.StringVar(value="No files selected")
        ttk.Label(select_bar, textvariable=self.selection_var, foreground="#555").pack(side=tk.LEFT)
        self.move_btn = ttk.Button(
            select_bar, text="Move Selected to Review Folder", command=self.move_selected, state="disabled"
        )
        self.move_btn.pack(side=tk.RIGHT)

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        columns = ("path", "size", "modified")
        self.tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.tree.heading("#0", text="Name")
        self.tree.heading("path", text="Folder")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Last Modified")
        self.tree.column("#0", width=260, minwidth=150)
        self.tree.column("path", width=380, minwidth=150)
        self.tree.column("size", width=100, minwidth=80, anchor=tk.E)
        self.tree.column("modified", width=130, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("evenrow", background="#fafbfc")
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_selection_changed)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open Folder", command=self.open_selected)
        self.context_menu.add_command(label="Copy Path", command=self.copy_selected_path)

    def refresh_folder_label(self):
        root = self.app.get_root_path()
        if root:
            self.info_var.set(f"Folder: {root}")
            self.scan_btn.config(state="normal")
        else:
            self.info_var.set("No folder selected yet — choose one in Disk View.")
            self.scan_btn.config(state="disabled")

    def start_scan(self):
        root = self.app.get_root_path()
        if not root:
            return
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()
        self._sizes.clear()
        self.on_selection_changed()
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.app.status.set(msg="Scanning for temp files...", items=None, size=None)
        t = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        t.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self, root):
        found = []
        count = 0
        now = time.time()
        for dirpath, dirnames, filenames in os.walk(root):
            if self.stop_event.is_set():
                break
            dirnames[:] = [d for d in dirnames if d != REVIEW_FOLDER_NAME]
            for fn in filenames:
                if not is_temp_file(fn):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if now - st.st_mtime < TEMP_MIN_AGE_SECONDS:
                    continue  # too recent — could still be actively in use
                found.append((full, st.st_size, st.st_mtime))
                count += 1
                if count % 200 == 0:
                    self.work_queue.put(("progress", count))
        found.sort(key=lambda x: x[1], reverse=True)  # biggest first
        limited = found[: self.MAX_SHOWN]
        self.work_queue.put(("done", limited, count, self.stop_event.is_set()))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "progress":
                    self.app.status.set(msg=f"Scanning for temp files... ({msg[1]:,} found)")
                elif msg[0] == "done":
                    _, limited, total, stopped = msg
                    self._populate(limited, total, stopped)
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _populate(self, limited, total, stopped):
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        for idx, (full, size, mtime) in enumerate(limited):
            name = os.path.basename(full)
            folder = os.path.dirname(full)
            tag = "evenrow" if idx % 2 == 0 else ""
            item = self.tree.insert(
                "", "end", text=f"  {name}",
                values=(folder, human_size(size), fmt_mtime(mtime)),
                tags=(tag,) if tag else (),
            )
            self._paths[item] = full
            self._sizes[item] = size
        note = " (stopped early)" if stopped else ""
        self.app.status.set(
            msg=f"Temp files scan complete{note} — showing {len(limited)} of {total:,} found",
            items=total, size=None,
        )

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def on_selection_changed(self, event=None):
        sel = self.tree.selection()
        count = len(sel)
        total = sum(self._sizes.get(i, 0) for i in sel)
        if count:
            self.selection_var.set(f"Selected: {count} file(s) · {human_size(total)}")
            self.move_btn.config(state="normal")
        else:
            self.selection_var.set("No files selected")
            self.move_btn.config(state="disabled")

    def open_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0])
        if path:
            open_in_file_manager(path)

    def copy_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0])
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def move_selected(self):
        sel = list(self.tree.selection())
        paths = [self._paths[i] for i in sel if i in self._paths]
        if not paths:
            return

        result = confirm_and_delete(paths, {self._paths[i]: self._sizes.get(i) for i in sel if i in self._paths})
        if result is None:
            return
        moved, failed = result
        removed_set = {orig for orig, _target in moved}

        for item in sel:
            if self._paths.get(item) in removed_set:
                self.tree.delete(item)
                self._paths.pop(item, None)
                self._sizes.pop(item, None)
        self.on_selection_changed()


# ---------------------------------------------------------------------------
# Empty Folders panel
# ---------------------------------------------------------------------------

class EmptyFoldersPanel(ttk.Frame):
    """
    Recursively finds folders whose entire subtree contains zero files
    (they may still contain nested empty folders). Since these hold zero
    data, deletion here is direct — no review folder / restore needed —
    with a live re-check right before each delete in case something was
    added since the scan.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.work_queue = queue.Queue()
        self.stop_event = threading.Event()
        self._paths = {}  # tree item id -> folder path
        self._build_ui()
        self.after(150, self._poll_queue)

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.info_var = tk.StringVar(value="No folder selected yet — choose one in Disk View.")
        ttk.Label(toolbar, textvariable=self.info_var).pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(toolbar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT)
        self.scan_btn = ttk.Button(toolbar, text="Scan for Empty Folders", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side=tk.RIGHT, padx=(0, 6))

        note = ttk.Label(
            self, foreground="#888",
            text="Folders whose entire contents (including nested folders) hold zero files. "
                 "Contains no data, so deletion is direct — no review folder involved."
        )
        note.pack(side=tk.TOP, anchor=tk.W, padx=12)

        select_bar = ttk.Frame(self, padding=(12, 6))
        select_bar.pack(side=tk.TOP, fill=tk.X)
        self.selection_var = tk.StringVar(value="No folders selected")
        ttk.Label(select_bar, textvariable=self.selection_var, foreground="#555").pack(side=tk.LEFT)
        self.delete_btn = ttk.Button(
            select_bar, text="Delete Selected Folders", command=self.delete_selected, state="disabled"
        )
        self.delete_btn.pack(side=tk.RIGHT)

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        columns = ("modified",)
        self.tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.tree.heading("#0", text="Folder Path")
        self.tree.heading("modified", text="Last Modified")
        self.tree.column("#0", width=650, minwidth=250)
        self.tree.column("modified", width=140, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("evenrow", background="#fafbfc")
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_selection_changed)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open Folder", command=self.open_selected)
        self.context_menu.add_command(label="Copy Path", command=self.copy_selected_path)

    def refresh_folder_label(self):
        root = self.app.get_root_path()
        if root:
            self.info_var.set(f"Folder: {root}")
            self.scan_btn.config(state="normal")
        else:
            self.info_var.set("No folder selected yet — choose one in Disk View.")
            self.scan_btn.config(state="disabled")

    def start_scan(self):
        root = self.app.get_root_path()
        if not root:
            return
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()
        self.on_selection_changed()
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.app.status.set(msg="Scanning for empty folders...", items=None, size=None)
        t = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        t.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self, root):
        empty = find_empty_folders(root, stop_event=self.stop_event)
        self.work_queue.put(("done", empty, self.stop_event.is_set()))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "done":
                    _, empty, stopped = msg
                    self._populate(empty, stopped)
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _populate(self, empty, stopped):
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        empty_sorted = sorted(empty, key=lambda p: p.lower())
        for idx, path in enumerate(empty_sorted):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            tag = "evenrow" if idx % 2 == 0 else ""
            item = self.tree.insert(
                "", "end", text=f"  {path}",
                values=(fmt_mtime(mtime),),
                tags=(tag,) if tag else (),
            )
            self._paths[item] = path
        note = " (stopped early)" if stopped else ""
        self.app.status.set(
            msg=f"Empty folder scan complete{note} — found {len(empty_sorted):,}",
            items=len(empty_sorted), size=None,
        )

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def on_selection_changed(self, event=None):
        sel = self.tree.selection()
        count = len(sel)
        if count:
            self.selection_var.set(f"Selected: {count} folder(s)")
            self.delete_btn.config(state="normal")
        else:
            self.selection_var.set("No folders selected")
            self.delete_btn.config(state="disabled")

    def open_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0])
        if path:
            open_in_file_manager(path)

    def copy_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self._paths.get(sel[0])
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def delete_selected(self):
        sel = list(self.tree.selection())
        paths = [self._paths[i] for i in sel if i in self._paths]
        if not paths:
            return

        top_level = filter_top_level_paths(paths)
        if not ask_items(
            "Delete empty folders",
            f"Permanently delete {_plural(len(top_level), 'empty folder')}?",
            group_items([(p, None) for p in top_level], by_type=False, noun="folder"),
            note="They contain zero data, and nothing is moved to the review folder for this action — "
                 "this cannot be undone.",
        ):
            return

        deleted = []
        failed = []
        for path in top_level:
            # Re-verify it's still empty right before deleting, in case
            # something was added since the scan.
            still_empty = True
            try:
                for _, _, filenames in os.walk(path):
                    if filenames:
                        still_empty = False
                        break
            except OSError as e:
                failed.append((path, str(e)))
                continue
            if not still_empty:
                failed.append((path, "no longer empty — skipped"))
                continue
            try:
                shutil.rmtree(path)
                deleted.append(path)
            except Exception as e:
                failed.append((path, str(e)))

        deleted_set = set(deleted)
        for item in sel:
            p = self._paths.get(item)
            if p in deleted_set or any(p.startswith(d + os.sep) or p == d for d in deleted_set):
                self.tree.delete(item)
        self.on_selection_changed()

        show_items(
            "Done",
            f"Deleted {len(deleted):,} of {_plural(len(top_level), 'folder')}.",
            result_groups([
                ("Deleted", [(p, None) for p in deleted]),
                ("Skipped or failed", [(p, None, err) for p, err in failed]),
            ], noun="folder"),
        )


# ---------------------------------------------------------------------------
# Leftovers panel (Windows)
# ---------------------------------------------------------------------------

class LeftoversPanel(ttk.Frame):
    """
    Finds folders that uninstalled programs left behind (find_leftovers) in
    AppData, ProgramData and Program Files, biggest first. Folders used in
    the last LEFTOVER_RECENT_DAYS days are hidden unless asked for, since
    they're probably still in use. Selected folders move whole into the
    review folder, so they can be restored. Folders only an administrator
    can move are marked, and the app can restart itself elevated. Folders
    the user marks "not a leftover" stay hidden in later scans (see
    load_leftover_exclusions) until unhidden.
    """

    CONFIRM_NOTE = (
        "Make sure these programs really are uninstalled. Game saves and portable apps can "
        "look like leftovers, so check folders marked \"may hold game saves\" first."
    )
    # Context-menu labels (also used to enable/disable the entries).
    HIDE_LABEL = "Not a Leftover — Hide It"
    UNHIDE_LABEL = "Unhide (List It Again)"

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.work_queue = queue.Queue()
        self.stop_event = threading.Event()
        self._rows = []    # every folder the last scan found, including recently used and hidden ones
        self._paths = {}   # tree item id -> folder path
        self._sizes = {}   # tree item id -> size
        self._excluded = load_leftover_exclusions()
        self._build_ui()
        self.after(150, self._poll_queue)

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.info_var = tk.StringVar(
            value="Folders no installed program accounts for, in AppData, ProgramData and Program Files."
            if winreg is not None else "The leftover scan is available on Windows only."
        )
        ttk.Label(toolbar, textvariable=self.info_var).pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(toolbar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT)
        self.scan_btn = ttk.Button(
            toolbar, text="Scan for Leftovers", command=self.start_scan,
            state="normal" if winreg is not None else "disabled",
        )
        self.scan_btn.pack(side=tk.RIGHT, padx=(0, 6))
        if winreg is not None and not is_admin():
            ttk.Button(toolbar, text="Restart as Administrator", command=self.restart_as_admin).pack(
                side=tk.RIGHT, padx=(0, 6)
            )

        note = ttk.Label(
            self, foreground="#888", wraplength=820, justify=tk.LEFT,
            text="Only top-level folders are checked. Anything that matches an installed program's name, "
                 "publisher or install folder is left out, and so are Windows' own folders. Folders used in "
                 f"the last {LEFTOVER_RECENT_DAYS} days are hidden unless you tick the box — they're probably "
                 "still in use. To keep a folder out of future scans (a game you still play, say), select it "
                 "and press Hide Selected, or right-click it. Exceptions can also be added or removed in "
                 "Settings. Moved folders can be restored from the Review Folder tab.",
        )
        note.pack(side=tk.TOP, anchor=tk.W, padx=12)

        filter_bar = ttk.Frame(self, padding=(12, 6, 12, 0))
        filter_bar.pack(side=tk.TOP, fill=tk.X)
        self.show_recent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_bar, text=f"Also show folders used in the last {LEFTOVER_RECENT_DAYS} days",
            variable=self.show_recent_var, command=self._render,
        ).pack(side=tk.LEFT)
        self.show_hidden_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_bar, text="Show folders you hid", variable=self.show_hidden_var, command=self._render,
        ).pack(side=tk.LEFT, padx=(12, 0))

        select_bar = ttk.Frame(self, padding=(12, 6))
        select_bar.pack(side=tk.TOP, fill=tk.X)
        self.selection_var = tk.StringVar(value="No folders selected")
        ttk.Label(select_bar, textvariable=self.selection_var, foreground="#555").pack(side=tk.LEFT)
        self.move_btn = ttk.Button(
            select_bar, text="Move Selected to Review Folder", command=self.move_selected, state="disabled"
        )
        self.move_btn.pack(side=tk.RIGHT)
        self.hide_btn = ttk.Button(
            select_bar, text="Hide Selected (Not a Leftover)", command=self.hide_selected, state="disabled"
        )
        self.hide_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.unhide_btn = ttk.Button(
            select_bar, text="Unhide Selected", command=self.unhide_selected, state="disabled"
        )
        self.unhide_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self.summary_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.summary_var, font=("Segoe UI", 9, "bold")).pack(
            side=tk.TOP, anchor=tk.W, padx=12, pady=(0, 4)
        )

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        columns = ("location", "size", "files", "last_used", "note")
        self.tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.tree.heading("#0", text="Folder")
        self.tree.heading("location", text="Location")
        self.tree.heading("size", text="Size")
        self.tree.heading("files", text="Files")
        self.tree.heading("last_used", text="Last Used")
        self.tree.heading("note", text="Note")
        self.tree.column("#0", width=240, minwidth=150)
        self.tree.column("location", width=150, minwidth=100)
        self.tree.column("size", width=90, minwidth=70, anchor=tk.E)
        self.tree.column("files", width=70, minwidth=60, anchor=tk.E)
        self.tree.column("last_used", width=100, minwidth=90, anchor=tk.CENTER)
        self.tree.column("note", width=220, minwidth=100)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("evenrow", background="#fafbfc")
        self.tree.tag_configure("dim", foreground="#999")  # used recently, or hidden by the user
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_selection_changed)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Open Folder", command=self.open_selected)
        self.context_menu.add_command(label="Copy Path", command=self.copy_selected_path)
        self.context_menu.add_separator()
        self.context_menu.add_command(label=self.HIDE_LABEL, command=self.hide_selected)
        self.context_menu.add_command(label=self.UNHIDE_LABEL, command=self.unhide_selected)

    def start_scan(self):
        self._rows = []
        self._excluded = load_leftover_exclusions()
        self._render()
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.app.status.set(msg="Scanning for leftovers...", items=None, size=None)
        t = threading.Thread(target=self._scan_worker, daemon=True)
        t.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self):
        rows = find_leftovers(self.stop_event, lambda path: self.work_queue.put(("progress", path)))
        self.work_queue.put(("done", rows, self.stop_event.is_set()))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "progress":
                    self.app.status.set(msg=f"Scanning for leftovers... measuring {msg[1]}")
                elif msg[0] == "done":
                    _, rows, stopped = msg
                    self._rows = rows
                    self._render()
                    self.scan_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    note = " (stopped early)" if stopped else ""
                    self.app.status.set(
                        msg=f"Leftovers scan complete{note} — {len(rows):,} folder(s) no installed program accounts for",
                        items=len(rows), size=None,
                    )
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()
        self._sizes.clear()

        show_recent = self.show_recent_var.get()
        show_hidden = self.show_hidden_var.get()
        recent_off = hidden_off = 0
        rows = []
        for row in self._rows:
            if self._is_hidden(row["path"]):
                if not show_hidden:
                    hidden_off += 1
                    continue
            elif row["recent"] and not show_recent:
                recent_off += 1
                continue
            rows.append(row)

        for idx, row in enumerate(rows):
            hidden = self._is_hidden(row["path"])
            notes = ["hidden by you"] if hidden else []
            if row["needs_admin"]:
                notes.append("needs admin")
            if row["saves"]:
                notes.append("may hold game saves")
            if row["recent"]:
                notes.append("used recently")
            tag = "dim" if hidden or row["recent"] else ("evenrow" if idx % 2 == 0 else "")
            item = self.tree.insert(
                "", "end", text=f"  {row['name']}",
                values=(row["location"], human_size(row["size"]), f"{row['files']:,}",
                        fmt_mtime(row["last_used"]), ", ".join(notes)),
                tags=(tag,) if tag else (),
            )
            self._paths[item] = row["path"]
            self._sizes[item] = row["size"]

        summary = f"{len(rows):,} folder(s) · {human_size(sum(r['size'] for r in rows))}" if self._rows else ""
        if recent_off:
            summary += f" · {recent_off:,} used in the last {LEFTOVER_RECENT_DAYS} days not shown"
        if hidden_off:
            summary += f" · {hidden_off:,} you hid not shown"
        self.summary_var.set(summary)
        self.on_selection_changed()

    def _is_hidden(self, path):
        return _path_key(path) in self._excluded

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item not in self.tree.selection():
            self.tree.selection_set(item)  # right-clicking inside a selection keeps it, so hiding works on several
        can_hide, can_unhide = self._hide_states()
        self.context_menu.entryconfig(self.HIDE_LABEL, state="normal" if can_hide else "disabled")
        self.context_menu.entryconfig(self.UNHIDE_LABEL, state="normal" if can_unhide else "disabled")
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _selected_paths(self):
        return [self._paths[i] for i in self.tree.selection() if i in self._paths]

    def _hide_states(self):
        """(can hide, can unhide) for the selection: some selected folder isn't hidden / is hidden."""
        paths = self._selected_paths()
        hidden = sum(1 for p in paths if self._is_hidden(p))
        return hidden < len(paths), hidden > 0

    def _save_exclusions(self, excluded):
        try:
            save_leftover_exclusions(excluded.values())
        except OSError as e:
            messagebox.showerror("Couldn't save", f"Couldn't save the list of hidden folders:\n{e}")
            return False
        self._excluded = excluded
        self._render()
        return True

    def reload_exclusions(self):
        """Pick up exceptions added or removed in Settings (runs when this sub-tab is shown)."""
        excluded = load_leftover_exclusions()
        if excluded != self._excluded:
            self._excluded = excluded
            self._render()

    def hide_selected(self):
        paths = [p for p in self._selected_paths() if not self._is_hidden(p)]
        if paths and self._save_exclusions({**self._excluded, **{_path_key(p): p for p in paths}}):
            self.app.status.set(
                msg=f"Hid {len(paths)} folder(s): they won't be listed in future scans. "
                    "Tick \"Show folders you hid\" to see them again, or manage the list in Settings."
            )

    def unhide_selected(self):
        keys = {_path_key(p) for p in self._selected_paths()} & self._excluded.keys()
        if keys and self._save_exclusions({k: p for k, p in self._excluded.items() if k not in keys}):
            self.app.status.set(msg=f"Unhid {len(keys)} folder(s): they'll be listed again.")

    def on_selection_changed(self, event=None):
        sel = self.tree.selection()
        count = len(sel)
        total = sum(self._sizes.get(i, 0) for i in sel)
        can_hide, can_unhide = self._hide_states()
        self.hide_btn.config(state="normal" if can_hide else "disabled")
        self.unhide_btn.config(state="normal" if can_unhide else "disabled")
        if count:
            self.selection_var.set(f"Selected: {count} folder(s) · {human_size(total)}")
            self.move_btn.config(state="normal")
        else:
            self.selection_var.set("No folders selected")
            self.move_btn.config(state="disabled")

    def open_selected(self):
        sel = self.tree.selection()
        if sel and sel[0] in self._paths:
            open_in_file_manager(self._paths[sel[0]])

    def copy_selected_path(self):
        sel = self.tree.selection()
        if sel and sel[0] in self._paths:
            self.clipboard_clear()
            self.clipboard_append(self._paths[sel[0]])

    def move_selected(self):
        sel = list(self.tree.selection())
        paths = [self._paths[i] for i in sel if i in self._paths]
        if not paths:
            return

        sizes = {self._paths[i]: self._sizes.get(i) for i in sel if i in self._paths}
        result = confirm_and_delete(paths, sizes, note=self.CONFIRM_NOTE)
        if result is None:
            return
        moved, failed = result
        removed_set = {orig for orig, _target in moved}
        self._rows = [r for r in self._rows if r["path"] not in removed_set]
        self._render()

        denied = [p for p, err in failed if "WinError 5" in err or "Access is denied" in err]
        if denied and not is_admin():
            self.app.status.set(
                msg=f"{len(denied)} folder(s) need administrator rights — use Restart as Administrator, then scan again."
            )

    def restart_as_admin(self):
        if not messagebox.askyesno(
            "Restart as administrator",
            "Close the app and open it again with administrator rights? Windows will ask for "
            "permission. Folders in Program Files and ProgramData can then be moved."
        ):
            return
        script = os.path.abspath(__file__)
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, subprocess.list2cmdline([script]), os.path.dirname(script), 1
            )
        except Exception as e:
            messagebox.showerror("Couldn't restart", str(e))
            return
        if result > 32:  # ShellExecute returns 32 or less on failure, e.g. when the prompt is declined
            self.app.destroy()


# ---------------------------------------------------------------------------
# File Types panel (recursive, with drill-down)
# ---------------------------------------------------------------------------

class FileTypesPanel(ttk.Frame):
    MAX_SHOWN = 500  # cap on files shown per type, sorted by size

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.work_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.type_agg = {}       # ext -> {"count": n, "total_size": s}
        self.groups = {}         # ext -> list of (path, size, mtime)
        self._type_item_ext = {}  # tree item id -> ext (main table)
        self._types_sort = ("total_size", True)   # (column, reverse)
        self._detail_sort = ("size", True)
        self._current_type = None
        self._detail_paths = {}  # tree item id -> full path (detail table)
        self._detail_sizes = {}  # tree item id -> size (detail table)

        self._build_ui()
        self.after(150, self._poll_queue)

    # ---- UI ----

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.info_var = tk.StringVar(value="No folder selected yet — choose one in Disk View.")
        ttk.Label(toolbar, textvariable=self.info_var).pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(toolbar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT)
        self.scan_btn = ttk.Button(toolbar, text="Scan File Types", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side=tk.RIGHT, padx=(0, 6))

        ttk.Label(
            self, foreground="#888",
            text=f"Recursively scans every file, grouped by extension. Each type shows up to {self.MAX_SHOWN} files, sorted by size."
        ).pack(side=tk.TOP, anchor=tk.W, padx=12)

        self.container = ttk.Frame(self)
        self.container.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 12))

        self.types_view = ttk.Frame(self.container)
        self.detail_view = ttk.Frame(self.container)
        for v in (self.types_view, self.detail_view):
            v.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_types_view()
        self._build_detail_view()

        self.types_view.tkraise()

    def _build_types_view(self):
        frame = ttk.Frame(self.types_view)
        frame.pack(fill=tk.BOTH, expand=True)

        columns = ("files", "total_size", "avg_size")
        self.types_tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        self.types_tree.heading("#0", text="Type", command=lambda: self.sort_types("name"))
        self.types_tree.heading("files", text="Files", command=lambda: self.sort_types("files"))
        self.types_tree.heading("total_size", text="Total Size", command=lambda: self.sort_types("total_size"))
        self.types_tree.heading("avg_size", text="Avg. Size", command=lambda: self.sort_types("avg_size"))
        self.types_tree.column("#0", width=220, minwidth=140)
        self.types_tree.column("files", width=100, minwidth=70, anchor=tk.E)
        self.types_tree.column("total_size", width=130, minwidth=90, anchor=tk.E)
        self.types_tree.column("avg_size", width=130, minwidth=90, anchor=tk.E)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.types_tree.yview)
        self.types_tree.configure(yscrollcommand=vsb.set)
        self.types_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.types_tree.tag_configure("evenrow", background="#fafbfc")
        self.types_tree.bind("<<TreeviewSelect>>", self.on_type_select)

    def _build_detail_view(self):
        top = ttk.Frame(self.detail_view)
        top.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        ttk.Button(top, text="< Back to File Types", command=self.show_types).pack(side=tk.LEFT)
        self.detail_title_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.detail_title_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(12, 0))

        select_bar = ttk.Frame(self.detail_view)
        select_bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.detail_selection_var = tk.StringVar(value="No files selected")
        ttk.Label(select_bar, textvariable=self.detail_selection_var, foreground="#555").pack(side=tk.LEFT)
        self.detail_move_btn = ttk.Button(
            select_bar, text="Move Selected to Review Folder", command=self.move_selected_detail, state="disabled"
        )
        self.detail_move_btn.pack(side=tk.RIGHT)

        frame = ttk.Frame(self.detail_view)
        frame.pack(fill=tk.BOTH, expand=True)

        columns = ("path", "size", "modified")
        self.detail_tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.detail_tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.detail_tree.heading("#0", text="Name", command=lambda: self.sort_detail("name"))
        self.detail_tree.heading("path", text="Folder", command=lambda: self.sort_detail("path"))
        self.detail_tree.heading("size", text="Size", command=lambda: self.sort_detail("size"))
        self.detail_tree.heading("modified", text="Last Modified", command=lambda: self.sort_detail("modified"))
        self.detail_tree.column("#0", width=260, minwidth=150)
        self.detail_tree.column("path", width=360, minwidth=150)
        self.detail_tree.column("size", width=100, minwidth=80, anchor=tk.E)
        self.detail_tree.column("modified", width=130, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.detail_tree.yview)
        self.detail_tree.configure(yscrollcommand=vsb.set)
        self.detail_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.detail_tree.tag_configure("evenrow", background="#fafbfc")
        self.detail_tree.bind("<Button-3>", self.on_detail_right_click)
        self.detail_tree.bind("<<TreeviewSelect>>", self.on_detail_selection_changed)

        self.detail_context_menu = tk.Menu(self, tearoff=0)
        self.detail_context_menu.add_command(label="Open Folder", command=self.open_selected_detail)
        self.detail_context_menu.add_command(label="Copy Path", command=self.copy_selected_detail_path)

    def refresh_folder_label(self):
        root = self.app.get_root_path()
        if root:
            self.info_var.set(f"Folder: {root}")
            self.scan_btn.config(state="normal")
        else:
            self.info_var.set("No folder selected yet — choose one in Disk View.")
            self.scan_btn.config(state="disabled")

    # ---- Scanning ----

    def start_scan(self):
        root = self.app.get_root_path()
        if not root:
            return
        self.show_types()
        self.type_agg.clear()
        self.groups.clear()
        self.types_tree.delete(*self.types_tree.get_children())
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.app.status.set(msg="Scanning file types...", items=None, size=None)
        t = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        t.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self, root):
        records = []
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            if self.stop_event.is_set():
                break
            dirnames[:] = [d for d in dirnames if d != REVIEW_FOLDER_NAME]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                ext = os.path.splitext(fn)[1].lstrip(".").upper() or "(no extension)"
                records.append((full, st.st_size, st.st_mtime, ext))
                count += 1
                if count % 500 == 0:
                    self.work_queue.put(("progress", count))
        self.work_queue.put(("done", records, count, self.stop_event.is_set()))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "progress":
                    self.app.status.set(msg=f"Scanning file types... ({msg[1]:,} files found)")
                elif msg[0] == "done":
                    _, records, count, stopped = msg
                    self._on_scan_done(records, count, stopped)
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _on_scan_done(self, records, count, stopped):
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

        self.groups = {}
        self.type_agg = {}
        for full, size, mtime, ext in records:
            self.groups.setdefault(ext, []).append((full, size, mtime))
        for ext, files in self.groups.items():
            total = sum(f[1] for f in files)
            n = len(files)
            self.type_agg[ext] = {"count": n, "total_size": total, "avg_size": total / n if n else 0}

        self._render_types_table()

        note = " (stopped early)" if stopped else ""
        total_size = sum(a["total_size"] for a in self.type_agg.values())
        self.app.status.set(
            msg=f"File types scan complete{note} — {len(self.type_agg)} types across {count:,} files",
            items=count, size=human_size(total_size),
        )

    # ---- Types table ----

    def sort_types(self, col):
        cur_col, cur_rev = self._types_sort
        if cur_col == col:
            self._types_sort = (col, not cur_rev)
        else:
            self._types_sort = (col, col != "name")
        self._render_types_table()

    def _render_types_table(self):
        self.types_tree.delete(*self.types_tree.get_children())
        self._type_item_ext.clear()

        col, reverse = self._types_sort
        items = list(self.type_agg.items())
        if col == "name":
            items.sort(key=lambda kv: kv[0], reverse=reverse)
        elif col == "files":
            items.sort(key=lambda kv: kv[1]["count"], reverse=reverse)
        elif col == "avg_size":
            items.sort(key=lambda kv: kv[1]["avg_size"], reverse=reverse)
        else:
            items.sort(key=lambda kv: kv[1]["total_size"], reverse=reverse)

        for idx, (ext, agg) in enumerate(items):
            tag = "evenrow" if idx % 2 == 0 else ""
            item_id = self.types_tree.insert(
                "", "end", text=f"  {ext}",
                values=(f"{agg['count']:,}", human_size(agg["total_size"]), human_size(agg["avg_size"])),
                tags=(tag,) if tag else (),
            )
            self._type_item_ext[item_id] = ext

    def on_type_select(self, event):
        sel = self.types_tree.selection()
        if not sel:
            return
        ext = self._type_item_ext.get(sel[0])
        if ext:
            self.show_detail(ext)

    # ---- Detail (drill-down) table ----

    def show_detail(self, ext):
        self._current_type = ext
        agg = self.type_agg.get(ext, {"count": 0, "total_size": 0})
        self.detail_title_var.set(f"{ext} — {agg['count']:,} files, {human_size(agg['total_size'])} total")
        self._detail_sort = ("size", True)
        self._render_detail_table()
        self.detail_view.tkraise()

    def sort_detail(self, col):
        cur_col, cur_rev = self._detail_sort
        if cur_col == col:
            self._detail_sort = (col, not cur_rev)
        else:
            self._detail_sort = (col, col == "size")
        self._render_detail_table()

    def _render_detail_table(self):
        self.detail_tree.delete(*self.detail_tree.get_children())
        self._detail_paths.clear()
        self._detail_sizes.clear()

        records = self.groups.get(self._current_type, [])
        col, reverse = self._detail_sort
        if col == "name":
            records = sorted(records, key=lambda r: os.path.basename(r[0]).lower(), reverse=reverse)
        elif col == "path":
            records = sorted(records, key=lambda r: os.path.dirname(r[0]).lower(), reverse=reverse)
        elif col == "modified":
            records = sorted(records, key=lambda r: r[2], reverse=reverse)
        else:
            records = sorted(records, key=lambda r: r[1], reverse=reverse)

        shown = records[: self.MAX_SHOWN]
        for idx, (full, size, mtime) in enumerate(shown):
            tag = "evenrow" if idx % 2 == 0 else ""
            item_id = self.detail_tree.insert(
                "", "end", text=f"  {os.path.basename(full)}",
                values=(os.path.dirname(full), human_size(size), fmt_mtime(mtime)),
                tags=(tag,) if tag else (),
            )
            self._detail_paths[item_id] = full
            self._detail_sizes[item_id] = size
        self.on_detail_selection_changed()

    def show_types(self):
        self.types_view.tkraise()

    def on_detail_right_click(self, event):
        item = self.detail_tree.identify_row(event.y)
        if item:
            self.detail_tree.selection_set(item)
            self.detail_context_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_detail(self):
        sel = self.detail_tree.selection()
        if not sel:
            return
        path = self._detail_paths.get(sel[0])
        if path:
            open_in_file_manager(path)

    def copy_selected_detail_path(self):
        sel = self.detail_tree.selection()
        if not sel:
            return
        path = self._detail_paths.get(sel[0])
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def on_detail_selection_changed(self, event=None):
        sel = self.detail_tree.selection()
        count = len(sel)
        total = sum(self._detail_sizes.get(i, 0) for i in sel)
        if count:
            self.detail_selection_var.set(f"Selected: {count} file(s) · {human_size(total)}")
            self.detail_move_btn.config(state="normal")
        else:
            self.detail_selection_var.set("No files selected")
            self.detail_move_btn.config(state="disabled")

    def move_selected_detail(self):
        sel = list(self.detail_tree.selection())
        paths = [self._detail_paths[i] for i in sel if i in self._detail_paths]
        if not paths:
            return

        sizes = {self._detail_paths[i]: self._detail_sizes.get(i) for i in sel if i in self._detail_paths}
        result = confirm_and_delete(paths, sizes)
        if result is None:
            return
        moved, failed = result
        removed_set = {orig for orig, _target in moved}

        # Update in-memory aggregates so the summary table stays accurate
        # without requiring a full rescan.
        ext = self._current_type
        if ext in self.groups:
            self.groups[ext] = [r for r in self.groups[ext] if r[0] not in removed_set]
            remaining = self.groups[ext]
            if remaining:
                total = sum(r[1] for r in remaining)
                n = len(remaining)
                self.type_agg[ext] = {"count": n, "total_size": total, "avg_size": total / n}
            else:
                self.groups.pop(ext, None)
                self.type_agg.pop(ext, None)

        self._render_detail_table()
        self._render_types_table()


# ---------------------------------------------------------------------------
# Duplicates panel
# ---------------------------------------------------------------------------

class DuplicatesPanel(ttk.Frame):
    """
    Finds duplicate files via the three-stage funnel (size -> partial hash ->
    full/sampled hash). Files up to 1GB are fully verified ("Confirmed");
    files over 1GB are only sample-matched ("Likely") and require an extra
    manual acknowledgment before deletion. Same two-level summary/drill-down
    pattern as File Types, reusing the same select-and-delete flow.
    """

    MAX_SHOWN = 500

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.work_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.groups = {}           # group_id -> {"kind": "confirmed"/"likely", "files": [(path,size,mtime),...]}
        self._group_item_id = {}   # tree item id -> group_id (summary table)
        self._types_sort = ("reclaimable", True)
        self._detail_sort = ("size", True)
        self._current_group = None
        self._detail_paths = {}
        self._detail_sizes = {}

        self._build_ui()
        self.after(150, self._poll_queue)

    # ---- UI ----

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.info_var = tk.StringVar(value="No folder selected yet — choose one in Disk View.")
        ttk.Label(toolbar, textvariable=self.info_var).pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(toolbar, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side=tk.RIGHT)
        self.scan_btn = ttk.Button(toolbar, text="Scan for Duplicates", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side=tk.RIGHT, padx=(0, 6))

        ttk.Label(
            self, foreground="#888", wraplength=900, justify=tk.LEFT,
            text="Reads file contents to verify true duplicates, so this can take a while on large "
                 "folders. Files over 1GB are only sample-checked (start/middle/end), not fully read — "
                 "those show as \"Likely\" and need manual confirmation before deleting."
        ).pack(side=tk.TOP, anchor=tk.W, padx=12)

        self.container = ttk.Frame(self)
        self.container.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 12))

        self.types_view = ttk.Frame(self.container)
        self.detail_view = ttk.Frame(self.container)
        for v in (self.types_view, self.detail_view):
            v.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_types_view()
        self._build_detail_view()

        self.types_view.tkraise()

    def _build_types_view(self):
        bulk_bar = ttk.Frame(self.types_view)
        bulk_bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        ttk.Button(
            bulk_bar, text="Select All Duplicates to Delete (Keep Oldest, Confirmed Only)",
            command=self.bulk_select_confirmed
        ).pack(side=tk.LEFT)

        frame = ttk.Frame(self.types_view)
        frame.pack(fill=tk.BOTH, expand=True)

        columns = ("copies", "size_each", "reclaimable", "verification")
        self.types_tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        self.types_tree.heading("#0", text="Duplicate Group", command=lambda: self.sort_types("name"))
        self.types_tree.heading("copies", text="Copies", command=lambda: self.sort_types("copies"))
        self.types_tree.heading("size_each", text="Size Each", command=lambda: self.sort_types("size_each"))
        self.types_tree.heading("reclaimable", text="Reclaimable", command=lambda: self.sort_types("reclaimable"))
        self.types_tree.heading("verification", text="Verification", command=lambda: self.sort_types("verification"))
        self.types_tree.column("#0", width=280, minwidth=160)
        self.types_tree.column("copies", width=70, minwidth=60, anchor=tk.E)
        self.types_tree.column("size_each", width=100, minwidth=80, anchor=tk.E)
        self.types_tree.column("reclaimable", width=110, minwidth=90, anchor=tk.E)
        self.types_tree.column("verification", width=170, minwidth=120)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.types_tree.yview)
        self.types_tree.configure(yscrollcommand=vsb.set)
        self.types_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.types_tree.tag_configure("evenrow", background="#fafbfc")
        self.types_tree.tag_configure("likely", foreground="#a06400")
        self.types_tree.bind("<<TreeviewSelect>>", self.on_type_select)

    def _build_detail_view(self):
        top = ttk.Frame(self.detail_view)
        top.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        ttk.Button(top, text="< Back to Duplicates", command=self.show_types).pack(side=tk.LEFT)
        self.detail_title_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.detail_title_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(12, 0))

        self.detail_warning_var = tk.StringVar(value="")
        ttk.Label(
            self.detail_view, textvariable=self.detail_warning_var, foreground="#a06400",
            wraplength=900, justify=tk.LEFT
        ).pack(side=tk.TOP, anchor=tk.W, pady=(0, 4))

        select_bar = ttk.Frame(self.detail_view)
        select_bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.detail_selection_var = tk.StringVar(value="No files selected")
        ttk.Label(select_bar, textvariable=self.detail_selection_var, foreground="#555").pack(side=tk.LEFT)
        self.detail_move_btn = ttk.Button(
            select_bar, text="Move Selected to Review Folder", command=self.move_selected_detail, state="disabled"
        )
        self.detail_move_btn.pack(side=tk.RIGHT)

        frame = ttk.Frame(self.detail_view)
        frame.pack(fill=tk.BOTH, expand=True)

        columns = ("path", "size", "modified")
        self.detail_tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.detail_tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.detail_tree.heading("#0", text="Name", command=lambda: self.sort_detail("name"))
        self.detail_tree.heading("path", text="Folder", command=lambda: self.sort_detail("path"))
        self.detail_tree.heading("size", text="Size", command=lambda: self.sort_detail("size"))
        self.detail_tree.heading("modified", text="Last Modified", command=lambda: self.sort_detail("modified"))
        self.detail_tree.column("#0", width=260, minwidth=150)
        self.detail_tree.column("path", width=360, minwidth=150)
        self.detail_tree.column("size", width=100, minwidth=80, anchor=tk.E)
        self.detail_tree.column("modified", width=130, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.detail_tree.yview)
        self.detail_tree.configure(yscrollcommand=vsb.set)
        self.detail_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.detail_tree.tag_configure("evenrow", background="#fafbfc")
        self.detail_tree.bind("<Button-3>", self.on_detail_right_click)
        self.detail_tree.bind("<<TreeviewSelect>>", self.on_detail_selection_changed)

        self.detail_context_menu = tk.Menu(self, tearoff=0)
        self.detail_context_menu.add_command(label="Open Folder", command=self.open_selected_detail)
        self.detail_context_menu.add_command(label="Copy Path", command=self.copy_selected_detail_path)

    def refresh_folder_label(self):
        root = self.app.get_root_path()
        if root:
            self.info_var.set(f"Folder: {root}")
            self.scan_btn.config(state="normal")
        else:
            self.info_var.set("No folder selected yet — choose one in Disk View.")
            self.scan_btn.config(state="disabled")

    # ---- Scanning ----

    def start_scan(self):
        root = self.app.get_root_path()
        if not root:
            return
        self.show_types()
        self.groups.clear()
        self.types_tree.delete(*self.types_tree.get_children())
        self.stop_event.clear()
        self.scan_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.app.status.set(msg="Scanning for duplicates...", items=None, size=None)
        t = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        t.start()

    def stop_scan(self):
        self.stop_event.set()

    def _scan_worker(self, root):
        def progress(msg):
            self.work_queue.put(("progress", msg))
        confirmed, likely = find_duplicate_groups(root, stop_event=self.stop_event, progress_callback=progress)
        self.work_queue.put(("done", confirmed, likely, self.stop_event.is_set()))

    def _poll_queue(self):
        try:
            while True:
                msg = self.work_queue.get_nowait()
                if msg[0] == "progress":
                    self.app.status.set(msg=msg[1])
                elif msg[0] == "done":
                    _, confirmed, likely, stopped = msg
                    self._on_scan_done(confirmed, likely, stopped)
                self.work_queue.task_done()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _on_scan_done(self, confirmed, likely, stopped):
        self.scan_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

        self.groups = {}
        for i, files in enumerate(confirmed):
            self.groups[("confirmed", i)] = {"kind": "confirmed", "files": files}
        for i, files in enumerate(likely):
            self.groups[("likely", i)] = {"kind": "likely", "files": files}

        self._render_types_table()

        note = " (stopped early)" if stopped else ""
        total_reclaimable = sum(self._reclaimable(info) for info in self.groups.values())
        self.app.status.set(
            msg=f"Duplicate scan complete{note} — {len(self.groups)} group(s), "
                f"{human_size(total_reclaimable)} reclaimable",
            items=len(self.groups), size=human_size(total_reclaimable),
        )

    @staticmethod
    def _reclaimable(info):
        files = info["files"]
        if len(files) < 2:
            return 0
        size = files[0][1]
        return (len(files) - 1) * size

    # ---- Types (summary) table ----

    def sort_types(self, col):
        cur_col, cur_rev = self._types_sort
        if cur_col == col:
            self._types_sort = (col, not cur_rev)
        else:
            self._types_sort = (col, col != "name")
        self._render_types_table()

    def _render_types_table(self):
        self.types_tree.delete(*self.types_tree.get_children())
        self._group_item_id.clear()

        col, reverse = self._types_sort
        items = list(self.groups.items())
        if col == "name":
            items.sort(key=lambda kv: os.path.basename(kv[1]["files"][0][0]).lower(), reverse=reverse)
        elif col == "copies":
            items.sort(key=lambda kv: len(kv[1]["files"]), reverse=reverse)
        elif col == "size_each":
            items.sort(key=lambda kv: kv[1]["files"][0][1] if kv[1]["files"] else 0, reverse=reverse)
        elif col == "verification":
            items.sort(key=lambda kv: kv[1]["kind"], reverse=reverse)
        else:
            items.sort(key=lambda kv: self._reclaimable(kv[1]), reverse=reverse)

        for idx, (group_id, info) in enumerate(items):
            files = info["files"]
            if not files:
                continue
            name = os.path.basename(files[0][0])
            size_each = files[0][1]
            verification = "Confirmed" if info["kind"] == "confirmed" else "Likely (large file, unverified)"
            base_tag = "evenrow" if idx % 2 == 0 else ""
            tag = "likely" if info["kind"] == "likely" else base_tag
            item_id = self.types_tree.insert(
                "", "end", text=f"  {name}",
                values=(len(files), human_size(size_each), human_size(self._reclaimable(info)), verification),
                tags=(tag,) if tag else (),
            )
            self._group_item_id[item_id] = group_id

    def on_type_select(self, event):
        sel = self.types_tree.selection()
        if not sel:
            return
        group_id = self._group_item_id.get(sel[0])
        if group_id is not None:
            self.show_detail(group_id)

    # ---- Detail (drill-down) table ----

    def show_detail(self, group_id):
        self._current_group = group_id
        info = self.groups.get(group_id, {"kind": "confirmed", "files": []})
        files = info["files"]
        self.detail_title_var.set(
            f"{len(files)} copies · {human_size(files[0][1]) if files else '0 B'} each · "
            f"{human_size(self._reclaimable(info))} reclaimable"
        )
        if info["kind"] == "likely":
            self.detail_warning_var.set(
                "⚠ Not fully verified — these files only matched on sampled portions (start/middle/end), "
                "since they're over 1GB. Please manually confirm they're actually identical before deleting."
            )
        else:
            self.detail_warning_var.set("")
        self._detail_sort = ("size", True)
        self._render_detail_table()
        self.detail_view.tkraise()

    def sort_detail(self, col):
        cur_col, cur_rev = self._detail_sort
        if cur_col == col:
            self._detail_sort = (col, not cur_rev)
        else:
            self._detail_sort = (col, col == "size")
        self._render_detail_table()

    def _render_detail_table(self):
        self.detail_tree.delete(*self.detail_tree.get_children())
        self._detail_paths.clear()
        self._detail_sizes.clear()

        info = self.groups.get(self._current_group, {"files": []})
        records = info["files"]
        col, reverse = self._detail_sort
        if col == "name":
            records = sorted(records, key=lambda r: os.path.basename(r[0]).lower(), reverse=reverse)
        elif col == "path":
            records = sorted(records, key=lambda r: os.path.dirname(r[0]).lower(), reverse=reverse)
        elif col == "modified":
            records = sorted(records, key=lambda r: r[2], reverse=reverse)
        else:
            records = sorted(records, key=lambda r: r[1], reverse=reverse)

        shown = records[: self.MAX_SHOWN]
        for idx, (full, size, mtime) in enumerate(shown):
            tag = "evenrow" if idx % 2 == 0 else ""
            item_id = self.detail_tree.insert(
                "", "end", text=f"  {os.path.basename(full)}",
                values=(os.path.dirname(full), human_size(size), fmt_mtime(mtime)),
                tags=(tag,) if tag else (),
            )
            self._detail_paths[item_id] = full
            self._detail_sizes[item_id] = size
        self.on_detail_selection_changed()

    def show_types(self):
        self.types_view.tkraise()

    def on_detail_right_click(self, event):
        item = self.detail_tree.identify_row(event.y)
        if item:
            self.detail_tree.selection_set(item)
            self.detail_context_menu.tk_popup(event.x_root, event.y_root)

    def open_selected_detail(self):
        sel = self.detail_tree.selection()
        if not sel:
            return
        path = self._detail_paths.get(sel[0])
        if path:
            open_in_file_manager(path)

    def copy_selected_detail_path(self):
        sel = self.detail_tree.selection()
        if not sel:
            return
        path = self._detail_paths.get(sel[0])
        if path:
            self.clipboard_clear()
            self.clipboard_append(path)

    def on_detail_selection_changed(self, event=None):
        sel = self.detail_tree.selection()
        count = len(sel)
        total = sum(self._detail_sizes.get(i, 0) for i in sel)
        if count:
            self.detail_selection_var.set(f"Selected: {count} file(s) · {human_size(total)}")
            self.detail_move_btn.config(state="normal")
        else:
            self.detail_selection_var.set("No files selected")
            self.detail_move_btn.config(state="disabled")

    def move_selected_detail(self):
        info = self.groups.get(self._current_group)
        if not info:
            return
        sel = list(self.detail_tree.selection())
        paths = [self._detail_paths[i] for i in sel if i in self._detail_paths]
        if not paths:
            return

        if info["kind"] == "likely":
            if not messagebox.askyesno(
                "Unverified match",
                "These files were only matched by sampling large portions (start/middle/end) "
                "because they're over 1GB — they have NOT been fully verified as byte-for-byte "
                "identical. Are you sure these are duplicates before proceeding?"
            ):
                return

        if len(paths) >= len(info["files"]):
            if not messagebox.askyesno(
                "No copies will remain",
                f"This will remove all {len(paths)} cop{'y' if len(paths) == 1 else 'ies'} in this group "
                "— none will remain. Continue?"
            ):
                return

        sizes = {self._detail_paths[i]: self._detail_sizes.get(i) for i in sel if i in self._detail_paths}
        result = confirm_and_delete(paths, sizes)
        if result is None:
            return
        moved, failed = result
        removed_set = {orig for orig, _target in moved}

        if self._current_group in self.groups:
            remaining = [r for r in self.groups[self._current_group]["files"] if r[0] not in removed_set]
            if len(remaining) >= 2:
                self.groups[self._current_group]["files"] = remaining
            else:
                self.groups.pop(self._current_group, None)

        self._render_detail_table()
        self._render_types_table()

    # ---- Bulk convenience action ----

    def bulk_select_confirmed(self):
        candidates = []
        for group_id, info in self.groups.items():
            if info["kind"] != "confirmed":
                continue
            files = info["files"]
            if len(files) < 2:
                continue
            files_sorted = sorted(files, key=lambda r: r[2])  # oldest first
            candidates.extend(files_sorted[1:])  # keep the oldest, mark the rest

        if not candidates:
            messagebox.showinfo("No duplicates to clean up", "No confirmed duplicate groups were found.")
            return

        paths = [c[0] for c in candidates]
        result = confirm_and_delete(
            paths, {c[0]: c[1] for c in candidates},
            note="This keeps the oldest copy in each confirmed duplicate group and moves the others. "
                 "\"Likely\" (unverified, large-file) groups are excluded from this bulk action — "
                 "review those individually.",
        )
        if result is None:
            return
        moved, failed = result
        removed_set = {orig for orig, _target in moved}

        for group_id in list(self.groups.keys()):
            info = self.groups[group_id]
            remaining = [r for r in info["files"] if r[0] not in removed_set]
            if len(remaining) >= 2:
                info["files"] = remaining
            else:
                self.groups.pop(group_id, None)

        self._render_types_table()


# ---------------------------------------------------------------------------
# Cleanup panel (the cleanup tools as sub-tabs)
# ---------------------------------------------------------------------------

class CleanupPanel(ttk.Frame):
    """
    One sidebar item for all the cleanup tools — Old Files, Temp Files,
    Empty Folders, Leftovers, Duplicates — shown as sub-tabs. The tool
    panels themselves are unchanged: they're created here, set on the app
    as before (app.old_files, ...), and each one's refresh runs whenever
    its sub-tab is shown.
    """

    # (key, sub-tab label, panel class, method run when the sub-tab is shown)
    TOOLS = [
        ("old_files", "Old Files", OldFilesPanel, "refresh_folder_label"),
        ("temp_files", "Temp Files", TempFilesPanel, "refresh_folder_label"),
        ("empty_folders", "Empty Folders", EmptyFoldersPanel, "refresh_folder_label"),
        ("leftovers", "Leftovers", LeftoversPanel, "reload_exclusions"),
        ("duplicates", "Duplicates", DuplicatesPanel, "refresh_folder_label"),
    ]

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        self.pages = {}       # key -> panel
        self._on_show = {}    # panel widget name -> its refresh method
        for key, label, panel_class, on_show in self.TOOLS:
            panel = panel_class(self.notebook, app)
            self.notebook.add(panel, text=label)
            setattr(app, key, panel)
            self.pages[key] = panel
            self._on_show[str(panel)] = getattr(panel, on_show)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self.refresh_current())

    def select(self, key):
        self.notebook.select(self.pages[key])

    def refresh_current(self):
        refresh = self._on_show.get(self.notebook.select())
        if refresh:
            refresh()


# ---------------------------------------------------------------------------
# Review Folder panel
# ---------------------------------------------------------------------------

class ReviewFolderPanel(ttk.Frame):
    """
    Lists everything currently sitting in the review folders (the main one
    and one per other drive that has been used, see get_review_folders) —
    real deleted files, and whole folders (from the Leftovers tab) shown as
    one item each; sidecar .meta bookkeeping files are hidden.
    Supports Restore (reads the sidecar to move a file back to its
    original location, recreating missing folders and renaming on
    collision) and Delete Permanently (two-step confirmation).
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._paths = {}     # tree item id -> full path in review folder
        self._sizes = {}     # tree item id -> size
        self._metas = {}     # tree item id -> sidecar dict or None
        self._sort_state = ("deleted_on", True)
        self._build_ui()

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.path_var = tk.StringVar(value=get_review_folder_path())
        ttk.Label(toolbar, textvariable=self.path_var, foreground="#555").pack(side=tk.LEFT)

        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Open Review Folder", command=self.open_folder).pack(side=tk.RIGHT, padx=(0, 6))

        note_text = (
            "Deleted files land here. Files from another drive go to a review folder on that "
            "drive, so moving them is instant; all of them are listed together. These are normal "
            "folders — feel free to browse them yourself; the app only reads/writes them through "
            "the actions below. Open Review Folder opens the selected file's folder."
        )
        ttk.Label(self, text=note_text, foreground="#888", wraplength=820, justify=tk.LEFT).pack(
            side=tk.TOP, anchor=tk.W, padx=12, pady=(0, 6)
        )

        select_bar = ttk.Frame(self, padding=(12, 0))
        select_bar.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.selection_var = tk.StringVar(value="No files selected")
        ttk.Label(select_bar, textvariable=self.selection_var, foreground="#555").pack(side=tk.LEFT)
        self.delete_btn = ttk.Button(
            select_bar, text="Delete Permanently", command=self.delete_selected_permanently, state="disabled"
        )
        self.delete_btn.pack(side=tk.RIGHT)
        self.restore_btn = ttk.Button(
            select_bar, text="Restore Selected", command=self.restore_selected, state="disabled"
        )
        self.restore_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self.summary_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.summary_var, font=("Segoe UI", 9, "bold")).pack(
            side=tk.TOP, anchor=tk.W, padx=12, pady=(0, 4)
        )

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        columns = ("size", "original", "deleted_on")
        self.tree = TickTreeview(frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.add_select_buttons(select_bar, before=select_bar.pack_slaves()[0])
        self.tree.heading("#0", text="Name", command=lambda: self.sort_by("name"))
        self.tree.heading("size", text="Size", command=lambda: self.sort_by("size"))
        self.tree.heading("original", text="Original Location", command=lambda: self.sort_by("original"))
        self.tree.heading("deleted_on", text="Deleted On", command=lambda: self.sort_by("deleted_on"))
        self.tree.column("#0", width=260, minwidth=150)
        self.tree.column("size", width=90, minwidth=80, anchor=tk.E)
        self.tree.column("original", width=380, minwidth=150)
        self.tree.column("deleted_on", width=140, minwidth=100, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("evenrow", background="#fafbfc")
        self.tree.tag_configure("unrestorable", foreground="#999")
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_selection_changed)

        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Restore", command=self.restore_selected)
        self.context_menu.add_command(label="Delete Permanently", command=self.delete_selected_permanently)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Original Path", command=self.copy_selected_original_path)

        self._entries_cache = []  # raw scanned rows, kept for re-sorting without rescanning disk

    # ---- Listing ----

    def refresh(self):
        review_roots = get_review_folders()
        self.path_var.set("   ·   ".join(review_roots))
        self._entries_cache = []

        for review_root in review_roots:
            if not os.path.isdir(review_root):
                continue
            try:
                for entry in os.scandir(review_root):
                    if entry.name.endswith(".meta") or entry.name in (REVIEW_FOLDERS_FILE, LEFTOVER_EXCLUSIONS_FILE):
                        continue  # bookkeeping file, not a real deleted item
                    is_folder = entry.is_dir(follow_symlinks=False)
                    if not is_folder and not entry.is_file():
                        continue
                    meta = _read_sidecar(entry.path)
                    if is_folder:
                        if not meta:
                            continue  # a folder the app didn't put here
                        size = meta.get("size") or 0  # measured when moved; no walk on every refresh
                    else:
                        try:
                            size = entry.stat().st_size
                        except OSError:
                            continue
                    self._entries_cache.append({
                        "path": entry.path,
                        "name": entry.name + (" (folder)" if is_folder else ""),
                        "size": size,
                        "meta": meta,
                    })
            except OSError as e:
                self.summary_var.set(f"Couldn't read review folder {review_root}: {e}")
                self._render()
                return

        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._paths.clear()
        self._sizes.clear()
        self._metas.clear()

        col, reverse = self._sort_state
        if col == "name":
            rows = sorted(self._entries_cache, key=lambda r: r["name"].lower(), reverse=reverse)
        elif col == "size":
            rows = sorted(self._entries_cache, key=lambda r: r["size"], reverse=reverse)
        elif col == "original":
            rows = sorted(
                self._entries_cache,
                key=lambda r: (r["meta"] or {}).get("original_path", "").lower(),
                reverse=reverse,
            )
        else:  # deleted_on
            rows = sorted(
                self._entries_cache,
                key=lambda r: (r["meta"] or {}).get("deleted_at", 0),
                reverse=reverse,
            )

        total_size = 0
        for idx, row in enumerate(rows):
            total_size += row["size"]
            meta = row["meta"]
            original = meta.get("original_path", "—") if meta else "—"
            deleted_on = fmt_mtime(meta.get("deleted_at")) if meta else "—"
            base_tag = "evenrow" if idx % 2 == 0 else ""
            tag = "unrestorable" if not meta else base_tag
            item = self.tree.insert(
                "", "end", text=f"  {row['name']}",
                values=(human_size(row["size"]), original, deleted_on),
                tags=(tag,) if tag else (),
            )
            self._paths[item] = row["path"]
            self._sizes[item] = row["size"]
            self._metas[item] = meta

        self.summary_var.set(f"{len(rows):,} file(s) · {human_size(total_size)} total")
        self.on_selection_changed()

    def sort_by(self, col):
        current_col, current_reverse = self._sort_state
        reverse = (not current_reverse) if col == current_col else True
        self._sort_state = (col, reverse)
        self._render()

    # ---- Selection ----

    def on_selection_changed(self, event=None):
        sel = self.tree.selection()
        count = len(sel)
        total = sum(self._sizes.get(i, 0) for i in sel)
        if count:
            self.selection_var.set(f"Selected: {count} file(s) · {human_size(total)}")
            self.restore_btn.config(state="normal")
            self.delete_btn.config(state="normal")
        else:
            self.selection_var.set("No files selected")
            self.restore_btn.config(state="disabled")
            self.delete_btn.config(state="disabled")

    def on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.tk_popup(event.x_root, event.y_root)

    def copy_selected_original_path(self):
        sel = self.tree.selection()
        if not sel:
            return
        meta = self._metas.get(sel[0])
        if meta and meta.get("original_path"):
            self.clipboard_clear()
            self.clipboard_append(meta["original_path"])

    def open_folder(self):
        sel = self.tree.selection()
        if sel and sel[0] in self._paths:
            open_in_file_manager(self._paths[sel[0]])  # its own review folder, which may be on another drive
            return
        review_root = get_review_folder_path()
        os.makedirs(review_root, exist_ok=True)
        open_in_file_manager(review_root)

    # ---- Restore ----

    def _shown_entries(self, sel):
        """
        For the selected rows: review-folder path -> (original path, size),
        for the dialogs, which show where each item came from rather than its
        flattened review-folder name (its own path if there's no record).
        Built once by path: searching the rows for each file got slow with
        thousands selected.
        """
        shown = {}
        for i in sel:
            meta = self._metas.get(i)
            original = meta.get("original_path") if meta else None
            shown[self._paths[i]] = (original or self._paths[i], self._sizes.get(i))
        return shown

    def restore_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            return
        paths = [self._paths[i] for i in sel]
        meta_by_path = {self._paths[i]: self._metas.get(i) for i in sel}
        shown = self._shown_entries(sel)

        restorable = [p for p in paths if meta_by_path.get(p)]
        no_meta_count = len(paths) - len(restorable)

        if not restorable:
            messagebox.showinfo(
                "Can't restore", "None of the selected files have an original-location record "
                "(likely edited or added outside the app), so they can't be restored."
            )
            return

        total = _sum_sizes(shown[p][1] for p in restorable) or 0
        if not ask_items(
            "Restore files",
            f"Restore {_plural(len(restorable), 'item')} ({human_size(total)}) to their original locations?",
            group_items([shown[p] for p in restorable], by_type=False, noun="item"),
            note=f"{no_meta_count} of the selected item(s) have no location record and will be skipped."
            if no_meta_count else "",
        ):
            return

        restored, failed, skipped = run_with_progress("Restoring", restore_files, restorable)
        self._forget_rows(sel, {rp for rp, _target in restored})

        done = {rp for rp, _target in restored} | {rp for rp, _err in failed} | set(skipped)
        show_items(
            "Restore complete",
            f"Restored {len(restored):,} of {_plural(len(restorable), 'item')}.",
            result_groups([
                ("Restored", [(target, shown[rp][1]) for rp, target in restored]),
                ("Failed", [(*shown[rp], err) for rp, err in failed]),
                ("Skipped (no location record)", [shown[rp] for rp in skipped]),
                ("Cancelled — still in the review folder", [shown[p] for p in restorable if p not in done]),
            ], noun="item"),
        )

    def _forget_rows(self, items, gone):
        """
        Drop the rows (and cached entries) whose file is in the set `gone`,
        in one pass, after a restore or delete. Set lookups and a single
        tree.delete keep this quick with thousands of files.
        """
        rows = [i for i in items if self._paths.get(i) in gone]
        if rows:
            self.tree.delete(*rows)
        for i in rows:
            self._paths.pop(i, None)
            self._sizes.pop(i, None)
            self._metas.pop(i, None)
        self._entries_cache = [r for r in self._entries_cache if r["path"] not in gone]
        self.on_selection_changed()

    # ---- Delete Permanently ----

    def delete_selected_permanently(self):
        sel = list(self.tree.selection())
        if not sel:
            return
        paths = [self._paths[i] for i in sel]
        shown = self._shown_entries(sel)
        total_size = _sum_sizes(size for _original, size in shown.values()) or 0

        # Step 1: plain count-only warning.
        if not messagebox.askyesno(
            "Delete Permanently",
            f"{len(paths)} selected file(s) will be permanently deleted. Continue?"
        ):
            return

        # Step 2: itemized confirmation, grouped by where each item came from.
        if not ask_items(
            "Confirm Permanent Deletion",
            f"Permanently delete {_plural(len(paths), 'item')} ({human_size(total_size)})?",
            group_items([shown[p] for p in paths], by_type=False, noun="item"),
            note="Are you sure? This cannot be undone.",
        ):
            return

        deleted, failed = run_with_progress("Deleting", delete_permanently, paths)
        self._forget_rows(sel, set(deleted))

        done = set(deleted) | {p for p, _err in failed}
        show_items(
            "Done",
            f"Permanently deleted {len(deleted):,} of {_plural(len(paths), 'item')}.",
            result_groups([
                ("Deleted", [shown[p] for p in deleted]),
                ("Failed", [(*shown[p], err) for p, err in failed]),
                ("Cancelled — still in the review folder", [shown[p] for p in paths if p not in done]),
            ], noun="item"),
        )


# ---------------------------------------------------------------------------
# Settings panel
# ---------------------------------------------------------------------------

class SettingsPanel(ttk.Frame):
    """
    The current scanned folder and review folder locations (read-only), and
    the Leftovers exceptions: folders the Leftovers scan must never list,
    added here with a folder picker or from the Leftovers tab itself.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._exception_items = {}  # tree item id -> _path_key of that exception
        self._build_ui()

    def _build_ui(self):
        wrap = ttk.Frame(self, padding=20)
        wrap.pack(side=tk.TOP, fill=tk.X, anchor=tk.N)

        ttk.Label(wrap, text="Settings", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        self._row(wrap, 1, "Currently scanned folder:", "root_var")
        self._row(wrap, 2, "Review folder locations:", "review_var")

        ttk.Button(wrap, text="Open Review Folder", command=self.open_review_folder).grid(
            row=3, column=0, sticky="w", pady=(14, 0)
        )
        ttk.Button(wrap, text="Refresh", command=self.refresh).grid(
            row=3, column=1, sticky="w", pady=(14, 0), padx=(8, 0)
        )

        exceptions = ttk.Frame(self, padding=(20, 0, 20, 20))
        exceptions.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        ttk.Label(exceptions, text="Leftovers exceptions", font=("Segoe UI", 11, "bold")).pack(
            anchor=tk.W, pady=(10, 2)
        )
        ttk.Label(
            exceptions, foreground="#888", wraplength=760, justify=tk.LEFT,
            text="Folders listed here are never shown in Cleanup → Leftovers, for example a game you still "
                 "play or a portable app. You can also hide folders right in the Leftovers tab, with Hide "
                 "Selected or by right-clicking them.",
        ).pack(anchor=tk.W, pady=(0, 6))

        bar = ttk.Frame(exceptions)
        bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(bar, text="Add Folder...", command=self.add_exception).pack(side=tk.LEFT)
        self.remove_exception_btn = ttk.Button(
            bar, text="Remove Selected", command=self.remove_exceptions, state="disabled"
        )
        self.remove_exception_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.exceptions_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.exceptions_var, foreground="#555").pack(side=tk.RIGHT)

        frame = ttk.Frame(exceptions)
        frame.pack(fill=tk.BOTH, expand=True)
        self.exceptions_tree = TickTreeview(frame, show="tree", selectmode="extended", height=8)
        self.exceptions_tree.add_select_buttons(bar)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.exceptions_tree.yview)
        self.exceptions_tree.configure(yscrollcommand=vsb.set)
        self.exceptions_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.exceptions_tree.bind(
            "<<TreeviewSelect>>",
            lambda e: self.remove_exception_btn.config(
                state="normal" if self.exceptions_tree.selection() else "disabled"
            ),
        )

    def _show_exceptions(self):
        exceptions = load_leftover_exclusions()
        self.exceptions_tree.delete(*self.exceptions_tree.get_children())
        self._exception_items.clear()
        for key, path in sorted(exceptions.items(), key=lambda kv: kv[1].casefold()):
            item = self.exceptions_tree.insert("", "end", text=f"  {path}")
            self._exception_items[item] = key
        self.exceptions_var.set(f"{len(exceptions):,} folder(s) hidden" if exceptions else "No folders hidden yet")
        self.remove_exception_btn.config(state="disabled")

    def add_exception(self):
        path = filedialog.askdirectory(title="Choose a folder the Leftovers scan should never list", mustexist=True)
        if not path:
            return
        path = os.path.normpath(path)
        exceptions = load_leftover_exclusions()
        if _path_key(path) not in exceptions:
            roots = {_path_key(p) for _label, p, _admin in leftover_locations()}
            if _path_key(os.path.dirname(path)) not in roots and not messagebox.askyesno(
                "Not a scanned folder",
                f"{path}\n\nThis folder isn't one the Leftovers scan checks (it only looks at the folders "
                "directly inside AppData, ProgramData and Program Files), so hiding it changes nothing.\n\n"
                "Add it anyway?"
            ):
                return
            exceptions[_path_key(path)] = path
            try:
                save_leftover_exclusions(exceptions.values())
            except OSError as e:
                messagebox.showerror("Couldn't save", f"Couldn't save the list of hidden folders:\n{e}")
                return
        self._show_exceptions()

    def remove_exceptions(self):
        keys = {self._exception_items[i] for i in self.exceptions_tree.selection() if i in self._exception_items}
        if not keys:
            return
        remaining = [p for k, p in load_leftover_exclusions().items() if k not in keys]
        try:
            save_leftover_exclusions(remaining)
        except OSError as e:
            messagebox.showerror("Couldn't save", f"Couldn't save the list of hidden folders:\n{e}")
            return
        self._show_exceptions()

    def _row(self, parent, row, label, var_attr):
        ttk.Label(parent, text=label, foreground="#555").grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value="")
        setattr(self, var_attr, var)
        ttk.Label(parent, textvariable=var, font=("Segoe UI", 9, "bold")).grid(
            row=row, column=1, sticky="w", padx=(12, 0), pady=3
        )

    def refresh(self):
        root = self.app.get_root_path()
        self.root_var.set(root if root else "(none selected — choose one in Disk View)")
        self.review_var.set("\n".join(get_review_folders()))
        self._show_exceptions()

    def open_review_folder(self):
        review_root = get_review_folder_path()
        os.makedirs(review_root, exist_ok=True)
        open_in_file_manager(review_root)


# ---------------------------------------------------------------------------
# Main App (sidebar shell)
# ---------------------------------------------------------------------------

class App(tk.Tk):
    NAV_ITEMS = [
        ("disk_view", "Disk View"),
        ("file_types", "File Types"),
        ("cleanup", "Cleanup"),  # Old Files, Temp Files, Empty Folders, Leftovers, Duplicates (CleanupPanel)
        ("review_folder", "Review Folder"),
        ("settings", "Settings"),
    ]

    def __init__(self):
        super().__init__()
        self.title("Folder Size Explorer")
        self.geometry("1080x680")
        self.minsize(760, 480)
        self.root_path = None

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook.Tab", font=("Segoe UI", 10), padding=(12, 5))  # Cleanup's sub-tabs

        self._build_ui()

    def _build_ui(self):
        body = ttk.Frame(self)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        ttk.Separator(self, orient="horizontal").pack(side=tk.BOTTOM, fill=tk.X)
        self.status = StatusBar(self)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        sidebar = tk.Frame(body, width=170, bg="#f7f8fa")
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        header = tk.Label(sidebar, text="Folder Size\nExplorer", bg="#f7f8fa",
                           font=("Segoe UI", 12, "bold"), justify=tk.LEFT, anchor="w")
        header.pack(fill=tk.X, padx=14, pady=(16, 18))

        content = ttk.Frame(body)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.disk_view = DiskViewPanel(content, self)
        self.file_types = FileTypesPanel(content, self)
        self.cleanup = CleanupPanel(content, self)  # also sets self.old_files, self.leftovers, ...
        self.review_folder = ReviewFolderPanel(content, self)
        self.settings = SettingsPanel(content, self)
        self._panel_map = {
            "disk_view": self.disk_view,
            "file_types": self.file_types,
            "cleanup": self.cleanup,
            "review_folder": self.review_folder,
            "settings": self.settings,
        }
        for p in self._panel_map.values():
            p.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.nav_buttons = {}
        for key, label in self.NAV_ITEMS:
            btn = tk.Button(
                sidebar, text=label, anchor="w", relief=tk.FLAT, bd=0,
                bg="#f7f8fa", activebackground="#e8edff", fg="#333",
                font=("Segoe UI", 10), padx=14, pady=9, cursor="hand2",
                command=lambda k=key: self.show_panel(k),
            )
            btn.pack(fill=tk.X, padx=8, pady=1)
            self.nav_buttons[key] = btn

        self.show_panel("disk_view")

    def show_panel(self, key):
        """Show a sidebar item; a cleanup tool's key ("old_files", "leftovers", ...) opens Cleanup on that sub-tab."""
        tool = key if key in self.cleanup.pages else None
        if tool:
            key = "cleanup"
        for k, btn in self.nav_buttons.items():
            active = (k == key)
            btn.config(
                bg="#e0e9ff" if active else "#f7f8fa",
                font=("Segoe UI", 10, "bold" if active else "normal"),
            )
        self._panel_map[key].tkraise()
        if key == "cleanup":
            if tool:
                self.cleanup.select(tool)
            self.cleanup.refresh_current()
        elif key == "file_types":
            self.file_types.refresh_folder_label()
        elif key == "review_folder":
            self.review_folder.refresh()
        elif key == "settings":
            self.settings.refresh()

    def get_root_path(self):
        return self.root_path

    def set_root_path(self, path):
        self.root_path = path


if __name__ == "__main__":
    app = App()
    app.mainloop()
