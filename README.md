# Folder Size Explorer

A lightweight desktop application for Windows, macOS, and Linux that
helps you understand what is using disk space and safely review files
you may want to remove.

Built with Python and Tkinter, Folder Size Explorer uses Python's
standard library and does not require third-party Python packages.

## Features

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

### Old Files

-   Recursively find files and list the oldest first.
-   Displays up to 500 results to keep the interface responsive.
-   Supports multi-selection and moving selected files to the review
    folder.

### Temp Files

-   Find common temporary or junk-file patterns, including `.tmp`,
    `.temp`, `.bak`, `.old`, `.log`, `.swp`, `.crdownload`, `.part`,
    `Thumbs.db`, `desktop.ini`, `.DS_Store`, and Office lock files.
-   Only includes matching files that are at least one day old.
-   Displays up to 500 results, with the largest files first.
-   Supports multi-selection and moving selected files to the review
    folder.

### Empty Folders

-   Find folders whose entire directory tree contains no files.
-   Re-checks folders before deleting them.
-   Empty folders are deleted directly rather than moved to the review
    folder. Review the selected paths carefully before confirming.

### Duplicates

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
views are placed in a review folder **on the same drive as the file**:

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
3.  Use **File Types**, **Old Files**, **Temp Files**, or **Duplicates**
    to investigate potential cleanup candidates.
4.  Select the items you want to review and choose **Move Selected to
    Review Folder**.
5.  Open **Review Folder** to restore files or permanently remove them
    after checking.
6.  Use **Empty Folders** separately and confirm the selected paths
    before direct deletion.

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
