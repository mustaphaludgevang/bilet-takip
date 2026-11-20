# Bilet Takip (Biletinial)

Muammer Karaca Tiyatrosu ve İBB Kültür mekan sayfalarını 10 dakikada bir tarar, etkinliklerin satışa açılış saatlerini bulur ve ntfy.sh ile telefona bildirim gönderir.

## Dosyalar
| Dosya | Görev |
|---|---|
| `takip.py` | Playwright ile sayfaları tarar, state'i günceller, bildirim gönderir, `.ics` üretir |
| `events_state.json` | Etkinlikler, satış saatleri (Europe/Istanbul), `initial_notified`, `reminder_15m_sent` |
| `satis_takvimi.ics` | Satış saatleri, 15 dk önce alarmlı (otomatik üretilir) |
| `.github/workflows/bilet_takip.yml` | GitHub Actions: her 10 dk çalıştırır, state'i geri commit eder |

## Bildirimler
1. **İlk keşif:** satış saati gelecekte olan yeni bir etkinlik bulununca (ad, mekan, satış saati, link).
2. **15 dk kala:** satışa 10–25 dk kaldığında `⏰ BİLET SATIŞINA 15 DK KALDI!`. GitHub cron gecikip pencere kaçarsa ve satışa 0–10 dk kaldıysa "X DK KALDI" diye geç uyarı gönderilir.

Satış saati değişirse iki bildirim de yeniden tetiklenir.

## Yerelde deneme (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
$env:NTFY_TOPIC = "kendi-gizli-konu-adin"   # boş bırakırsan bildirimler sadece ekrana yazılır
python takip.py
```

## Ayarlar (ortam değişkenleri)
- `NTFY_TOPIC`: ntfy konu adı (GitHub'da Secret olarak).
- `DETAIL_RECHECK_MIN` (60): bilinen etkinliğin detay sayfası kaç dakikada bir yeniden kontrol edilsin.
- `MAX_DETAIL_PAGES` (60): tek çalıştırmada açılacak en fazla detay sayfası.
- `NOTIFY_WITHOUT_SALE` (0): `1` yapılırsa satış saati olmayan yeni etkinlikler için de bildirim gelir.
