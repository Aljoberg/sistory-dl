# SIstory-dl

A simple script for downloading SIstory publications. :D

Required Python 3.10 or newer and the `requests` library. It will automatically install when you first run the script.

To run:

```bash
uv run main.py 1/7/397/407
```

To run this, you'll need a _menu path_, the thing after `/menu/` in a SIstory URL. The script creates a `downloads` directory, mirrors the menu's folder names recursively, and downloads the publication files into the matching folders. Files are named after their publication titles and receive a per-folder sequence number, for example `01 - Publication title.pdf`. If a publication has multiple files, their names end in `-file1`, `-file2`, and so on. Existing files are skipped. :P

You can change the output directory with `--output` (or `-o`):

```bash
uv run main.py 1/7/397/407 --output ./sistory-files
```

You can `--dry-run` if you want to inspect what would be downloaded without creating folders or downloading files.

Downloads are streamed directly to `.part` files and atomically renamed when complete.
