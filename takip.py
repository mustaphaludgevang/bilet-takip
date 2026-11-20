#!/usr/bin/env python3
"""
Biletinial mekan takipçisi.

- İki mekan sayfasını headless Chromium (Playwright) ile açar.
- Listelenen etkinliklerin adını, linkini ve mekanını toplar.
- Etkinlik detay sayfalarında "... 15:00'da satışa açılacak" gibi metinleri arar.
- Durumu events_state.json içinde saklar (Europe/Istanbul saat dilimi).
- ntfy.sh üzerinden iki tip bildirim gönderir:
    1) İlk keşif   (initial_notified == False)
    2) 15 dk kala  (reminder_15m_sent == False, satışa 10-25 dk kala)
- Satış saati bilinen etkinlikler için satis_takvimi.ics üretir (15 dk önce alarm).

Ortam değişkenleri:
  NTFY_TOPIC            (zorunlu değil; yoksa bildirimler sadece ekrana yazılır)
  DETAIL_RECHECK_MIN    Bilinen bir etkinliğin detay sayfası kaç dakikada bir
                        yeniden kontrol edilsin (varsayılan 60)
  MAX_DETAIL_PAGES      Bir çalıştırmada en fazla kaç detay sayfası açılsın (varsayılan 60)
  NOTIFY_WITHOUT_SALE   "1" ise satış saati olmayan yeni etkinlikler için de
                        ilk keşif bildirimi gönderilir (varsayılan 0)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright

TZ = ZoneInfo("Europe/Istanbul")
BASE = "https://biletinial.com"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "events_state.json"
ICS_FILE = ROOT / "satis_takvimi.ics"

VENUE_PAGES = [
    {
        "url": "https://biletinial.com/tr-tr/mekan/muammer-karaca-tiyatrosu",
        "label": "Muammer Karaca Tiyatrosu",
    },
    {
        "url": "https://biletinial.com/tr-tr/mekan/ibb-kultur-mudurlugu-2-2",
        "label": "İBB Kültür",
    },
]

# Detay sayfasında mekan adı ararken öncelik verilecek bilinen salonlar
KNOWN_VENUES = [
    "Muammer Karaca Tiyatrosu",
    "Forsa PSM",
    "Forsa Performans Sanatları Merkezi",
    "Cemal Reşit Rey",
    "Harbiye Muhsin Ertuğrul",
    "Kadıköy Haldun Taner",
    "Üsküdar Tekel",
    "Kağıthane Sadabad",
    "Fatih Reşat Nuri",
]

DETAIL_RECHECK_MIN = int(os.environ.get("DETAIL_RECHECK_MIN", "60"))
MAX_DETAIL_PAGES = int(os.environ.get("MAX_DETAIL_PAGES", "60"))
NOTIFY_WITHOUT_SALE = os.environ.get("NOTIFY_WITHOUT_SALE", "0") == "1"
REMINDER_MIN, REMINDER_MAX = 10, 25  # dakika
PRUNE_AFTER_DAYS = 21

EVENT_PATH_RE = re.compile(
    r"^/tr-tr/(tiyatro|muzik|egitim|etkinlik|sinema|stand-up|opera-bale|"
    r"gosteri|konser|cocuk|festival|spor|sergi|seminer)/[^/?#]+/?$"
)

AYLAR = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5,
    "mayis": 5, "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8,
    "eylül": 9, "eylul": 9, "ekim": 10, "kasım": 11, "kasim": 11,
    "aralık": 12, "aralik": 12,
}
AY_RE = "|".join(sorted(AYLAR, key=len, reverse=True))

# Örnekler: "21 Kasım Cumartesi 15:00'da satışa açılacak"
#           "21 Kasım 2026 15.00'te satışa açılacaktır"
SALE_RE = re.compile(
    rf"(?P<gun>\d{{1,2}})\s+(?P<ay>{AY_RE})(?:\s+(?P<yil>20\d\d))?"
    rf"(?:\s*,?\s*[A-Za-zÇĞİÖŞÜçğıöşü]+)?\s*,?\s*(?:saat\s*)?"
    rf"(?P<saat>\d{{1,2}})[:.](?P<dk>\d{{2}})"
    rf"\s*(?:['’`]?\s*(?:da|de|ta|te))?\s+(?:biletler\s+)?satış(?:a|ları)\s+açıl",
    re.IGNORECASE,
)
# Daha gevşek yedek: "satışa açıl" ifadesinin çevresinde tarih+saat ara
NEAR_SALE_RE = re.compile(r"satış\w*\s+açıl\w*", re.IGNORECASE)
DATE_TIME_RE = re.compile(
    rf"(?P<gun>\d{{1,2}})\s+(?P<ay>{AY_RE})(?:\s+(?P<yil>20\d\d))?"
    rf"[^\d]{{0,25}}?(?P<saat>\d{{1,2}})[:.](?P<dk>\d{{2}})",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def now_tr() -> datetime:
    return datetime.now(TZ)


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def event_key(href: str) -> str:
    """Sorgu parametresiz yol: /tr-tr/tiyatro/deniz-kizi-mia"""
    return urlsplit(href).path.rstrip("/")


def build_dt(m: re.Match, ref: datetime) -> datetime | None:
    try:
        gun = int(m.group("gun"))
        ay = AYLAR[tr_lower(m.group("ay"))]
        saat, dk = int(m.group("saat")), int(m.group("dk"))
        yil = int(m.group("yil")) if m.group("yil") else ref.year
        dt = datetime(yil, ay, gun, saat, dk, tzinfo=TZ)
        # Yıl yazılmamışsa ve tarih 60 günden fazla geçmişte kalıyorsa: gelecek yıl
        if not m.group("yil") and dt < ref - timedelta(days=60):
            dt = dt.replace(year=yil + 1)
        return dt
    except (KeyError, ValueError):
        return None


def parse_sale(text: str, ref: datetime) -> tuple[datetime | None, str | None]:
    """Metinden satış açılış zamanını ve ham ifadeyi çıkarır."""
    if not text:
        return None, None
    flat = re.sub(r"\s+", " ", text)
    m = SALE_RE.search(flat)
    if m:
        dt = build_dt(m, ref)
        if dt:
            start = max(0, m.start())
            raw = flat[start : m.end() + 12].strip()
            return dt, raw
    # Yedek yöntem
    for near in NEAR_SALE_RE.finditer(flat):
        window = flat[max(0, near.start() - 90) : near.end() + 90]
        dm = DATE_TIME_RE.search(window)
        if dm:
            dt = build_dt(dm, ref)
            if dt:
                return dt, window.strip()
    return None, None


def fmt_dt(dt: datetime) -> str:
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    aylar = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    return f"{dt.day} {aylar[dt.month]} {dt.year} {gunler[dt.weekday()]} {dt:%H:%M}"


# --------------------------------------------------------------------------- #
# Durum dosyası
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            data.setdefault("events", {})
            return data
        except json.JSONDecodeError:
            print("! events_state.json bozuk, sıfırdan başlıyorum", file=sys.stderr)
    return {"events": {}}


def save_state(state: dict) -> None:
    state["timezone"] = "Europe/Istanbul"
    state["events"] = dict(sorted(state["events"].items()))
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Bildirim (ntfy.sh)
# --------------------------------------------------------------------------- #
def _hdr(value: str) -> str:
    """HTTP başlıkları latin-1 olmak zorunda; Türkçe/emoji için RFC 2047 kodla."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode() + "?="


def notify(title: str, message: str, link: str, priority: str = "default",
           tags: str = "") -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print(f"[DRY-RUN bildirim] {title}\n{message}\n{link}\n")
        return True
    headers = {"Title": _hdr(title), "Click": link, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        print(f"✓ bildirim gönderildi: {title}")
        return True
    except requests.RequestException as e:
        print(f"✗ bildirim gönderilemedi ({title}): {e}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# Kazıma
# --------------------------------------------------------------------------- #
LIST_JS = r"""
(pattern) => {
  const re = new RegExp(pattern);
  const out = {};
  for (const a of document.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { continue; }
    if (u.hostname !== location.hostname || !re.test(u.pathname)) continue;
    const key = u.pathname.replace(/\/$/, '');
    // Kart kapsayıcısını bul: yukarı çık, başka etkinlik linki içermeyen en geniş öğe
    let card = a;
    for (let i = 0; i < 6 && card.parentElement; i++) {
      const p = card.parentElement;
      const links = new Set([...p.querySelectorAll('a[href]')]
        .map(x => { try { return new URL(x.getAttribute('href'), location.href).pathname.replace(/\/$/, ''); } catch (e) { return ''; } })
        .filter(x => re.test(x + '')));
      if (links.size > 1) break;
      card = p;
    }
    const heading = card.querySelector('h1,h2,h3,h4,h5,[class*="title" i],[class*="name" i]');
    const img = card.querySelector('img[alt]');
    const name = (a.getAttribute('title') || (heading && heading.innerText) ||
                  (img && img.getAttribute('alt')) || a.innerText || '').trim();
    const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
    if (!out[key] || (name && name.length > (out[key].name || '').length && name.length < 160)) {
      out[key] = { href: u.href, name, text };
    }
  }
  return Object.values(out);
}
"""

DETAIL_JS = r"""
(known) => {
  const body = document.body ? document.body.innerText : '';
  const h1 = document.querySelector('h1');
  let venue = '';
  // 1) bilinen salon adları
  for (const k of known) { if (body.includes(k)) { venue = k; break; } }
  // 2) class adında place/venue/mekan geçen öğeler
  if (!venue) {
    for (const el of document.querySelectorAll('[class*="venue" i],[class*="mekan" i],[class*="place" i]')) {
      const t = (el.innerText || '').trim();
      if (t && t.length > 3 && t.length < 80 && !/\n/.test(t) && !/seç|konum/i.test(t)) { venue = t; break; }
    }
  }
  return { title: h1 ? h1.innerText.trim() : document.title, venue, body };
}
"""


async def new_context(browser):
    ctx = await browser.new_context(
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
    )

    async def block_heavy(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block_heavy)
    return ctx


async def scroll_to_bottom(page, rounds: int = 12) -> None:
    """Tembel yüklenen kartlar için sayfayı aşağı kaydır."""
    last = 0
    for _ in range(rounds):
        h = await page.evaluate("document.body.scrollHeight")
        if h == last:
            break
        last = h
        await page.mouse.wheel(0, h)
        await page.wait_for_timeout(700)


async def scrape_listing(ctx, venue_page: dict) -> list[dict]:
    page = await ctx.new_page()
    try:
        await page.goto(venue_page["url"], wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await scroll_to_bottom(page)
        items = await page.evaluate(LIST_JS, EVENT_PATH_RE.pattern)
        for it in items:
            it["source_label"] = venue_page["label"]
            it["source_url"] = venue_page["url"]
        print(f"• {venue_page['label']}: {len(items)} etkinlik bulundu")
        return items
    except Exception as e:
        print(f"✗ {venue_page['url']} okunamadı: {e}", file=sys.stderr)
        return []
    finally:
        await page.close()


async def scrape_detail(ctx, href: str, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=40000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            return await page.evaluate(DETAIL_JS, KNOWN_VENUES)
        except Exception as e:
            print(f"  ✗ detay okunamadı {href}: {e}", file=sys.stderr)
            return None
        finally:
            await page.close()


# --------------------------------------------------------------------------- #
# Ana akış
# --------------------------------------------------------------------------- #
def needs_detail(ev: dict | None, ref: datetime) -> bool:
    if ev is None or not ev.get("detail_checked_at"):
        return True
    sale = ev.get("sale_at")
    if sale:
        sale_dt = datetime.fromisoformat(sale)
        if sale_dt < ref:  # satış başladı, tekrar bakmaya gerek yok
            return False
        # Satışa 3 saatten az kaldıysa her turda doğrula (saat değişmiş olabilir)
        if sale_dt - ref < timedelta(hours=3):
            return True
    last = datetime.fromisoformat(ev["detail_checked_at"])
    return ref - last >= timedelta(minutes=DETAIL_RECHECK_MIN)


async def collect(state: dict) -> list[str]:
    ref = now_tr()
    events = state["events"]
    seen_now: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        ctx = await new_context(browser)

        listing: list[dict] = []
        for vp in VENUE_PAGES:
            listing.extend(await scrape_listing(ctx, vp))

        # Listeyi state'e işle
        for it in listing:
            key = event_key(it["href"])
            seen_now.append(key)
            ev = events.get(key)
            if ev is None:
                ev = events[key] = {
                    "name": it["name"] or key.rsplit("/", 1)[-1],
                    "url": it["href"],
                    "venue": it["source_label"],
                    "source_page": it["source_label"],
                    "first_seen": ref.isoformat(timespec="seconds"),
                    "sale_at": None,
                    "sale_text": None,
                    "initial_notified": False,
                    "reminder_15m_sent": False,
                }
            if it["name"]:
                ev["name"] = it["name"]
            ev["url"] = it["href"]
            ev["last_seen"] = ref.isoformat(timespec="seconds")
            ev["card_text"] = it["text"][:200]
            # Listede satış metni varsa doğrudan al
            dt, raw = parse_sale(it["text"], ref)
            if dt:
                apply_sale(ev, dt, raw)

        # Detay sayfaları (sınırlı, öncelik: yeni + satışı yakın olanlar)
        todo = [k for k in dict.fromkeys(seen_now) if needs_detail(events.get(k), ref)]
        todo.sort(key=lambda k: (events[k].get("detail_checked_at") is not None,
                                 events[k].get("detail_checked_at") or ""))
        todo = todo[:MAX_DETAIL_PAGES]
        print(f"• {len(todo)} detay sayfası kontrol edilecek")

        sem = asyncio.Semaphore(4)
        results = await asyncio.gather(*(scrape_detail(ctx, events[k]["url"], sem) for k in todo))
        for key, res in zip(todo, results):
            if not res:
                continue
            ev = events[key]
            ev["detail_checked_at"] = ref.isoformat(timespec="seconds")
            if res.get("venue"):
                ev["venue"] = res["venue"]
            dt, raw = parse_sale(res.get("body", ""), ref)
            if dt:
                apply_sale(ev, dt, raw)

        await browser.close()
    return seen_now


def apply_sale(ev: dict, dt: datetime, raw: str | None) -> None:
    iso = dt.isoformat(timespec="minutes")
    if ev.get("sale_at") != iso:
        if ev.get("sale_at"):
            print(f"  ↻ satış saati değişti: {ev['name']} {ev['sale_at']} → {iso}")
            # Saat değiştiyse bildirimleri yeniden tetikle
            ev["initial_notified"] = False
            ev["reminder_15m_sent"] = False
        ev["sale_at"] = iso
    ev["sale_text"] = raw


def send_notifications(state: dict) -> None:
    ref = now_tr()
    for key, ev in state["events"].items():
        sale_dt = datetime.fromisoformat(ev["sale_at"]) if ev.get("sale_at") else None

        # Bildirim 1: ilk keşif
        if not ev.get("initial_notified"):
            if sale_dt and sale_dt > ref:
                msg = (
                    f"🎭 {ev['name']}\n"
                    f"📍 {ev['venue']}\n"
                    f"🕒 Satış açılışı: {fmt_dt(sale_dt)}\n"
                    f"🔗 {ev['url']}"
                )
                if notify("🎟️ Yeni etkinlik: satış saati belli", msg, ev["url"],
                          priority="high", tags="ticket"):
                    ev["initial_notified"] = True
            elif not sale_dt and NOTIFY_WITHOUT_SALE:
                msg = f"🎭 {ev['name']}\n📍 {ev['venue']}\n🔗 {ev['url']}"
                if notify("🆕 Yeni etkinlik eklendi", msg, ev["url"], tags="new"):
                    ev["initial_notified"] = True
            elif sale_dt and sale_dt <= ref:
                # Satışı zaten açılmış; bildirim gerekmez
                ev["initial_notified"] = True

        # Bildirim 2: satışa 10–25 dk kala
        if sale_dt and not ev.get("reminder_15m_sent"):
            kalan = (sale_dt - ref).total_seconds() / 60
            if REMINDER_MIN <= kalan <= REMINDER_MAX:
                msg = (
                    f"{ev['name']} ({ev['venue']})\n"
                    f"Satış saati: {sale_dt:%H:%M} (~{round(kalan)} dk kaldı)\n"
                    f"{ev['url']}"
                )
                if notify("⏰ BİLET SATIŞINA 15 DK KALDI!", msg, ev["url"],
                          priority="urgent", tags="alarm_clock"):
                    ev["reminder_15m_sent"] = True
            elif 0 < kalan < REMINDER_MIN:
                # GitHub cron gecikirse pencere kaçabilir: geç de olsa uyar
                msg = f"{ev['name']} ({ev['venue']})\nSatış saati: {sale_dt:%H:%M}\n{ev['url']}"
                if notify(f"⏰ BİLET SATIŞINA {max(1, round(kalan))} DK KALDI!", msg,
                          ev["url"], priority="urgent", tags="alarm_clock"):
                    ev["reminder_15m_sent"] = True
            elif kalan <= 0:
                ev["reminder_15m_sent"] = True  # kaçırıldı; tekrar deneme


def prune(state: dict) -> None:
    cutoff = now_tr() - timedelta(days=PRUNE_AFTER_DAYS)
    for key in list(state["events"]):
        last = state["events"][key].get("last_seen")
        if last and datetime.fromisoformat(last) < cutoff:
            del state["events"][key]


def write_ics(state: dict) -> None:
    """Satış saati gelecekte olan etkinlikler için 15 dk önce alarmlı takvim."""
    ref = now_tr()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//bilet-takip//biletinial//TR",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Bilet Satış Saatleri",
        "X-WR-TIMEZONE:Europe/Istanbul",
    ]

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    for key, ev in state["events"].items():
        if not ev.get("sale_at"):
            continue
        sale = datetime.fromisoformat(ev["sale_at"])
        if sale < ref - timedelta(days=1):
            continue
        utc = sale.astimezone(ZoneInfo("UTC"))
        end = utc + timedelta(minutes=15)
        uid = key.strip("/").replace("/", "-") + "@bilet-takip"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{utc:%Y%m%dT%H%M%SZ}",
            f"DTEND:{end:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{esc('🎟️ Satış: ' + ev['name'])}",
            f"LOCATION:{esc(ev['venue'])}",
            f"DESCRIPTION:{esc(ev['url'])}",
            f"URL:{ev['url']}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc('15 dk sonra satış: ' + ev['name'])}",
            "TRIGGER:-PT15M",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    ICS_FILE.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def main() -> int:
    state = load_state()
    try:
        seen = asyncio.run(collect(state))
    except Exception as e:  # tarayıcı tamamen çöktüyse yine de hatırlatmaları gönder
        print(f"✗ kazıma hatası: {e}", file=sys.stderr)
        seen = []
    send_notifications(state)
    prune(state)
    state["last_run"] = now_tr().isoformat(timespec="seconds")
    state["last_run_found"] = len(set(seen))
    save_state(state)
    write_ics(state)

    with_sale = [e for e in state["events"].values() if e.get("sale_at")]
    print(f"• Toplam {len(state['events'])} etkinlik, {len(with_sale)} tanesinde satış saati var")
    for e in sorted(with_sale, key=lambda x: x["sale_at"]):
        print(f"   - {e['sale_at']}  {e['name']}  [{e['venue']}]")
    return 0 if seen else 1


if __name__ == "__main__":
    sys.exit(main())
