"""Tarayıcı kurmadan sadece özet bildirimi gönderir (workflow 'ozet_gonder')."""
import sys, types
# takip.py playwright'ı içe aktarıyor; özet için gerekmediğinden sahte modül ver
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
m = types.ModuleType("playwright.async_api"); m.async_playwright = None
sys.modules.setdefault("playwright.async_api", m)
import takip
sys.exit(0 if takip.send_summary(takip.load_state()) else 1)
