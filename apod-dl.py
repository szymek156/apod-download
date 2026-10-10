#!/usr/bin/env python3
# Copyright © 2023, Huy Dao
"""Download pictures from NASA's Astronomy Picture of the Day (APOD) as wallpapers.

APOD moved from apod.nasa.gov to https://science.nasa.gov/apod/ in 2026.
Every day has a page https://science.nasa.gov/apod/?date=YYMMDD that
redirects to https://science.nasa.gov/image-article/apod-YYYY-month-D-title/;
legacy links such as https://apod.nasa.gov/apod/ap201109.html redirect there
too.  A date without an APOD (a few gaps in 1995, or the future) does not
redirect and simply shows the landing page.

The crawl is driven by the calendar: starting from the start page's date it
walks back day by day to the first APOD (1995 June 16), fetching and rendering
several days in parallel.  Days already listed in album_list.txt whose picture
still exists are skipped without touching the network.

Each picture is scaled to fit the wallpaper size on a black canvas (aspect
ratio kept) and captioned in the lower-right corner with its title and date,
white text on a black box.  The result is saved as JPEG.

After every run the script also (re)writes a MATE/GNOME background slideshow
(apod-slideshow.xml) that cycles through all pictures in the directory in a
random order, plus a small apod.xml that registers the slideshow in the
desktop's Appearance / Background dialog.
"""
import argparse
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps
from requests.adapters import HTTPAdapter
from tqdm.auto import tqdm
from urllib3.util.retry import Retry

APOD_HOME = "https://science.nasa.gov/apod/"
FIRST_APOD = date(1995, 6, 16)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
}
TIMEOUT = 30
RETRIES = 3  # per request, with exponential backoff, on 429/5xx and network errors
DEFAULT_JOBS = 4  # parallel downloads; be polite to NASA's servers

WALLPAPER_SIZE = (1920, 1080)  # default for --size; use your screen resolution
JPEG_QUALITY = 92
TITLE_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DATE_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TITLE_SIZE = 36
DATE_SIZE = 26
CAPTION_MARGIN = 40  # distance of the caption box from the canvas edges
CAPTION_PADDING = 16  # space between the text and the edge of the black box
CAPTION_GAP = 8  # vertical space between caption lines

ALBUM_FILE = "album_list.txt"
SLIDESHOW_FILE = "apod-slideshow.xml"
PROPERTIES_FILE = "apod.xml"  # copy to /usr/share/mate-background-properties/
SLIDE_SECONDS = 5 * 60  # how long each picture stays on screen
FADE_SECONDS = 5  # cross-fade between pictures (part of SLIDE_SECONDS)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
SPACE_RE = re.compile(r"\s+")
TITLE_DATE_RE = re.compile(r"APOD:\s*(\d{4}\s+[A-Z][a-z]+\s+\d{1,2})")
SLUG_DATE_RE = re.compile(r"/apod-(\d{4})-([a-z]+)-(\d{1,2})-", re.I)
APOD_DATE_RE = re.compile(r"(\d{4}) ([A-Z][a-z]+) (\d{1,2})")
URL_YYMMDD_RE = re.compile(r"(?:[?&]date=|/ap)(\d{2})(\d{2})(\d{2})(?:\.html|&|$)")
ARTICLE_HREF_RE = re.compile(r"/image-article/apod-")

album_lock = threading.Lock()
_thread_local = threading.local()


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def make_session():
    sess = requests.Session()
    sess.headers.update(HEADERS)
    retry = Retry(
        total=RETRIES,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
    )
    sess.mount("https://", HTTPAdapter(max_retries=retry))
    sess.mount("http://", HTTPAdapter(max_retries=retry))
    return sess


def thread_session():
    """One requests.Session per worker thread (sessions are not thread-safe)."""
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = _thread_local.session = make_session()
    return sess


def fetch_page(sess, url):
    """GET a page (following redirects) and return (final_url, soup)."""
    resp = sess.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.url, BeautifulSoup(resp.text, features="lxml")


def download_bytes(sess, url):
    resp = sess.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.content


# ----------------------------------------------------------------------------
# Page parsing
# ----------------------------------------------------------------------------
def day_url(d):
    return f"{APOD_HOME}?date={d:%y%m%d}"


def to_date(text):
    """'2026 October 7' -> datetime.date, or None."""
    m = APOD_DATE_RE.fullmatch(text.strip())
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(int(m.group(1)), MONTHS.index(m.group(2)) + 1, int(m.group(3)))
    except ValueError:
        return None


def is_article_page(soup):
    return soup.find("article", class_="type-image-article") is not None


def find_article_link(soup):
    """On the landing page the day's picture links to its canonical per-day page.

    Skip the "Yesterday's Image" / "Tomorrow's Image" navigation links.
    """
    nav = soup.find(class_="wp-block-smd-block-next-prev")
    for a in soup.find_all("a", href=ARTICLE_HREF_RE):
        if nav is not None and nav in a.parents:
            continue
        return a["href"]
    return None


def original_image_url(src):
    """Turn a resized 'dynamicimage' URL into the URL of the original asset.

    The <img> tags point at an image service that caps output at 1280px:
      https://assets.science.nasa.gov/dynamicimage/assets/.../photo.jpg?w=...&h=...
    The untouched original lives at:
      https://assets.science.nasa.gov/content/dam/.../photo.jpg
    """
    parts = urlsplit(src)
    path = parts.path.replace("/dynamicimage/assets/", "/content/dam/", 1)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def resized_image_url(original_url, size):
    """URL of a server-side copy scaled to fit within `size` (aspect kept, never upscaled)."""
    parts = urlsplit(original_url)
    path = parts.path.replace("/content/dam/", "/dynamicimage/assets/", 1)
    w, h = size
    return urlunsplit((parts.scheme, parts.netloc, path, f"w={w}&h={h}&fit=clip", ""))


def find_image_url(soup):
    """Return the full-resolution URL of the day's picture, or None (e.g. video days)."""
    img = None
    fig = soup.find("figure", class_="hds-media-inner")
    if fig is not None:
        img = fig.find("img")
    if img is None:  # before/after "rollover" pictures
        img = soup.find("img", class_="smd-image-rollover__image--primary")
    if img is None:
        img = soup.find("img", src=re.compile(r"assets\.science\.nasa\.gov/.*/apod/"))
    if img is None or not img.get("src"):
        return None
    return original_image_url(img["src"])


def find_date(soup, page_url):
    """Return the APOD date as e.g. '2026 October 9'."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = TITLE_DATE_RE.search(title)
    if m:
        return SPACE_RE.sub(" ", m.group(1))
    m = SLUG_DATE_RE.search(page_url)
    if m:
        return f"{m.group(1)} {m.group(2).capitalize()} {int(m.group(3))}"
    # Landing page without a link to the per-day page: APOD publishes just
    # after midnight US Eastern, so the current US Eastern date is the APOD date.
    t = time.gmtime(time.time() - 4 * 3600)
    return f"{t.tm_year} {MONTHS[t.tm_mon - 1]} {t.tm_mday}"


def find_title(soup):
    for h in soup.find_all(["h1", "h2"], class_="display-48"):
        text = h.get_text(" ", strip=True)
        if text and text != "Astronomy Picture of the Day":
            return text
    h1 = soup.find("h1")
    if h1 is not None:
        return h1.get_text(" ", strip=True)
    return ""


def find_explanation(soup):
    p = soup.find("p", class_="media-detail-hero__description")
    if p is None:
        return ""
    return SPACE_RE.sub(" ", p.get_text(" ", strip=True))


# ----------------------------------------------------------------------------
# Wallpaper rendering
# ----------------------------------------------------------------------------
def fetch_image(sess, original_url, size):
    """Prefer the server-side scaled copy (much smaller download).

    The image service does not handle every format (GIFs return 404), so fall
    back to the original file, which is then scaled locally.
    """
    try:
        return download_bytes(sess, resized_image_url(original_url, size))
    except requests.HTTPError:
        return download_bytes(sess, original_url)


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    """Greedy word-wrap so no line is wider than max_width pixels."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_caption(canvas, title, date_text):
    """Draw title and date, white on a black box, in the lower-right corner."""
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(TITLE_FONT, TITLE_SIZE)
    date_font = load_font(DATE_FONT, DATE_SIZE)
    max_text_width = canvas.width - 2 * CAPTION_MARGIN - 2 * CAPTION_PADDING

    lines = [(line, title_font) for line in wrap_text(draw, title, title_font, max_text_width)]
    if date_text:
        lines.append((date_text, date_font))
    if not lines:
        return

    # textbbox gives (left, top, right, bottom) relative to the drawing origin.
    boxes = [draw.textbbox((0, 0), text, font=font) for text, font in lines]
    text_w = max(r - l for l, t, r, b in boxes)
    text_h = sum(b - t for l, t, r, b in boxes) + CAPTION_GAP * (len(lines) - 1)

    x1 = canvas.width - CAPTION_MARGIN
    y1 = canvas.height - CAPTION_MARGIN
    x0 = x1 - text_w - 2 * CAPTION_PADDING
    y0 = y1 - text_h - 2 * CAPTION_PADDING
    draw.rectangle([x0, y0, x1, y1], fill="black")

    y = y0 + CAPTION_PADDING
    for (text, font), (l, t, r, b) in zip(lines, boxes):
        x = x1 - CAPTION_PADDING - (r - l)  # right-aligned
        draw.text((x - l, y - t), text, font=font, fill="white")
        y += (b - t) + CAPTION_GAP


def make_wallpaper(data, title, date_text, size):
    """Scale the picture to fit `size` on a black canvas and caption it."""
    with Image.open(BytesIO(data)) as src:
        picture = ImageOps.contain(src.convert("RGB"), size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(picture, ((size[0] - picture.width) // 2, (size[1] - picture.height) // 2))
    draw_caption(canvas, title, date_text)
    return canvas


def save_wallpaper(wallpaper, dest):
    """Write via a .part file so an abort never leaves a truncated image."""
    part = dest + ".part"
    wallpaper.save(part, "JPEG", quality=JPEG_QUALITY, optimize=True)
    os.replace(part, dest)


# ----------------------------------------------------------------------------
# Album list
# ----------------------------------------------------------------------------
def load_done(adir):
    """Dates recorded in album_list.txt whose picture is still on disk."""
    done = set()
    try:
        fd = open(os.path.join(adir, ALBUM_FILE), encoding="utf-8")
    except FileNotFoundError:
        return done
    with fd:
        for line in fd:
            parts = line.rstrip("\n").split(" - ", 2)
            if len(parts) < 2:
                continue
            d = to_date(parts[0])
            if d is not None and os.path.exists(os.path.join(adir, parts[1])):
                done.add(d)
    return done


def append_album(adir, imgdate, imgfilename, imgtitle, imgtext):
    with album_lock:
        with open(os.path.join(adir, ALBUM_FILE), "at", encoding="utf-8") as fd:
            fd.write(f"{imgdate} - {imgfilename} - {imgtitle} - {imgtext}\n\n")


# ----------------------------------------------------------------------------
# Crawling
# ----------------------------------------------------------------------------
def date_from_url(url):
    """Date named by a ?date=YYMMDD, legacy apYYMMDD.html or per-day slug URL, else None."""
    m = URL_YYMMDD_RE.search(url)
    if m:
        yy, mm, dd = (int(g) for g in m.groups())
        year = 1900 + yy if yy >= FIRST_APOD.year % 100 else 2000 + yy
        try:
            return date(year, mm, dd)
        except ValueError:
            return None
    m = SLUG_DATE_RE.search(url)
    if m:
        return to_date(f"{m.group(1)} {m.group(2).capitalize()} {int(m.group(3))}")
    return None


def start_date_of(url):
    """Date of the start page: read it from the URL, else fetch the page once."""
    d = date_from_url(url)
    if d is not None:
        return d
    with make_session() as sess:
        page_url, soup = fetch_page(sess, url)
        if not is_article_page(soup):
            article = find_article_link(soup)
            if article:
                page_url, soup = fetch_page(sess, urljoin(page_url, article))
    d = to_date(find_date(soup, page_url))
    if d is None:
        raise SystemExit(f"cannot work out the APOD date of {url}")
    return d


def process_day(d, adir, size):
    """Download and render one day's picture.  Returns a one-line status."""
    sess = thread_session()
    page_url, soup = fetch_page(sess, day_url(d))
    if not is_article_page(soup):
        return f"{d}: no APOD on this day"

    imgdate = find_date(soup, page_url)
    imgtitle = find_title(soup)
    imgtext = find_explanation(soup)
    imgurl = find_image_url(soup)
    if imgurl is None:
        return f"{imgdate} - {imgtitle}: no image found (video or interactive APOD?)"

    stem = os.path.splitext(unquote(os.path.basename(urlsplit(imgurl).path)))[0]
    imgfilename = stem + ".jpg"
    dest = os.path.join(adir, imgfilename)
    if os.path.exists(dest):
        return f"{imgdate} - {imgfilename}: already downloaded"

    data = fetch_image(sess, imgurl, size)
    try:
        wallpaper = make_wallpaper(data, imgtitle, imgdate, size)
    except (Image.UnidentifiedImageError, OSError) as exc:
        return f"{imgdate} - {imgfilename}: cannot render ({exc})"
    save_wallpaper(wallpaper, dest)
    append_album(adir, imgdate, imgfilename, imgtitle, imgtext)
    return f"{imgdate} - {imgfilename}: saved ({imgtitle})"


def crawl(dates, adir, size, jobs):
    """Process the given days in parallel with a progress bar.  Ctrl+C aborts."""
    failed = 0
    executor = ThreadPoolExecutor(max_workers=jobs)
    futures = {executor.submit(process_day, d, adir, size): d for d in dates}
    try:
        with tqdm(total=len(futures), unit="day") as bar:
            for fut in as_completed(futures):
                try:
                    bar.write(fut.result())
                except Exception as exc:  # keep going, report at the end
                    failed += 1
                    bar.write(f"{futures[fut]}: FAILED ({exc})")
                bar.update(1)
    except KeyboardInterrupt:
        print("\nfinishing the downloads in flight (Ctrl+C again to drop them)...")
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    if failed:
        print(f"{failed} day(s) failed; run again to retry them")


# ----------------------------------------------------------------------------
# Desktop slideshow
# ----------------------------------------------------------------------------
def write_slideshow(adir):
    """Write a shuffled MATE/GNOME slideshow XML of every picture in adir.

    The desktop's slideshow format has no "random" or "whole directory" option,
    so every picture is listed explicitly; shuffling here gives a different
    order each time the script runs.  Returns the slideshow path, or None when
    the directory holds no pictures.
    """
    adir = os.path.abspath(adir)
    try:
        names = sorted(n for n in os.listdir(adir) if n.lower().endswith(IMAGE_EXTENSIONS))
    except FileNotFoundError:
        names = []
    if not names:
        print(f"no pictures in {adir}, slideshow not written")
        return None
    random.shuffle(names)
    files = [os.path.join(adir, n) for n in names]

    static = float(SLIDE_SECONDS - FADE_SECONDS)
    parts = [
        "<background>",
        "  <starttime>",
        "    <year>2000</year>",
        "    <month>01</month>",
        "    <day>01</day>",
        "    <hour>00</hour>",
        "    <minute>00</minute>",
        "    <second>00</second>",
        "  </starttime>",
        f"<!-- Generated by apod-dl.py: {len(files)} pictures, one every {SLIDE_SECONDS // 60} minutes. -->",
    ]
    for i, f in enumerate(files):
        nxt = files[(i + 1) % len(files)]
        parts += [
            "  <static>",
            f"    <duration>{static:.1f}</duration>",
            f"    <file>{xml_escape(f)}</file>",
            "  </static>",
            "  <transition>",
            f"    <duration>{float(FADE_SECONDS):.1f}</duration>",
            f"    <from>{xml_escape(f)}</from>",
            f"    <to>{xml_escape(nxt)}</to>",
            "  </transition>",
        ]
    parts.append("</background>")

    slideshow = os.path.join(adir, SLIDESHOW_FILE)
    with open(slideshow, "w", encoding="utf-8") as fd:
        fd.write("\n".join(parts) + "\n")

    properties = os.path.join(adir, PROPERTIES_FILE)
    with open(properties, "w", encoding="utf-8") as fd:
        fd.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE wallpapers SYSTEM "mate-wp-list.dtd">\n'
            "<wallpapers>\n"
            '  <wallpaper deleted="false">\n'
            "    <name>APOD - Astronomy Picture of the Day</name>\n"
            f"    <filename>{xml_escape(slideshow)}</filename>\n"
            "    <options>scaled</options>\n"
            "  </wallpaper>\n"
            "</wallpapers>\n"
        )
    print(f"slideshow of {len(files)} pictures written to {slideshow}")
    return slideshow


def xml_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def parse_size(text):
    """'1920x1200' -> (1920, 1200)"""
    m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, e.g. 1920x1200, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Download pictures from NASA's Astronomy Picture of the Day as "
            "captioned wallpapers."
        )
    )
    ap.add_argument(
        "-u",
        "--url",
        type=str,
        help=(
            "start url.  Start from this page's date and go backwards.  If omitted, "
            "will start with today's page.  Accepts https://science.nasa.gov/apod/?date=YYMMDD, "
            "a per-day https://science.nasa.gov/image-article/apod-... page, or a legacy "
            "https://apod.nasa.gov/apod/apYYMMDD.html link"
        ),
    )
    ap.add_argument(
        "-d",
        "--dir",
        type=str,
        default="apod-images",
        help="name of directory to save files to.  If omitted, will create and save files to ./apod-images/",
    )
    ap.add_argument(
        "-o",
        "--oneday",
        action="store_true",
        help="just get the one day's apod and nothing else",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"number of days to download in parallel (default: {DEFAULT_JOBS})",
    )
    ap.add_argument(
        "-s",
        "--size",
        type=parse_size,
        default=WALLPAPER_SIZE,
        metavar="WxH",
        help=(
            "wallpaper size, use your screen resolution so nothing gets cropped "
            f"(default: {WALLPAPER_SIZE[0]}x{WALLPAPER_SIZE[1]})"
        ),
    )
    ap.add_argument(
        "-x",
        "--slideshow-only",
        action="store_true",
        help=(
            f"download nothing, just rewrite {SLIDESHOW_FILE} (new random order) "
            f"and {PROPERTIES_FILE} from the pictures already in the directory"
        ),
    )
    args = ap.parse_args()
    if args.jobs < 1:
        ap.error("--jobs must be at least 1")

    try:
        if not args.slideshow_only:
            os.makedirs(args.dir, exist_ok=True)
            start = start_date_of(args.url or APOD_HOME)
            if args.oneday:
                dates = [start]
            else:
                dates = [start - timedelta(days=i) for i in range((start - FIRST_APOD).days + 1)]
            done = load_done(args.dir)
            todo = [d for d in dates if d not in done]
            print(
                f"{len(dates)} day(s) from {start} back to {dates[-1]}: "
                f"{len(dates) - len(todo)} already downloaded, "
                f"{len(todo)} to fetch with {args.jobs} parallel downloads"
            )
            if todo:
                crawl(todo, args.dir, args.size, args.jobs)
    except KeyboardInterrupt:
        print("\naborted")
    finally:
        write_slideshow(args.dir)


if __name__ == "__main__":
    main()
