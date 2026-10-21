"""TOEICリスニング素材（第1週）の音声を作り、別フィード docs/listening/feed.xml を生成する。

既存のニュース配信（docs/feed.xml・docs/episodes/・episodes.json）には触らない。
手動実行用（GitHub Actionsには組み込まない）。
"""
import asyncio
import json
import re
import subprocess
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import edge_tts

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "listening"
BASE = "https://bbbryo6410-lab.github.io/news-reading-podcast/listening"
JA_VOICE = "ja-JP-NanamiNeural"
JA_RATE = "-12%"
SR = 24000
JST = timezone(timedelta(hours=9))

# (日付表示, 声: ラベル->voice, 種別, タイトル補足, 本文)
LESSONS = [
    ("1日目（10/22）", {"Woman": "en-US-JennyNeural", "Man": "en-US-GuyNeural"}, "Part3 会話", "経理：新しい会計ソフトの購入",
     "Woman: Hi, Ken. Are you available this afternoon? I need to purchase a new accounting program for our team. Man: Sure. Did you refer to the budget report? The finance office is likely to approve it if we provide a clear cost estimate. Woman: I did. Following the conference call yesterday, I asked the vendor to offer a discount. Man: Great. Anyway, let's check the numbers together at three, and then tell all employees."),
    ("2日目（10/23）", {"Speaker": "en-GB-RyanNeural"}, "Part4 アナウンス", "ゲーム会社：発売日の延期",
     "Speaker: Good morning, everyone. I have an important announcement about our new action game. Due to some technical problems, we have decided to postpone the release by two weeks. The development team will fix the remaining errors and improve the loading speed. This will not affect our budget, but we must adjust our marketing schedule. Please check the updated timeline on the company website, and contact your manager if you have any questions."),
    ("3日目（10/24）", {"Woman": "en-AU-NatashaNeural", "Man": "en-GB-ThomasNeural"}, "Part3 会話", "アニメスタジオ：納期の打ち合わせ",
     "Man: Hi, Mia. How is the new episode coming along? The deadline is next Friday. Woman: Well, we have a small problem. Two of our artists are sick, so the final scenes are delayed. Man: I see. Can we ask other staff members to help? Woman: Probably. I'll arrange a meeting with the team and confirm the new plan by tomorrow. Man: Good. Please let the client know as soon as possible."),
    ("4日目（10/25）", {"Speaker": "en-GB-SoniaNeural"}, "Part4 アナウンス", "スタジアムの入場案内",
     "Speaker: Welcome to the stadium, ladies and gentlemen. The match will begin in thirty minutes. All spectators must have a valid ticket and show it at the entrance. Please note that large bags are not allowed inside, and you can leave them at the storage area near Gate Five. Food and beverages are available at the concession stands. If you need assistance, please ask any staff member. We hope you enjoy the game."),
    ("5日目（10/26）", {"Woman": "en-CA-ClaraNeural", "Man": "en-CA-LiamNeural"}, "Part3 会話", "経理：請求書の金額の確認",
     "Woman: Excuse me, Tom. I found a mistake in this invoice. The total is higher than the price we agreed on. Man: Really? Let me check. Oh, they charged us for an extra delivery fee. I'll contact the supplier and ask for a correction. Woman: Thanks. We should also keep a record of every payment from now on. Man: Good idea. I'll send you the corrected invoice by Thursday, so we can pay before the deadline."),
    ("6日目（10/27）", {"Speaker": "en-IE-EmilyNeural"}, "Part4 留守番電話", "注文した商品の発送連絡",
     "Speaker: Hello, this is Laura from Harbor Office Supplies. I'm calling about the items you purchased last week. Unfortunately, one item is currently unavailable, so we cannot ship your entire package today. We will send the remaining item next Monday. If you prefer a refund, please call us back at five five five, zero one two three. Thank you for your patience."),
    ("7日目（10/28）", {"Woman": "en-US-AriaNeural", "Man": "en-US-AndrewNeural"}, "Part3 会話", "ゲームイベント：当日のスケジュール",
     "Woman: The game show starts at ten tomorrow. Have you checked the program? Man: Yes. The opening ceremony will take place at ten, and the main presentation is at eleven. Woman: Good. I'll demonstrate our new game at our booth. Could you bring extra copies of the brochure? Man: Sure. I'll also check that all the equipment is working properly before the visitors arrive."),
]


def parse(text, voices):
    parts = re.split(r"(Woman|Man|Speaker):\s*", text)
    return [(voices[parts[i]], parts[i + 1].strip()) for i in range(1, len(parts), 2)]


async def tts(text, voice, path, rate="+0%"):
    for attempt in range(5):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
            return
        except edge_tts.exceptions.NoAudioReceived:
            print(f"retry {attempt + 1}: {voice} {text[:30]}", flush=True)
            await asyncio.sleep(2)
    raise RuntimeError(f"TTS failed: {voice} {text[:40]}")


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", str(SR), "-ac", "1", str(dst)], check=True)


def read_pcm(path):
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def silence(sec):
    return b"\x00\x00" * int(SR * sec)


async def make_clip(tmp, name, text, voice, rate="+0%"):
    mp3, wav = tmp / f"{name}.mp3", tmp / f"{name}.wav"
    await tts(text, voice, mp3, rate)
    to_wav(mp3, wav)
    return read_pcm(wav)


async def build_lesson(idx, lesson, tmp):
    label, voices, kind, topic, text = lesson
    n = idx + 1
    ja = lambda k, t: make_clip(tmp, f"{n}_{k}", t, JA_VOICE, JA_RATE)
    # 英文（話者ごと）を1回分つなぐ。話者の切り替わりに0.4秒の間
    eng = b""
    for j, (voice, line) in enumerate(parse(text, voices)):
        eng += await make_clip(tmp, f"{n}_e{j}", line, voice)
        eng += silence(0.4)
    cues = {
        "start": f"素材{n}。まず、英文を読んでください。",
        "listen": "それでは、英文を一度、通して聞きます。目で追ってください。",
        "overlap": "次は、オーバーラッピング。原稿を見ながら、音声と同時に、小声で読みます。二回続けます。",
        "shadow": "最後は、シャドーイング。原稿を伏せて、少し遅れて、声に出して追いかけます。二回続けます。",
        "end": "おつかれさま。",
    }
    c = {k: await ja(k, v) for k, v in cues.items()}
    audio = c["start"] + silence(60)
    audio += c["listen"] + silence(1) + eng
    audio += silence(1.5) + c["overlap"] + silence(1) + eng + silence(5) + eng
    audio += silence(1.5) + c["shadow"] + silence(1) + eng + silence(1.5) + eng
    audio += silence(1.5) + c["end"] + silence(0.5)
    wav = tmp / f"track{n}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(audio)
    title = f"リスニング素材 第1週 {label} {kind} {topic}"
    fname = f"listening-w1-d{n}.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-b:a", "96k",
                    "-metadata", f"title={title}", str(OUT / fname)], check=True)
    return {"n": n, "title": title, "file": fname, "seconds": len(audio) / 2 / SR,
            "size": (OUT / fname).stat().st_size, "text": text}


def build_feed(eps):
    base_time = datetime(2026, 10, 21, 21, 0, tzinfo=JST)
    items = ""
    for e in eps:
        # 1日目が一番新しい（Overcastの先頭に来る）ように、日が進むほど1分ずつ古くする
        pub = (base_time - timedelta(minutes=e["n"] - 1)).strftime("%a, %d %b %Y %H:%M:%S %z")
        items += f"""
    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e['text'])}</description>
      <pubDate>{pub}</pubDate>
      <guid isPermaLink="false">toeic-listening-w1-d{e['n']}</guid>
      <enclosure url="{BASE}/{e['file']}" length="{e['size']}" type="audio/mpeg"/>
      <itunes:duration>{int(e['seconds'])}</itunes:duration>
    </item>"""
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>TOEICリスニング素材</title>
    <link>{BASE}/</link>
    <language>ja</language>
    <itunes:author>TOEICリスニング素材</itunes:author>
    <description>毎朝5分。英文を見ながら、通し聞き・オーバーラッピング・シャドーイングをするためのTOEIC Part3/4素材です。</description>
    <itunes:summary>毎朝5分。英文を見ながら、通し聞き・オーバーラッピング・シャドーイングをするためのTOEIC Part3/4素材です。</itunes:summary>
    <itunes:image href="https://bbbryo6410-lab.github.io/news-reading-podcast/cover.jpg"/>
    <itunes:category text="Education"/>
    <itunes:explicit>false</itunes:explicit>
    <atom:link href="{BASE}/feed.xml" rel="self" type="application/rss+xml"/>
{items}
  </channel>
</rss>
"""
    (OUT / "feed.xml").write_text(feed, encoding="utf-8")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    eps = []
    with tempfile.TemporaryDirectory() as t:
        for i, lesson in enumerate(LESSONS):
            e = await build_lesson(i, lesson, Path(t))
            print(f"{e['title']}  {e['seconds']:.0f}秒  {e['size']//1024}KB", flush=True)
            eps.append(e)
    build_feed(eps)


asyncio.run(main())
