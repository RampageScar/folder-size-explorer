# Folder Size Explorer

A lightweight desktop application built with Python and Tkinter to analyze disk usage, identify large files and folders, find cleanup candidates, and manage disk space safely.

Folder Size Explorer provides an expandable folder-size tree, file-type analysis, duplicate detection, and cleanup tools — with a review folder that lets you restore moved files before permanently deleting them.

## Features

### Disk View
- Scan a selected folder and explore its contents in an expandable size tree.
- Calculate folder sizes recursively.
- View the largest files and folders first.
- See a treemap-style strip of the largest items at the current level.
- Track scan time, item counts, and total size.

### File Types
- Group files by extension.
- View file counts and total storage used by each file type.
- Drill down into individual files.
- Select multiple files for cleanup.

### Cleanup Tools

- **Old Files:** Find files that have not been modified for a selected period, from 15 days to one year.
- **Temp Files:** Identify common temporary files, logs, backups, browser partial downloads, and Office lock files that are at least 24 hours old.
- **Empty Folders:** Find folders whose entire subtree contains no files.
- **Leftovers (Windows):** Identify potential folders left behind by uninstalled programs in AppData, ProgramData, and Program Files. Supports exclusions and administrator access where required.
- **Duplicates:** Find potential duplicate files using file sizes, partial hashes, and content hashes. Large files over 1 GB are sample-checked and marked as likely duplicates rather than confirmed duplicates.

### Review Folder
- Move selected files and folders into a review folder instead of permanently deleting them immediately.
- Restore moved items to their original locations.
- Automatically rename restored items if a name conflict exists.
- Permanently delete reviewed items when you are ready.
- View items stored in review folders across multiple drives.

### Settings
- View the current scan location and review folder locations.
- Manage exclusions for the Leftovers scanner.

## Safety Features

- **Review before permanent deletion:** Cleanup actions move selected items into a review folder.
- **Restore support:** Metadata files preserve original paths to enable restoration.
- **Collision handling:** Unique filenames prevent items from overwriting one another in the review folder.
- **Progress feedback:** Longer move, restore, and permanent-delete operations run on a worker thread with a progress window and cancellation support.
- **Review-folder exclusion:** Review folders are excluded from normal scans to prevent their contents from being counted as ordinary files.

> **Important:** Review items before permanently deleting them. Leftover detection is heuristic-based, and some files marked as temporary or old may still be needed by an application. Back up important data before performing cleanup operations.

## Requirements

- Windows, Linux, or macOS for the core application.
- Python 3.
- Tkinter, which is included with many standard Python installations.

The application uses Python's standard library and does not require third-party Python packages.

**Note:** The Leftovers scanner is Windows-specific.

## Installation

1. Install Python 3 from [python.org](https://www.python.org/downloads/).
2. Clone this repository or download the source code.
3. Open a terminal in the project directory.
4. Run the application:

```bash
python folder_size_tree.py
```

On Windows, you can also try:

```powershell
py folder_size_tree.py
```

If Tkinter is unavailable in your Python installation, install the appropriate Tkinter package for your operating system.

## Usage

1. Launch Folder Size Explorer.
2. Select a folder to scan in Disk View.
3. Explore folder sizes and identify large files.
4. Use File Types or the Cleanup tools to find items you may no longer need.
5. Review your selections before confirming a cleanup operation.
6. Open Review Folder to restore items or permanently delete them.
7. Use Settings to manage Leftovers exclusions.

Scanning large directories may take time, especially when folders contain many files or access permissions are restricted.

## Technology Stack

- **Language:** Python
- **GUI:** Tkinter and ttk
- **File system operations:** `os`, `os.scandir`, `shutil`
- **Duplicate detection:** SHA-1 hashing
- **Background operations:** Python threading and queues
- **Persistent review metadata:** JSON

## Platform Support

| Feature | Windows | Linux / macOS |
|---|---|---|
| Disk View | Supported | Supported |
| File Types | Supported | Supported |
| Old Files | Supported | Supported |
| Temp Files | Supported | Supported |
| Empty Folders | Supported | Supported |
| Duplicates | Supported | Supported |
| Review and Restore | Supported | Supported |
| Leftovers Scanner | Supported | Not available |

Actual access to files and folders depends on operating-system permissions.

## Project Structure

```text
Folder-Size-Explorer/
├── folder_size_tree.py
├── README.md
```

The application is implemented in a single Python source file. Additional project files may be added as the project evolves.

## License

No license has been specified yet. If you plan to publish or distribute this project publicly, consider adding a license file that defines how others may use, modify, and redistribute your code.
