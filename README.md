# Lazy, deduplicated container images with Django REST Framework

This project is a small solution to the problem described in this video:

**[Building a File System for Speed: Modal's Approach to Faster Containers](https://www.youtube.com/watch?v=ttRwXIR-xTc)** (devtools.fm, about 4 minutes)

It's a Django REST API that stores container images the way the video describes: every file is stored once,
under its checksum, a push only uploads files the server doesn't have yet, and a machine downloads a file only
when that file is actually read.

**Contents**

1. [The video in short](#1-the-video-in-short)
2. [The actual problem, in detail](#2-the-actual-problem-in-detail)
3. [The solution from the video](#3-the-solution-from-the-video)
4. [What this project builds](#4-what-this-project-builds)
5. [How it works, step by step](#5-how-it-works-step-by-step)
6. [Code tour](#6-code-tour)
7. [API reference](#7-api-reference)
8. [Run it](#8-run-it)
9. [The demo and what its numbers mean](#9-the-demo-and-what-its-numbers-mean)
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
data was ever read.** The machine waits for gigabytes it doesn't need.

### Problem 2: the same data is stored and moved again and again

Docker images are stacks of layers. Each layer is a tar file named by its checksum, and a registry or machine
stores a layer once and shares it between images. But that sharing only works **when the whole layer is
exactly the same**:

- **One changed file means a whole new layer.** If a security fix changes one library in a 500 MB base layer,
  that's a brand-new 500 MB layer. It's uploaded again, stored again, and every machine downloads all of it again.
- **The same file in different layers isn't shared.** Two unrelated images that both contain the same Python
  interpreter, in different layers, store and download it twice.

So the same bytes cross the network and fill up disks many times, both when images are pushed and when they're
pulled.

### Why it matters

For a platform like Modal, users wait while a container starts. Every second spent pulling and unpacking is
delay the user feels (often called a "cold start"), and every duplicate byte is wasted bandwidth and storage.

---

## 3. The solution from the video

The video combines four ideas:

1. **Content-addressed storage.** Name each file's content by its checksum (for example SHA-256), not by its
   path. The same content always gets the same name, so it's stored exactly once, however many images
   contain it.
2. **An index per image.** An image becomes a small list of `path → checksum`, plus a shared pool of contents
   ("blobs"). The index is tiny compared to the image.
3. **Lazy loading.** Show the container a complete file system right away (Modal does this with FUSE), but
   fetch a file's content only when the container actually reads it. Files that are never read are never
   downloaded.
4. **A local cache, like a CDN.** Keep fetched blobs on the machine's SSD, named by checksum. Any container or
   image on that machine that needs the same content reuses it. Content behind a checksum never changes, so it
   can be cached forever.

Both problems are solved: ideas 3 and 4 stop the machine from moving data that's never read, and ideas 1, 2
and 4 make sure each unique file is stored and moved only once.

---

## 4. What this project builds

A **Django REST API** that acts as a tiny image registry built on those four ideas, plus a small **Python
client** (`imagestore/client.py`) with two parts:

- `push_tar()` uploads an image and sends only the files the server doesn't have yet.
- `LazyImage` plays the role of the lazy file system on a machine starting containers.

| Idea from the video | Where it is in this project |
|---|---|
| Store files by checksum, once | `imagestore/storage.py` (`save_blob`) and the `Blob` model |
| An index per image | The `ImageFile` model and `GET /api/images/{id}/index/` |
| Don't move data the server already has | `push_tar()`, with `POST /api/blobs/missing/`, `PUT /api/blobs/{checksum}/` and `POST /api/images/push/` |
| Fetch a file only when it's read | `LazyImage.read()` and `GET /api/blobs/{checksum}/` |
| Local cache named by checksum | `LazyImage`'s cache folder |
| "Like a CDN": cache forever | `Cache-Control: immutable` and `ETag` headers on blob downloads |
| Trust the content | The server re-hashes every upload; `LazyImage` re-hashes every download |

What it deliberately doesn't do (see [section 11](#11-whats-simplified-and-what-a-real-system-would-add)): it
doesn't run containers, and it doesn't mount a real file system with FUSE. `LazyImage.read(path)` stands in for
"the container opened a file".

---

## 5. How it works, step by step

There are two ways to get an image in, and one way to use it.

```text
 A) UPLOAD A WHOLE TAR (one request; handy with curl)

  .tar of the image ──► POST /api/images/ ──► services.import_tar()
                                                for each file in the tar:
                                                  1. compute its SHA-256 checksum
                                                  2. store the content under that checksum,
                                                     unless it's already stored
                                                  3. add "path → checksum" to the image's index


 B) PUSH ONLY WHAT'S MISSING (client.push_tar; this is what the demo uses)

  on the developer's machine                              on the server
  1. read the tar, compute every file's SHA-256
  2. POST /api/blobs/missing/  {"digests": [...]}   ──►   which of these don't I have?
                                                    ◄──   {"missing": [...]}
  3. for each missing checksum only:
     PUT /api/blobs/{checksum}/   (the raw bytes)   ──►   hash the bytes; they must match the URL;
                                                          store them under that checksum
  4. POST /api/images/push/                         ──►   every listed blob must exist,
     {"name", "tag", "files": [{"path", "digest"}]}       then save the index


 Either way, the server ends up with:

              Database (SQLite)                                        blobstore/ folder
              Image      demo/webapp:1.0                               1d/1d3791eb…   file contents,
              ImageFile  /usr/bin/python3 → 1d3791eb…  (the index)     ad/ad82e165…   named by checksum
              Blob       1d3791eb…, 2,500,000 bytes                    …


 C) START A CONTAINER (on any machine, with client.LazyImage)

  LazyImage(server, image_id, cache_dir)
    1. GET /api/images/{id}/index/    → every path and its checksum (small, no file contents)

  lazy.read("/usr/bin/python3")
    2. look up the path's checksum in the index
    3. is a file with that checksum in the local cache?
         yes → read it from the cache, no download
         no  → GET /api/blobs/{checksum}/ → check the SHA-256 → save it in the cache
```

### What the database looks like after two versions

```text
Image        id=1  demo/webapp:1.0        id=2  demo/webapp:2.0

ImageFile    image=1  /usr/bin/python3  → blob 1d37…   ┐ same blob, stored once
             image=2  /usr/bin/python3  → blob 1d37…   ┘
             image=1  /app/main.py      → blob ad82…   ┐ different content,
             image=2  /app/main.py      → blob d038…   ┘ different blobs

Blob         1d37…  2,500,000 bytes
             ad82…  36 bytes
             d038…  36 bytes
```

### Details worth knowing

- **Why the checksum is a good name:** SHA-256 gives a different 64-character value for any different
  content, and the same value for the same content. The name proves what the content is: anyone can
  recompute it.
- **Safe writes:** a blob is written to a temporary file, flushed to disk (`fsync`), and then renamed to its
  checksum in one step. A half-written or lost-in-a-crash file never appears under a checksum name. That
  matters because a blob that already exists is never written again. The client's cache does the same.
- **Safe uploads:** the tar is never unpacked onto disk. Files are stored by the checksum the server computes,
  so a path inside the tar can't decide where anything is written. Paths with `..` are still rejected to keep
  the index clean.
- **Pushed data is checked:** the server hashes every `PUT` and refuses bytes that don't match the checksum in
  the URL. A push is refused, with the list of missing checksums, if any file in the index hasn't been
  uploaded yet.
- **Limits:** `IMAGE_MAX_SIZE` (2 GB) and `IMAGE_MAX_ENTRIES` (100,000) in `settings.py`. For a tar they're
  checked from the tar headers before anything is written. Without them, a 48 KB `.tar.gz` of zeros could
  unpack into 50 MB on disk (or much more). They also cap a single `PUT` and the size of a pushed index.
- **The client doesn't blindly trust the server:** every checksum in an index must be a real SHA-256 before it
  is used as a cache file name. Otherwise a bad index could say a file's checksum is `../../.ssh/id_rsa`, and
  the client would hand that local file to the container. Every download is also re-hashed.
- **All or nothing:** saving an image runs in a database transaction. If anything fails (for example a bad
  path), no image is saved. Blob files already written stay on disk; they're harmless, and a later upload
  reuses them.
- **Pushing a name and tag again replaces that image**, the way pushing a new build of a tag to a registry works.

---

## 6. Code tour

```text
E2E_Second/
├── README.md                  this file
├── requirements.txt           Django and Django REST Framework
├── manage.py
├── e2e_second/                Django project settings
│   ├── settings.py            adds rest_framework + imagestore, BLOB_STORAGE_DIR and the upload limits
│   └── urls.py                mounts the API under /api/
└── imagestore/                the app that does the work
    ├── models.py              Blob, Image, ImageFile (the database tables)
    ├── storage.py             saves and opens file contents by checksum on disk
    ├── services.py            import_tar() for a whole tar, push_image() for a pushed index
    ├── serializers.py         JSON output for images, the upload form and the push body
    ├── views.py               the API endpoints
    ├── urls.py                the API's URLs
    ├── admin.py               shows images and blobs in Django admin
    ├── client.py              push_tar() and LazyImage (standard library only)
    ├── management/commands/
    │   └── demo.py            `python3 manage.py demo`: shows both problems being solved
    └── migrations/            database migrations
```

Suggested reading order: `models.py` → `storage.py` → `services.py` → `views.py` → `client.py` → `demo.py`.

**The three models**

| Model | One row means | Key fields |
|---|---|---|
| `Blob` | One unique file content, stored once | `digest` (SHA-256, the primary key), `size` |
| `Image` | One image, such as `demo/webapp:1.0` | `name`, `tag`, `file_count`, `total_size` |
| `ImageFile` | One line of an image's index: this path has that content | `image`, `path`, `blob` |

`ImageFile.blob` uses `on_delete=PROTECT`, so a blob that an image still uses can't be deleted by accident.

---

## 7. API reference

| Method and URL | What it does |
|---|---|
| `POST /api/images/` | Upload a whole image in one request: multipart form with `name`, `tag` and `file` (a `.tar` or `.tar.gz`). |
| `POST /api/blobs/missing/` | Push, step 1: send `{"digests": [...]}`, get back `{"missing": [...]}`. |
| `PUT /api/blobs/{checksum}/` | Push, step 2: upload one file's raw bytes. They must hash to the checksum in the URL. |
| `POST /api/images/push/` | Push, step 3: create the image from `{"name", "tag", "files": [{"path", "digest"}]}`. |
| `GET /api/images/` | List images, 50 per page: `{"count", "next", "previous", "results"}`. |
| `GET /api/images/{id}/` | One image. |
| `GET /api/images/{id}/index/` | The image's index: every path with its checksum and size, no file contents. |
| `GET /api/blobs/{checksum}/` | The raw bytes of one file. Cacheable forever. |
| `GET /api/stats/` | How much space deduplication saves across all images. |

There's no login: anyone who can reach the API can read and upload (see
[section 11](#11-whats-simplified-and-what-a-real-system-would-add)). You can also open
`http://127.0.0.1:8000/api/` in a browser. DRF's browsable API lets you click through the images and upload a
`.tar` with a form.

**Examples** (with the server running):

```bash
curl -F name=demo/hello -F tag=1.0 -F file=@image.tar http://127.0.0.1:8000/api/images/
```

```json
{
  "image": {"id": 1, "name": "demo/hello", "tag": "1.0", "file_count": 2, "total_size": 33, "created_at": "…"},
  "stats": {"files": 2, "total_bytes": 33, "new_bytes_stored": 33, "bytes_saved_by_dedup": 0}
}
```

```bash
curl http://127.0.0.1:8000/api/images/1/index/
```

```json
{
  "image": {"id": 1, "name": "demo/hello", "tag": "1.0", "file_count": 2, "total_size": 33, "created_at": "…"},
  "files": [
    {"path": "/app/main.py", "digest": "03e693d9f2f687e0f40e36a8df7fcb4d1c22974012b7c2a55c000eb30f305824", "size": 15},
    {"path": "/etc/os-release", "digest": "1f23edd073b62977457058dfc878b29badae6c593508b347e543a17655ba1124", "size": 18}
  ]
}
```

```bash
curl -i http://127.0.0.1:8000/api/blobs/03e693d9f2f687e0f40e36a8df7fcb4d1c22974012b7c2a55c000eb30f305824/
```

```text
HTTP/1.1 200 OK
Content-Type: application/octet-stream
Content-Length: 15
Cache-Control: public, max-age=31536000, immutable
ETag: "03e693d9f2f687e0f40e36a8df7fcb4d1c22974012b7c2a55c000eb30f305824"

print('hello')
```

Pushing with the Python client (run it from the project folder; `client.py` only uses the standard library):

```python
from imagestore.client import push_tar

image, uploaded, skipped = push_tar("http://127.0.0.1:8000", "image.tar", "demo/hello", "2.0")
print(f"uploaded {uploaded} bytes, skipped {skipped} bytes the server already had")
```

Errors come back as JSON:

- **400:** not a tar file, a path with `..`, an image over the limits, a `PUT` whose bytes don't match its
  checksum, or a push that lists blobs nobody uploaded (the response lists them under `missing`).
- **404:** an unknown image or checksum.
- **411:** a `PUT` without a `Content-Length` header.
- **413:** a `PUT` larger than the size limit.

To try a real image (needs Docker), export its files to a tar and upload or push it:

```bash
docker export $(docker create python:3.12-slim) -o python.tar
```

```bash
curl -F name=python -F tag=3.12-slim -F file=@python.tar http://127.0.0.1:8000/api/images/
```

---

## 8. Run it

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

```bash
python3 manage.py migrate
```

```bash
python3 manage.py runserver
```

Then, in a second terminal:

```bash
python3 manage.py demo
```

Optional: to browse images and blobs in Django admin at `http://127.0.0.1:8000/admin/`, create a user first:

```bash
python3 manage.py createsuperuser
```

To start over with an empty store, stop the server and delete `db.sqlite3` and the `blobstore/` folder, then
run `migrate` again.

Outside local development, set these environment variables instead of relying on the defaults in
`settings.py`: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0` and `DJANGO_ALLOWED_HOSTS` (comma-separated).

---

## 9. The demo and what its numbers mean

`python3 manage.py demo` builds two versions of a made-up Python web app image and runs the whole solution
through the API. The images are synthetic but shaped like real ones: a few files are needed to start the app
(the interpreter, some standard library modules, the app's code), and most bytes are things a running app
never opens (the standard library's test suite, a static library, translations, package docs). Version 2.0
changes the app and applies a "security fix" to one library.

Output from a fresh database:

```text
Step 1: push two versions of demo/webapp. Only files the server doesn't have are uploaded.
  demo/webapp:1.0   760 files, 33.4 MB   uploaded: 33.4 MB   already on the server:  0.0 MB
  demo/webapp:2.0   760 files, 33.4 MB   uploaded:  0.7 MB   already on the server: 32.7 MB

  Both images together: 66.9 MB of files, but only 34.1 MB stored on disk.

Step 2: start both versions on a fresh machine. Starting the app reads 25 files.
  demo/webapp:1.0   downloaded 3.9 MB of 33.4 MB (12%)   files found in the machine's cache: 0
  demo/webapp:2.0   downloaded 0.7 MB of 33.4 MB (2%)    files found in the machine's cache: 22

  A normal pull would download the whole image (33.4 MB) before the app could start.
```

How to read it:

- **Step 1 solves Problem 2 (duplicate data) on the way up.** Version 2.0 is 33.4 MB of files, but only 0.7 MB
  of it is new (the changed library and app files). `push_tar` asks the server which checksums it's missing and
  uploads just those, so only 0.7 MB crosses the network and only 0.7 MB more is stored. Uploading the
  version 2.0 tar in one piece would send all 34 MB. With Docker layers, the changed library would make its
  whole base layer new.
- **Step 2 solves Problem 1 (data that's never read).** Starting version 1.0 downloads only the 25 files it
  reads: 3.9 MB instead of 33.4 MB.
- **Step 2 also shows the cache.** Starting version 2.0 on the same machine finds 22 of its 25 files already in
  the cache (same checksums), so it downloads only the 3 changed files: 0.7 MB.

The first push takes a few seconds, because each of its 760 files is a separate request. If you run the demo
again, step 1 shows `uploaded: 0.0 MB` for both versions: the server already has every file from the first
run. Step 2 always starts with an empty cache, so its numbers stay the same.

---

## 10. How to explain this project

### A two-minute version

1. A container starts from an image: a bundle of every file a program needs. Normally a machine downloads the
   whole image before the program can start.
2. That wastes two things. Most of an image is never read (one study found only about 6% of the downloaded
   data is). And the same files are stored, uploaded and downloaded again and again, because Docker only shares
   whole layers.
3. Modal solved this with their own file system. They store every file by its checksum, so identical files
   exist once. Each image is just an index of `path → checksum`. Containers see all their files immediately, but
   a file is downloaded only when it's read, and then cached on the machine.
4. My project builds that idea with Django REST Framework. Pushing an image first asks the server which
   checksums it's missing, uploads only those files, then sends the index. The index endpoint returns an image's
   paths and checksums without any contents. The blob endpoint returns one file's bytes by checksum, cacheable
   forever. The `LazyImage` client downloads the index, fetches files only when they're read, checks every
   checksum, and caches by checksum.
5. In the demo, pushing version 2.0 of a 33 MB image uploads only 0.7 MB, and the two versions take 34 MB of
   storage instead of 67 MB. Starting the app downloads 3.9 MB instead of 33.4 MB, and starting the second
   version afterwards downloads only 0.7 MB.

### Questions you might get

**Why SHA-256?** It's the standard checksum for this; Docker and OCI image digests use it too. For practical
purposes, two different files never get the same SHA-256.

**How does the client know a download is correct?** The checksum is the file's name. The client recomputes
the SHA-256 of what it downloaded and refuses it if it doesn't match (`ChecksumMismatch`). The server does the
same with every `PUT`.

**Why push file by file instead of uploading one tar?** So nothing the server already has crosses the network.
The trade-off is one request per missing file. That's fine here, but a real system would upload in parallel or
pack small files together. The one-request tar upload is still there for quick tests with curl.

**What if two uploads store the same file at the same time?** Both write identical bytes to temporary files
and rename them to the same checksum name. The rename is atomic and the content is the same, so it doesn't
matter which one wins. The database row is created with `get_or_create`, which handles the race.

**Isn't the first read of each file slower?** Yes, it has network delay. Real systems reduce it by caching
on local SSDs (as Modal does), serving blobs from a CDN close to the machines, and prefetching files that
containers usually read at start. Overall it's still far faster than downloading the whole image first.

**Why not just use Docker layers?** Layers are all or nothing. A one-file change makes a whole new layer, the
same file in two different layers is stored twice, and a container can't start until all layers are
downloaded and unpacked.

**Who can upload?** Anyone who can reach the API; there's no login, to keep the project small. In production
I'd add DRF authentication with `IsAuthenticatedOrReadOnly`, so pulling stays public and pushing needs a user.

**Is uploading a malicious tar dangerous?** The tar is never unpacked to disk, contents are stored under
checksums the server computes, and `..` paths are rejected. Size and entry limits stop archives that unpack
into huge amounts of data, and saving an image is all-or-nothing.

**Can a broken or hostile server trick the client?** It can't make the client accept wrong content, because
every download is re-hashed. It also can't make the client read local files: checksums in the index must be
real SHA-256 values before they're used as cache file names.

**Why are the blob endpoints a plain Django view, not DRF?** They move raw bytes. DRF is great for validating
and producing JSON, which the other endpoints use, but here it would add nothing.

**Where would FUSE fit in?** A FUSE file system would answer "list folder" and "file info" requests from the
index, and call `LazyImage.read` when a file is opened. That's how the container would see a normal folder.

**How would this scale?** Put blobs in object storage (such as S3) behind a CDN, use PostgreSQL, require a
login for uploads, and garbage-collect blobs no image uses anymore.

---

## 11. What's simplified, and what a real system would add

Kept out on purpose, to keep the project small and easy to follow:

- **No FUSE mount and no container runtime.** `LazyImage.read()` stands in for a container opening a file.
- **No login.** Anyone who can reach the API can upload, push or replace images. Production needs
  authentication in front of the write endpoints.
- **Only regular files are indexed.** Folders, symlinks, permissions and owners are skipped (hard links are
  kept). A real image index needs all of them.
- **Whole files only.** Large files would be split into chunks so a program reading part of a file downloads
  only that part.
- **One request per missing file when pushing.** A real system would upload in parallel or pack small files
  together.
- **No prefetching.** Real systems download the files a container usually needs at start in the background.
- **No garbage collection.** Blobs that no image uses anymore (after a tag is replaced, or pushed but never
  used) stay on disk.
- **No special handling for concurrent pushes.** SQLite allows one writer at a time, and two pushes of the
  same tag at the same moment aren't turned into a clean `409 Conflict`.
- **Request size isn't capped before Django receives the upload.** In production, a proxy (for example
  nginx's `client_max_body_size`) should cap it. Python's `tarfile` also reads some header data fully into
  memory, so a crafted archive can still use a lot of memory.
- **The client reads each file into memory.** Fine for the demo; a real file system would stream.
- **Blobs live on local disk and data in SQLite.** In production: object storage plus a CDN, and PostgreSQL.

Related open-source projects that do this for real: Nydus (part of the Dragonfly project), Dragonfly, and
stargz / eStargz for containerd.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| Container | A program running in isolation, with its own view of the file system. |
| Image | Everything a container needs to start: all its files plus some settings. |
| Layer | A Docker image is a stack of layers; each layer is a tar of files added or changed. |
| Registry | A server that stores images, such as Docker Hub. |
| Pull / push | Downloading an image from / uploading an image to a registry. |
| Two-step push | Asking the server which checksums it's missing, uploading only those, then sending the index. |
| Root file system | The folder a container sees as `/`. |
| Container runtime, `runc` | The program that actually starts a container from a root file system. |
| FUSE | "Filesystem in Userspace": lets a normal program act as a file system. |
| Checksum, SHA-256 | A fixed-length fingerprint computed from content. Same content, same checksum. |
| Content-addressed storage | Storing data under its checksum instead of a path or name. |
| Blob | One stored file content, named by its checksum. |
| Index | An image's list of `path → checksum`. |
| Deduplication | Storing identical data only once. |
| Lazy loading | Fetching data only when it's actually needed. |
| Cache | A local copy kept so the same data isn't fetched twice. |
| CDN | A network of servers that keeps copies of content close to users. |
| Cold start | The delay before a freshly started container can do its work. |
