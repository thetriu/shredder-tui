# SHREDDER

A terminal file & folder shredder with a full TUI: folder navigation, image
and video previews, select-by-date, and word-lookup search — all in one
Python file with no required dependencies.

Runs on **Termux (Android)** and **Linux** (Arch/CachyOS, Debian, Fedora, …).
Same script, same features, on both.

```
 ◆ SHREDDER  secure eraser                              STANDARD 3×
 ⌂ ~/Downloads                                        14 items · name
    NAME                                          SIZE           DATE
 ▸  Projects/                                        —   2026-09-21
 ▸  Screenshots/                                      —   2026-09-19
 ●  budget_report.txt                              12K   2026-08-04
    old_backup.zip                                 88M   2026-06-11
```

## Features

- **Folder navigation** — arrow keys / `hjkl`, `:` to jump to a path
  (Tab-completes), mouse & touch support, sort by name/date/size.
- **Image previews** — rendered in-terminal as true-color block art
  (needs Pillow).
- **Video thumbnails** — pulled with ffmpeg and rendered the same way.
- **Select by date** (`t`) — pick everything modified *before* or *after*
  a date, e.g. `7d`, `2w`, `3mo`, `yesterday`, `2025-01-31`. Optionally
  recurse into subfolders.
- **Word lookup** (`/`) — search file/folder names recursively as you type,
  select matches (or `Ctrl+A` for all of them), and shred.
- **Three shred methods** — Quick (1 pass), Standard (3 pass), Paranoid
  (7 pass, alternating patterns + random). Files are overwritten, truncated,
  renamed, then deleted.
- Refuses to touch protected paths (`/`, home, `/usr`, `/etc`, `/sdcard`, …)
  and never follows symlinks when shredding.
- Full-screen preview mode (`v`), select-all/invert/clear, hidden-file
  toggle, live progress with abort.

## Install

**Termux:**
```sh
pkg install python python-pillow ffmpeg
termux-setup-storage
cp shredder.py $PREFIX/bin/shredder
chmod +x $PREFIX/bin/shredder
shredder
```

**Linux (Arch/CachyOS):**
```sh
sudo pacman -S python python-pillow ffmpeg
sudo install -m755 shredder.py /usr/local/bin/shredder
shredder
```

**Linux (Debian/Ubuntu):**
```sh
sudo apt install python3 python3-pil ffmpeg
sudo install -m755 shredder.py /usr/local/bin/shredder
shredder
```

Pillow and ffmpeg are optional — the app works without them, just without
image/video previews. Everything else is Python standard library.

You can also run it directly without installing: `python shredder.py [path]`.

## Keybindings

| Key | Action |
|---|---|
| `↑ ↓` / `j k` | move |
| `→ l ⏎` | open folder / view file |
| `← h ⌫` | parent folder |
| `g` / `G` | top / bottom |
| `:` | go to path (Tab completes) |
| `~` / `0` | home / shared storage |
| `␣` | select item, move down |
| `a` / `i` / `c` | select all / invert / clear |
| `t` | select by date |
| `/` | word lookup: search & select by name |
| `v` | full-screen preview (`←→` to browse) |
| `p` | toggle preview panel |
| `s` | cycle sort |
| `.` | toggle hidden files |
| `x` / `Del` | shred selection (or item under cursor) |
| `m` | cycle shred method |
| `?` | help |
| `q` | quit |

## A note on security

On flash storage (phones, SSDs), overwriting a file's data is
**best-effort** — the underlying hardware can retain copies of old data
that software can't reach or address directly. This tool still makes
casual/forensic recovery much harder than a normal delete, but if you need
a hard guarantee, use full-disk encryption; encrypting the drive from the
start is what actually makes deleted data unrecoverable.

## License

MIT — see [LICENSE](LICENSE).
