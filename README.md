# A lazy file system that starts containers faster

My solution to the problem in this video:

**[Building a File System for Speed: Modal's Approach to Faster Containers](https://www.youtube.com/watch?v=ttRwXIR-xTc)** (devtools.fm, about 4 minutes)

There are two parts:

- **A registry API in Django REST Framework.** It stores every file of every image once, under its checksum,
  and keeps a small index per image.
- **A lazy file system (FUSE).** It mounts an image from that API so a container sees all of it right away,
  but it only downloads a file the first time something opens it.

`demo.sh` starts real `python:3.12-slim` and `python:3.11-slim` containers both ways and times them.

## Demo video

[`demo.cast`](demo.cast) is an asciinema screencast of `./demo.sh`: 33 seconds, recorded at real speed. To play
it in a terminal:

```bash
asciinema play demo.cast
```

GitHub can't play `.cast` files in the page. `asciinema upload demo.cast` gives a link that plays in any
browser. Install the player first with `pip install asciinema` if you don't have it.

## Results

From the recorded run, on my laptop. Both ways start the same container: Python inside `python:3.12-slim`, in
its own namespaces, without root.

| Seconds until the container printed "hello" | Usual way | Lazy way |
|---|---:|---:|
| Localhost, first start | 0.75 | 0.76 |
| Localhost, second start on the same machine | 0.14 | 0.32 |
| Simulated 1 Gbit/s link with 2 ms latency, first start | 1.28 | 1.59 |
| Simulated 100 Mbit/s link with 10 ms latency, first start | 10.14 | **3.99** |
| Then `python:3.11-slim` on the same machine (100 Mbit/s) | – | **2.35** |

- The **usual way** downloads the whole image (123.9 MB), unpacks it, then starts the container.
- The **lazy way** mounts the image and starts the container right away. It downloaded only 84 files,
  22.5 MB, which is 19% of the image.
- **`python:3.11-slim`** needed just 7.2 MB more, because 33 of its file opens found the content already in the
  cache from 3.12.

What the numbers say:

- **When the network is the slow part, the lazy way wins clearly:** 10.1 s becomes 4.0 s at 100 Mbit/s. That's
  the case the video is about: machines pulling images from a registry, not from localhost.
- **It always moves far less data:** 5.5 times less for the first image, and only what's new for the next one.
- **On localhost or a fast 1 Gbit/s link it's about even.** Downloading is almost free there, and the lazy way
  pays one network round trip per file while Python opens about 85 files one after another. A FUSE file system
  written in Python also adds overhead to every file access. That's also why a second start from an
  already-unpacked folder is quicker (0.14 s vs 0.32 s).
- **The known fix is prefetching.** Record once which files an image reads at start-up, then download them in
  parallel as soon as the image is mounted. eStargz and Nydus do this. It's the next step (see
  [section 11](#11-whats-simplified-and-what-a-real-system-would-add)).

Expect some variation between runs. The slow links are simulated in the client (see `Link` in
`imagestore/client.py`). Both ways go through the same code, so the comparison is fair.

**Contents**

1. [The video in short](#1-the-video-in-short)
2. [The actual problem, in detail](#2-the-actual-problem-in-detail)
3. [The solution from the video](#3-the-solution-from-the-video)
4. [What this project builds](#4-what-this-project-builds)
5. [How it works, step by step](#5-how-it-works-step-by-step)
6. [Code tour](#6-code-tour)
7. [API reference](#7-api-reference)
8. [Run it](#8-run-it)
9. [The demos and what their numbers mean](#9-the-demos-and-what-their-numbers-mean)
10. [How to explain this project](#10-how-to-explain-this-project)
11. [What's simplified, and what a real system would add](#11-whats-simplified-and-what-a-real-system-would-add)
12. [Glossary](#12-glossary)

---

## 1. The video in short

The video is a clip from the devtools.fm podcast. The hosts ask a guest from **Modal** (a cloud platform that
runs people's code in containers on demand) why Modal wrote its own file system. A summary in my own words:

| Time | What's said |
|---|---|
| 0:00 | The hosts ask why anyone would rebuild a file system to make things faster. |
| 0:11 | Building a file system is easier than it sounds, thanks to **FUSE** (file systems written as normal programs). Modal's first prototype was even written in Python. |
| 0:35 | The goal is to **start containers very quickly**. A container is a few Linux features for isolation and security, plus a **root file system** it starts from. |
| 0:57 | **Problem 1:** container images are large, and most of their files are **never read**. Pushing and pulling images moves a lot of data for nothing. |
| 1:16 | **Problem 2:** many images contain the **same data over and over**. Docker shares data per *layer*, which doesn't help much. |
| 1:31 | **Idea 1:** show the container a complete file system whose files aren't really on the local disk. When the container runtime (`runc`) opens a file, fetch that file over the network. |
| 1:59 | **Idea 2:** checksum every file and store it by checksum instead of by path. That's a **content-addressed** file system, with **index files** that point paths to the stored contents ("blobs"). |
| 2:32 | Fetched blobs are **cached on the machine's local SSDs**. Result: containers start with very low delay, and the cache is used very efficiently. |
| 3:09 | The hosts call it a **lazy file system**. The guest says it works a lot like a **CDN**. |
| 3:41 | Modal's file system is closed source. Open-source projects with similar ideas exist: **Nydus**, **Dragonfly** (Nydus is part of it) and **stargz**. |

---

## 2. The actual problem, in detail

### How a container normally starts

A container image is a bundle of every file a program needs: an operating system's basic files, a language
runtime such as Python, libraries, and the app itself. Starting a container from an image normally goes like this:

1. A machine is told to run image `X`.
2. It **pulls** the image: it downloads all of the image's **layers** (compressed `.tar` files) from a registry.
3. It **unpacks** the layers onto its local disk to build the container's root file system (its `/`).
4. It asks the container runtime (`runc`) to start the program with that folder as its root.
5. The program starts and reads the files it needs.

Steps 2 and 3 have to finish before step 4 can begin. They take longer the bigger the image is, no matter how
few of its files the program uses.

### Problem 1: most of an image is never read

A Python image contains the whole Python standard library and its test suite, documentation, translations,
static libraries, tools such as compilers, and so on. A typical app touches only a small part of that.

Researchers measured this in the paper *Slacker: Fast Distribution with Lazy Docker Containers*
(Harter et al., USENIX FAST 2016): **pulling took 76% of container start time, but only 6.4% of the pulled
data was ever read.** In this project's demo, starting Python reads 19% of `python:3.12-slim`.

### Problem 2: the same data is stored and moved again and again

Docker images are stacks of layers. Each layer is a tar file named by its checksum, and a registry or machine
stores a layer once and shares it between images. But that sharing only works **when the whole layer is
exactly the same**:

- **One changed file means a whole new layer.** If a security fix changes one library in a 500 MB base layer,
  that's a brand-new 500 MB layer. It's uploaded again, stored again, and every machine downloads all of it again.
- **The same file in different layers isn't shared.** Two unrelated images that both contain the same Python
  interpreter, in different layers, store and download it twice.

So the same bytes cross the network and fill up disks many times. In this project, uploading `python:3.11-slim`
after `python:3.12-slim` stores only 33.7 MB of its 125.5 MB, because the rest are files 3.12 already has.

### Why it matters

For a platform like Modal, users wait while a container starts. Every second spent pulling and unpacking is
delay the user feels (often called a "cold start"), and every duplicate byte is wasted bandwidth and storage.

---

## 3. The solution from the video

The video combines four ideas:

1. **Content-addressed storage.** Name each file's content by its checksum (for example SHA-256), not by its
   path. The same content always gets the same name, so it's stored exactly once, however many images
   contain it.
2. **An index per image.** An image becomes a small list of `path → checksum` (plus folders, symlinks and
   permissions), and a shared pool of contents ("blobs"). The index is tiny compared to the image.
3. **Lazy loading.** Show the container a complete file system right away (Modal does this with FUSE), but
   fetch a file's content only when the container actually reads it. Files that are never read are never
   downloaded.
4. **A local cache, like a CDN.** Keep fetched blobs on the machine's SSD, named by checksum. Any container or
   image on that machine that needs the same content reuses it. Content behind a checksum never changes, so it
   can be cached forever.

---

## 4. What this project builds

| Part | What it does | Where |
|---|---|---|
| Registry API | Stores each file once by checksum, keeps an index per image, takes pushes that upload only missing files, and can hand out a whole image as one tar | `imagestore/` (Django app) |
| Lazy file system | Mounts an image with FUSE. Folder listings and file info come from the index. A file is downloaded on first open, checked, and cached by checksum | `imagestore/lazyfs.py` |
| Client and CLI | `push` an image, `pull` it the usual way, or `mount` it lazily. Uses only the standard library, apart from FUSE for `mount` | `imagestore/client.py`, `imagestore/__main__.py` |
| The proof | Starts real containers both ways on three network conditions and times them | `demo.sh`, recorded in `demo.cast` |

How each idea from the video maps to the code:

| Idea from the video | Where it is |
|---|---|
| Store files by checksum, once | `storage.save_blob()` and the `Blob` model |
| An index per image | The `ImageFile` model (files, folders, symlinks, modes, mtimes) and `GET /api/images/{id}/index/` |
| Show the whole file system right away | `LazyFS.getattr` / `readdir` / `readlink`, answered from the index |
| Fetch a file only when it's opened | `LazyFS.open()`, which calls `BlobCache.path()` and downloads `GET /api/blobs/{checksum}/` |
| Local cache named by checksum | `BlobCache`, shared by every image mounted on the machine |
| "Like a CDN": cache forever | `Cache-Control: immutable` on blobs, and kernel caching options on the mount |
| Don't move data the server already has | `push_tar()` with `POST /api/blobs/missing/` and `PUT /api/blobs/{checksum}/` |
| Trust the content | The server re-hashes every upload; the client re-hashes every download |

---

## 5. How it works, step by step

```text
 A) GET AN IMAGE IN (once)

  docker export → image.tar ──► POST /api/images/            (whole tar, one request)
                       or  ──► python3 -m imagestore push    (only files the server lacks)
                                     │
                                     ▼
              Database (SQLite)                               blobstore/ folder
              Image      python:3.12-slim                     1d/1d3791eb…   file contents,
              ImageFile  /usr/local/bin/python3.12 → 1d37…    ad/ad82e165…   named by checksum
                         /bin → usr/bin (symlink), /etc (folder, 0755), …


 B) START A CONTAINER THE LAZY WAY (python3 -m imagestore mount …)

  1. GET /api/images/?name=python&tag=3.12-slim     find the image
  2. GET /api/images/1/index/                        5,580 entries, about 1 MB, no file contents
  3. mount the FUSE file system on mnt/               the container sees every file immediately
  4. unshare … chroot mnt/ /usr/local/bin/python3     the container starts
  5. Python opens a file ──► LazyFS.open()
                               checksum in the local cache?   yes → open the cached copy
                                                               no  → GET /api/blobs/{checksum}/,
                                                                     check SHA-256, save, open


 C) START A CONTAINER THE USUAL WAY (python3 -m imagestore pull …), for comparison

  1. GET /api/images/1/tar/                           the whole image as one tar, streamed
  2. unpack it into rootfs/
  3. unshare … chroot rootfs/ /usr/local/bin/python3
```

### What "container" means here

`demo.sh` runs the program with `unshare --user --map-root-user --mount --pid --net --uts --ipc --fork chroot
<root> …`. That gives it its own user, mount, PID, network, hostname and IPC namespaces, and its own root file
system. Inside, it's PID 1 and root; outside, it's your normal user, so no `sudo` is needed. Those are the Linux
features a container runtime like `runc` sets up. What's missing compared with a full runtime: cgroups
(resource limits), seccomp filters and dropped capabilities. A real setup would hand the mount to `runc` or
containerd as the root file system.

### Details worth knowing

- **Why the checksum is a good name:** SHA-256 gives a different value for any different content and the same
  value for the same content, so the name proves what the content is.
- **The index keeps what a real file system needs:** folders, symlinks (`/bin → usr/bin`), permission bits
  and modification times.
  - **Modification times matter for speed.** Python only trusts its cached `.pyc` files when the `.py` file's
    mtime matches. Without mtimes, every start would recompile the standard library.
  - **Owners aren't kept.** Everything belongs to whoever mounted the image, which shows up as root inside the
    container.
- **The mount is read-only, and the kernel may cache everything.** An image never changes, so file contents,
  attributes and even "this file doesn't exist" answers are cached. The last one matters for Python, whose
  imports look up many files that aren't there.
- **Safe writes:** blobs and cached files are written to a temp file, `fsync`ed and renamed into place, so a
  half-written file never appears under a checksum name.
- **Safe uploads:** the tar is never unpacked on the server. Contents are stored under the checksum the server
  computes, `..` paths are rejected, and size and entry limits (`IMAGE_MAX_SIZE`, `IMAGE_MAX_ENTRIES`) are
  checked from the tar headers before anything is written.
- **The client doesn't blindly trust the server:** checksums in an index must be real SHA-256 values before
  they become cache file names, and every download is re-hashed (`ChecksumMismatch` otherwise).
- **All or nothing:** saving an image runs in one database transaction. Pushing a name and tag again replaces
  that image, like a registry does.

---

## 6. Code tour

```text
e2e-second-round-task/
├── README.md                  this file
├── demo.sh                    the proof: starts real containers both ways and times them
├── demo.cast                  screencast of demo.sh (asciinema)
├── requirements.txt           Django, Django REST Framework, mfusepy
├── manage.py
├── e2e_second/                Django project settings (DATA_DIR, BLOB_STORAGE_DIR, limits)
└── imagestore/
    ├── models.py              Blob, Image, ImageFile
    ├── storage.py             saves and opens blobs on disk, by checksum
    ├── services.py            import_tar(), push_image(), save_image(), export_tar()
    ├── serializers.py         upload form, push body, JSON output
    ├── views.py               the API endpoints
    ├── urls.py
    ├── client.py              BlobCache, Link, fetch_index(), pull_image(), push_tar(), LazyImage
    ├── lazyfs.py              the FUSE file system
    ├── __main__.py            CLI: python3 -m imagestore push | pull | mount
    ├── admin.py
    ├── management/commands/
    │   └── demo.py            quick demo without FUSE or Docker: python3 manage.py demo
    └── migrations/
```

Suggested reading order: `models.py` → `storage.py` → `services.py` → `views.py` → `client.py` → `lazyfs.py` →
`demo.sh`.

**The three models**

| Model | One row means | Key fields |
|---|---|---|
| `Blob` | One unique file content, stored once | `digest` (SHA-256, the primary key), `size` |
| `Image` | One image, such as `python:3.12-slim` | `name`, `tag`, `file_count`, `total_size` |
| `ImageFile` | One entry of an image's file system: a file, folder or symlink | `image`, `path`, `kind`, `mode`, `mtime`, `target`, `blob` (files only) |

`ImageFile.blob` uses `on_delete=PROTECT`, so a blob that an image still uses can't be deleted by accident.

---

## 7. API reference

| Method and URL | What it does |
|---|---|
| `POST /api/images/` | Upload a whole image: multipart form with `name`, `tag` and `file` (a `.tar` or `.tar.gz`). |
| `POST /api/blobs/missing/` | Push, step 1: send `{"digests": [...]}`, get back `{"missing": [...]}`. |
| `PUT /api/blobs/{checksum}/` | Push, step 2: upload one file's raw bytes. They must hash to the checksum in the URL. |
| `POST /api/images/push/` | Push, step 3: create the image from `{"name", "tag", "entries": [...]}`. Each entry has `path`, `kind` (`file`, `dir` or `symlink`), `mode`, `mtime`, and `digest` for files or `target` for symlinks. |
| `GET /api/images/` | List images, 50 per page. `?name=python&tag=3.12-slim` finds one. |
| `GET /api/images/{id}/` | One image. |
| `GET /api/images/{id}/index/` | The index: every entry with `path`, `kind`, `mode`, `mtime`, `target`, `digest`, `size`. No file contents. |
| `GET /api/images/{id}/tar/` | The whole image as one streamed tar. This is what the usual way downloads. |
| `GET /api/blobs/{checksum}/` | The raw bytes of one file. Cacheable forever. |
| `GET /api/stats/` | How much space deduplication saves across all images. |

There's no login: anyone who can reach the API can read and upload (see
[section 11](#11-whats-simplified-and-what-a-real-system-would-add)). `http://127.0.0.1:8000/api/` also works in
a browser, through DRF's browsable API.

**Examples** (with the server running):

```bash
curl -F name=python -F tag=3.12-slim -F file=@python.tar http://127.0.0.1:8000/api/images/
```

```json
{
  "image": {"id": 1, "name": "python", "tag": "3.12-slim", "file_count": 4416, "total_size": 119827876, "created_at": "…"},
  "stats": {"files": 4416, "total_bytes": 119827876, "new_bytes_stored": 115407247, "bytes_saved_by_dedup": 4420629}
}
```

```bash
curl http://127.0.0.1:8000/api/images/1/index/
```

```json
{
  "image": {"id": 1, "name": "python", "tag": "3.12-slim", "file_count": 4416, "total_size": 119827876, "created_at": "…"},
  "entries": [
    {"path": "/", "kind": "dir", "mode": 493, "mtime": 0, "target": "", "digest": null, "size": 0},
    {"path": "/bin", "kind": "symlink", "mode": 511, "mtime": 1784307900, "target": "usr/bin", "digest": null, "size": 0},
    {"path": "/usr/local/bin/python3.12", "kind": "file", "mode": 493, "mtime": 1793865609, "target": "", "digest": "…", "size": 14472},
    "…"
  ]
}
```

(`mode` is shown in decimal: 493 is `0o755`, 511 is `0o777`.)

Errors come back as JSON:

- **400:** not a tar file, a path with `..`, an image over the limits, a `PUT` whose bytes don't match its
  checksum, or a push that lists blobs nobody uploaded (the response lists them under `missing`).
- **404:** an unknown image or checksum.
- **411:** a `PUT` without a `Content-Length` header.
- **413:** a `PUT` larger than the size limit.

---

## 8. Run it

Needs Linux with FUSE 3 (`fusermount3` and `libfuse3`), unprivileged user namespaces (check that
`unshare --user --map-root-user true` works), Python 3.10 or newer, and `curl`. The demo also needs Docker, only
to export the two sample images the first time.

```bash
python3 -m venv venv
```

```bash
venv/bin/pip install -r requirements.txt
```

```bash
venv/bin/python manage.py migrate
```

Then run the proof. It starts its own server on port 8765 with its own throwaway database in `demo-work/`, so
it never touches your data:

```bash
./demo.sh
```

To try it by hand instead, run `venv/bin/python manage.py runserver` in one terminal, upload a tar as in
[section 7](#7-api-reference), then:

```bash
mkdir -p /tmp/py && venv/bin/python -m imagestore mount python:3.12-slim /tmp/py --cache /tmp/py-cache &
```

```bash
unshare --user --map-root-user chroot /tmp/py /usr/local/bin/python3 -c 'print("hello")'
```

```bash
fusermount3 -u /tmp/py
```

When the mount ends, it prints how many files it downloaded. `--bandwidth 100 --latency 10` on `mount` or `pull`
simulates a slower network.

To get a tar of a real image:

```bash
docker export $(docker create python:3.12-slim) -o python.tar
```

If you set up the project before folders and symlinks were added, run `migrate` again, then re-upload your
images so their indexes include them. To start over with an empty store, delete `db.sqlite3` and `blobstore/`
and run `migrate`.

Outside local development, set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0` and `DJANGO_ALLOWED_HOSTS`
(comma-separated). `IMAGESTORE_DATA_DIR` moves the database and blob store somewhere else.

---

## 9. The demos and what their numbers mean

### `./demo.sh`: real containers, timed

What it does:
1. Uploads `python:3.12-slim` and `python:3.11-slim`.
2. Starts Python in a container from each image, on localhost and then on two simulated links.
3. Prints the table shown under [Results](#results).

Each lazy start uses an empty cache, except the second start and the `python:3.11` one, which show what the
cache brings. The output also lists how many files and bytes each start downloaded.

### `python3 manage.py demo`: a quick version without FUSE or Docker

It uses two made-up 33 MB images and `LazyImage`, which reads files through the cache without mounting
anything. Its output on an empty database:

```text
Step 1: push two versions of demo/webapp. Only files the server doesn't have are uploaded.
  demo/webapp:1.0   760 files, 33.4 MB   uploaded: 33.4 MB   already on the server:  0.0 MB
  demo/webapp:2.0   760 files, 33.4 MB   uploaded:  0.7 MB   already on the server: 32.7 MB

  Both images together: 66.9 MB of files, but only 34.1 MB stored on disk.

Step 2: start both versions on a fresh machine. Starting the app reads 25 files.
  demo/webapp:1.0   downloaded 3.9 MB of 33.4 MB (12%)   files found in the machine's cache: 0
  demo/webapp:2.0   downloaded 0.7 MB of 33.4 MB (2%)    files found in the machine's cache: 22
```

It shows the two-step push: version 2.0 uploads only the 0.7 MB that changed. It needs the API running
(`runserver`).

---

## 10. How to explain this project

### A two-minute version

1. A container starts from an image, a bundle of every file a program needs. Normally a machine downloads and
   unpacks the whole image before the program can start.
2. That wastes two things. Most of an image is never read (Python reads 19% of `python:3.12-slim` to start).
   And the same files are stored and moved again and again, because Docker only shares whole layers.
3. Modal's answer is a lazy, content-addressed file system: store every file once by checksum, describe each
   image as an index of paths and checksums, show the container the whole file system right away, and download
   a file only when it's opened, caching it by checksum on the machine.
4. I built exactly that:
   - A Django REST registry stores files by checksum and serves indexes and blobs.
   - A FUSE file system mounts an image from it: listings and file info come from the index, and file
     contents are downloaded on first open, verified and cached.
   - `demo.sh` starts real Python containers, rootless with their own namespaces, both from the lazy mount and
     the usual way (download everything, unpack, start).
5. Results: on a 100 Mbit/s link, start-up goes from 10.1 s to 4.0 s, and the lazy way downloads 22.5 MB instead
   of 124 MB. The next image reuses cached files and needs only 7.2 MB. On a fast local network it's about even,
   because each file costs a round trip. The next step is prefetching the start-up files in parallel.

### Questions you might get

**Why FUSE?** It lets a normal program be a file system, so the container sees ordinary files while my code
decides when to fetch them. Modal's first prototype was also FUSE in Python. In production you'd write it in
Rust or Go, or use the kernel's EROFS with fscache, as Nydus does.

**Isn't a FUSE file system written in Python slow?** It adds overhead to every file access. You can see it in
the second start: 0.32 s from the cache against 0.14 s from an unpacked folder. It doesn't change the big
picture, though: the cold start is dominated by downloads, and that's where laziness wins.

**Why is the lazy way not faster on localhost or 1 Gbit/s?** Python opens about 85 files one after another, and
each first open waits for a download, so each one costs a round trip. When the network is almost free, that
adds up to about what a bulk download costs. The fix is prefetching: record the start-up files once per image
and download them in parallel at mount time.

**How did you simulate the network, and is that fair?** `Link` in `client.py` adds latency before each request
and keeps every download to the chosen bandwidth. Both ways use it the same way, so the comparison is fair. It
doesn't model TCP slow start or packet loss, and real networks are usually worse than the simulation, which
favours the lazy way even more.

**Why keep modification times?** Python checks the `.py` file's mtime before trusting the cached `.pyc`. Without
it, every start would recompile the standard library and try to write `.pyc` files to a read-only mount.

**Is `unshare` plus `chroot` a real container?** It has the isolation a container gets from namespaces, but no
cgroups, seccomp or dropped capabilities. The next step would be pointing `runc`'s `root.path` at the mount, or
writing a containerd snapshotter, which is what stargz-snapshotter does.

**Why is there a tar export endpoint?** For a fair baseline. The usual way should download the image in one
stream, the way `docker pull` downloads layers, not in 4,400 small requests.

**Why SHA-256?** It's the standard checksum for this, and Docker and OCI digests use it too. There are no
practical collisions.

**What if two uploads store the same file at once?** Both write identical bytes to temp files and rename them to
the same name. The rename is atomic and the content is identical, so it doesn't matter which one wins.
`get_or_create` handles the database row.

**Can a broken or hostile server trick the client?** It can't make the client accept wrong content, because
every download is re-hashed. It can't make it read local files either: index checksums must be real SHA-256
values before they become cache file names.

**Who can upload?** Anyone who can reach the API; there's no login, to keep the project small. In production I'd
add DRF authentication with `IsAuthenticatedOrReadOnly`, so pulling stays public and pushing needs a user.

**How would this scale?** Blobs in object storage behind a CDN, Postgres, login for uploads, garbage collection
of unused blobs, a shared cache per machine (one daemon), and prefetching. Large files would be chunked, so a
program reading part of a file downloads only that part.

---

## 11. What's simplified, and what a real system would add

- **No prefetching.** The next step: record which files an image reads at start-up, then download them in
  parallel when it's mounted. That removes the per-file round trips that make the lazy way about even on fast
  networks.
- **The file system is written in Python.** It's easy to read but adds overhead to every file access. Production
  versions are in Rust or Go, or use the kernel (EROFS with fscache).
- **Whole files only.** A big library is downloaded in full the first time it's opened. Splitting files into
  chunks would let a program fetch only the parts it reads.
- **The container is `unshare` plus `chroot`.** There are no cgroups, seccomp or dropped capabilities.
  Integrating with `runc` or containerd is the next step.
- **Owners aren't kept** (everything belongs to whoever mounts the image), and device files are skipped.
- **The cache never evicts anything**, and each mount has its own cache object. A real machine would run one
  cache daemon shared by all its containers.
- **No login, and no garbage collection** of blobs that no image uses anymore.
- **One request per missing file when pushing.** Fine for small images; `demo.sh` uploads whole tars instead.
- **The network simulation** caps each download separately and adds a fixed latency per request. It's good
  enough to compare the two ways, not to predict exact times on a real network.
- **SQLite and local disk.** In production: Postgres, plus object storage behind a CDN.

Related open-source projects that do this for real: Nydus (part of the Dragonfly project), Dragonfly, and
stargz-snapshotter (eStargz) for containerd.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| Container | A program running in isolation, with its own namespaces and its own root file system. |
| Image | Everything a container needs to start: all its files plus some settings. |
| Layer | A Docker image is a stack of layers; each layer is a tar of files added or changed. |
| Registry | A server that stores images, such as Docker Hub. This project's API plays that role. |
| Pull / push | Downloading an image from / uploading an image to a registry. |
| Root file system | The folder a container sees as `/`. |
| Namespace | A Linux feature that gives a process its own view of something: PIDs, mounts, network, users… |
| `unshare`, `chroot` | Commands that start a program in new namespaces, and with a different root folder. |
| Container runtime, `runc` | The program that normally starts a container from a root file system. |
| FUSE | "Filesystem in Userspace": lets a normal program act as a file system. |
| Mount, mountpoint | Attaching a file system to a folder (the mountpoint), so its files appear there. |
| Checksum, SHA-256 | A fixed-length fingerprint computed from content. Same content, same checksum. |
| Content-addressed storage | Storing data under its checksum instead of a path or name. |
| Blob | One stored file content, named by its checksum. |
| Index | An image's list of entries: each path with its kind, permissions, mtime, and checksum or symlink target. |
| `.pyc` | Python's cached compiled version of a `.py` file, trusted only if the `.py` mtime matches. |
| Deduplication | Storing identical data only once. |
| Lazy loading | Fetching data only when it's actually needed. |
| Prefetching | Fetching data you expect to need before it's asked for. |
| Cold start | The delay before a freshly started container can do its work. |
