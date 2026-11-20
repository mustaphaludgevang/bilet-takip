#!/usr/bin/env python3
"""
Tek seferlik tarama: tiyatrolar.com.tr'deki Kadıköy sahnelerini ve bu sahnelerdeki
oyunların bilet fiyatlarını toplar; en ucuzdan pahalıya sıralar.

Çıktılar: raporlar/kadikoy_fiyatlar.json ve raporlar/kadikoy_fiyatlar.csv
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
import re
import sys
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://tiyatrolar.com.tr"
OUT = Path(__file__).resolve().parent / "raporlar"
CONCURRENCY = 3
MAX_VENUES = 900

KADIKOY_RE = re.compile(r"Kad[ıi]k[öo]y\s*/\s*[İI]stanbul", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
TIME_RE = re.compile(r"\b(\d{2}):(\d{2})\b")
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*TL\s*([A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü\s\-]{0,25}?)(?=\s*(?:\d|SEANS|$|\n))")


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


async def polite():
    await asyncio.sleep(random.uniform(0.8, 1.6))


async def new_context(browser):
    ctx = await browser.new_context(
        locale="tr-TR", timezone_id="Europe/Istanbul",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    )

    async def block(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block)
    return ctx


async def all_istanbul_venues(ctx) -> list[str]:
    page = await ctx.new_page()
    await page.goto(f"{BASE}/sahneler/?il=34", wait_until="domcontentloaded", timeout=60000)
    last = -1
    for _ in range(120):
        links = await page.eval_on_selector_all(
            "a[href*='/sahne/istanbul/']", "els => [...new Set(els.map(e => e.href))]")
        if len(links) == last:
            break
        last = len(links)
        btn = page.get_by_text(re.compile("DAHA FAZLA Y[ÜU]KLE", re.I))
        if await btn.count() == 0:
            break
        try:
            await btn.first.click(timeout=5000)
            await page.wait_for_timeout(1500)
        except Exception:
            break
    links = await page.eval_on_selector_all(
        "a[href*='/sahne/istanbul/']", "els => [...new Set(els.map(e => e.href.split('?')[0].split('#')[0]))]")
    await page.close()
    print(f"• İstanbul'da {len(links)} sahne bulundu")
    return links[:MAX_VENUES]


async def check_venue(ctx, url: str, sem) -> dict | None:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            data = await page.evaluate("""() => ({
                name: (document.querySelector('h1')||{}).innerText || document.title,
                body: document.body.innerText,
                plays: [...new Set([...document.querySelectorAll("a[href*='/tiyatro/']")]
                        .map(a => a.href.split('?')[0].split('#')[0]))]
            })""")
            await polite()
            if not KADIKOY_RE.search(data["body"]):
                return None
            m = re.search(r"([^\n]{5,160}Kad[ıi]k[öo]y\s*/\s*[İI]stanbul)", data["body"])
            return {"url": url, "name": data["name"].strip(),
                    "address": m.group(1).strip() if m else "Kadıköy / İstanbul",
                    "plays": [p for p in data["plays"] if re.search(r"/tiyatro/[^/]+$", p)]}
        except Exception as e:
            print(f"  ✗ {url}: {e}", file=sys.stderr)
            return None
        finally:
            await page.close()


ROWS_JS = r"""() => {
  const out = [];
  const btns = [...document.querySelectorAll('a,button')].filter(b => /SEANS\s*SE[ÇC]/i.test(b.innerText||''));
  for (const b of btns) {
    let el = b;
    for (let i = 0; i < 8 && el.parentElement; i++) {
      el = el.parentElement;
      const t = el.innerText || '';
      if (/\d{2}\.\d{2}\.\d{4}/.test(t)) break;
    }
    out.push(el.innerText || '');
  }
  if (!out.length) {
    // yedek: tablo satırları
    for (const tr of document.querySelectorAll('tr')) {
      const t = tr.innerText || '';
      if (/\d{2}\.\d{2}\.\d{4}/.test(t)) out.push(t);
    }
  }
  return {title: (document.querySelector('h1')||{}).innerText || document.title, rows: out};
}"""


def parse_prices(text: str) -> list[tuple[float, str]]:
    res = []
    for amount, label in PRICE_RE.findall(text):
        val = float(amount.replace(".", "").replace(",", "."))
        res.append((val, label.strip()))
    return res


async def check_play(ctx, play_url: str, kadikoy_names: list[str], sem) -> list[dict]:
    slug = play_url.rstrip("/").rsplit("/", 1)[-1]
    url = f"{BASE}/tiyatro-bilet/{slug}"
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            data = await page.evaluate(ROWS_JS)
            await polite()
        except Exception as e:
            print(f"  ✗ {url}: {e}", file=sys.stderr)
            return []
        finally:
            await page.close()

    today = date.today()
    sessions = []
    lowered = [(n, tr_lower(n)) for n in kadikoy_names]
    for row in data["rows"]:
        dm = DATE_RE.search(row)
        if not dm:
            continue
        d = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
        if d < today:
            continue
        rl = tr_lower(row)
        venue = next((n for n, nl in lowered if nl and nl in rl), None)
        if not venue:
            continue
        tm = TIME_RE.search(row[dm.end():])
        prices = parse_prices(row)
        sessions.append({
            "oyun": data["title"].strip(),
            "link": f"{BASE}/tiyatro/{slug}",
            "bilet_link": url,
            "sahne": venue,
            "tarih": d.isoformat(),
            "saat": f"{tm.group(1)}:{tm.group(2)}" if tm else "",
            "fiyatlar": [{"tutar": v, "tur": l} for v, l in prices],
            "tukendi": "tükendi" in rl or "tukendi" in rl,
        })
    return sessions


async def main():
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await new_context(browser)
        venues = await all_istanbul_venues(ctx)
        sem = asyncio.Semaphore(CONCURRENCY)
        res = await asyncio.gather(*(check_venue(ctx, v, sem) for v in venues))
        kadikoy = [r for r in res if r]
        print(f"• Kadıköy'de {len(kadikoy)} sahne: " + ", ".join(v["name"] for v in kadikoy))
        names = [v["name"] for v in kadikoy]
        plays = sorted({p for v in kadikoy for p in v["plays"]})
        print(f"• {len(plays)} oyun sayfası kontrol edilecek")
        all_sessions = []
        for chunk in await asyncio.gather(*(check_play(ctx, p, names, sem) for p in plays)):
            all_sessions.extend(chunk)
        await browser.close()

    # Oyun bazında özet: en ucuz fiyat (tam / öğrenci / genel)
    by_play: dict[str, dict] = {}
    for s in all_sessions:
        key = s["link"]
        p = by_play.setdefault(key, {
            "oyun": s["oyun"], "link": s["link"], "bilet_link": s["bilet_link"],
            "sahneler": set(), "seans_sayisi": 0, "ilk_seans": None,
            "en_ucuz": None, "en_ucuz_tur": "", "en_ucuz_tam": None, "en_ucuz_ogrenci": None,
            "fiyatsiz_seans": 0, "tukenen_seans": 0,
        })
        p["sahneler"].add(s["sahne"])
        p["seans_sayisi"] += 1
        stamp = f"{s['tarih']} {s['saat']}".strip()
        p["ilk_seans"] = min(filter(None, [p["ilk_seans"], stamp]))
        if s["tukendi"]:
            p["tukenen_seans"] += 1
        if not s["fiyatlar"]:
            p["fiyatsiz_seans"] += 1
        for f in s["fiyatlar"]:
            v, t = f["tutar"], tr_lower(f["tur"])
            if p["en_ucuz"] is None or v < p["en_ucuz"]:
                p["en_ucuz"], p["en_ucuz_tur"] = v, f["tur"]
            if "tam" in t and (p["en_ucuz_tam"] is None or v < p["en_ucuz_tam"]):
                p["en_ucuz_tam"] = v
            if "öğrenci" in t or "ogrenci" in t:
                if p["en_ucuz_ogrenci"] is None or v < p["en_ucuz_ogrenci"]:
                    p["en_ucuz_ogrenci"] = v
    summary = []
    for p in by_play.values():
        p["sahneler"] = ", ".join(sorted(p["sahneler"]))
        summary.append(p)
    summary.sort(key=lambda p: (p["en_ucuz_tam"] if p["en_ucuz_tam"] is not None
                                else p["en_ucuz"] if p["en_ucuz"] is not None else 1e9))

    payload = {
        "olusturma": datetime.now().isoformat(timespec="minutes"),
        "kadikoy_sahneleri": [{k: v for k, v in x.items() if k != "plays"} for x in kadikoy],
        "oyunlar": summary,
        "seanslar": all_sessions,
    }
    (OUT / "kadikoy_fiyatlar.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(OUT / "kadikoy_fiyatlar.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Oyun", "Sahne", "En ucuz Tam (TL)", "En ucuz Öğrenci (TL)",
                    "En ucuz (TL)", "Tür", "İlk seans", "Seans sayısı", "Fiyatsız seans", "Link"])
        for p in summary:
            w.writerow([p["oyun"], p["sahneler"], p["en_ucuz_tam"], p["en_ucuz_ogrenci"],
                        p["en_ucuz"], p["en_ucuz_tur"], p["ilk_seans"], p["seans_sayisi"],
                        p["fiyatsiz_seans"], p["bilet_link"]])
    print(f"• {len(summary)} oyun, {len(all_sessions)} seans kaydedildi")


if __name__ == "__main__":
    asyncio.run(main())
