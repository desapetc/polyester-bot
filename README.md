# Polyester Testnet Bot (BTC-USDT, multi-akun)

Bot auto trade untuk **Polyester testnet**. Bot ini bolak-balik **market buy** dan
**market sell** BTC-USDT pakai **saldo maksimal**, dengan jeda 6 detik setelah tiap
order. Bisa jalan untuk banyak akun sekaligus (sudah disiapkan untuk 100+ akun).
Fee per akun dibaca otomatis sesuai VIP-nya.

## 1. Kebutuhan
- Python **3.11 atau lebih baru** (https://www.python.org/downloads/).
  Di Windows, centang "Add Python to PATH" waktu install.
- Koneksi internet. Bisa dijalankan di PC atau VPS.

## 2. Setup (sekali saja)
**Linux / Mac / VPS**
```bash
./setup.sh
```
**Windows**: klik dua kali `setup.bat`.

## 3. Isi akun
Salin `accounts.example.csv` jadi `accounts.csv`, lalu buka pakai Excel atau Google
Sheets. Satu baris untuk satu akun:

| kolom | isi |
|---|---|
| `name` | nama bebas (harus unik), muncul di log |
| `api_key_id` | dari testnet.polyester.com, menu **API** |
| `api_private_key` | private key 64 karakter (cuma muncul sekali waktu key dibuat) |
| `account_id` | dari menu **Profile**, bagian **Account ID** |
| `max_slippage_bps` | opsional. 100 = 1%. Kosong = pakai default dari `.env` |
| `interval_sec` | opsional, jeda setelah tiap order |
| `max_trades` | opsional, stop setelah N trade (0/kosong = tanpa batas) |
| `stop_below_usdt` | opsional, stop kalau total saldo di bawah angka ini |
| `enabled` | isi `0` untuk mematikan akun itu sementara |

Waktu membuat API key: aktifkan **Spot trading** dan izinkan market **BTC-USDT**.

Baris judul boleh tidak ditulis. Kalau tidak ada, kolom dibaca berurutan:
`name,api_key_id,api_private_key,account_id`. Contoh isi minimal (bisa diedit pakai
Notepad atau editor MobaXterm):
```
akun1,ak_xxx,private_key_64_karakter,ACCOUNTID1
akun2,ak_yyy,private_key_64_karakter,ACCOUNTID2
```

Simpan sebagai CSV (pemisah koma atau titik koma dua-duanya bisa). Kalau
`accounts.csv` ada, file itu yang dipakai. Kalau tidak ada, bot memakai `accounts.json`.

> `accounts.csv` berisi private key. Jangan dibagikan ke siapa pun.

## 4. Pengaturan umum (`.env`)
Berlaku untuk semua akun, kecuali di-override per akun lewat CSV.

| setting | default | arti |
|---|---|---|
| `DRY_RUN` | `1` | `1` = simulasi (tidak kirim order). **Ganti ke `0` untuk trade beneran** |
| `STRATEGY` | `flip` | `flip` = buy-sell bolak balik. Ada juga `dca` dan `rsi` |
| `INTERVAL_SEC` | `6` | jeda setelah tiap order sebelum cek saldo dan order berikutnya |
| `QUOTE_PER_TRADE` | `max` | `max` = semua saldo USDT, atau isi angka (minimal 5) |
| `MAX_SLIPPAGE_BPS` | `100` | batas slippage market order (100 = 1%) |
| `MAX_TRADES` | `0` | 0 = tanpa batas |
| `STOP_BELOW_USDT` | `0` | 0 = mati |
| `START_STAGGER_SEC` | `0.5` | jeda start antar akun |

## 5. Menjalankan
1. Tes dulu dengan `DRY_RUN=1`. Cek di log: tiap akun harus menampilkan VIP, fee,
   dan saldo yang benar.
2. Kalau sudah benar, ganti `DRY_RUN=0`.

**Linux / VPS**
```bash
./run.sh            # jalan di terminal, stop dengan Ctrl+C
./start-bg.sh       # jalan di background (tetap jalan walau terminal ditutup)
tail -f logs/bot.log
./stop.sh           # stop bot yang jalan di background
```
**Windows**: klik dua kali `run.bat`. Stop dengan Ctrl+C atau tutup jendelanya.

Waktu berhenti, bot menulis ringkasan per akun: jumlah trade, volume, dan perkiraan fee.

## 6. Cara kerja dan hal yang perlu diketahui
- Tiap langkah: cek saldo. Kalau nilai terbesar ada di USDT → market buy pakai semua
  USDT. Kalau di BTC → market sell semua BTC. Lalu tunggu `INTERVAL_SEC`.
- Server testnet **sering membalas "Service temporarily unavailable" padahal order-nya
  masuk**, dan data saldo bisa telat 6–40 detik. Karena itu bot tidak mengirim ulang
  order. Bot menunggu, cek saldo, dan baru kirim lagi kalau saldonya memang belum
  berubah. Pesan `Insufficient funds: balance not updated yet` dan
  `Balance still updating; waiting` itu normal.
- Trade dihitung dari saldo yang benar-benar berubah (`Filled: now holding ...`).
- Tiap buy menyisakan sekitar 1% USDT (server menyisihkan dana untuk batas slippage).
  Sisa itu ikut terpakai di putaran berikutnya.
- Tiap trade kena fee taker sesuai VIP akun (VIP0 0,15% sampai VIP10 0,045%). Saldo
  akan terus berkurang. Bot berhenti sendiri kalau saldo sudah di bawah 5 USDT.
- SDK resmi menulis alamat testnet `api-testnet.polyester.com` (salah, domain itu
  tidak ada). Bot ini memakai `api.testnet.polyester.com`. Kalau alamatnya berubah,
  set `POLYESTER_API_URL` dan `POLYESTER_WS_URL` di `.env`.
