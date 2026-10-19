"""
Notionの「News Reading」データベースから当日分の記事を取得し、
英文本文をedge-ttsで音声化してPodcastのfeed.xmlを更新する。
GitHub Actions上で毎晩実行される想定。
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from mutagen.mp3 import MP3

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATA_SOURCE_ID = "5fcfcfce-8ade-401a-aecd-49a916637fdb"
NOTION_VERSION = "2025-09-03"
VOICE = "en-US-AriaNeural"

# 話者ラベル(Part3は Woman:/Man:、Part4は Speaker:)ごとの声の割り当て。
# ラベルのない行(旧形式やPart2の地の文)は直前の話者の声を引き継ぐ。
SPEAKER_VOICES = {
    "Woman": "en-US-AriaNeural",
    "Man": "en-US-GuyNeural",
    "Speaker": "en-US-AriaNeural",
}

# Part2: 質問(Q)と選択肢(A/B/C)の直後に置くポーズの長さ(秒)。
# 「次の番号の前 1.5秒」は、Cの直後のポーズとして表現する
# (Cの次に来る行は次セットの番号行なので、Cの直後=次の番号の前と同じ位置になる)。
PART2_PAUSE_AFTER = {"Q": 1.0, "A": 0.7, "B": 0.7, "C": 1.5}

# Part3(Woman:/Man:)でターン(話者)が切り替わる所に入れるポーズの長さ(秒)。
# Part4(Speaker:のみ)は声が変わらないので、このポーズは自動的に入らない。
SPEAKER_SWITCH_PAUSE = 0.4

JAPANESE_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")
NUMBER_LINE_RE = re.compile(r"^\d+\.\s*$")
LABEL_LINE_RE = re.compile(r"^(Woman|Man|Speaker|Q|A|B|C):\s*(.*)$")

BASE_URL = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # "owner/repo"
JSDELIVR_BRANCH = "main"

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
EPISODES_DIR = DOCS_DIR / "episodes"
EPISODES_JSON = DOCS_DIR / "episodes.json"
FEED_PATH = DOCS_DIR / "feed.xml"
COVER_URL = f"{BASE_URL}/cover.jpg"

# GitHub Pagesはmp3にContent-Type: audio/mp3を返しApple Podcastsに拒否されるため、
# 音声ファイルだけは正しいContent-Type(audio/mpeg)で配信されるjsDelivr CDN経由にする。
AUDIO_BASE_URL = f"https://cdn.jsdelivr.net/gh/{GITHUB_REPOSITORY}@{JSDELIVR_BRANCH}/docs/episodes"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

JST = timezone(timedelta(hours=9))

CHANNEL_TITLE = "News Reading（音読ニュース）"
CHANNEL_DESC = "Notionに毎晩追加される英語ニュース記事を、英語音声のまま聴けるPodcastです。"


def today_jst_str():
    return datetime.now(JST).strftime("%Y-%m-%d")


def query_pending_articles(date_str):
    """日付が実行日以前で、まだ「音読済み」でない記事を全部取得する。
    cronの実行時刻とJST日付のズレで「今日ちょうど」の記事が
    空振りするのを防ぐため、日付の完全一致ではなく「以前」で拾う。"""
    url = f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "日付", "date": {"on_or_before": date_str}},
                {"property": "音読済み", "checkbox": {"equals": False}},
            ]
        },
        "sorts": [{"property": "日付", "direction": "ascending"}],
    }
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_page_title(page):
    props = page["properties"]
    title_prop = props.get("Name", {}).get("title", [])
    if title_prop:
        return "".join(t.get("plain_text", "") for t in title_prop)
    return "News Reading"


def get_page_date(page):
    date_prop = page["properties"].get("日付", {}).get("date")
    if date_prop and date_prop.get("start"):
        return date_prop["start"][:10]
    return today_jst_str()


def get_english_lines(page_id):
    """ページ本文から「## 英文全体」見出しの直後の行を1行ずつのリストで取り出す。
    新形式は1行1発話(paragraphブロックが行ごとに分かれる想定)だが、
    念のためブロック内の改行(\n)でも分割し、旧形式(1ブロックに全文)でも
    崩れないようにする。"""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    blocks = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    capturing = False
    lines = []
    for block in blocks:
        btype = block.get("type")
        if btype in ("heading_1", "heading_2", "heading_3"):
            heading_text = "".join(
                t.get("plain_text", "") for t in block[btype].get("rich_text", [])
            ).strip()
            if heading_text == "英文全体":
                capturing = True
                continue
            if capturing:
                break  # 次の見出しに到達したら終了
        elif capturing and btype == "paragraph":
            para_text = "".join(
                t.get("plain_text", "") for t in block["paragraph"].get("rich_text", [])
            )
            for line in para_text.split("\n"):
                if line.strip():
                    lines.append(line.strip())

    return lines


def build_speech_segments(lines):
    """「英文全体」の各行を、読み上げ用のセグメント(声・テキスト・直後のポーズ秒数)に変換する。
    旧形式(ラベルも記号も日本語もないプレーンな行)はそのまま1セグメントになり、
    今までと同じ結果になる。"""
    segments = []
    current_voice = VOICE
    speaker_started = False  # Woman/Man/Speakerラベルに一度でも遭遇したか

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if JAPANESE_RE.search(line):
            continue  # 日本語が混ざった行は読み上げない(想定外の混入への保険)

        line = line.replace("✓", "").strip()  # 想定外の混入への保険
        if not line:
            continue

        if NUMBER_LINE_RE.match(line):
            # Part2の番号行(例: "1.")。そのまま読み上げ、ポーズなしで次の行(Q)へ続く。
            segments.append({"voice": current_voice, "text": line, "pause_after": 0.0})
            continue

        m = LABEL_LINE_RE.match(line)
        if m:
            label, text = m.group(1), m.group(2).strip()
            if label in SPEAKER_VOICES:
                new_voice = SPEAKER_VOICES[label]
                # Part3のターン切り替え(Woman<->Man)の境目に短いポーズを入れる。
                # 最初のターンや、Part4(ずっとSpeaker=声が変わらない)では発動しない。
                # Part2のポーズ(0より大きい)を誤って上書きしないよう、0の時だけ書き換える。
                if (
                    speaker_started
                    and new_voice != current_voice
                    and segments
                    and segments[-1]["pause_after"] == 0.0
                ):
                    segments[-1]["pause_after"] = SPEAKER_SWITCH_PAUSE
                speaker_started = True
                current_voice = new_voice
                pause = 0.0
            else:  # Q/A/B/C
                pause = PART2_PAUSE_AFTER[label]
            if text:
                segments.append({"voice": current_voice, "text": text, "pause_after": pause})
            continue

        # ラベルのないプレーンな行(旧形式、またはPart3/4の地の文)
        segments.append({"voice": current_voice, "text": line, "pause_after": 0.0})

    return segments


def build_speech_segments_safe(lines):
    """build_speech_segments()が例外を出したり、結果が空になったりした場合は、
    従来どおり「英文全体を丸ごと1回で読む」方式に自動で戻す。
    Podcastの配信が止まることを避けるための保険。"""
    fallback_text = " ".join(line.strip() for line in lines if line.strip())

    try:
        segments = build_speech_segments(lines)
    except Exception as e:
        print(f"新しい解析でエラーが発生したため、従来方式(英文全体を丸ごと1回)にフォールバックします: {e}")
        segments = []

    if not segments:
        if not fallback_text:
            return []
        print("読み上げ用テキストが空だったため、従来方式(英文全体を丸ごと1回)にフォールバックします。")
        segments = [{"voice": VOICE, "text": fallback_text, "pause_after": 0.0}]

    return segments


def mark_as_read(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": {"音読済み": {"checkbox": True}}}
    resp = requests.patch(url, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()


def synthesize_speech(segments, out_path, title):
    """セグメントごとに指定の声でedge-tts変換し、必要なら無音(ポーズ)を挟んで
    ffmpegで1本に結合する。edge-ttsは24kHz/48kbpsの低品質mp3しか出力しないため、
    結合後にffmpegで一般的なPodcast品質(44.1kHz/128kbps)に変換し、ID3タグも付与する。"""
    stem = out_path.stem
    part_files = []
    concat_list_path = out_path.with_name(f"{stem}.concat.txt")
    combined_path = out_path.with_name(f"{stem}.combined.mp3")

    try:
        for i, seg in enumerate(segments):
            text_file = out_path.with_name(f"{stem}_{i:03d}.txt")
            seg_path = out_path.with_name(f"{stem}_{i:03d}.raw.mp3")
            text_file.write_text(seg["text"], encoding="utf-8")
            try:
                subprocess.run(
                    [
                        "edge-tts",
                        "--voice", seg["voice"],
                        "--file", str(text_file),
                        "--write-media", str(seg_path),
                    ],
                    check=True,
                )
            finally:
                text_file.unlink(missing_ok=True)
            part_files.append(seg_path)

            pause = seg.get("pause_after", 0.0)
            if pause > 0:
                silence_path = out_path.with_name(f"{stem}_{i:03d}.silence.mp3")
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-f", "lavfi",
                        "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
                        "-t", str(pause),
                        "-c:a", "libmp3lame",
                        "-b:a", "48k",
                        str(silence_path),
                    ],
                    check=True,
                )
                part_files.append(silence_path)

        concat_list_path.write_text(
            "\n".join(f"file '{p.name}'" for p in part_files), encoding="utf-8"
        )
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_path),
                "-c", "copy",
                str(combined_path),
            ],
            check=True,
            cwd=str(out_path.parent),
        )

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(combined_path),
                "-ar", "44100",
                "-ac", "1",
                "-b:a", "128k",
                "-id3v2_version", "3",
                "-metadata", f"title={title}",
                "-metadata", "artist=News Reading",
                "-metadata", "album=News Reading（音読ニュース）",
                str(out_path),
            ],
            check=True,
        )
    finally:
        for p in part_files:
            p.unlink(missing_ok=True)
        concat_list_path.unlink(missing_ok=True)
        combined_path.unlink(missing_ok=True)


def load_episodes():
    if not EPISODES_JSON.exists():
        return []
    episodes = json.loads(EPISODES_JSON.read_text(encoding="utf-8"))
    for ep in episodes:
        if "filename" not in ep:
            ep["filename"] = ep["audio_url"].rsplit("/", 1)[-1]
        ep.pop("audio_url", None)
    return episodes


def purge_jsdelivr_cache(filename):
    if not GITHUB_REPOSITORY:
        return
    url = f"https://purge.jsdelivr.net/gh/{GITHUB_REPOSITORY}@{JSDELIVR_BRANCH}/docs/episodes/{filename}"
    try:
        requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"jsDelivrキャッシュの更新リクエストに失敗しました(無視して続行): {e}")


def save_episodes(episodes):
    EPISODES_JSON.write_text(
        json.dumps(episodes, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_feed(episodes):
    episodes_sorted = sorted(episodes, key=lambda e: e["pub_date_raw"], reverse=True)
    items_xml = []
    for ep in episodes_sorted:
        audio_url = f"{AUDIO_BASE_URL}/{ep['filename']}"
        items_xml.append(f"""
    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['description'])}</description>
      <pubDate>{ep['pub_date_rfc822']}</pubDate>
      <enclosure url="{escape(audio_url)}" length="{ep['file_size']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{ep['guid']}</guid>
      <itunes:duration>{ep['duration']}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(CHANNEL_TITLE)}</title>
    <link>{BASE_URL}/</link>
    <language>ja</language>
    <itunes:author>News Reading</itunes:author>
    <description>{escape(CHANNEL_DESC)}</description>
    <itunes:summary>{escape(CHANNEL_DESC)}</itunes:summary>
    <itunes:image href="{COVER_URL}"/>
    <image>
      <url>{COVER_URL}</url>
      <title>{escape(CHANNEL_TITLE)}</title>
      <link>{BASE_URL}/</link>
    </image>
    <itunes:category text="Education"/>
    <itunes:explicit>false</itunes:explicit>
    <atom:link href="{BASE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
{''.join(items_xml)}
  </channel>
</rss>
"""
    FEED_PATH.write_text(feed, encoding="utf-8")


def main():
    if not BASE_URL:
        print("PAGES_BASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    today_str = today_jst_str()
    articles = query_pending_articles(today_str)

    if not articles:
        print(f"{today_str} 時点で未処理の記事は見つかりませんでした。処理をスキップします。")
        return

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    episodes = load_episodes()
    existing_guids = {e["guid"] for e in episodes}

    for page in articles:
        page_id = page["id"]
        if page_id in existing_guids:
            continue

        title = get_page_title(page)
        lines = get_english_lines(page_id)
        if not lines:
            print(f"本文が見つかりませんでした: {title}")
            continue

        segments = build_speech_segments_safe(lines)
        if not segments:
            print(f"読み上げ可能なテキストが見つかりませんでした: {title}")
            continue
        body = " ".join(seg["text"] for seg in segments)

        article_date = get_page_date(page)
        short_id = page_id.replace("-", "")[:8]
        filename = f"{article_date}-{short_id}.mp3"
        out_path = EPISODES_DIR / filename

        print(f"音声を生成中: {title}")
        synthesize_speech(segments, out_path, title)

        audio = MP3(str(out_path))
        duration_seconds = int(audio.info.length)
        h, rem = divmod(duration_seconds, 3600)
        m, s = divmod(rem, 60)
        duration_str = f"{h:02d}:{m:02d}:{s:02d}"

        pub_dt = datetime.now(JST)
        episodes.append({
            "guid": page_id,
            "title": title,
            "description": body,
            "filename": filename,
            "file_size": out_path.stat().st_size,
            "duration": duration_str,
            "pub_date_raw": pub_dt.isoformat(),
            "pub_date_rfc822": pub_dt.strftime("%a, %d %b %Y %H:%M:%S %z"),
        })

        purge_jsdelivr_cache(filename)
        mark_as_read(page_id)
        print(f"完了: {title}")

    save_episodes(episodes)
    build_feed(episodes)
    print("すべての処理が完了しました。")


if __name__ == "__main__":
    main()
