import argparse

from .client import BlobCache, Link, fetch_index, find_image, pull_image, push_tar


def mb(size):
    return f"{size / 1e6:.1f} MB"


def main():
    parser = argparse.ArgumentParser(prog="python3 -m imagestore", description="Image store client.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="API server (default: %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)

    push_cmd = commands.add_parser("push", help="upload an image, sending only files the server doesn't have")
    push_cmd.add_argument("tar", help="the image's files as a tar, e.g. from docker export")
    push_cmd.add_argument("ref", help="name:tag")

    pull_cmd = commands.add_parser("pull", help="download the whole image and unpack it (the usual way)")
    pull_cmd.add_argument("ref", help="name:tag")
    pull_cmd.add_argument("dest", help="folder to unpack into")

    mount_cmd = commands.add_parser("mount", help="mount the image; each file is downloaded when it's opened")
    mount_cmd.add_argument("ref", help="name:tag")
    mount_cmd.add_argument("mountpoint")
    mount_cmd.add_argument("--cache", default="cache", help="local blob cache (default: %(default)s)")

    for command in (pull_cmd, mount_cmd):
        command.add_argument("--bandwidth", type=float, help="pretend the link is this many Mbit/s")
        command.add_argument("--latency", type=float, default=0, help="pretend each request takes this many ms more")

    args = parser.parse_args()

    if args.command == "push":
        name, _, tag = args.ref.partition(":")
        image, uploaded, skipped = push_tar(args.server, args.tar, name, tag or "latest")
        print(f"{args.ref}: {image['file_count']:,} files, {mb(image['total_size'])}; "
              f"uploaded {mb(uploaded)}, the server already had {mb(skipped)}")
        return

    link = Link(args.bandwidth, args.latency)
    image = find_image(args.server, args.ref)
    if args.command == "pull":
        downloaded = pull_image(args.server, image["id"], args.dest, link)
        print(f"downloaded the whole image ({mb(downloaded)}) and unpacked it")
    else:
        from .lazyfs import mount  # needs mfusepy, which push and pull don't

        index = fetch_index(args.server, image["id"], link)
        cache = BlobCache(args.server, args.cache, link)
        mount(index, cache, args.mountpoint)
        share = cache.downloaded_bytes / image["total_size"] if image["total_size"] else 0
        print(f"downloaded {cache.downloads} files, {mb(cache.downloaded_bytes)} ({share:.0%} of the image), "
              f"{cache.hits} opens served from the cache")


if __name__ == "__main__":
    main()
