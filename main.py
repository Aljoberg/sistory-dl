from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import requests

ORIGIN = "https://sistory.si"
LOCALE = "slv"
DEFAULT_OUTPUT_DIRECTORY = "downloads"
REQUEST_TIMEOUT = (10, 120)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Options:
    root_segments: list[str]
    output_directory: Path
    dry_run: bool


@dataclass
class Stats:
    folders: int = 0
    publications: int = 0
    files_found: int = 0
    downloaded: int = 0
    renamed: int = 0
    skipped: int = 0
    failed: int = 0


class SIstoryClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SIstory-dl/1.0"

    def close(self) -> None:
        self.session.close()

    def get(
        self,
        url: str,
        description: str,
        *,
        stream: bool = False,
    ) -> requests.Response:
        response = self.session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            stream=stream,
        )
        if not response.ok:
            response.close()
            raise RuntimeError(
                f"Could not fetch {description}: "
                f"{response.status_code} {response.reason} ({url})"
            )
        return response

    def get_json(self, url: str, description: str) -> dict[str, Any]:
        with self.get(url, description) as response:
            data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"{description} returned invalid JSON data.")
        return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recursively download publications from a SIstory menu.",
    )
    parser.add_argument(
        "menu_path",
        help="the part after /menu/ in a SIstory URL (for example 1/7/397/407)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT_DIRECTORY,
        help=f"output directory (default: {DEFAULT_OUTPUT_DIRECTORY})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show folders and files without writing anything",
    )
    return parser


def parse_options(argv: list[str] | None = None) -> Options:
    arguments = build_parser().parse_args(argv)
    root_segments = [
        segment for segment in arguments.menu_path.strip("/").split("/") if segment
    ]
    if not root_segments or any(segment in {".", ".."} for segment in root_segments):
        raise ValueError(f"Invalid menu path: {arguments.menu_path}")
    return Options(
        root_segments=root_segments,
        output_directory=Path(arguments.output).resolve(),
        dry_run=arguments.dry_run,
    )


def encode_path(segments: list[str]) -> str:
    return "/".join(quote(segment, safe="") for segment in segments)


def site_menu_url(segments: list[str]) -> str:
    return f"{ORIGIN}/{LOCALE}/menu/{encode_path(segments)}"


def bootstrap_root(
    client: SIstoryClient,
    root_segments: list[str],
) -> tuple[str, dict[str, Any]]:
    url = site_menu_url(root_segments)
    with client.get(url, "the root menu page") as response:
        html = response.content.decode("utf-8")
    match = re.search(
        r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
        html,
        flags=re.IGNORECASE,
    )
    if not match:
        raise RuntimeError(f"Could not find __NEXT_DATA__ in {url}")

    bootstrap = json.loads(match.group(1))
    build_id = bootstrap.get("buildId")
    data = ((bootstrap.get("props") or {}).get("pageProps") or {}).get("data")
    if not isinstance(build_id, str) or not isinstance(data, dict):
        raise RuntimeError(f"The root menu page returned incomplete data: {url}")
    return build_id, data


def menu_data_url(
    build_id: str,
    segments: list[str],
    page: int = 1,
    page_size: int = 8,
) -> str:
    path = encode_path(segments)
    query: list[tuple[str, str]] = [("path", segment) for segment in segments]
    if page > 1:
        query.append(("p", f"{page}-{page_size}"))
    return (
        f"{ORIGIN}/_next/data/{quote(build_id, safe='')}/{LOCALE}/menu/"
        f"{path}.json?{urlencode(query)}"
    )


def fetch_menu_data(
    client: SIstoryClient,
    build_id: str,
    segments: list[str],
    page: int = 1,
    page_size: int = 8,
) -> dict[str, Any]:
    menu_path = "/".join(segments)
    response = client.get_json(
        menu_data_url(build_id, segments, page, page_size),
        f"menu {menu_path}, page {page}",
    )
    data = (response.get("pageProps") or {}).get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Menu {menu_path}, page {page} returned no data.")
    return data


def publication_data_url(build_id: str, publication_id: str) -> str:
    encoded_id = quote(publication_id, safe="")
    query = urlencode({"publicationId": publication_id})
    return (
        f"{ORIGIN}/_next/data/{quote(build_id, safe='')}/{LOCALE}/publication/"
        f"{encoded_id}.json?{query}"
    )


def fetch_publication_data(
    client: SIstoryClient,
    build_id: str,
    publication_id: str,
) -> dict[str, Any]:
    response = client.get_json(
        publication_data_url(build_id, publication_id),
        f"publication {publication_id}",
    )
    data = (response.get("pageProps") or {}).get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Publication {publication_id} returned no data.")
    return data


def sanitize_path_component(
    value: str,
    fallback: str,
    maximum_length: int = 140,
) -> str:
    result = unicodedata.normalize("NFKC", value)
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", result).strip()
    result = re.sub(r"[. ]+$", "", result)
    if not result or result in {".", ".."}:
        result = fallback
    if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", result, re.I):
        result = f"_{result}"
    result = re.sub(r"[. ]+$", "", result[:maximum_length])
    return result or fallback


def sanitize_file_name(value: str, fallback: str) -> str:
    cleaned = sanitize_path_component(value, fallback, sys.maxsize)
    if len(cleaned) <= 180:
        return cleaned
    extension = Path(cleaned).suffix
    stem_length = max(1, 180 - len(extension))
    stem = re.sub(r"[. ]+$", "", cleaned[:stem_length])
    return f"{stem}{extension}"


def remote_file_name(remote_url: str, publication_id: str) -> str:
    encoded_name = PurePosixPath(urlsplit(remote_url).path).name
    decoded_name = unquote(encoded_name)
    return sanitize_file_name(decoded_name, f"publication-{publication_id}")


def publication_title(
    publication: dict[str, Any],
    data: dict[str, Any],
) -> str:
    detail_titles = data.get("titles") or ([data["title"]] if data.get("title") else [])
    title_values = detail_titles or publication.get("titles") or []
    parts: list[str] = []
    for title in title_values:
        value = title if isinstance(title, str) else title.get("text")
        if isinstance(value, str):
            value = " ".join(value.split())
            if value:
                parts.append(value)
    return " - ".join(parts) or f"publication-{publication['id']}"


def publication_file_name(
    title: str,
    publication_id: str,
    publication_number: int,
    number_of_publications: int,
    file_number: int,
    number_of_files: int,
    remote_url: str,
) -> str:
    number_width = max(2, len(str(number_of_publications)))
    prefix = f"{publication_number:0{number_width}d} - "
    file_suffix = f"-file{file_number}" if number_of_files > 1 else ""
    original_extension = Path(remote_file_name(remote_url, publication_id)).suffix
    safe_title = sanitize_path_component(
        title,
        f"publication-{publication_id}",
        sys.maxsize,
    )
    available_length = max(
        1,
        180 - len(prefix) - len(file_suffix) - len(original_extension),
    )
    shortened_title = re.sub(r"[. ]+$", "", safe_title[:available_length])
    return f"{prefix}{shortened_title}{file_suffix}{original_extension}"


def add_suffix_to_file_name(file_name: str, suffix: str) -> str:
    extension = Path(file_name).suffix
    stem = file_name[: -len(extension)] if extension else file_name
    return sanitize_file_name(f"{stem} {suffix}{extension}", f"file-{suffix}")


def filesystem_path(path: Path) -> Path:
    # holy I hate Windows
    if os.name != "nt":
        return path

    absolute = str(path if path.is_absolute() else path.absolute())
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{absolute[2:]}")
    return Path(f"\\\\?\\{absolute}")


def direct_children(data: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for item in data.get("items") or []:
        if item.get("uriKey") is None:
            children.extend(item.get("items") or [])
        else:
            children.append(item)

    unique_children: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in children:
        uri_key = item.get("uriKey")
        if uri_key is None or str(uri_key) in seen:
            continue
        seen.add(str(uri_key))
        unique_children.append(item)
    return unique_children


def publication_page(data: dict[str, Any]) -> dict[str, Any]:
    publications = data.get("publications") or {}
    page = publications.get("publications") or {}
    return page if isinstance(page, dict) else {}


def all_publications(
    client: SIstoryClient,
    build_id: str,
    segments: list[str],
    first_data: dict[str, Any],
) -> list[dict[str, Any]]:
    first_page = publication_page(first_data)
    publications = list(first_page.get("results") or [])
    pagination = first_page.get("pagination") or {}
    page_size = int(pagination.get("pageSize") or 8)
    number_of_pages = int(pagination.get("nPages") or 1)
    for page_number in range(2, number_of_pages + 1):
        data = fetch_menu_data(
            client,
            build_id,
            segments,
            page_number,
            page_size,
        )
        publications.extend(publication_page(data).get("results") or [])
    return publications


def download_file(
    client: SIstoryClient,
    remote_path: str,
    requested_file_name: str,
    publication_id: str,
    folder_path: Path,
    options: Options,
    claimed_files: dict[str, str],
    stats: Stats,
) -> None:
    remote_url = urljoin(ORIGIN, remote_path)
    file_name = requested_file_name
    local_path = folder_path / file_name
    claim_key = str(local_path).lower()

    existing_claim = claimed_files.get(claim_key)
    if existing_claim and existing_claim != remote_url:
        file_name = add_suffix_to_file_name(file_name, f"({publication_id})")
        local_path = folder_path / file_name
        claim_key = str(local_path).lower()
    claimed_files[claim_key] = remote_url
    stats.files_found += 1

    if options.dry_run:
        print(f"  [dry-run] {remote_url} -> {local_path}")
        return
    local_filesystem_path = filesystem_path(local_path)
    if local_filesystem_path.exists():
        stats.skipped += 1
        print(f"  [skip] {local_path}")
        return

    legacy_path = folder_path / remote_file_name(remote_url, publication_id)
    legacy_filesystem_path = filesystem_path(legacy_path)
    if legacy_path != local_path and legacy_filesystem_path.exists():
        legacy_filesystem_path.rename(local_filesystem_path)
        stats.renamed += 1
        print(f"  [rename] {legacy_path} -> {local_path}")
        return

    partial_path = Path(f"{local_path}.part")
    partial_filesystem_path = filesystem_path(partial_path)
    partial_filesystem_path.unlink(missing_ok=True)
    try:
        print(f"  [download] {remote_url} -> {local_path}")
        with client.get(
            remote_url, f"file {urlsplit(remote_url).path}", stream=True
        ) as response:
            with partial_filesystem_path.open("xb") as destination:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        destination.write(chunk)
        os.replace(partial_filesystem_path, local_filesystem_path)
        stats.downloaded += 1
    except Exception:
        partial_filesystem_path.unlink(missing_ok=True)
        raise


def download_publication(
    client: SIstoryClient,
    build_id: str,
    publication: dict[str, Any],
    publication_number: int,
    number_of_publications: int,
    folder_path: Path,
    options: Options,
    claimed_files: dict[str, str],
    stats: Stats,
) -> None:
    publication_id = str(publication["id"])
    data = fetch_publication_data(client, build_id, publication_id)
    stats.publications += 1
    files = [
        file for file in data.get("files") or [] if (file.get("href") or {}).get("url")
    ]
    title = publication_title(publication, data)

    for file_index, file in enumerate(files, start=1):
        remote_path = file["href"]["url"]
        remote_url = urljoin(ORIGIN, remote_path)
        file_name = publication_file_name(
            title,
            publication_id,
            publication_number,
            number_of_publications,
            file_index,
            len(files),
            remote_url,
        )
        try:
            download_file(
                client,
                remote_path,
                file_name,
                publication_id,
                folder_path,
                options,
                claimed_files,
                stats,
            )
        except Exception as error:
            stats.failed += 1
            print(f"  [error] Publication {publication_id}: {error}", file=sys.stderr)


def crawl_menu(
    client: SIstoryClient,
    build_id: str,
    segments: list[str],
    parent_folder: Path,
    options: Options,
    claimed_files: dict[str, str],
    visited_menus: set[str],
    stats: Stats,
    preloaded_data: dict[str, Any] | None = None,
) -> None:
    menu_path = "/".join(segments)
    if menu_path in visited_menus:
        return
    visited_menus.add(menu_path)

    data = preloaded_data or fetch_menu_data(client, build_id, segments)
    fallback = segments[-1] if segments else "root"
    folder_name = sanitize_path_component(data.get("title") or fallback, fallback)
    folder_path = parent_folder / folder_name
    stats.folders += 1
    print(f"[folder] {folder_path}")
    if not options.dry_run:
        filesystem_path(folder_path).mkdir(parents=True, exist_ok=True)

    publications = all_publications(client, build_id, segments, data)
    for publication_index, publication in enumerate(publications, start=1):
        try:
            download_publication(
                client,
                build_id,
                publication,
                publication_index,
                len(publications),
                folder_path,
                options,
                claimed_files,
                stats,
            )
        except Exception as error:
            stats.failed += 1
            print(
                f"  [error] Publication {publication.get('id')}: {error}",
                file=sys.stderr,
            )

    for child in direct_children(data):
        try:
            crawl_menu(
                client,
                build_id,
                [*segments, str(child["uriKey"])],
                folder_path,
                options,
                claimed_files,
                visited_menus,
                stats,
            )
        except Exception as error:
            stats.failed += 1
            print(
                f"  [error] Menu {menu_path}/{child.get('uriKey')}: {error}",
                file=sys.stderr,
            )


def run(options: Options) -> Stats:
    root_path = "/".join(options.root_segments)
    print(f"Reading SIstory menu {root_path}...")
    client = SIstoryClient()
    stats = Stats()
    try:
        build_id, data = bootstrap_root(client, options.root_segments)
        crawl_menu(
            client,
            build_id,
            options.root_segments,
            options.output_directory,
            options,
            {},
            set(),
            stats,
            data,
        )
    finally:
        client.close()

    print(
        " ".join(
            [
                "Done.",
                f"{stats.folders} folders",
                f"{stats.publications} publications",
                f"{stats.files_found} files found",
                f"{stats.downloaded} downloaded",
                f"{stats.renamed} renamed",
                f"{stats.skipped} already present",
                f"{stats.failed} failed",
            ]
        )
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    try:
        stats = run(parse_options(argv))
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
