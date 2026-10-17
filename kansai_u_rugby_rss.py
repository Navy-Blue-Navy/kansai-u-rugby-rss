import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.kansai-u.ac.jp/sports/news/"
START_URL = BASE_URL + "?cc=00094&x=27&y=13"

OUTPUT_FILE = Path("kansai_u_rugby.xml")

FEED_TITLE = "関西大学 スポーツ振興グループ ラグビー部"
FEED_LINK = START_URL
FEED_DESCRIPTION = "関西大学 スポーツ振興グループのラグビー部新着情報"

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    )
}

FIRST_YEAR = 2012
CURRENT_YEAR = datetime.now(JST).year

MAX_PAGES_PER_YEAR = 30
RETRIES = 3
TIMEOUT = 30
WAIT_SECONDS = 10


def fetch(session, url, params=None):
    for attempt in range(1, RETRIES + 1):
        try:
            if params:
                print(
                    f"取得 {attempt}/{RETRIES}: "
                    f"{url}?{urlencode(params)}"
                )
            else:
                print(f"取得 {attempt}/{RETRIES}: {url}")

            response = session.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=TIMEOUT,
            )

            print(
                f"HTTP: {response.status_code} / "
                f"{len(response.content)} bytes"
            )

            response.raise_for_status()
            return response

        except requests.RequestException as e:
            print(f"取得失敗: {e}")

            if attempt < RETRIES:
                print(f"{WAIT_SECONDS}秒待って再試行します")
                time.sleep(WAIT_SECONDS)

    raise RuntimeError(f"取得できませんでした: {url}")


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def parse_date_text(text):
    match = re.search(
        r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b",
        text,
    )

    if not match:
        return None

    year, month, day = map(int, match.groups())

    try:
        return datetime(
            year,
            month,
            day,
            0,
            0,
            0,
            tzinfo=JST,
        )
    except ValueError:
        return None


def extract_date_from_context(anchor):
    candidates = []

    node = anchor

    for _ in range(5):
        if node is None:
            break

        try:
            text = normalize_text(node.get_text(" ", strip=True))
        except Exception:
            text = ""

        if text:
            candidates.append(text)

        node = node.parent

    for text in candidates:
        dt = parse_date_text(text)

        if dt:
            return dt

    return None


def clean_title(text):
    text = normalize_text(text)

    text = re.sub(
        r"^ラグビー部[\s　]*",
        "",
        text,
    )

    return text.strip()


def parse_articles(html):
    soup = BeautifulSoup(html, "html.parser")

    articles = {}

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        text = normalize_text(anchor.get_text(" ", strip=True))

        if not href or not text:
            continue

        if "detail.php?c=" not in href:
            continue

        if not text.startswith("ラグビー部"):
            continue

        link = urljoin(BASE_URL, href)
        title = clean_title(text)

        if not title:
            continue

        pub_date = extract_date_from_context(anchor)

        if not pub_date:
            continue

        articles[link] = {
            "title": title,
            "link": link,
            "pub_date": pub_date,
            "description": "",
        }

    return list(articles.values())


def extract_pagination_data(html):
    soup = BeautifulSoup(html, "html.parser")

    page_numbers = set()

    for anchor in soup.find_all("a"):
        text = normalize_text(anchor.get_text(" ", strip=True))

        if re.fullmatch(r"\d+", text):
            number = int(text)

            if 1 <= number <= MAX_PAGES_PER_YEAR:
                page_numbers.add(number)

    scripts = soup.find_all("script")

    script_text = "\n".join(
        script.get_text("\n", strip=False)
        for script in scripts
    )

    patterns = [
        r"(?:page|pageno|pagenum|page_no|pageNum)\s*[=:]\s*['\"]?(\d+)",
        r"[?&](?:page|pageno|pagenum|page_no)=(\d+)",
    ]

    for pattern in patterns:
        for value in re.findall(
            pattern,
            script_text,
            flags=re.IGNORECASE,
        ):
            number = int(value)

            if 1 <= number <= MAX_PAGES_PER_YEAR:
                page_numbers.add(number)

    return sorted(page_numbers), script_text


def try_page_parameters(session, year, expected_first_links):
    candidates = [
        "page",
        "p",
        "pageno",
        "pagenum",
        "page_no",
        "pageNum",
    ]

    working = []

    for parameter in candidates:
        params = {
            "year": year,
            "cc": "00094",
            parameter: 2,
        }

        try:
            response = fetch(session, BASE_URL, params=params)
        except RuntimeError:
            continue

        articles = parse_articles(response.text)

        if not articles:
            continue

        links = {item["link"] for item in articles}

        if links != expected_first_links:
            working.append(parameter)
            print(
                f"ページ送り候補を検出: "
                f"{parameter}=2 / {len(articles)}件"
            )
            break

        time.sleep(0.5)

    return working[0] if working else None


def crawl_year(session, year):
    print()
    print("=" * 70)
    print(f"{year}年")
    print("=" * 70)

    params = {
        "year": year,
        "cc": "00094",
    }

    response = fetch(session, BASE_URL, params=params)
    first_articles = parse_articles(response.text)

    print(f"1ページ目の記事数: {len(first_articles)}")

    collected = {
        item["link"]: item
        for item in first_articles
    }

    if not first_articles:
        return list(collected.values())

    page_numbers, script_text = extract_pagination_data(
        response.text
    )

    print(
        "画面上のページ番号: "
        + (
            ", ".join(map(str, page_numbers))
            if page_numbers
            else "なし"
        )
    )

    first_links = {
        item["link"]
        for item in first_articles
    }

    page_parameter = try_page_parameters(
        session,
        year,
        first_links,
    )

    if page_parameter:
        max_page = max(page_numbers) if page_numbers else MAX_PAGES_PER_YEAR

        for page in range(2, max_page + 1):
            print()
            print(f"--- {year}年 ページ {page} ---")

            params = {
                "year": year,
                "cc": "00094",
                page_parameter: page,
            }

            response = fetch(
                session,
                BASE_URL,
                params=params,
            )

            articles = parse_articles(response.text)

            print(f"このページの記事数: {len(articles)}")

            if not articles:
                print("記事がないため、この年の巡回を終了します")
                break

            links = {
                item["link"]
                for item in articles
            }

            if links == first_links:
                print(
                    "1ページ目と同じ内容のため、"
                    "この年の巡回を終了します"
                )
                break

            before = len(collected)

            for item in articles:
                collected[item["link"]] = item

            added = len(collected) - before

            print(f"新たに追加された記事数: {added}")

            if added == 0:
                print(
                    "新しい記事が増えないため、"
                    "この年の巡回を終了します"
                )
                break

            time.sleep(1)

    else:
        print(
            "通常のURLパラメータによるページ送りは"
            "検出されませんでした"
        )

    print(f"{year}年の取得件数: {len(collected)}")

    return list(collected.values())


def load_existing():
    if not OUTPUT_FILE.exists():
        return {}

    try:
        tree = ET.parse(OUTPUT_FILE)
        root = tree.getroot()

        existing = {}

        for item in root.findall("./channel/item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date_text = item.findtext(
                "pubDate",
                "",
            ).strip()
            description = item.findtext(
                "description",
                "",
            ).strip()

            if not title or not link:
                continue

            try:
                pub_date = parsedate_to_datetime(
                    pub_date_text
                )

                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(
                        tzinfo=JST
                    )

                pub_date = pub_date.astimezone(JST)

            except Exception:
                continue

            existing[link] = {
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "description": description,
            }

        return existing

    except Exception as e:
        print(f"既存RSS読み込み失敗: {e}")
        return {}


def make_guid(link):
    return hashlib.sha256(
        link.encode("utf-8")
    ).hexdigest()


def write_rss(items):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
        },
    )

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(
        channel,
        "title",
    ).text = FEED_TITLE

    ET.SubElement(
        channel,
        "link",
    ).text = FEED_LINK

    ET.SubElement(
        channel,
        "description",
    ).text = FEED_DESCRIPTION

    ET.SubElement(
        channel,
        "language",
    ).text = "ja"

    ET.SubElement(
        channel,
        "lastBuildDate",
    ).text = format_datetime(
        datetime.now(JST)
    )

    for article in items:
        item = ET.SubElement(channel, "item")

        ET.SubElement(
            item,
            "title",
        ).text = article["title"]

        ET.SubElement(
            item,
            "link",
        ).text = article["link"]

        guid = ET.SubElement(
            item,
            "guid",
            {
                "isPermaLink": "false",
            },
        )

        guid.text = make_guid(article["link"])

        ET.SubElement(
            item,
            "pubDate",
        ).text = format_datetime(
            article["pub_date"]
        )

        ET.SubElement(
            item,
            "description",
        ).text = article.get(
            "description",
            "",
        )

    tree = ET.ElementTree(rss)

    ET.indent(
        tree,
        space="  ",
    )

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    print("=" * 70)
    print("関西大学 スポーツ振興グループ ラグビー部 RSS")
    print("=" * 70)

    existing = load_existing()

    print(f"既存RSS件数: {len(existing)}")

    session = requests.Session()

    current = {}

    try:
        for year in range(
            CURRENT_YEAR,
            FIRST_YEAR - 1,
            -1,
        ):
            articles = crawl_year(
                session,
                year,
            )

            for article in articles:
                current[article["link"]] = article

            time.sleep(1)

    except Exception as e:
        print()
        print(f"取得処理でエラー: {e}")
        print(
            "既存RSSがある場合は変更せず終了します"
        )
        raise

    if not current:
        raise RuntimeError(
            "ラグビー部の記事を1件も取得できませんでした"
        )

    print()
    print(
        f"サイトから取得した記事総数: "
        f"{len(current)}"
    )

    new_links = set(current) - set(existing)

    merged = dict(existing)
    merged.update(current)

    items = list(merged.values())

    items.sort(
        key=lambda x: x["pub_date"],
        reverse=True,
    )

    print(f"新規記事数: {len(new_links)}")

    print()
    print("最新30件:")

    for i, article in enumerate(
        items[:30],
        start=1,
    ):
        date_text = article[
            "pub_date"
        ].astimezone(JST).strftime(
            "%Y-%m-%d"
        )

        print(
            f"{i:2d}. "
            f"{date_text} "
            f"{article['title']}"
        )

        print(
            f"    {article['link']}"
        )

    write_rss(items)

    print()
    print(f"保存完了: {OUTPUT_FILE}")
    print(f"RSS総件数: {len(items)}")


if __name__ == "__main__":
    main()