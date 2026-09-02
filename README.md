# Project NovaAI

## Large Files Excluded from Repository

The following files are present in the working directory but are **not tracked** in the Git repository because they exceed GitHub's file size limits and are ignored via `.gitignore`.

| Filename | Path | Size | Reason for exclusion | Contents description | How to obtain / recreate |
|---|---|---|---|---|---|
| wikipedia_en_geography_nopic_2026-04.zim | `data/maps/wikipedia_en_geography_nopic_2026-04.zim` | 521 MB | Exceeds GitHub 100 MB limit; ignored by `*.zim` rule in `.gitignore`. | ZIM archive containing Wikipedia geography data without pictures. | Re‑download from the original source or generate using the ZIM creation tools used by the project. |
| wikipedia_en_simple_all_mini_2026-05.zim | `data/zim/wikipedia_en_simple_all_mini_2026-05.zim` | 469 MB | Exceeds GitHub 100 MB limit; ignored by `*.zim` rule in `.gitignore`. | Small‑footprint ZIM archive of simplified English Wikipedia. | Re‑download from the source or recreate with the ZIM tooling provided in the project documentation. |
| NOVA-Setup.exe | `packaging/out/NOVA-Setup.exe` | 307 MB | Exceeds GitHub 100 MB limit; ignored by `*.exe` rule in `.gitignore`. | Windows installer executable for the NovaAI application. | Build the installer locally using the project's build scripts (`pyinstaller` or similar) as described in the developer guide. |

These files are required for full functionality but are intentionally omitted from the repository to keep the repo size manageable and to comply with GitHub's file size restrictions. Users should obtain them separately following the instructions above.
