# Folder Size Explorer

A lightweight desktop application for Windows, macOS, and Linux that
helps you understand what is using disk space and safely review files
you may want to remove.

Built with Python and Tkinter, Folder Size Explorer uses Python's
standard library and does not require third-party Python packages.

## Features

The sidebar has five items: **Disk View**, **File Types**, **Cleanup**,
**Review Folder** and **Settings**. **Cleanup** holds the cleanup tools
as tabs along its top: Old Files, Temp Files, Empty Folders, Leftovers
and Duplicates.

### Selecting items

-   Every list where you can pick several items (Old Files, Temp Files,
    Empty Folders, Leftovers, a file type's files, a duplicate group's
    copies, Review Folder, and the Leftovers exceptions in Settings) shows
    a tick box on each row: ☐ / ☑.
-   Click a row to tick or untick it; no Ctrl needed. Shift+click ticks a
    range.
-   **Select All** and **Select None** sit at the left of the list's
    buttons. Ctrl+A also ticks everything.
-   The File Types and Duplicates overviews stay one at a time: click a
    type or group to open it, then tick its files.
-   Confirmation and result boxes list the items grouped by type, then
    folder (folders by location). Click a group to open it. The box always
    fits on screen.

### Disk View

-   Choose a folder and browse its contents in an expandable size tree.
-   View item sizes, file and folder counts, percentage of the parent
    folder, and last-modified dates.
-   See a treemap-style strip of the largest items at the selected
    level.
-   Rescan folders, open selected locations in your file manager, and
    copy paths.

### File Types

-   Scan files recursively and group them by extension.
-   Drill into a file type to review individual files.
-   Select files to move to the review folder.

### Cleanup

The cleanup tools, each on its own tab inside **Cleanup**.

#### Old Files

-   Recursively find files and list the oldest first.
-   Only lists files not modified for a while: pick **Older than** 15
    days, 30 days (the default), 6 months or 1 year, then scan.
-   Displays up to 500 results to keep the interface responsive.
-   Supports multi-selection and moving selected files to the review
    folder.

#### Temp Files

-   Find common temporary or junk-file patterns, including `.tmp`,
    `.temp`, `.bak`, `.old`, `.log`, `.swp`, `.crdownload`, `.part`,
    `Thumbs.db`, `desktop.ini`, `.DS_Store`, and Office lock files.
-   Only includes matching files that are at least one day old.
-   Displays up to 500 results, with the largest files first.
-   Supports multi-selection and moving selected files to the review
    folder.

#### Empty Folders

-   Find folders whose entire directory tree contains no files.
-   Re-checks folders before deleting them.
-   Empty folders are deleted directly rather than moved to the review
    folder. Review the selected paths carefully before confirming.

#### Leftovers (Windows)

-   Finds folders that uninstalled programs left behind. It works on its
    own, with no need to pick a folder in Disk View.
-   **Where it looks:** the top-level folders in `AppData\Roaming`,
    `AppData\Local`, `AppData\LocalLow`, `AppData\Local\Programs`,
    `ProgramData`, `Program Files` and `Program Files (x86)`.
-   **What it lists:** a folder that no installed program accounts for.
    "Installed programs" means Windows' list of installed programs (from
    the registry) plus Store apps.
-   **What it leaves out.** The matching is deliberately generous, so a
    folder isn't listed if:
    -   its name loosely matches a program's name or publisher (for
        example, `StardewValley` matches "Stardew Valley")
    -   it is a program's install folder
    -   it has the same name as a folder some program installed into
        (for example, `LGHUB`)
    -   its name is shorter than 4 letters
    -   it is one of Windows' own folders, a shared component (such as
        anti-cheat or crash reporters), or a link
-   **Recently used folders are hidden.** Folders changed in the last 90
    days are probably still in use, so they only appear when you tick
    **Also show folders used in the last 90 days**.
-   **Notes on each row:**
    -   **may hold game saves:** a `Saves`/`Saved` folder or `.sav` files
        were found inside.
    -   **needs admin:** in Program Files or ProgramData, where only an
        administrator can move folders. **Restart as Administrator**
        reopens the app with those rights; Windows asks for permission.
-   **Exceptions:** you can keep a folder out of future scans, for
    example a game you still play or a portable app. There are three ways:
    -   select it (or several) and press **Hide Selected (Not a
        Leftover)**
    -   right-click it and choose **Not a Leftover — Hide It**
    -   add it in **Settings → Leftovers exceptions** (see below)
-   **Hidden folders:**
    -   They are remembered between runs, in `.leftover_exclusions.json`
        inside the home review folder.
    -   Tick **Show folders you hid** to see them again (marked "hidden by
        you"). Then press **Unhide Selected**, or right-click and choose
        **Unhide**, to have them listed again.
-   **Moving:** nothing is selected for you. Selected folders move
    **whole** into the review folder on their own drive, appear there as
    one item, and can be restored.
-   **Check before moving:** it can't tell an uninstalled program from a
    game or portable app that never registered with Windows, so look
    through the list first. Registry entries are not touched.

#### Duplicates

-   Finds duplicate candidates using a staged process: file size, a
    partial hash, and then a final hash check.
-   Files up to 1 GB are checked using a full-content hash and can be
    reported as confirmed duplicates.
-   Files larger than 1 GB are checked using samples from the beginning,
    middle, and end. Matching samples are labelled **Likely**, not
    confirmed.
-   Provides a convenience action to select duplicates while keeping the
    oldest file in confirmed groups.

### Review Folder

Files moved from the Old Files, Temp Files, File Types, and Duplicates
views, and whole folders moved from the Leftovers view, are placed in a
review folder **on the same drive as the file**:

``` text
~/FolderSizeExplorer_Review          files on your home drive
D:\FolderSizeExplorer_Review         files on D: (any other drive gets its own, at its root)
```

-   Because a file never leaves its drive, moving it is a rename: it is
    instant whatever the file's size, and it doesn't use up space on your
    home drive.
-   Moves, restores and permanent deletes run in the background behind
    a progress window with a **Cancel** button (it stops after the
    current file), so the app stays responsive, even with thousands of
    files. Small batches finish before the window appears.
-   The Review Folder view lists the files from every review folder
    together. A drive that isn't connected is skipped until it is
    plugged in again. The other drives' review folders are remembered in
    `.review_folders.json` inside the home review folder.
-   A folder moved from Leftovers is one item, marked **(folder)**, with
    the size it had when moved. Restore puts the whole folder back, and
    Delete Permanently removes it with everything inside (read-only files
    included). A folder is never copied across drives: if its drive has
    no review folder, the move is refused and the folder is left as it
    was.
-   Original paths are recorded in `.meta` sidecar files.
-   Restore recreates missing parent directories and avoids overwriting
    an existing file by choosing a unique name.
-   Files can be permanently deleted from the Review Folder view when
    you are sure you no longer need them.

**Important:** Moving a file to the review folder is not the same as
permanently deleting it. If the app can't create a review folder at a
drive's root (no write permission), it falls back to the home review
folder. Those moves copy the file across drives and are slow. A file
that is open in another program is not moved: it is reported as failed,
and no partial copy is left behind. Back up important data before
cleanup. Empty-folder deletion is direct and is not recoverable through
the review folder.

### Settings

-   Shows the folder currently being scanned and where the review
    folders are.
-   **Leftovers exceptions:** the folders that Cleanup → Leftovers never
    lists, whether you hid them in the Leftovers tab or added them here.
    -   **Add Folder...** picks a folder to add. The Leftovers scan only
        checks folders directly inside AppData, ProgramData and Program
        Files, so for any other folder it asks first, since hiding it
        would change nothing.
    -   **Remove Selected** takes folders off the list, and they are listed
        again in the next scan.

## Requirements

-   Python 3.9 or later recommended.
-   Tkinter, which is included with most standard Python installations.
-   Read access to the folder you want to scan.
-   Write/delete permissions for files you choose to move or folders you
    choose to delete.

On some Linux distributions, Tkinter may need to be installed separately
(for example, through the distribution's `python3-tk` package).

## Run the application

1.  Save `folder_size_tree.py` to a local folder.
2.  Open a terminal or command prompt in that folder.
3.  Run:

``` bash
python folder_size_tree.py
```

On systems where Python 3 is invoked as `python3`, use:

``` bash
python3 folder_size_tree.py
```

The application window should open. Select **Disk View → Browse...** to
choose the folder you want to inspect.

## Basic workflow

1.  Open **Disk View** and select a folder.
2.  Review the size tree and treemap to identify large folders and
    files.
3.  Use **File Types**, or the **Old Files**, **Temp Files** or
    **Duplicates** tabs in **Cleanup**, to investigate potential cleanup
    candidates.
4.  Select the items you want to review and choose **Move Selected to
    Review Folder**.
5.  Open **Review Folder** to restore files or permanently remove them
    after checking.
6.  Use **Cleanup → Empty Folders** separately and confirm the selected
    paths before direct deletion.
7.  On Windows, use **Cleanup → Leftovers** to find folders that
    uninstalled programs left behind:
    -   Move the ones you're sure about to the review folder.
    -   Hide the ones that aren't leftovers, so they stop showing up.

Rescan relevant views after moving files so displayed results reflect
the current filesystem state.

## Safety and limitations

-   The app does not silently permanently delete files moved through the
    file-review workflow; it moves them to the review folder after
    confirmation.
-   The **Empty Folders** view permanently removes selected empty
    directories after confirmation and a final emptiness check.
-   Duplicate matches for files larger than 1 GB are only **Likely**
    matches because the full contents are not hashed. Manually verify
    these before removing anything.
-   **Leftovers** can't tell an uninstalled program from a game or
    portable app that never registered with Windows. Check its list, and
    especially folders marked **may hold game saves**, before moving
    anything. It only looks at top-level folders and never touches the
    registry.
-   Files that cannot be accessed because of permissions, filesystem
    errors, or changing paths may be skipped.
-   Old Files, Temp Files, and File Types limit displayed results to 500
    entries. A scan may find more than are shown.
-   Scanning large folders or checking duplicates can take time,
    especially when files need full hashing.
-   The app excludes its review folders (any folder named
    `FolderSizeExplorer_Review`, on any drive) from scans to avoid
    reprocessing items awaiting review.

## Project structure

The application is currently implemented as a single Python file:

``` text
folder-size-explorer/
└── folder_size_tree.py
```

## Troubleshooting

-   **The app does not start:** Confirm Python is installed and Tkinter
    is available. Run the command from a terminal to see any error
    messages.
-   **A folder appears to be missing items:** Check permissions and
    rescan. Inaccessible files and directories may be skipped.
-   **A file cannot be moved or restored:** Check that the file is not
    in use (an open file fails with "being used by another process") and
    that you have the required permissions and available disk space.
-   **A duplicate is labelled Likely:** The file is larger than 1 GB and
    was sample-checked rather than fully hashed. Verify it manually
    before cleanup.

## License

No license has been specified yet. Add a license before distributing or
publishing this project if you want to define how others may use,
modify, or redistribute it.
